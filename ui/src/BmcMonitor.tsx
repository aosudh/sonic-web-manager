import {
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  DataTable,
  Empty,
  Icon,
  Notice,
  Panel,
  Properties,
} from "./components";
import { display, timestamp } from "./model";
import {
  SerialController,
  SERIAL_BAUD_RATES,
  serialUnavailableReason,
  type SerialApiLike,
  type SerialState,
} from "./serial";
import type { BmcSection, BmcSnapshot, Session } from "./types";
const RemoteConsoles = lazy(() =>
  import("./RemoteConsoles").then((module) => ({
    default: module.RemoteConsoles,
  })),
);

function CollectionState({ state }: { state?: string }) {
  return (
    <span
      className={`bmc-collection-state ${state === "partial" ? "partial" : ""}`}
    >
      <Icon name={state === "partial" ? "warn" : "info"} size={13} />
      {state === "ok"
        ? "读取完成"
        : state === "partial"
          ? "部分可用"
          : "读取状态未知"}
    </span>
  );
}
function ReadSection({
  title,
  section,
}: {
  title: string;
  section?: BmcSection;
}) {
  return (
    <Panel title={title} aside={<CollectionState state={section?.state} />}>
      {section?.error && <Notice kind="warn">{section.error}</Notice>}
      {section?.rows.length ? (
        <Properties
          items={section.rows.map((row) => ({
            label: row.field,
            value: row.value,
          }))}
        />
      ) : (
        <Empty title="尚无可用信息">
          本次采集未获得这些字段，请检查采集状态。
        </Empty>
      )}
    </Panel>
  );
}
function localSerialApi(): SerialApiLike | undefined {
  return (navigator as Navigator & { serial?: SerialApiLike }).serial;
}

export function LocalSerialConsole({
  mode,
  role,
}: {
  mode: Session["mode"];
  role: Session["role"];
}) {
  const [state, setState] = useState<SerialState>({
    phase: "idle",
    error: "",
    hasPort: false,
  });
  const [output, setOutput] = useState("");
  const [input, setInput] = useState("");
  const [hiddenInput, setHiddenInput] = useState(false);
  const [baud, setBaud] = useState(115200);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [follow, setFollow] = useState(true);
  const controller = useRef<SerialController | undefined>(undefined);
  const terminal = useRef<HTMLPreElement>(null);
  const permission = serialUnavailableReason(
    mode,
    role,
    window.isSecureContext,
    !!localSerialApi(),
  );
  useEffect(() => {
    setState({ phase: "idle", error: "", hasPort: false });
    setInput("");
    setOutput("");
    const api = localSerialApi();
    if (permission || !api) return;
    const serial = new SerialController(api, {
      onState: setState,
      onOutput: setOutput,
    });
    controller.current = serial;
    // pagehide also covers back-forward cache; retain an idle controller if restored.
    const leave = () => {
      void serial.disconnect();
    };
    window.addEventListener("pagehide", leave);
    return () => {
      window.removeEventListener("pagehide", leave);
      controller.current = undefined;
      void serial.dispose();
    };
  }, [permission]);
  useEffect(() => {
    if (follow && terminal.current)
      terminal.current.scrollTop = terminal.current.scrollHeight;
  }, [output, follow]);
  const connected = state.phase === "connected";
  const busy = ["selecting", "opening", "disconnecting"].includes(state.phase);
  const phaseLabel = {
    idle: "未连接",
    selecting: "等待选择串口",
    opening: "正在打开串口",
    connected: "本地串口已连接",
    disconnecting: "正在释放串口",
    error: "连接需检查",
  }[state.phase];
  async function connect() {
    setError("");
    setInput("");
    if (permission || !controller.current) return;
    try {
      await controller.current.connect(baud);
    } catch (e) {
      setError(e instanceof Error ? e.message : "连接失败，请重试。");
    }
  }
  async function send(interrupt = false) {
    if (sending || !connected || !controller.current) return;
    const line = input;
    // Do not retain submitted text, even if the write fails.
    setInput("");
    setSending(true);
    setError("");
    try {
      await (interrupt
        ? controller.current.sendInterrupt()
        : controller.current.sendLine(line));
    } catch (e) {
      setError(e instanceof Error ? e.message : "发送失败，请检查连接。");
    } finally {
      setSending(false);
    }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    void send();
  }
  return (
    <Panel
      title="浏览器本地串口"
      className="serial-panel"
      aside={
        <span className={`serial-phase ${connected ? "connected" : ""}`}>
          <i />
          {phaseLabel}
        </span>
      }
    >
      <p className="serial-intro">
        连接当前电脑的 USB / 串口线，与 BMC 网络 SOL 独立。行式控制台，非完整
        VT100；全屏程序和光标定位不受支持。仅显示设备返回的文本，不自动发送输入。
      </p>
      {permission && <Notice>{permission}</Notice>}
      {(error || state.error) && (
        <Notice kind="error">{error || state.error}</Notice>
      )}
      <div className="serial-toolbar">
        <label>
          波特率
          <select
            aria-label="串口波特率"
            value={baud}
            onChange={(event) => setBaud(Number(event.target.value))}
            disabled={!!permission || busy || state.hasPort}
          >
            {SERIAL_BAUD_RATES.map((rate) => (
              <option key={rate} value={rate}>
                {rate}
              </option>
            ))}
          </select>
        </label>
        <span className="serial-format">
          8 数据位 · 无校验 · 1 停止位 · 无流控
        </span>
        <button
          className="primary"
          onClick={connect}
          disabled={!!permission || busy || state.hasPort}
        >
          <Icon name="network" size={14} />
          {state.phase === "selecting" ? "等待选口…" : "选择串口并连接"}
        </button>
        <button
          disabled={
            !!permission ||
            state.phase === "disconnecting" ||
            (!state.hasPort && !busy)
          }
          onClick={() => {
            setInput("");
            void controller.current?.disconnect();
          }}
        >
          断开
        </button>
      </div>
      <div className="serial-output-header">
        <span>设备输出 · 仅保留当前页面最近 64 KiB 文本</span>
        <div>
          <label>
            <input
              type="checkbox"
              checked={follow}
              onChange={(event) => setFollow(event.target.checked)}
            />
            跟随输出
          </label>
          <button
            className="link-button"
            onClick={() => controller.current?.clearOutput()}
            disabled={!output}
          >
            清空显示
          </button>
        </div>
      </div>
      <pre
        className="serial-output"
        aria-label="串口设备输出"
        tabIndex={0}
        ref={terminal}
      >
        {output || "尚无输出。连接后可主动发送空 Enter 让设备显示提示符。"}
      </pre>
      <form className="serial-input-form" onSubmit={submit} autoComplete="off">
        <label className="sr-only" htmlFor="serial-line">
          发送一行到串口
        </label>
        <input
          id="serial-line"
          type={hiddenInput ? "password" : "text"}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          autoCapitalize="off"
          maxLength={4096}
          disabled={!connected || !!permission || sending}
          placeholder="输入一行；留空发送 Enter"
        />
        <button
          type="submit"
          className="primary"
          disabled={!connected || !!permission || sending}
        >
          {sending ? "发送中…" : input ? "发送一行" : "发送 Enter"}
        </button>
        <button
          type="button"
          disabled={!connected || !!permission || sending}
          onClick={() => {
            void send(true);
          }}
        >
          Ctrl-C
        </button>
      </form>
      <div className="serial-privacy">
        <label>
          <input
            type="checkbox"
            checked={hiddenInput}
            onChange={(event) => setHiddenInput(event.target.checked)}
          />
          隐藏输入
        </label>
        <span>
          输入不作本地回显、不保存历史或日志。设备自行回显的内容仍会出现在输出区；离开本页或退出会释放串口。
        </span>
      </div>
    </Panel>
  );
}

