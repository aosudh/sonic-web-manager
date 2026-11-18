// Browser-local Web Serial only. No API calls, persistent storage or command logging.
export interface SerialPortLike {
  readable: ReadableStream<Uint8Array> | null;
  writable: WritableStream<Uint8Array> | null;
  open(options: {
    baudRate: number;
    dataBits: 8;
    stopBits: 1;
    parity: "none";
    flowControl: "none";
    bufferSize: number;
  }): Promise<void>;
  close(): Promise<void>;
}
export interface SerialApiLike {
  requestPort(): Promise<SerialPortLike>;
  addEventListener?(type: "disconnect", listener: EventListener): void;
  removeEventListener?(type: "disconnect", listener: EventListener): void;
}
export interface SerialState {
  phase:
    "idle" | "selecting" | "opening" | "connected" | "disconnecting" | "error";
  error: string;
  hasPort: boolean;
}
export const SERIAL_OUTPUT_LIMIT = 64 * 1024;
export const SERIAL_BAUD_RATES = [
  9600, 19200, 38400, 57600, 115200, 230400,
] as const;

export function serialUnavailableReason(
  mode: string | undefined,
  role: string | undefined,
  secure: boolean,
  supported: boolean,
): string {
  if (mode !== "live") return "DEMO 不连接真实串口；请在实机管理会话中使用。";
  if (role !== "admin") return "本地串口控制台仅对管理员开放。";
  if (!secure)
    return "浏览器串口需要安全上下文，请使用可信 HTTPS 或本机 localhost。";
  if (!supported)
    return "此浏览器未提供 Web Serial。请使用支持该功能的桌面 Chrome 或 Edge。";
  return "";
}

// Stateful ANSI stripper: sequences may span arbitrary USB chunks. Escape
// payloads are discarded as they arrive, so an unterminated OSC cannot grow memory.
export class SerialTextBuffer {
  private text = "";
  private state: "text" | "escape" | "csi" | "string" | "string-escape" =
    "text";
  private previousCR = false;
  private encoder = new TextEncoder();
  get value(): string {
    return this.text;
  }
  clear(): void {
    this.text = "";
    this.state = "text";
    this.previousCR = false;
  }
  append(chunk: string): string {
    let added = "";
    for (const char of chunk) {
      const n = char.codePointAt(0)!;
      if (this.state === "string") {
        if (n === 7 || n === 0x9c) this.state = "text";
        else if (n === 27) this.state = "string-escape";
        continue;
      }
      if (this.state === "string-escape") {
        this.state =
          char === "\\" ? "text" : n === 27 ? "string-escape" : "string";
        continue;
      }
      if (this.state === "csi") {
        if (n >= 0x40 && n <= 0x7e) this.state = "text";
        else if (n === 27) this.state = "escape";
        continue;
      }
      if (this.state === "escape") {
        if (char === "[") this.state = "csi";
        else if ("]PX^_".includes(char)) this.state = "string";
        else if (n < 0x20 || n > 0x2f) this.state = "text";
        continue;
      }
      if (n === 27) {
        this.state = "escape";
        continue;
      }
      if (n === 0x9b) {
        this.state = "csi";
        continue;
      }
      if ([0x90, 0x98, 0x9d, 0x9e, 0x9f].includes(n)) {
        this.state = "string";
        continue;
      }
      if (char === "\r") {
        added += "\n";
        this.previousCR = true;
        continue;
      }
      if (char === "\n") {
        if (!this.previousCR) added += "\n";
        this.previousCR = false;
        continue;
      }
      this.previousCR = false;
      if (char === "\b") {
        const target = added || this.text;
        const trimmed = target.replace(
          /[\uD800-\uDBFF][\uDC00-\uDFFF]$|[\s\S]$/,
          "",
        );
        if (added) added = trimmed;
        else this.text = trimmed;
        continue;
      }
      if ((n < 32 && char !== "\t") || (n >= 0x7f && n <= 0x9f)) continue;
      added += char;
    }
    this.text += added;
    const bytes = this.encoder.encode(this.text);
    if (bytes.length > SERIAL_OUTPUT_LIMIT) {
      let start = bytes.length - SERIAL_OUTPUT_LIMIT;
      while ((bytes[start] & 0xc0) === 0x80) start++;
      this.text = new TextDecoder().decode(bytes.subarray(start));
    }
    return this.text;
  }
}

