import { useCallback, useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api } from "./api";
import { Icon, Notice, Panel } from "./components";
import { timestamp } from "./model";
import {
  RemoteConsoleController,
  consoleUnavailableReason,
  type ConsoleCapabilities,
  type ConsoleRequest,
  type ConsoleState,
  type ConsoleTarget,
} from "./remoteConsole";
import type { Session } from "./types";

function RemoteTerminal({
  target,
  session,
  capabilities,
  request,
  active,
  onConnected,
}: {
  target: ConsoleTarget;
  session: Session;
  capabilities?: ConsoleCapabilities;
  request: ConsoleRequest;
  active: boolean;
  onConnected: (target: ConsoleTarget, connected: boolean) => void;
}) {
  const [state, setState] = useState<ConsoleState>({
    phase: "idle",
    message: "",
    hasSession: false,
  });
  const [notice, setNotice] = useState("");
  const host = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | undefined>(undefined);
  const fit = useRef<FitAddon | undefined>(undefined);
  const connection = useRef<RemoteConsoleController | undefined>(undefined);
  const permission = consoleUnavailableReason(
    session.mode,
    session.role,
    capabilities,
    target,
  );
  const targetName = target === "sonic" ? "SONiC 系统终端" : "BMC 系统终端";
  const resize = useCallback(() => {
    if (
      !host.current ||
      host.current.clientWidth < 20 ||
      host.current.clientHeight < 20
    )
      return;
    try {
      const size = fit.current?.proposeDimensions();
      if (size)
        terminal.current?.resize(
          Math.max(10, Math.min(400, size.cols)),
          Math.max(2, Math.min(200, size.rows)),
        );
    } catch {
      /* Hidden containers are fitted once visible. */
    }
  }, []);
  useEffect(() => {
    if (!host.current) return;
    const element = host.current;
    const term = new Terminal({
      cols: 80,
      rows: 24,
      cursorBlink: true,
      disableStdin: true,
      fontFamily: 'Consolas, "Cascadia Code", "Courier New", monospace',
      fontSize: 13,
      scrollback: 1000,
      convertEol: false,
      allowProposedApi: false,
      logLevel: "off",
      linkHandler: { activate: () => {} },
      theme: {
        background: "#13232d",
        foreground: "#d6e3eb",
        cursor: "#d4ecfa",
        selectionBackground: "#355972",
        black: "#152630",
        brightBlack: "#718b9b",
        red: "#e88787",
        green: "#8ccca5",
        yellow: "#e4c785",
        blue: "#7eb7e0",
        magenta: "#bba0d7",
        cyan: "#80c6ca",
        white: "#dbe8ee",
      },
    });
    const addon = new FitAddon();
    term.loadAddon(addon);
    term.open(element);
    terminal.current = term;
    fit.current = addon;
    const controller = new RemoteConsoleController(target, request, {
      onState: (value) => {
        setState(value);
        term.options.disableStdin = value.phase !== "connected";
        onConnected(target, value.phase === "connected");
      },
      onOutput: (bytes) => {
        term.write(bytes);
      },
      onNotice: setNotice,
    });
    connection.current = controller;
    setState(controller.state);
    setNotice("");
    const input = term.onData((text) => {
      controller.sendText(text);
    });
    const binary = term.onBinary((text) => {
      controller.sendBinary(text);
    });
    const sizing = term.onResize((size) => {
      controller.resize(size.rows, size.cols);
    });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    const leave = () => {
      void controller.disconnect();
    };
    window.addEventListener("pagehide", leave);
    resize();
    return () => {
      window.removeEventListener("pagehide", leave);
      observer.disconnect();
      input.dispose();
      binary.dispose();
      sizing.dispose();
      void controller.dispose();
      onConnected(target, false);
      term.dispose();
      element.replaceChildren();
      connection.current = undefined;
      terminal.current = undefined;
      fit.current = undefined;
    };
  }, [target, request, permission, onConnected, resize]);
  useEffect(() => {
    if (!active) return;
    const frame = requestAnimationFrame(resize);
    return () => cancelAnimationFrame(frame);
  }, [active, resize]);
  const connected = state.phase === "connected";
  const busy = state.phase === "connecting" || state.phase === "disconnecting";
  const phaseLabel = {
    idle: "未连接",
    connecting: "正在创建会话",
    connected: "已连接",
    disconnecting: "正在释放会话",
    closed: "已关闭",
    error: "连接需检查",
  }[state.phase];
  async function connect() {
    if (permission || !connection.current || !terminal.current) return;
    setNotice("");
    terminal.current.reset();
    resize();
    try {
      await connection.current.connect(
        terminal.current.rows,
        terminal.current.cols,
      );
    } catch {
      setNotice("当前会话尚未释放，请先断开。");
    }
    if (connection.current?.state.phase === "connected")
      terminal.current?.focus();
  }
  return (
    <div
      className="remote-terminal-panel"
      hidden={!active}
      role="tabpanel"
      id={`remote-panel-${target}`}
      aria-labelledby={`remote-tab-${target}`}
    >
      <div className="remote-target-heading">
        <div>
          <strong>{targetName}</strong>
          <span>
            {capabilities?.[target].identity ||
              (target === "sonic"
                ? `当前管理账号：${session.user || "未知"} · 本机 SONiC`
                : "服务器预配置的 BMC SSH 目标")}
          </span>
        </div>
        <span className={`serial-phase ${connected ? "connected" : ""}`}>
          <i />
          {phaseLabel}
        </span>
      </div>
      {permission && <Notice>{permission}</Notice>}
      {state.message && (
        <Notice kind={state.phase === "error" ? "error" : "info"}>
          {state.message}
        </Notice>
      )}
      {notice && <Notice kind="warn">{notice}</Notice>}
      <div className="remote-terminal-tools">
        <button
          className="primary"
          disabled={!!permission || busy || state.hasSession}
          onClick={connect}
        >
          <Icon name="network" size={14} />
          {busy ? "处理中…" : `连接 ${target === "sonic" ? "SONiC" : "BMC"}`}
        </button>
        <button
          disabled={
            state.phase === "disconnecting" ||
            (!state.hasSession && state.phase !== "connecting")
          }
          onClick={() => {
            void connection.current?.disconnect();
          }}
        >
          断开
        </button>
        <button
          disabled={!connected}
          onClick={() => {
            connection.current?.sendText("\x03");
            terminal.current?.focus();
          }}
        >
          Ctrl-C
        </button>
        <button
          className="link-button"
          onClick={() => terminal.current?.clear()}
        >
          清空显示
        </button>
        <span>
          {state.expiresAt
            ? `最迟结束 ${timestamp(state.expiresAt)}`
            : "5 分钟无输入自动关闭 · 单次最长 30 分钟"}
        </span>
      </div>
      <div className="remote-terminal-screen">
        <div className="xterm-host" ref={host} aria-label={targetName} />
        {state.phase === "idle" && (
          <div className="remote-terminal-empty">
            <Icon name="network" size={25} />
            <strong>{targetName}</strong>
            <span>
              {permission
                ? "当前未建立系统会话"
                : "点击连接后，在此使用交互终端"}
            </span>
          </div>
        )}
      </div>
      <div className="remote-terminal-footnote">
        <span>
          完整交互终端；按目标系统提示登录，密码回显由目标终端控制。页面不保存输入、输出或密码。
        </span>
        <span>无自动命令或自动重连</span>
      </div>
    </div>
  );
}