export function BmcMonitor({
  snapshot,
  session,
}: {
  snapshot?: BmcSnapshot;
  session: Session;
}) {
  const [tab, setTab] = useState(0);
  const [consoleVisited, setConsoleVisited] = useState(false);
  const collection = snapshot?.collection;
  const events = snapshot?.events;
  const sensors = snapshot?.sensors;
  const tabs = ["BMC 概览", "网络与控制台", "传感器", "SEL 事件"];
  const numberWithUnit = (value: number | null | undefined, unit: string) =>
    value != null && Number.isFinite(value) ? `${value} ${unit}` : "未知";
  return (
    <div className="bmc-monitor">
      <div className="bmc-sampling">
        <div>
          <Icon name="info" size={16} />
          <strong>
            {snapshot?.mode === "demo" ? "DEMO · 模拟 BMC" : "BMC 只读监控"}
          </strong>
          <span>采集状态不等同于硬件健康</span>
        </div>
        <dl>
          <div>
            <dt>本次采样</dt>
            <dd>
              {collection?.sampled_at
                ? timestamp(collection.sampled_at)
                : "未知"}
            </dd>
          </div>
          <div>
            <dt>缓存周期</dt>
            <dd>{numberWithUnit(collection?.cache_seconds, "秒")}</dd>
          </div>
          <div>
            <dt>采集耗时</dt>
            <dd>{numberWithUnit(collection?.elapsed_ms, "ms")}</dd>
          </div>
        </dl>
      </div>
      <div className="bmc-tabs" role="tablist" aria-label="BMC 信息分类">
        {tabs.map((label, index) => (
          <button
            key={label}
            role="tab"
            id={`bmc-tab-${index}`}
            aria-selected={tab === index}
            aria-controls={`bmc-panel-${index}`}
            className={tab === index ? "active" : ""}
            onClick={() => {
              setTab(index);
              if (index === 1) setConsoleVisited(true);
            }}
          >
            {label}
            {index === 2 && sensors ? (
              <small>{sensors.rows.length}</small>
            ) : index === 3 && events ? (
              <small>{Math.min(30, events.rows.length)}</small>
            ) : null}
          </button>
        ))}
      </div>
      <div
        id="bmc-panel-0"
        role="tabpanel"
        aria-labelledby="bmc-tab-0"
        hidden={tab !== 0}
      >
        <div className="bmc-grid">
          <ReadSection title="控制器身份" section={snapshot?.identity} />
          <ReadSection title="控制器状态" section={snapshot?.status} />
        </div>
        <Notice>
          IPMI
          命令返回成功只代表本次读取完成。请结合传感器状态、采样时间与事件记录判断硬件状态。
        </Notice>
      </div>
      <div
        id="bmc-panel-1"
        role="tabpanel"
        aria-labelledby="bmc-tab-1"
        hidden={tab !== 1}
      >
        <div className="bmc-grid">
          <ReadSection title="BMC 网络配置" section={snapshot?.network} />
          <Panel
            title="BMC 网络 SOL 能力"
            aside={<CollectionState state={snapshot?.console.state} />}
          >
            <Properties
              items={[
                {
                  label: "SOL 支持",
                  value:
                    snapshot?.console.supported === true
                      ? "支持；连接可用性需另行核对"
                      : snapshot?.console.supported === false
                        ? "未支持"
                        : "未知",
                },
                { label: "网页 SOL 连接", value: "未开放" },
                ...(snapshot?.console.rows.map((row) => ({
                  label: row.field,
                  value: row.value,
                })) || []),
              ]}
            />
            <Notice>
              {snapshot?.console.reason ||
                "未确认可用的 BMC 网络控制台；此页面不创建 SOL 会话。"}
            </Notice>
            {snapshot?.console.error && (
              <Notice kind="warn">{snapshot.console.error}</Notice>
            )}
          </Panel>
        </div>
        {consoleVisited && (
          <Suspense fallback={<Notice>正在加载交互终端…</Notice>}>
            <RemoteConsoles session={session} active={tab === 1} />
          </Suspense>
        )}
        <details className="local-serial-optional">
          <summary>可选：连接本电脑的 USB / 本地串口</summary>
          <LocalSerialConsole
            mode={
              session.mode === "live" && snapshot?.mode === "demo"
                ? "demo"
                : session.mode
            }
            role={session.role}
          />
        </details>
      </div>
      <div
        id="bmc-panel-2"
        role="tabpanel"
        aria-labelledby="bmc-tab-2"
        hidden={tab !== 2}
      >
        <Panel
          title="BMC 传感器"
          aside={
            <span className="muted">{sensors?.rows.length ?? 0} 条读数</span>
          }
        >
          {sensors?.error && <Notice kind="warn">{sensors.error}</Notice>}
          <DataTable
            columns={[
              { key: "category", label: "类别" },
              { key: "name", label: "传感器" },
              { key: "value", label: "读数" },
              { key: "unit", label: "单位" },
              { key: "status", label: "状态" },
              { key: "sample_time", label: "采样时间（本地）" },
            ]}
            rows={(sensors?.rows || []).map((row) => ({
              ...row,
              sample_time: row.timestamp ? timestamp(row.timestamp) : "未知",
            }))}
            empty="没有可用传感器读数；不代表设备健康。"
          />
        </Panel>
      </div>
      <div
        id="bmc-panel-3"
        role="tabpanel"
        aria-labelledby="bmc-tab-3"
        hidden={tab !== 3}
      >
        <Panel
          title="最近 30 条 SEL 事件"
          aside={<CollectionState state={events?.state} />}
        >
          <Notice>
            SEL
            是历史事件记录。断言与恢复可能成对出现，不能把历史断言直接当作当前告警；本页不清除事件。
          </Notice>
          {events?.error && <Notice kind="warn">{events.error}</Notice>}
          <DataTable
            columns={[
              { key: "id", label: "事件 ID" },
              { key: "event_time", label: "时间（BMC 原始）" },
              { key: "sensor", label: "传感器" },
              { key: "event", label: "事件" },
              { key: "direction", label: "方向" },
            ]}
            rows={(events?.rows || []).slice(0, 30).map((row) => ({
              ...row,
              event_time: row.timestamp ? display(row.timestamp) : "未知",
            }))}
            empty={
              events?.state === "ok"
                ? "本次读取没有返回 SEL 事件。"
                : "未能取得 SEL 记录，不能据此认定没有告警。"
            }
          />
        </Panel>
      </div>
      <details className="bmc-command-details">
        <summary>
          查看采集详情{" "}
          <span>{collection?.commands.length ?? 0} 项命令记录</span>
        </summary>
        <DataTable
          columns={[
            { key: "section", label: "分类" },
            { key: "command_id", label: "采集项" },
            { key: "result", label: "读取结果" },
            { key: "duration", label: "耗时" },
            { key: "error", label: "读取错误" },
          ]}
          rows={(collection?.commands || []).map((row) => ({
            ...row,
            result: row.ok === true ? "读取成功" : "未读取成功",
            duration: numberWithUnit(row.elapsed_ms, "ms"),
            error: row.error || "—",
          }))}
          empty="尚未提供命令采集记录。"
        />
      </details>
    </div>
  );
}
