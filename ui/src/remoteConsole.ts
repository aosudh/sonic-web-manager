// Fixed-target PTY transport. No terminal text, key presses, credentials or
// output are logged, persisted, or sent anywhere outside the same-origin API.
export type ConsoleTarget = "sonic" | "bmc";
export interface ConsoleCapability {
  available: boolean;
  reason: string;
  identity?: string;
}
export interface ConsoleCapabilities {
  mode: "live" | "demo";
  sonic: ConsoleCapability;
  bmc: ConsoleCapability;
}
export interface ConsoleCreated {
  id: string;
  target: ConsoleTarget;
  mode: "live" | "demo";
  cursor: number;
  created_at: string;
  expires_at: string;
}
export interface ConsoleRead {
  id: string;
  data: string;
  cursor: number;
  next_cursor: number;
  oldest_cursor: number;
  truncated: boolean;
  closed: boolean;
  exit_code: number | null;
  reason?: string;
}
export interface ConsoleState {
  phase:
    "idle" | "connecting" | "connected" | "disconnecting" | "closed" | "error";
  message: string;
  hasSession: boolean;
  expiresAt?: string;
}
export interface ConsoleRequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  keepalive?: boolean;
}
export type ConsoleRequest = <T>(
  path: string,
  options?: ConsoleRequestOptions,
) => Promise<T>;
export const CONSOLE_INPUT_CHUNK = 4096;
export const CONSOLE_INPUT_RATE = 16 * 1024;
export const CONSOLE_INPUT_QUEUE = 64 * 1024;

export function consoleUnavailableReason(
  mode: string,
  role: string | undefined,
  capabilities: ConsoleCapabilities | undefined,
  target: ConsoleTarget,
): string {
  if (mode !== "live") return "DEMO 不创建真实 SONiC 或 BMC 系统终端。";
  if (role !== "admin") return "远程系统终端仅对管理员开放。";
  if (!capabilities) return "正在确认终端连接能力。";
  if (capabilities.mode !== "live") return "当前后端为 DEMO，不连接真实系统。";
  return capabilities[target].available === true
    ? ""
    : capabilities[target].reason || "此目标尚未配置可用的系统终端。";
}
export function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192)
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}
export function base64ToBytes(value: string): Uint8Array {
  if (value.length > 350000) throw new Error("终端输出超过缓冲上限。");
  return Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
}
export function consoleEndMessage(
  reason?: string,
  exitCode?: number | null,
): string {
  if (reason === "idle_timeout")
    return "终端因 5 分钟没有输入而关闭，请按需重新连接。";
  if (reason === "max_duration")
    return "终端已达到 30 分钟时限，请按需重新连接。";
  if (reason === "logout") return "管理会话已退出，终端已关闭。";
  if (reason === "process_exit")
    return `目标进程已结束${exitCode == null ? "" : `（退出码 ${exitCode}）`}。`;
  return "终端会话已关闭。";
}