export function RemoteConsoles({
  session,
  active,
}: {
  session: Session;
  active: boolean;
}) {
  const [capabilities, setCapabilities] = useState<ConsoleCapabilities>();
  const [capError, setCapError] = useState("");
  const [target, setTarget] = useState<ConsoleTarget>("sonic");
  const [connected, setConnected] = useState({ sonic: false, bmc: false });
  const [refresh, setRefresh] = useState(0);
  const request = useCallback<ConsoleRequest>(
    (path, options = {}) => api(path, { ...options, csrf: session.csrf }),
    [session.csrf],
  );
  const onConnected = useCallback(
    (name: ConsoleTarget, value: boolean) =>
      setConnected((old) => ({ ...old, [name]: value })),
    [],
  );
  useEffect(() => {
    setCapError("");
    if (session.mode !== "live" || session.role !== "admin") {
      setCapabilities(undefined);
      return;
    }
    const abort = new AbortController();
    request<ConsoleCapabilities>("/consoles/capabilities", {
      signal: abort.signal,
    })
      .then(setCapabilities)
      .catch((error) => {
        if (!abort.signal.aborted) {
          setCapabilities(undefined);
          setCapError("终端能力读取失败，请检查管理服务后重试。");
        }
      });
    return () => abort.abort();
  }, [session.mode, session.role, request, refresh]);
  return (
    <Panel
      title="远程系统终端"
      aside={
        <button
          className="link-button"
          disabled={session.mode !== "live" || session.role !== "admin"}
          onClick={() => setRefresh((value) => value + 1)}
        >
          刷新连接能力
        </button>
      }
    >
      <p className="remote-terminal-intro">
        SONiC 终端进入当前账号的系统 Shell；BMC 终端通过服务器固定的 SSH
        目标进入 BMC 自身系统，不是
        SOL。终端指令直接执行，配置操作不经过网页计划预览。
      </p>
      {capError && <Notice kind="error">{capError}</Notice>}
      <div
        className="remote-target-tabs"
        role="tablist"
        aria-label="远程终端目标"
      >
        {(["sonic", "bmc"] as const).map((name) => (
          <button
            key={name}
            id={`remote-tab-${name}`}
            role="tab"
            aria-selected={target === name}
            aria-controls={`remote-panel-${name}`}
            className={target === name ? "active" : ""}
            onClick={() => setTarget(name)}
          >
            {name === "sonic" ? "SONiC 终端" : "BMC 终端"}
            {connected[name] && <i title="已连接" />}
          </button>
        ))}
      </div>
      {(["sonic", "bmc"] as const).map((name) => (
        <RemoteTerminal
          key={name}
          target={name}
          session={session}
          capabilities={capabilities}
          request={request}
          active={active && target === name}
          onConnected={onConnected}
        />
      ))}
    </Panel>
  );
}
