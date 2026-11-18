import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import catalogFallback from "../../shared/catalog.json";
import releaseVersion from "../../VERSION?raw";
import { api } from "./api";
import { BmcMonitor } from "./BmcMonitor";
import {
  Badge,
  DataTable,
  Empty,
  Icon,
  LineChart,
  Metrics,
  Notice,
  Panel,
  PortMap,
  Properties,
} from "./components";
import {
  display,
  initialValues,
  speed,
  timestamp,
  uptime,
  pendingSaveState,
  portConfiguration,
  selectedPortModes,
  configurationRequest,
  percent,
  cpuDisplay,
  fixedFanReason,
  deferredOperation,
  operationPending,
  operationTitle,
  type PortConfiguration,
} from "./model";
import type {
  Action,
  Catalog,
  Field,
  FanControl,
  Job,
  Operation,
  Overview,
  PageData,
  Plan,
  Port,
  PortDetail,
  PortsData,
  Session,
} from "./types";

const catalogDefault = catalogFallback as Catalog;
const RemoteConsoles = lazy(() =>
  import("./RemoteConsoles").then((module) => ({
    default: module.RemoteConsoles,
  })),
);
const portColumns = [
  { key: "name", label: "接口" },
  { key: "alias", label: "别名" },
  { key: "cage_id", label: "物理模块" },
  { key: "admin_status", label: "管理状态" },
  { key: "oper_status", label: "链路状态" },
  { key: "speed", label: "速率", type: "speed" },
  { key: "mtu", label: "MTU" },
  { key: "fec", label: "FEC" },
  { key: "rx_bps", label: "接收速率" },
  { key: "tx_bps", label: "发送速率" },
  { key: "description", label: "描述" },
];
const errorMessage = (e: unknown) =>
  e instanceof Error ? e.message : String(e);
const isAbort = (e: unknown) => e instanceof Error && e.name === "AbortError";

function useDialog(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const root = ref.current;
    root
      ?.querySelector<HTMLElement>(
        'button:not(:disabled), input, select, textarea, [tabindex="0"]',
      )
      ?.focus();
    const keydown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close.current();
      }
      if (e.key !== "Tab" || !root) return;
      const targets = [
        ...root.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]',
        ),
      ].filter((el) => el.offsetParent !== null);
      if (!targets.length) {
        e.preventDefault();
        return;
      }
      if (e.shiftKey && document.activeElement === targets[0]) {
        e.preventDefault();
        targets.at(-1)?.focus();
      } else if (!e.shiftKey && document.activeElement === targets.at(-1)) {
        e.preventDefault();
        targets[0].focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.body.style.overflow = previousOverflow;
      previous?.focus();
    };
  }, [open]);
  return ref;
}

function Login({
  mode,
  onLogin,
  serviceError,
  onRetry,
}: {
  mode?: string;
  onLogin: (s: Session) => void;
  serviceError: string;
  onRetry: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const session = await api<Session>("/login", {
        method: "POST",
        body: { username, password },
      });
      setPassword("");
      onLogin(session);
    } catch (e) {
      setError(errorMessage(e));
      setPassword("");
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-shell">
      <div className="login-brand">
        <span className="brand-mark">
          <Icon name="network" size={26} />
        </span>
        <div>
          <strong>SONiC</strong>
          <span>WEB MANAGER</span>
        </div>
      </div>
      <section className="login-panel">
        <div className="login-heading">
          <span className="eyebrow">设备管理控制台</span>
          <h1>登录交换机</h1>
          <p>使用设备账户访问配置与运行状态</p>
        </div>
        {mode === "demo" && (
          <Notice kind="warn">
            <b>DEMO 演示环境</b>
            <br />
            所有数据与操作均为模拟。使用 demo / demo 登录。
          </Notice>
        )}
        {serviceError && (
          <Notice kind="error">
            {serviceError}{" "}
            <button className="link-button" onClick={onRetry}>
              重新连接
            </button>
          </Notice>
        )}
        <form onSubmit={submit}>
          <label className="field-label" htmlFor="username">
            用户名
          </label>
          <input
            id="username"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            disabled={busy}
          />
          <label className="field-label" htmlFor="password">
            密码
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            disabled={busy}
          />
          {error && <Notice kind="error">{error}</Notice>}
          <button className="primary login-submit" disabled={busy}>
            {busy ? <span className="spinner" /> : <Icon name="lock" />}
            {busy ? "正在验证…" : "登录"}
          </button>
        </form>
        <div className="login-note">
          <Icon name="lock" size={14} />
          凭据由设备验证，不保存在浏览器中
        </div>
      </section>
      <footer className="login-footer">SONiC Web Manager · 独立管理应用</footer>
    </div>
  );
}

function FieldControl({
  field,
  value,
  onChange,
  ports,
}: {
  field: Field;
  value: unknown;
  onChange: (v: unknown) => void;
  ports: Port[];
}) {
  const id = `field-${field.name}`;
  const options: NonNullable<Field["options"]> = field.options?.length
    ? field.options
    : field.name === "interfaces"
      ? ports.map((p) => ({
          label: `${p.name}${p.alias ? ` · ${p.alias}` : ""}`,
          value: p.name,
        }))
      : [];
  if (field.type === "checkbox")
    return (
      <div className="form-field">
        <label className="check-label">
          <input
            id={id}
            type="checkbox"
            checked={value === true}
            onChange={(e) => onChange(e.target.checked)}
          />
          {field.label}
        </label>
        {field.help && <small>{field.help}</small>}
      </div>
    );
  return (
    <div className="form-field">
      <label htmlFor={id}>
        {field.label}
        {field.required && <em>*</em>}
      </label>
      {field.type === "select" ? (
        <select
          id={id}
          value={String(value ?? "")}
          required={field.required}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">
            {field.required ? "请选择" : "保持不变 / 不设置"}
          </option>
          {options.map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
              {o.disabled ? ` · ${o.reason || "当前不可用"}` : ""}
            </option>
          ))}
        </select>
      ) : field.type === "multiselect" ? (
        <div
          className="multi-select"
          role="group"
          aria-labelledby={`${id}-label`}
        >
          <span id={`${id}-label`} className="sr-only">
            {field.label}
          </span>
          <div className="multi-summary">
            已选 {Array.isArray(value) ? value.length : 0} 项
            <button
              type="button"
              className="link-button"
              onClick={() => onChange([])}
            >
              清空
            </button>
          </div>
          <div className="multi-options">
            {options.length ? (
              options.map((o) => (
                <label key={o.value}>
                  <input
                    type="checkbox"
                    checked={Array.isArray(value) && value.includes(o.value)}
                    onChange={(e) => {
                      const items = Array.isArray(value) ? [...value] : [];
                      onChange(
                        e.target.checked
                          ? [...items, o.value]
                          : items.filter((v) => v !== o.value),
                      );
                    }}
                  />
                  <span>{o.label}</span>
                </label>
              ))
            ) : (
              <span className="muted">服务未提供可选项</span>
            )}
          </div>
        </div>
      ) : field.type === "textarea" ? (
        <textarea
          id={id}
          rows={3}
          value={String(value ?? "")}
          required={field.required}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          id={id}
          type={field.type === "number" ? "number" : "text"}
          step={field.type === "number" ? (field.step ?? "any") : undefined}
          min={field.min}
          max={field.max}
          value={String(value ?? "")}
          required={field.required}
          autoComplete="off"
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      <small>
        {field.help ??
          (!field.required ? "留空表示不设置此项；具体规则由设备校验。" : "")}
      </small>
    </div>
  );
}