export class RemoteConsoleController {
  private target: ConsoleTarget;
  private request: ConsoleRequest;
  private onState: (state: ConsoleState) => void;
  private onOutput: (bytes: Uint8Array) => void;
  private onNotice: (message: string) => void;
  private id: string | undefined;
  private cursor = 0;
  private generation = 0;
  private stopping = false;
  private disposed = false;
  private connecting: Promise<void> | undefined;
  private closing: Promise<void> | undefined;
  private readAbort: AbortController | undefined;
  private inputAbort: AbortController | undefined;
  private resizeAbort: AbortController | undefined;
  private pollTimer: ReturnType<typeof setTimeout> | undefined;
  private wakePoll: (() => void) | undefined;
  private inputTimer: ReturnType<typeof setTimeout> | undefined;
  private inputQueue: Uint8Array[] = [];
  private queuedBytes = 0;
  private sending = false;
  private rateWindow: { at: number; bytes: number }[] = [];
  private resizeTimer: ReturnType<typeof setTimeout> | undefined;
  private wantedSize: { rows: number; cols: number } | undefined;
  private resizing = false;
  private pollMs: number;
  private current: ConsoleState = {
    phase: "idle",
    message: "",
    hasSession: false,
  };
  constructor(
    target: ConsoleTarget,
    request: ConsoleRequest,
    callbacks: {
      onState: (state: ConsoleState) => void;
      onOutput: (bytes: Uint8Array) => void;
      onNotice: (message: string) => void;
    },
    pollMs = 500,
  ) {
    if (target !== "sonic" && target !== "bmc")
      throw new Error("终端目标无效。");
    this.target = target;
    this.request = request;
    this.onState = callbacks.onState;
    this.onOutput = callbacks.onOutput;
    this.onNotice = callbacks.onNotice;
    this.pollMs = pollMs;
  }
  get state(): ConsoleState {
    return this.current;
  }
  private notify(
    phase: ConsoleState["phase"],
    message = "",
    expiresAt = this.current.expiresAt,
  ): void {
    this.current = { phase, message, hasSession: !!this.id, expiresAt };
    if (!this.disposed) this.onState(this.current);
  }
  private path(id: string): string {
    return `/consoles/${encodeURIComponent(id)}`;
  }
  connect(rows: number, cols: number): Promise<void> {
    if (this.disposed || this.connecting || this.closing || this.id)
      return Promise.reject(new Error("请先释放当前终端会话。"));
    this.stopping = false;
    const generation = ++this.generation;
    this.sending = false;
    this.resizing = false;
    this.cursor = 0;
    this.rateWindow = [];
    this.notify("connecting");
    const task = (async () => {
      try {
        const created = await this.request<ConsoleCreated>("/consoles", {
          method: "POST",
          body: { target: this.target, rows, cols },
        });
        if (typeof created.id !== "string" || !created.id)
          throw new Error("终端响应无效。");
        this.id = created.id;
        if (
          created.target !== this.target ||
          !Number.isSafeInteger(created.cursor) ||
          created.cursor < 0 ||
          created.mode !== "live" ||
          this.stopping ||
          this.disposed ||
          generation !== this.generation
        ) {
          await this.request(this.path(created.id) + "/close", {
            method: "POST",
            body: {},
            keepalive: true,
          });
          this.id = undefined;
          if (!this.stopping && !this.disposed)
            this.notify("error", "后端没有创建真实终端会话。");
          return;
        }
        this.cursor = created.cursor;
        this.notify("connected", "", created.expires_at);
        void this.poll(created.id, generation);
      } catch {
        if (!this.stopping && !this.disposed)
          this.notify(
            "error",
            "连接请求失败或未确认，请检查服务和目标状态；不要连续重复提交。",
          );
      }
    })();
    this.connecting = task;
    void task.then(() => {
      if (this.connecting === task) this.connecting = undefined;
    });
    return task;
  }
  private async poll(id: string, generation: number): Promise<void> {
    while (
      !this.stopping &&
      this.id === id &&
      !this.disposed &&
      generation === this.generation
    ) {
      this.readAbort = new AbortController();
      try {
        const value = await this.request<ConsoleRead>(
          `${this.path(id)}?cursor=${this.cursor}`,
          { signal: this.readAbort.signal },
        );
        if (
          this.stopping ||
          this.id !== id ||
          this.disposed ||
          generation !== this.generation
        )
          return;
        if (
          value.id !== id ||
          !Number.isSafeInteger(value.next_cursor) ||
          value.next_cursor < this.cursor
        )
          throw new Error("Invalid terminal cursor");
        const bytes = base64ToBytes(value.data);
        if (
          !Number.isSafeInteger(value.cursor) ||
          !Number.isSafeInteger(value.oldest_cursor) ||
          value.cursor < 0 ||
          value.oldest_cursor < 0 ||
          value.cursor < this.cursor ||
          value.next_cursor !== value.cursor + bytes.length
        )
          throw new Error("Invalid terminal byte range");
        if (value.truncated)
          this.onNotice(
            "部分输出已超出服务器缓冲范围，当前屏幕可能不完整。可重新运行所需查询。",
          );
        if (bytes.length) this.onOutput(bytes);
        this.cursor = value.next_cursor;
        if (value.closed && !bytes.length) {
          void this.disconnect(
            consoleEndMessage(value.reason, value.exit_code),
            "closed",
          );
          return;
        }
        if (value.closed)
          this.notify("disconnecting", "目标进程已结束，正在读取剩余输出。");
      } catch {
        if (
          !this.stopping &&
          !this.disposed &&
          this.id === id &&
          generation === this.generation
        )
          void this.disconnect(
            "终端连接中断。为避免重复输入，已停止发送；请重新连接后核对设备状态。",
            "error",
          );
        return;
      }
      await new Promise<void>((resolve) => {
        this.wakePoll = resolve;
        this.pollTimer = setTimeout(resolve, this.pollMs);
      });
      if (generation === this.generation) this.wakePoll = undefined;
    }
  }
  sendText(text: string): boolean {
    return this.enqueue(new TextEncoder().encode(text));
  }
  sendBinary(text: string): boolean {
    return this.enqueue(
      Uint8Array.from(text, (char) => char.charCodeAt(0) & 255),
    );
  }
  private enqueue(bytes: Uint8Array): boolean {
    if (this.current.phase !== "connected" || this.stopping || this.disposed)
      return false;
    if (this.queuedBytes + bytes.length > CONSOLE_INPUT_QUEUE) {
      bytes.fill(0);
      this.onNotice("输入过长，未加入发送队列。请将大段粘贴拆成较小部分。");
      return false;
    }
    for (let i = 0; i < bytes.length; i += CONSOLE_INPUT_CHUNK)
      this.inputQueue.push(bytes.slice(i, i + CONSOLE_INPUT_CHUNK));
    this.queuedBytes += bytes.length;
    bytes.fill(0);
    if (!this.sending && !this.inputTimer)
      this.inputTimer = setTimeout(() => {
        this.inputTimer = undefined;
        void this.pump();
      }, 10);
    return true;
  }
  private async pump(): Promise<void> {
    if (this.sending || this.stopping || !this.id || !this.inputQueue.length)
      return;
    const generation = this.generation,
      id = this.id;
    const now = Date.now();
    this.rateWindow = this.rateWindow.filter((entry) => now - entry.at < 1005);
    const chunk = this.inputQueue[0];
    if (
      this.rateWindow.reduce((sum, entry) => sum + entry.bytes, 0) +
        chunk.length >
      CONSOLE_INPUT_RATE
    ) {
      this.inputTimer = setTimeout(
        () => {
          this.inputTimer = undefined;
          void this.pump();
        },
        Math.max(10, 1006 - (now - this.rateWindow[0].at)),
      );
      return;
    }
    this.inputQueue.shift();
    this.queuedBytes -= chunk.length;
    this.sending = true;
    this.rateWindow.push({ at: now, bytes: chunk.length });
    this.inputAbort = new AbortController();
    try {
      const result = await this.request<{ accepted_bytes: number }>(
        this.path(id) + "/input",
        {
          method: "POST",
          body: { data: bytesToBase64(chunk) },
          signal: this.inputAbort.signal,
        },
      );
      if (result.accepted_bytes !== chunk.length)
        throw new Error("Partial input");
    } catch {
      if (
        !this.stopping &&
        !this.disposed &&
        generation === this.generation &&
        this.id === id
      )
        void this.disconnect(
          "输入发送未确认，已停止会话；不会自动重发。请重新连接并核对状态。",
          "error",
        );
    } finally {
      chunk.fill(0);
      if (generation === this.generation && this.id === id) {
        this.sending = false;
        if (!this.stopping && this.inputQueue.length) void this.pump();
      }
    }
  }
  resize(rows: number, cols: number): void {
    if (
      !Number.isInteger(rows) ||
      !Number.isInteger(cols) ||
      rows < 1 ||
      cols < 1
    )
      return;
    this.wantedSize = { rows, cols };
    if (this.current.phase !== "connected" || this.stopping || this.resizeTimer)
      return;
    this.resizeTimer = setTimeout(() => {
      this.resizeTimer = undefined;
      void this.sendResize();
    }, 150);
  }
  private async sendResize(): Promise<void> {
    if (this.resizing || this.stopping || !this.id || !this.wantedSize) return;
    const generation = this.generation,
      id = this.id;
    const size = this.wantedSize;
    this.wantedSize = undefined;
    this.resizing = true;
    this.resizeAbort = new AbortController();
    try {
      await this.request(this.path(id) + "/resize", {
        method: "POST",
        body: size,
        signal: this.resizeAbort.signal,
      });
    } catch {
      if (
        !this.stopping &&
        !this.disposed &&
        generation === this.generation &&
        this.id === id
      )
        this.onNotice("终端尺寸同步失败；请调整窗口大小后重试。");
    } finally {
      if (generation === this.generation && this.id === id) {
        this.resizing = false;
        const pending = this.wantedSize as
          { rows: number; cols: number } | undefined;
        if (pending && !this.stopping) this.resize(pending.rows, pending.cols);
      }
    }
  }
  disconnect(
    message = "终端已断开。",
    phase: "closed" | "error" = "closed",
  ): Promise<void> {
    if (this.closing) return this.closing;
    this.generation++;
    this.stopping = true;
    this.readAbort?.abort();
    this.inputAbort?.abort();
    this.resizeAbort?.abort();
    clearTimeout(this.pollTimer);
    this.wakePoll?.();
    clearTimeout(this.inputTimer);
    this.inputTimer = undefined;
    clearTimeout(this.resizeTimer);
    this.resizeTimer = undefined;
    for (const bytes of this.inputQueue) bytes.fill(0);
    this.inputQueue = [];
    this.queuedBytes = 0;
    this.wantedSize = undefined;
    this.notify("disconnecting");
    const task = (async () => {
      await this.connecting;
      if (this.id) {
        try {
          await this.request(this.path(this.id) + "/close", {
            method: "POST",
            body: {},
            keepalive: true,
          });
          this.id = undefined;
        } catch (error) {
          if (
            [404, 410].includes((error as { status?: number })?.status ?? 0)
          ) {
            this.id = undefined;
            this.notify(phase, message);
            return;
          }
          this.notify(
            "error",
            "关闭请求未确认，可再次点击断开；服务器仍会按会话时限回收。",
          );
          return;
        }
      }
      this.notify(phase, message);
    })();
    this.closing = task;
    void task.then(() => {
      if (this.closing === task) this.closing = undefined;
    });
    return task;
  }
  async dispose(): Promise<void> {
    this.disposed = true;
    await this.disconnect();
  }
}