export class SerialController {
  private api: SerialApiLike;
  private stateChanged: (state: SerialState) => void;
  private outputChanged: (text: string) => void;
  private port: SerialPortLike | undefined;
  private opened = false;
  private reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  private writer: WritableStreamDefaultWriter<Uint8Array> | undefined;
  private connecting: Promise<void> | undefined;
  private reading: Promise<void> | undefined;
  private writing: Promise<void> | undefined;
  private closing: Promise<void> | undefined;
  private stopping = false;
  private disposed = false;
  private pendingError = "";
  private buffer = new SerialTextBuffer();
  private current: SerialState = { phase: "idle", error: "", hasPort: false };
  private disconnected: EventListener;
  constructor(
    api: SerialApiLike,
    callbacks: {
      onState: (state: SerialState) => void;
      onOutput: (text: string) => void;
    },
  ) {
    this.api = api;
    this.stateChanged = callbacks.onState;
    this.outputChanged = callbacks.onOutput;
    this.disconnected = (event) => {
      if (
        this.port &&
        ((event.target as unknown) === this.port ||
          (event as Event & { port?: SerialPortLike }).port === this.port)
      ) {
        this.pendingError = "串口设备已断开，请检查 USB 连接后重新选择。";
        void this.disconnect();
      }
    };
    api.addEventListener?.("disconnect", this.disconnected);
  }
  get state(): SerialState {
    return this.current;
  }
  get output(): string {
    return this.buffer.value;
  }
  private notify(phase: SerialState["phase"], error = ""): void {
    this.current = { phase, error, hasPort: !!this.port };
    if (!this.disposed) this.stateChanged(this.current);
  }
  clearOutput(): void {
    this.buffer.clear();
    if (!this.disposed) this.outputChanged("");
  }
  connect(baudRate: number): Promise<void> {
    if (this.disposed) return Promise.reject(new Error("控制台已关闭。"));
    if (this.connecting || this.closing || this.port)
      return Promise.reject(new Error("连接正在处理，请先断开当前串口。"));
    if (!(SERIAL_BAUD_RATES as readonly number[]).includes(baudRate))
      return Promise.reject(new Error("请选择支持的波特率。"));
    this.stopping = false;
    this.pendingError = "";
    this.notify("selecting");
    const task = this.open(baudRate);
    this.connecting = task;
    void task.then(() => {
      if (this.connecting === task) this.connecting = undefined;
    });
    return task;
  }
  private async open(baudRate: number): Promise<void> {
    try {
      // Called synchronously from the user's button gesture, before any await.
      const port = await this.api.requestPort();
      if (this.stopping || this.disposed) return;
      this.port = port;
      this.notify("opening");
      await port.open({
        baudRate,
        dataBits: 8,
        stopBits: 1,
        parity: "none",
        flowControl: "none",
        bufferSize: 4096,
      });
      this.opened = true;
      if (this.stopping || this.disposed) return;
      if (!port.readable || !port.writable) throw new Error("missing_streams");
      this.notify("connected");
      this.reading = this.read(port);
      void this.reading.then(() => {
        if (!this.stopping && this.port === port) {
          this.pendingError ||= "串口连接已结束，请重新选择设备。";
          void this.disconnect();
        }
      });
    } catch (error) {
      const cancelled =
        !this.port && error instanceof Error && error.name === "NotFoundError";
      if (
        this.port &&
        (this.opened || this.port.readable || this.port.writable)
      ) {
        try {
          await this.port.close();
          this.port = undefined;
          this.opened = false;
        } catch {
          this.pendingError = "打开串口失败，且端口尚未释放；请点击断开重试。";
        }
      } else this.port = undefined;
      if (!this.stopping && !this.disposed)
        this.notify(
          cancelled ? "idle" : "error",
          cancelled
            ? ""
            : this.pendingError ||
                "无法打开串口。请检查浏览器权限、USB 连接及是否被其他程序占用。",
        );
    }
  }
  private async read(port: SerialPortLike): Promise<void> {
    const decoder = new TextDecoder();
    try {
      this.reader = port.readable!.getReader();
      while (!this.stopping) {
        const { value, done } = await this.reader.read();
        if (done) break;
        if (value && !this.stopping && !this.disposed)
          this.outputChanged(
            this.buffer.append(decoder.decode(value, { stream: true })),
          );
      }
      if (!this.stopping && !this.disposed)
        this.outputChanged(this.buffer.append(decoder.decode()));
    } catch {
      if (!this.stopping)
        this.pendingError =
          "串口读取中断。请检查线缆、波特率或设备是否已拔出。";
    } finally {
      this.reader?.releaseLock();
      this.reader = undefined;
    }
  }
  sendLine(line: string): Promise<void> {
    if (line.length > 4096 || /[\x00-\x08\x0a-\x1f\x7f]/.test(line))
      return Promise.reject(
        new Error("仅支持发送一行文本，最多 4096 个字符。"),
      );
    return this.write(new TextEncoder().encode(line + "\r"));
  }
  sendInterrupt(): Promise<void> {
    return this.write(new Uint8Array([3]));
  }
  private write(bytes: Uint8Array): Promise<void> {
    if (
      this.current.phase !== "connected" ||
      this.stopping ||
      !this.port?.writable
    )
      return Promise.reject(new Error("串口未连接。"));
    if (this.writing) return Promise.reject(new Error("上一条输入仍在发送。"));
    const task = (async () => {
      try {
        this.writer = this.port!.writable!.getWriter();
        await this.writer.write(bytes);
      } catch {
        this.pendingError = "串口发送失败，请检查连接后重试。";
        throw new Error(this.pendingError);
      } finally {
        this.writer?.releaseLock();
        this.writer = undefined;
        // No local echo and no command history, storage or console logging.
        bytes.fill(0);
      }
    })();
    this.writing = task;
    void task.then(
      () => {
        if (this.writing === task) this.writing = undefined;
      },
      () => {
        if (this.writing === task) this.writing = undefined;
        void this.disconnect();
      },
    );
    return task;
  }
  disconnect(): Promise<void> {
    if (this.closing) return this.closing;
    if (this.current.phase === "error" && this.port) this.pendingError = "";
    this.stopping = true;
    this.notify("disconnecting");
    const task = (async () => {
      await this.connecting;
      try {
        await this.reader?.cancel();
      } catch {
        /* Already disconnected. */
      }
      await this.reading;
      try {
        await this.writer?.abort();
      } catch {
        /* Already disconnected. */
      }
      try {
        await this.writing;
      } catch {
        /* Error is represented in state. */
      }
      let closed = true;
      if (
        this.port &&
        (this.opened || this.port.readable || this.port.writable)
      ) {
        try {
          await this.port.close();
        } catch {
          closed = false;
          this.pendingError =
            "串口释放失败，请点击断开重试，或关闭本页面释放浏览器连接。";
        }
      }
      if (closed) {
        this.port = undefined;
        this.opened = false;
      }
      this.reading = undefined;
      this.notify(this.pendingError ? "error" : "idle", this.pendingError);
    })();
    this.closing = task;
    void task.then(() => {
      if (this.closing === task) this.closing = undefined;
    });
    return task;
  }
  async dispose(): Promise<void> {
    this.disposed = true;
    this.api.removeEventListener?.("disconnect", this.disconnected);
    await this.disconnect();
    this.buffer.clear();
  }
}