function ActionDialog({
  action,
  session,
  revision,
  selected,
  ports,
  relatedActions = [],
  control,
  onClose,
  onComplete,
  onDeferredOperation,
  onAwaitRestore,
}: {
  action: Action;
  session: Session;
  revision?: string;
  selected: string[];
  ports: Port[];
  relatedActions?: Action[];
  control?: FanControl;
  onClose: () => void;
  onComplete: () => void;
  onDeferredOperation: (operation: Operation) => void;
  onAwaitRestore: () => void;
}) {
  const [values, setValues] = useState(() => {
    const defaults = initialValues(action, selected);
    if (action.id === "fan.control" && control) {
      if (
        action.fields
          .find((field) => field.name === "mode")
          ?.options?.some((option) => option.value === control.mode)
      )
        defaults.mode = control.mode;
      if (control.duty_percent != null)
        defaults.duty_percent = control.duty_percent;
    }
    return defaults;
  });
  const [plan, setPlan] = useState<Plan>();
  const [job, setJob] = useState<Job>();
  const [jobId, setJobId] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pollAttempt, setPollAttempt] = useState(0);
  const portNames = Array.isArray(values.interfaces)
    ? values.interfaces.map(String)
    : [];
  const modeChoices =
    action.id === "port.configure"
      ? selectedPortModes(ports, portNames, relatedActions)
      : { modes: [], reason: "" };
  const portMode = modeChoices.modes.find(
    (mode) => mode.value === values.port_mode,
  );
  const breakout = portMode?.kind === "breakout";
  const formFields: Field[] = action.fields
    .filter((field) => {
      if (action.id === "port.configure" && breakout)
        return ["interfaces", "speed"].includes(field.name);
      if (action.id === "fan.control" && field.name === "duty_percent")
        return values.mode === "fixed";
      return true;
    })
    .map((field) => {
      if (action.id === "port.configure" && field.name === "speed")
        return {
          ...field,
          name: "port_mode",
          label: "速率 / 拆分模式",
          type: "select",
          required: false,
          options: modeChoices.modes.map((mode) => ({
            value: mode.value,
            label: mode.label,
            disabled: mode.available !== true,
            reason: mode.reason,
          })),
          help: modeChoices.reason,
        };
      if (action.id === "fan.control" && field.name === "duty_percent")
        return {
          ...field,
          label: "固定占空比（%）",
          required: true,
          min: control?.min_percent ?? undefined,
          max: control?.max_percent ?? undefined,
          step: control?.step_percent ?? undefined,
          help:
            control?.min_percent != null && control?.max_percent != null
              ? `设备允许范围：${percent(control.min_percent)}–${percent(control.max_percent)}。`
              : "设备尚未提供允许的调节范围。",
        };
      if (action.id === "fan.control" && field.name === "mode")
        return {
          ...field,
          options: field.options?.map((option) =>
            option.value === "fixed" && fixedFanReason(control)
              ? { ...option, disabled: true, reason: fixedFanReason(control) }
              : option,
          ),
        };
      return field;
    });
  const running =
    !!jobId && !["succeeded", "failed"].includes(job?.status ?? "running");
  const finished = useRef(false);
  const awaitingRestore = running && !!error && plan?.reboot_required === true;
  const dialog = useDialog(true, () => {
    if (!busy && (!running || awaitingRestore)) {
      if (awaitingRestore) onAwaitRestore();
      onClose();
    }
  });
  useEffect(() => {
    if (!jobId) return;
    let stopped = false,
      timer: ReturnType<typeof setTimeout>;
    const started = Date.now();
    async function poll() {
      try {
        const next = await api<Job>(`/jobs/${encodeURIComponent(jobId)}`);
        if (stopped) return;
        setJob(next);
        setError("");
        if (next.status === "succeeded" || next.status === "failed") {
          if (!finished.current) {
            finished.current = true;
            const deferred = deferredOperation(next);
            if (deferred) onDeferredOperation(deferred);
            onComplete();
          }
        } else if (Date.now() - started < 600000)
          timer = setTimeout(poll, 2500);
        else
          setError(
            "任务仍未结束。自动查询已暂停，可点击“查询任务状态”继续；不要重复提交。",
          );
      } catch (e) {
        if (!stopped)
          setError(
            `无法读取任务状态：${errorMessage(e)}。请查询原任务，不要重复提交。`,
          );
      }
    }
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [jobId, pollAttempt, onComplete, onDeferredOperation]);
  async function createPlan(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const next = await api<Plan>("/plan", {
        method: "POST",
        csrf: session.csrf,
        body: {
          ...configurationRequest(
            action,
            values,
            ports,
            relatedActions,
            control,
          ),
          revision,
        },
      });
      setPlan(next);
      setAck(false);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  async function apply() {
    if (!plan || !ack) return;
    setBusy(true);
    setError("");
    try {
      const next = await api<{ job_id: string }>("/apply", {
        method: "POST",
        csrf: session.csrf,
        body: { plan_id: plan.id, acknowledged: true },
      });
      setJobId(next.job_id);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }
  const failed = job?.status === "failed" || job?.result?.success === false;
  const deferred = deferredOperation(job);
  return (
    <div className="modal-backdrop">
      <div
        className="modal action-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="action-title"
        ref={dialog}
      >
        <header>
          <div>
            <span className="eyebrow">
              {session.mode === "demo" ? "DEMO · 模拟操作" : "设备配置"}
            </span>
            <h2 id="action-title">{action.label}</h2>
          </div>
          <button
            className="icon-button"
            aria-label="关闭操作窗口"
            disabled={busy || running}
            onClick={onClose}
          >
            <Icon name="close" size={20} />
          </button>
        </header>
        <div className="workflow-steps">
          <span className={!plan ? "active" : "done"}>1 填写参数</span>
          <i />
          <span className={plan && !jobId ? "active" : jobId ? "done" : ""}>
            2 检查差异
          </span>
          <i />
          <span className={jobId ? "active" : ""}>3 执行结果</span>
        </div>
        <div className="modal-body">
          {error && <Notice kind="error">{error}</Notice>}
          {!plan ? (
            <form id="action-form" onSubmit={createPlan}>
              {action.description && (
                <p className="form-description">{action.description}</p>
              )}
              {action.dangerous && (
                <Notice kind="warn">
                  此操作可能中断连接或改变已有配置。下一步会展示影响与差异。
                </Notice>
              )}
              {breakout && (
                <Notice kind="warn">
                  将重建 {portMode.parent_interface}{" "}
                  所属大口的子接口。本次仅调整拆分模式，不同时修改管理状态、MTU、FEC
                  或描述。
                  {portMode.requires_reboot
                    ? "此模式将重启整台交换机，所有链路会中断。具体配置影响请检查下一页计划。"
                    : "具体依赖与中断范围请检查下一页计划。"}
                </Notice>
              )}
              {action.id === "fan.control" && control && (
                <>
                  <Notice>
                    {control.scope_label ||
                      control.scope ||
                      "控制对象由设备提供"}
                    。
                    {control.protection?.description ||
                      "保护能力尚未确认；以设备生成的计划说明为准。"}
                  </Notice>
                  {control.protection?.active === true && (
                    <Notice kind="warn">
                      保护已接管：
                      {control.protection.reason ||
                        "请检查当前温度和硬件状态。"}
                    </Notice>
                  )}
                </>
              )}
              {formFields.length ? (
                <div className="form-fields">
                  {formFields.map((f) => (
                    <FieldControl
                      key={f.name}
                      field={f}
                      value={values[f.name]}
                      onChange={(v) =>
                        setValues((old) => ({
                          ...old,
                          [f.name]: v,
                          ...(action.id === "port.configure" &&
                          f.name === "interfaces"
                            ? { port_mode: "" }
                            : {}),
                        }))
                      }
                      ports={ports}
                    />
                  ))}
                </div>
              ) : (
                <p>点击“预览变更”，由设备生成本次操作计划。</p>
              )}
            </form>
          ) : !jobId ? (
            <>
              <h3>{plan.title}</h3>
              <p>{plan.summary}</p>
              {plan.reboot_required && (
                <Notice kind="warn">
                  应用后将重启整台交换机，所有链路与管理连接会暂时中断。设备恢复并完成端口与
                  ASIC 检查后，才会标记拆分完成。
                </Notice>
              )}
              {plan.warnings.map((w, i) => (
                <Notice kind="warn" key={i}>
                  {w}
                </Notice>
              ))}
              <div className="diff-table">
                <div className="diff-header">
                  <span>目标</span>
                  <span>变更前</span>
                  <span>变更后</span>
                </div>
                {plan.changes.map((c, i) => (
                  <div className="diff-row" key={i}>
                    <strong>{c.target}</strong>
                    <pre>
                      {typeof c.before === "object"
                        ? JSON.stringify(c.before, null, 2)
                        : display(c.before)}
                    </pre>
                    <pre>
                      {typeof c.after === "object"
                        ? JSON.stringify(c.after, null, 2)
                        : display(c.after)}
                    </pre>
                  </div>
                ))}
                {!plan.changes.length && (
                  <Empty title="没有配置差异">{plan.summary}</Empty>
                )}
              </div>
              <div className="plan-meta">
                计划有效至 {timestamp(plan.expires_at)} · 配置版本{" "}
                <code>{plan.revision}</code>
              </div>
              <label className="acknowledge">
                <input
                  type="checkbox"
                  checked={ack}
                  onChange={(e) => setAck(e.target.checked)}
                />
                <span>
                  我已检查以上差异与影响，确认
                  {session.mode === "demo" ? "模拟执行" : "应用到设备"}。
                </span>
              </label>
            </>
          ) : (
            <div className="job-result">
              <div
                className={`job-symbol ${running ? "pending" : failed ? "failed" : "success"}`}
              >
                {running ? (
                  <span className="spinner" />
                ) : (
                  <Icon name={failed ? "warn" : "check"} size={30} />
                )}
              </div>
              <h3>
                {running
                  ? "设备正在处理操作"
                  : failed
                    ? "操作未成功完成"
                    : deferred
                      ? "拆分任务已提交"
                      : "操作已完成"}
              </h3>
              <p>
                {running
                  ? "正在查询同一任务的执行状态，请勿重复提交。"
                  : deferred
                    ? "正在等待设备重启并验证端口。任务提交成功不代表拆分已完成，可返回页面查看维护状态。"
                    : job?.result?.message || display(job?.error)}
              </p>
              <code className="job-id">任务 {jobId}</code>
              {failed && job?.result?.changed && (
                <Notice kind="warn">
                  部分变更已经生效。请检查执行详情和当前配置，勿直接重复操作。
                </Notice>
              )}
              {job?.result?.pending_save === true && (
                <Notice kind="warn">
                  运行配置已变更，仍需保存才能在重启后保留。
                </Notice>
              )}
              {job?.result?.pending_save === null && (
                <Notice>
                  保存状态未知。
                  {job.result.pending_save_reason ||
                    "暂时无法比较运行配置和启动配置，请检查设备状态。"}
                </Notice>
              )}
              {job?.result?.details !== undefined && (
                <details>
                  <summary>查看执行详情</summary>
                  <pre>
                    {typeof job.result.details === "string"
                      ? job.result.details
                      : JSON.stringify(job.result.details, null, 2)}
                  </pre>
                </details>
              )}
              {running && error && (
                <>
                  <button onClick={() => setPollAttempt((n) => n + 1)}>
                    查询任务状态
                  </button>
                  {awaitingRestore && (
                    <Notice>
                      设备可能正在重启。可返回监控页面等待恢复；请勿重复提交相同拆分。
                    </Notice>
                  )}
                </>
              )}
            </div>
          )}
        </div>
        <footer>
          {!plan ? (
            <>
              <button disabled={busy} onClick={onClose}>
                取消
              </button>
              <button
                className="primary"
                form="action-form"
                type="submit"
                disabled={busy}
              >
                {busy && <span className="spinner" />}预览变更
              </button>
            </>
          ) : !jobId ? (
            <>
              <button
                disabled={busy}
                onClick={() => {
                  setPlan(undefined);
                  setError("");
                }}
              >
                返回修改
              </button>
              <button
                className={action.dangerous ? "danger" : "primary"}
                disabled={!ack || busy}
                onClick={apply}
              >
                {busy && <span className="spinner" />}
                {plan.reboot_required
                  ? session.mode === "demo"
                    ? "模拟应用并重启"
                    : "应用并重启"
                  : session.mode === "demo"
                    ? "确认模拟执行"
                    : "确认应用"}
              </button>
            </>
          ) : (
            <button
              className="primary"
              disabled={running && !awaitingRestore}
              onClick={() => {
                if (awaitingRestore) onAwaitRestore();
                onClose();
              }}
            >
              {deferred || awaitingRestore ? "返回监控" : "完成"}
            </button>
          )}
        </footer>
      </div>
    </div>
  );
}

function PortDrawer({
  name,
  role,
  currentPage,
  onConfigure,
  onClose,
}: {
  name: string;
  role: Session["role"];
  currentPage?: PageData;
  onConfigure: (configuration: PortConfiguration) => void;
  onClose: () => void;
}) {
  const [data, setData] = useState<PortDetail>();
  const [error, setError] = useState("");
  const [tab, setTab] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [configuration, setConfiguration] = useState<PortConfiguration>();
  const [configurationError, setConfigurationError] = useState("");
  const dialog = useDialog(true, onClose);
  useEffect(() => {
    const controller = new AbortController();
    setData(undefined);
    setError("");
    api<PortDetail>(`/ports/${encodeURIComponent(name)}`, {
      signal: controller.signal,
    })
      .then(setData)
      .catch((e) => {
        if (!isAbort(e)) setError(errorMessage(e));
      });
    return () => controller.abort();
  }, [name, refresh]);
  useEffect(() => {
    const controller = new AbortController();
    setConfiguration(undefined);
    setConfigurationError("");
    if (role !== "admin") return;
    if (currentPage?.id === "ports.ports") {
      setConfiguration(portConfiguration(currentPage, role, name));
      return;
    }
    api<PageData>("/pages/ports.ports", { signal: controller.signal })
      .then((page) => setConfiguration(portConfiguration(page, role, name)))
      .catch((error) => {
        if (!isAbort(error))
          setConfigurationError(
            `端口配置能力暂时无法读取：${errorMessage(error)}`,
          );
      });
    return () => controller.abort();
  }, [name, role, currentPage, refresh]);
  const tabs = ["属性", "计数器", "光模块", "关联与历史"];
  return (
    <div className="drawer-backdrop">
      <button
        className="drawer-shade"
        aria-label="关闭端口详情"
        onClick={onClose}
      />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="port-title"
        ref={dialog}
      >
        <header>
          <div>
            <span className="eyebrow">端口详情</span>
            <h2 id="port-title">{name}</h2>
            <span className="muted">
              {data?.port.alias}{" "}
              {data?.port.cage_id && ` / ${data.port.cage_id}`}
            </span>
          </div>
          <button
            className="icon-button"
            aria-label="关闭端口详情"
            onClick={onClose}
          >
            <Icon name="close" size={20} />
          </button>
        </header>
        {data && (
          <div className="drawer-status">
            <Badge value={data.port.oper_status} />
            <span>{speed(data.port.speed)}</span>
            <span>FEC {display(data.port.fec)}</span>
            <button
              className="icon-button"
              aria-label="刷新端口详情"
              onClick={() => setRefresh((n) => n + 1)}
            >
              <Icon name="refresh" />
            </button>
          </div>
        )}
        {data && configuration && (
          <div className="drawer-config">
            <button
              className="primary"
              onClick={() => onConfigure(configuration)}
            >
              配置此端口
            </button>
            <span>仅选择 {name}，下一步预览变更</span>
          </div>
        )}
        <div className="detail-tabs" role="tablist" aria-label="端口详情分类">
          {tabs.map((t, i) => (
            <button
              key={t}
              role="tab"
              aria-selected={i === tab}
              onClick={() => setTab(i)}
            >
              {t}
            </button>
          ))}
        </div>
        <div className="drawer-body" role="tabpanel">
          {configurationError && (
            <Notice kind="warn">{configurationError}</Notice>
          )}
          {error ? (
            <Notice kind="error">
              {error}
              <button
                className="link-button"
                onClick={() => setRefresh((n) => n + 1)}
              >
                重试
              </button>
            </Notice>
          ) : !data ? (
            <div className="loading">
              <span className="spinner" />
              正在读取端口…
            </div>
          ) : tab === 0 ? (
            <>
              <Properties items={data.properties} />
              {Object.keys(data.capabilities ?? {}).length > 0 && (
                <details className="cap-details">
                  <summary>端口能力</summary>
                  <Properties
                    items={Object.entries(data.capabilities).map(
                      ([label, value]) => ({ label, value }),
                    )}
                  />
                </details>
              )}
            </>
          ) : tab === 1 ? (
            <>
              <Notice>计数器为设备实际采样；未返回的读数显示为未知。</Notice>
              <Properties items={data.counters} />
            </>
          ) : tab === 2 ? (
            <Properties items={data.transceiver} />
          ) : (
            <>
              <Properties items={data.relationships} />
              <Notice>
                当前接口提供关系信息与采样快照，尚未提供端口事件历史。
              </Notice>
            </>
          )}
        </div>
        <footer>采样时间：{timestamp(data?.sampled_at)}</footer>
      </aside>
    </div>
  );
}

function OverviewContent({
  overview,
  ports,
  onNavigate,
}: {
  overview?: Overview;
  ports?: PortsData;
  onNavigate: (id: string) => void;
}) {
  if (!overview)
    return <Empty title="等待系统概览">数据源尚未返回设备信息。</Empty>;
  const d = overview.device;
  const percent = (v: unknown) =>
    typeof v === "number" && Number.isFinite(v) ? `${v.toFixed(1)}%` : "未知";
  return (
    <>
      <div className="overview-strip">
        <div className="device-emblem">
          <Icon name="network" size={32} />
        </div>
        <div className="device-name">
          <span className="eyebrow">当前管理设备</span>
          <h2>{d.hostname || "主机名未知"}</h2>
          <span>
            {display(d.model)} <span className="separator">/</span>{" "}
            {display(d.hwsku)}
          </span>
        </div>
        <div className="health-state">
          <Badge value={overview.health.status} />
          <span>{overview.health.message}</span>
        </div>
      </div>
      <Metrics
        items={[
          {
            label: "前面板逻辑接口",
            value: ports ? ports.items.length : "未知",
          },
          {
            label: "链路 Up",
            value: ports
              ? ports.items.filter((p) => p.oper_status?.toLowerCase() === "up")
                  .length
              : "未知",
          },
          {
            label: "CPU 使用率",
            value: cpuDisplay(d.cpu_percent, d.cpu_state),
          },
          { label: "内存使用率", value: percent(d.memory_percent) },
        ]}
      />
      {d.metrics_sampled_at && (
        <div className="telemetry-meta">
          CPU / 内存采样：{timestamp(d.metrics_sampled_at)}
          {d.cpu_sample_seconds != null
            ? ` · CPU 采样窗口 ${d.cpu_sample_seconds} 秒`
            : ""}
          {d.cpu_state === "stale"
            ? " · 样本已过期"
            : d.cpu_state === "warming_up"
              ? " · 正在建立采样"
              : ""}
        </div>
      )}
      {d.cpu_warning && <Notice kind="warn">{d.cpu_warning}</Notice>}
      <div className="overview-columns">
        <Panel title="系统信息">
          <Properties
            items={[
              { label: "主机名", value: d.hostname },
              { label: "型号", value: d.model },
              { label: "平台", value: d.platform },
              { label: "HwSKU", value: d.hwsku },
              { label: "SONiC 版本", value: d.version },
              { label: "运行时间", value: uptime(d.uptime) },
            ]}
          />
        </Panel>
        <Panel title="运行摘要" aside={<span className="muted">设备采样</span>}>
          <Properties items={overview.summary} />
          <div className="quick-links">
            <button onClick={() => onNavigate("system.modules")}>
              <Icon name="network" />
              机箱与端口
            </button>
            <button onClick={() => onNavigate("status.temperature")}>
              温度与环境
            </button>
            <button onClick={() => onNavigate("status.logs")}>系统日志</button>
          </div>
        </Panel>
      </div>
    </>
  );
}

function FanControlStatus({ control }: { control: FanControl }) {
  const mode =
    control.mode === "auto"
      ? "自动控制"
      : control.mode === "fixed"
        ? "固定占空比"
        : "未知";
  return (
    <Panel
      title="风扇控制"
      aside={<span className="muted">控制值与转速读数分别显示</span>}
    >
      <div className="fan-control-state">
        <Properties
          items={[
            {
              label: "控制对象",
              value: control.scope_label || control.scope || "未知",
            },
            { label: "当前模式", value: mode },
            { label: "当前占空比", value: percent(control.duty_percent) },
            {
              label: "允许调节范围",
              value:
                control.min_percent != null && control.max_percent != null
                  ? `${percent(control.min_percent)}–${percent(control.max_percent)}`
                  : "设备尚未提供",
            },
          ]}
        />
        <div className="fan-protection">
          {control.manual_available === false ||
          control.manual_reason ||
          control.reason ? (
            <Notice>
              {control.manual_reason ||
                control.reason ||
                "尚无已验证的手动调节接口。"}
            </Notice>
          ) : null}
          {(control.protection?.active === true ||
            control.protection?.description !== control.reason) && (
            <Notice
              kind={control.protection?.active === true ? "warn" : "info"}
            >
              {control.protection?.active === true ? "保护已接管。" : ""}
              {control.protection?.description ||
                control.protection?.reason ||
                "保护状态尚未确认。"}
            </Notice>
          )}
          {control.temperatures?.length ? (
            <Properties items={control.temperatures} />
          ) : null}
        </div>
      </div>
    </Panel>
  );
}

export default function App() {
  const [session, setSession] = useState<Session>();
  const [sessionChecked, setSessionChecked] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const [catalog, setCatalog] = useState<Catalog>(catalogDefault);
  const [pageId, setPageId] = useState(
    () => window.location.hash.slice(1) || "status.summary",
  );
  const [overview, setOverview] = useState<Overview>();
  const [ports, setPorts] = useState<PortsData>();
  const [page, setPage] = useState<PageData>();
  const [pageError, setPageError] = useState("");
  const [globalError, setGlobalError] = useState("");
  const [overviewError, setOverviewError] = useState("");
  const [portsError, setPortsError] = useState("");
  const [loading, setLoading] = useState(false);
  const [pulse, setPulse] = useState(0);
  const [automatic, setAutomatic] = useState(false);
  const [selected, setSelected] = useState(new Set<string>());
  const [detailPort, setDetailPort] = useState("");
  const [action, setAction] = useState<{
    action: Action;
    revision?: string;
    selection?: string[];
    relatedActions?: Action[];
    control?: FanControl;
  }>();
  const [portView, setPortView] = useState<"table" | "map">("table");
  const [navQuery, setNavQuery] = useState("");
  const [exportBusy, setExportBusy] = useState(false);
  const [about, setAbout] = useState(false);
  const [operation, setOperation] = useState<Operation>();
  const [awaitingDevice, setAwaitingDevice] = useState(false);
  const [maintenanceExpired, setMaintenanceExpired] = useState(false);
  const currentGroup = catalog.groups.find((g) =>
    g.pages.some((p) => p.id === pageId),
  );
  const currentEntry = currentGroup?.pages.find((p) => p.id === pageId);
  const admin = session?.role === "admin";
  const refresh = useCallback(() => setPulse((n) => n + 1), []);
  const updateOperation = useCallback((next: Operation) => {
    setOperation(next);
    setAwaitingDevice(false);
  }, []);
  const awaitRestore = useCallback(() => {
    setAwaitingDevice(true);
    setMaintenanceExpired(false);
    setPulse((n) => n + 1);
  }, []);
  const navigate = useCallback((id: string) => {
    setPage(undefined);
    window.location.hash = id;
    setPageId(id);
    setNavQuery("");
  }, []);
  const aboutDialog = useDialog(about, () => setAbout(false));
  async function checkSession() {
    setSessionError("");
    try {
      setSession(await api<Session>("/session"));
    } catch (e) {
      setSessionError(errorMessage(e));
    } finally {
      setSessionChecked(true);
    }
  }
  useEffect(() => {
    void checkSession();
    const expired = () => {
      setSession(undefined);
      setOverview(undefined);
      setPorts(undefined);
      setPage(undefined);
      setAction(undefined);
      setDetailPort("");
      setSessionError("会话已过期，请重新登录。");
    };
    const hash = () =>
      setPageId(window.location.hash.slice(1) || "status.summary");
    window.addEventListener("session-expired", expired);
    window.addEventListener("hashchange", hash);
    return () => {
      window.removeEventListener("session-expired", expired);
      window.removeEventListener("hashchange", hash);
    };
  }, []);
  useEffect(() => {
    if (!session?.authenticated) return;
    const controller = new AbortController();
    api<Catalog>("/catalog", { signal: controller.signal })
      .then(setCatalog)
      .catch((e) => {
        if (!isAbort(e)) setGlobalError(`能力目录读取失败：${errorMessage(e)}`);
      });
    return () => controller.abort();
  }, [session]);
  useEffect(() => {
    if (!session?.authenticated) return;
    const controller = new AbortController();
    // Navigation reads a current overview as well as page metadata. Do not wait
    // for an unrelated port request before publishing a fresh CPU sample.
    api<Overview>("/overview", { signal: controller.signal })
      .then((data) => {
        if (controller.signal.aborted) return;
        setOverview(data);
        setOverviewError("");
      })
      .catch((e) => {
        if (!isAbort(e)) setOverviewError(`系统概览：${errorMessage(e)}`);
      });
    return () => controller.abort();
  }, [session, pulse, pageId]);
  useEffect(() => {
    if (!session?.authenticated) return;
    const controller = new AbortController();
    api<PortsData>("/ports", { signal: controller.signal })
      .then((p) => {
        if (controller.signal.aborted) return;
        setPorts(p);
        setPortsError("");
        if (p.operation) updateOperation(p.operation);
        setSelected(
          (old) =>
            new Set([...old].filter((n) => p.items.some((p) => p.name === n))),
        );
      })
      .catch((e) => {
        if (!isAbort(e)) setPortsError(`端口数据：${errorMessage(e)}`);
      });
    return () => controller.abort();
  }, [session, pulse, updateOperation]);
  useEffect(() => {
    if (!session?.authenticated || !currentEntry) return;
    const controller = new AbortController();
    setLoading(true);
    setPageError("");
    // Refresh must retain active consoles. Navigation/logout still unmount
    // them and release both remote sessions and optional local serial ports.
    setPage((old) =>
      (pageId === "status.bmc" || pageId === "system.console") &&
      old?.id === pageId
        ? old
        : undefined,
    );
    api<PageData>(`/pages/${encodeURIComponent(pageId)}`, {
      signal: controller.signal,
    })
      .then((data) => {
        setPage(data);
        if (data.operation) updateOperation(data.operation);
        if (data.pending_save !== undefined)
          setOverview((old) =>
            old
              ? {
                  ...old,
                  pending_save: data.pending_save ?? null,
                  pending_save_reason: data.pending_save_reason,
                }
              : old,
          );
      })
      .catch((e) => {
        if (!isAbort(e)) setPageError(errorMessage(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [session, pageId, pulse, currentEntry?.id, updateOperation]);
  const maintenance = operationPending(operation) || awaitingDevice;
  useEffect(() => {
    if (!maintenance || !session?.authenticated) return;
    setMaintenanceExpired(false);
    const started = Date.now();
    const timer = setInterval(() => {
      if (Date.now() - started >= 600000) {
        setMaintenanceExpired(true);
        clearInterval(timer);
      } else if (!document.hidden && !action) refresh();
    }, 10000);
    return () => clearInterval(timer);
  }, [maintenance, operation?.id, session?.authenticated, action, refresh]);
  useEffect(() => {
    if (!automatic || !session?.authenticated) return;
    const timer = setInterval(() => {
      if (!document.hidden && !action) refresh();
    }, 60000);
    return () => clearInterval(timer);
  }, [automatic, session, action, refresh]);
  async function logout() {
    try {
      await api("/logout", { method: "POST", csrf: session?.csrf, body: {} });
      setSession(undefined);
      setOverview(undefined);
      setPorts(undefined);
      setPage(undefined);
      setSelected(new Set());
      setOperation(undefined);
      setAwaitingDevice(false);
    } catch (e) {
      setGlobalError(errorMessage(e));
    }
  }
  async function save() {
    try {
      const p = await api<PageData>("/pages/setup.configurations");
      const saveAction = p.actions.find((a) => a.id === "config.save");
      if (!p.capability.write || !saveAction)
        throw new Error(p.capability.reason || "当前设备未提供保存操作");
      setAction({ action: saveAction, revision: p.revision });
    } catch (e) {
      setGlobalError(errorMessage(e));
    }
  }
  async function exportConfig() {
    setExportBusy(true);
    try {
      const config = await api<unknown>("/config/export");
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(config, null, 2)], {
          type: "application/json",
        }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `sonic-config-sanitized-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setGlobalError(errorMessage(e));
    } finally {
      setExportBusy(false);
    }
  }
  if (!sessionChecked)
    return (
      <div className="initial-loading">
        <span className="spinner" />
        正在连接管理服务…
      </div>
    );
  if (!session?.authenticated)
    return (
      <Login
        mode={session?.mode}
        serviceError={sessionError}
        onRetry={checkSession}
        onLogin={(s) => {
          setSession(s);
          setSessionError("");
        }}
      />
    );
  const pendingSave = pendingSaveState(
    page?.pending_save,
    overview?.pending_save,
  );
  const specialPorts = pageId === "ports.ports";
  const specialModules = pageId === "system.modules";
  const specialOverview = pageId === "status.summary";
  const specialBmc = pageId === "status.bmc";
  const specialConsole = pageId === "system.console";
  const actions = page?.actions ?? [];
  const canWrite = admin && page?.capability.write;
  const queryPages = catalog.groups
    .flatMap((g) => g.pages.map((p) => ({ ...p, group: g.label })))
    .filter(
      (p) =>
        p.label.toLowerCase().includes(navQuery.toLowerCase()) ||
        p.id.includes(navQuery.toLowerCase()),
    );
  return (
    <div className="app-shell">
      {session.mode === "demo" && (
        <div className="demo-banner">
          <b>DEMO</b> 演示环境 · 模拟设备数据与操作，不连接实际交换机
        </div>
      )}
      <header className="topbar">
        <a className="brand" href="#status.summary">
          <span className="brand-mark">
            <Icon name="network" size={24} />
          </span>
          <span>
            <strong>SONiC</strong>
            <small>WEB MANAGER</small>
          </span>
        </a>
        <div className="topbar-device">
          <span className="device-host">
            {overview?.device.hostname || "设备管理"}
          </span>
          <span>{overview?.device.model || "正在读取设备标识"}</span>
        </div>
        <div className="topbar-actions">
          <span className={`environment-pill ${session.mode}`}>
            {session.mode === "demo" ? "演示设备" : "实时设备"}
          </span>
          <button
            className="topbar-help"
            onClick={() => setAbout(true)}
            aria-label="关于此管理界面"
          >
            <Icon name="info" />
          </button>
          <span className="user">
            <Icon name="user" />
            <b>{session.user}</b>
            <small>{admin ? "管理员" : "只读"}</small>
          </span>
          <button onClick={logout} className="logout" title="退出登录">
            <Icon name="logout" />
            <span>退出</span>
          </button>
        </div>
      </header>
      <nav className="perspectives" aria-label="管理视角">
        {catalog.groups.map((g) => (
          <button
            key={g.id}
            className={currentGroup?.id === g.id ? "active" : ""}
            aria-current={currentGroup?.id === g.id ? "page" : undefined}
            onClick={() => navigate(g.pages[0].id)}
          >
            <strong>{g.label}</strong>
            <span>{g.english}</span>
          </button>
        ))}
      </nav>
      {(operation || awaitingDevice) && (
        <div
          className={`maintenance-banner ${operation?.status === "failed" ? "maintenance-failed" : operation?.verified === true ? "maintenance-complete" : ""}`}
          role="status"
        >
          <Icon
            name={
              operation?.status === "failed"
                ? "warn"
                : operation?.verified === true
                  ? "check"
                  : "info"
            }
          />
          <div>
            <b>{operation ? operationTitle(operation) : "等待确认设备状态"}</b>
            <span>
              {operation?.message ||
                "暂时无法查询任务，设备可能正在重启。恢复后请核对拆分状态，勿重复提交。"}
              {maintenance
                ? maintenanceExpired
                  ? " 自动检查已暂停，请手动刷新。"
                  : " 设备恢复后每 10 秒检查一次；如会话失效请重新登录。"
                : ""}
            </span>
          </div>
          <button onClick={refresh}>刷新状态</button>
        </div>
      )}
      {pendingSave && (
        <div className="save-banner">
          <Icon name="save" />
          <div>
            <b>存在未保存的运行配置</b>
            <span>保存后才能在设备重启时保留。</span>
          </div>
          {admin ? (
            <button onClick={save}>预览并保存</button>
          ) : (
            <span>请联系管理员保存</span>
          )}
        </div>
      )}
      {pendingSave === null && (
        <div className="save-banner save-unknown">
          <Icon name="info" />
          <div>
            <b>保存状态未知</b>
            <span>
              {page?.pending_save_reason ||
                overview?.pending_save_reason ||
                "暂时无法确定运行配置与启动配置是否一致。"}
            </span>
          </div>
          <button onClick={() => navigate("setup.configurations")}>
            查看配置状态
          </button>
        </div>
      )}
      <div className="workspace">
        <aside className="sidebar">
          <div className="sidebar-heading">
            {currentGroup?.english || "导航"}
            <span>{currentGroup?.pages.length ?? 0}</span>
          </div>
          <div className="nav-search">
            <Icon name="search" size={14} />
            <input
              aria-label="搜索所有管理页面"
              placeholder="查找功能…"
              value={navQuery}
              onChange={(e) => setNavQuery(e.target.value)}
            />
          </div>
          <nav aria-label="功能导航">
            {navQuery ? (
              queryPages.length ? (
                queryPages.map((p) => (
                  <button
                    key={p.id}
                    onClick={() => navigate(p.id)}
                    className={pageId === p.id ? "active" : ""}
                  >
                    <span>
                      {p.label}
                      <small>{p.group}</small>
                    </span>
                    <Icon name="chevron" size={12} />
                  </button>
                ))
              ) : (
                <div className="nav-empty">没有匹配的功能</div>
              )
            ) : (
              currentGroup?.pages.map((p) => (
                <button
                  key={p.id}
                  onClick={() => navigate(p.id)}
                  className={pageId === p.id ? "active" : ""}
                  aria-current={pageId === p.id ? "page" : undefined}
                >
                  <span>{p.label}</span>
                  <Icon name="chevron" size={12} />
                </button>
              ))
            )}
          </nav>
          <div className="sidebar-bottom">
            <Icon name="lock" size={14} />
            <span>
              {admin ? "管理员操作需预览并确认" : "当前账户仅有查看权限"}
            </span>
          </div>
        </aside>
        <main className="main-content">
          <div className="breadcrumbs">
            {currentGroup?.label || "管理"}
            <Icon name="chevron" size={11} />
            <span>{currentEntry?.label || "页面不存在"}</span>
          </div>
          <div className="page-heading">
            <div>
              <h1>{page?.title || currentEntry?.label || "页面不存在"}</h1>
              <p>
                {page?.description ||
                  (loading ? "正在读取设备状态…" : "SONiC 设备管理与运行状态")}
              </p>
            </div>
            <div className="refresh-tools">
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={automatic}
                  onChange={(e) => setAutomatic(e.target.checked)}
                />
                60 秒自动刷新
              </label>
              <button onClick={refresh} disabled={loading}>
                <Icon name="refresh" />
                {loading ? "读取中" : "刷新"}
              </button>
            </div>
          </div>
          {(globalError || overviewError || portsError) && (
            <Notice kind="error">
              {[globalError, overviewError, portsError]
                .filter(Boolean)
                .join("；")}
              （若下方保留旧采样，请以采样时间为准。）
            </Notice>
          )}
          {pageError && (
            <Notice kind="error">
              {pageError}
              <button className="link-button" onClick={refresh}>
                重试
              </button>
            </Notice>
          )}
          {!currentEntry && (
            <Empty title="页面不存在">
              <button onClick={() => navigate("status.summary")}>
                返回系统概览
              </button>
            </Empty>
          )}
          {page && (
            <>
              <div className="page-meta">
                <span
                  className={`capability-tag ${page.capability.write ? "writable" : page.capability.read ? "readonly" : "unsupported"}`}
                >
                  {specialConsole
                    ? "终端连接能力单独验证"
                    : page.capability.write
                      ? "支持配置"
                      : page.capability.read
                        ? "只读数据"
                        : "功能未适配"}
                </span>
                <span className="sample-time">
                  采样：{timestamp(page.sampled_at || overview?.sampled_at)}
                </span>
              </div>
              {page.capability.reason && (
                <Notice kind={page.capability.read ? "info" : "warn"}>
                  {page.capability.reason}
                </Notice>
              )}
              {actions.length > 0 && (
                <div className="action-toolbar">
                  <div>
                    {actions
                      .filter(
                        (a) =>
                          a.id !== "port.breakout" ||
                          !actions.some((item) => item.id === "port.configure"),
                      )
                      .map((a) => (
                        <button
                          key={a.id}
                          className={a.dangerous ? "subtle-danger" : ""}
                          disabled={!canWrite}
                          title={
                            !admin
                              ? "只读账户不能执行配置"
                              : !page.capability.write
                                ? page.capability.reason
                                : a.description
                          }
                          onClick={() =>
                            setAction({
                              action: a,
                              relatedActions: actions,
                              control: page.control,
                              revision:
                                page.revision ||
                                ports?.revision ||
                                overview?.revision,
                            })
                          }
                        >
                          {a.label}
                        </button>
                      ))}
                  </div>
                  {selected.size > 0 && specialPorts && (
                    <span>已选择 {selected.size} 个接口</span>
                  )}
                  {!admin && (
                    <span>
                      <Icon name="lock" size={13} />
                      只读账户
                    </span>
                  )}
                </div>
              )}
              {pageId === "setup.configurations" && (
                <div className="export-bar">
                  <div>
                    <b>导出脱敏配置</b>
                    <span>敏感字段会被隐藏；此文件不等同于完整恢复备份。</span>
                  </div>
                  <button onClick={exportConfig} disabled={exportBusy}>
                    <Icon name="download" />
                    {exportBusy ? "导出中…" : "下载 JSON"}
                  </button>
                </div>
              )}
              {specialOverview ? (
                <OverviewContent
                  overview={overview}
                  ports={ports}
                  onNavigate={navigate}
                />
              ) : (
                <Metrics
                  items={
                    page.metrics
                      ? [
                          {
                            label: "CPU 使用率",
                            value: cpuDisplay(
                              page.metrics.cpu_percent,
                              page.metrics.cpu_state,
                            ),
                          },
                          {
                            label: "内存使用率",
                            value: percent(page.metrics.memory_percent),
                          },
                        ]
                      : page.summary
                  }
                />
              )}{" "}
              {page.metrics?.metrics_sampled_at && (
                <div className="telemetry-meta">
                  独立采样：{timestamp(page.metrics.metrics_sampled_at)}
                  {page.metrics.cpu_sample_seconds != null
                    ? ` · CPU 采样窗口 ${page.metrics.cpu_sample_seconds} 秒`
                    : ""}
                  {page.metrics.cpu_state === "stale" ? " · 样本已过期" : ""}
                </div>
              )}
              {page.metrics?.cpu_warning && (
                <Notice kind="warn">{page.metrics.cpu_warning}</Notice>
              )}
              {pageId === "status.fans" && page.control && (
                <FanControlStatus control={page.control} />
              )}
              {specialBmc && (
                <BmcMonitor snapshot={page.bmc} session={session} />
              )}
              {specialConsole && (
                <Suspense
                  fallback={
                    <div className="loading">
                      <span className="spinner" />
                      正在加载终端组件…
                    </div>
                  }
                >
                  <RemoteConsoles session={session} active />
                </Suspense>
              )}
              {(specialModules || specialPorts) && ports && (
                <Panel
                  title={specialModules ? "模块与逻辑端口" : "前面板接口"}
                  aside={
                    specialPorts ? (
                      <div className="segmented">
                        <button
                          className={portView === "table" ? "active" : ""}
                          onClick={() => setPortView("table")}
                        >
                          表格
                        </button>
                        <button
                          className={portView === "map" ? "active" : ""}
                          onClick={() => setPortView("map")}
                        >
                          模块示意
                        </button>
                      </div>
                    ) : (
                      <span className="muted">
                        {ports.items.length} 个逻辑接口
                      </span>
                    )
                  }
                >
                  {specialModules || portView === "map" ? (
                    <PortMap
                      data={ports}
                      onOpen={(p) => setDetailPort(p.name)}
                      selected={specialPorts ? selected : undefined}
                      onSelection={specialPorts ? setSelected : undefined}
                    />
                  ) : (
                    <DataTable
                      columns={portColumns}
                      rows={ports.items}
                      selected={selected}
                      onSelection={setSelected}
                      onOpen={(row) => setDetailPort(String(row.name))}
                      label="端口"
                    />
                  )}
                </Panel>
              )}
              {page.charts && page.charts.length > 0 && (
                <div className="chart-grid">
                  {page.charts.map((chart, i) => (
                    <LineChart chart={chart} key={`${chart.name}-${i}`} />
                  ))}
                </div>
              )}
              {!specialPorts &&
                !specialModules &&
                !specialOverview &&
                !specialBmc &&
                !specialConsole &&
                (page.capability.read || page.rows.length > 0) && (
                  <Panel
                    title={page.title}
                    aside={
                      <span className="muted">{page.rows.length} 条记录</span>
                    }
                  >
                    <DataTable
                      key={page.id}
                      columns={
                        pageId === "status.fans"
                          ? page.columns.map((column) =>
                              column.key === "speed" && !column.type
                                ? { ...column, type: "percent" }
                                : column,
                            )
                          : page.columns
                      }
                      rows={page.rows}
                      empty="设备当前没有此类配置或运行记录。"
                    />
                  </Panel>
                )}
              {(specialModules || specialOverview) && page.rows.length > 0 && (
                <Panel title="详细状态">
                  <DataTable columns={page.columns} rows={page.rows} />
                </Panel>
              )}
              {!page.capability.read &&
                !page.rows.length &&
                !specialModules &&
                !specialPorts &&
                !specialOverview &&
                !specialBmc &&
                !specialConsole && (
                  <div className="unsupported-panel">
                    <Icon name="box" size={32} />
                    <h2>此设备暂未提供该功能</h2>
                    <p>
                      {page.capability.reason ||
                        "当前发行版或平台适配器尚未提供该项能力。"}
                    </p>
                    <span>请查看上方原因，或选择其他管理页面。</span>
                  </div>
                )}
            </>
          )}
          {loading && !page && (
            <div className="loading">
              <span className="spinner" />
              正在读取 {currentEntry?.label}…
            </div>
          )}
          <div className="content-bottom">
            <span>
              数据来自{" "}
              {session.mode === "demo" ? "DEMO 模拟适配器" : "当前设备 SONiC"} ·
              未知读数显示为“—”
            </span>
            <span>{page?.revision ? `配置版本 ${page.revision}` : ""}</span>
          </div>
        </main>
      </div>
      <footer className="statusbar">
        <span>
          <i
            className={`link-light ${session.mode === "demo" ? "unknown" : "up"}`}
          />
          {session.mode === "demo" ? "DEMO 模拟会话" : "已认证管理会话"}
        </span>
        <span>{overview?.device.platform || "平台待识别"}</span>
        <span>SONiC Web Manager {releaseVersion.trim()}</span>
      </footer>
      {detailPort && (
        <PortDrawer
          name={detailPort}
          role={session.role}
          currentPage={page?.id === "ports.ports" ? page : undefined}
          onConfigure={(configuration) => {
            setDetailPort("");
            setAction(configuration);
          }}
          onClose={() => setDetailPort("")}
        />
      )}{" "}
      {action && (
        <ActionDialog
          action={action.action}
          revision={action.revision}
          session={session}
          selected={action.selection ?? [...selected]}
          ports={ports?.items ?? []}
          relatedActions={action.relatedActions}
          control={action.control}
          onClose={() => setAction(undefined)}
          onComplete={refresh}
          onDeferredOperation={updateOperation}
          onAwaitRestore={awaitRestore}
        />
      )}{" "}
      {about && (
        <div className="modal-backdrop">
          <div
            className="modal about-modal"
            ref={aboutDialog}
            role="dialog"
            aria-modal="true"
            aria-labelledby="about-title"
          >
            <header>
              <h2 id="about-title">SONiC Web Manager</h2>
              <button
                className="icon-button"
                aria-label="关闭说明"
                onClick={() => setAbout(false)}
              >
                <Icon name="close" />
              </button>
            </header>
            <div className="modal-body">
              <p>独立的 SONiC 设备管理界面，参考经典 Onyx 的功能组织方式。</p>
              <Properties
                items={[
                  { label: "界面版本", value: releaseVersion.trim() },
                  {
                    label: "运行模式",
                    value:
                      session.mode === "demo" ? "DEMO · 模拟数据" : "实时设备",
                  },
                  { label: "账户权限", value: admin ? "管理员" : "只读" },
                  {
                    label: "导航",
                    value: `${catalog.groups.length} 个主视角 / ${catalog.groups.reduce((n, g) => n + g.pages.length, 0)} 个功能页面`,
                  },
                ]}
              />
              <p className="muted">
                功能可用性以设备能力与后端返回为准。每次配置均先生成计划，再由管理员确认执行；界面不直接操作
                ASIC 或 BMC 固件。
              </p>
            </div>
            <footer>
              <button className="primary" onClick={() => setAbout(false)}>
                关闭
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
