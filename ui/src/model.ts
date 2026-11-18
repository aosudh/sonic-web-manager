import type {
  Action,
  FanControl,
  Job,
  Operation,
  PageData,
  Port,
  PortMode,
  Row,
  Session,
} from "./types.ts";

export interface PortConfiguration {
  action: Action;
  revision?: string;
  selection: string[];
  relatedActions: Action[];
}

export function portConfiguration(
  page: PageData | undefined,
  role: Session["role"],
  name: string,
): PortConfiguration | undefined {
  if (
    role !== "admin" ||
    !name ||
    page?.id !== "ports.ports" ||
    !page.capability.write
  )
    return;
  const action = page.actions.find((item) => item.id === "port.configure");
  if (
    !action?.fields.some(
      (field) => field.name === "interfaces" && field.type === "multiselect",
    )
  )
    return;
  return {
    action,
    revision: page.revision,
    selection: [name],
    relatedActions: page.actions,
  };
}

export function percent(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(String(value).replace(/%$/, ""));
  return Number.isFinite(number)
    ? `${number.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}%`
    : "未知";
}

export function formatCellValue(
  value: unknown,
  key: string,
  type?: string,
  unit?: string,
): string {
  if (type === "temperature") return temperature(value, unit);
  if (type === "percent") return percent(value);
  if (type === "speed") return speed(value);
  if (type === "rate" || key.endsWith("_bps")) return rate(value);
  return display(value);
}
export function temperature(value: unknown, unit = "°C"): string {
  if (value === null || value === undefined || value === "") return "未知";
  const n =
    typeof value === "number"
      ? value
      : typeof value === "string" &&
          /^[+-]?(?:\d+\.?\d*|\.\d+)$/.test(value.trim())
        ? Number(value)
        : NaN;
  return Number.isFinite(n)
    ? `${n.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${unit || "°C"}`
    : "未知";
}
export function cpuDisplay(value: unknown, state?: string): string {
  if (state === "warming_up") return "采样中";
  if (state === "unknown" || state === "stopped") return "未知";
  return typeof value === "number" && Number.isFinite(value)
    ? `${value.toFixed(1)}%`
    : "未知";
}

export function selectedPortModes(
  ports: Port[],
  names: string[],
  relatedActions: Action[],
): { modes: PortMode[]; reason: string } {
  if (!names.length)
    return {
      modes: [],
      reason: "请先选择接口，再查看设备支持的速率与拆分模式。",
    };
  const selected = names.map((name) =>
    ports.find((port) => port.name === name),
  );
  if (selected.some((port) => !port?.port_modes))
    return {
      modes: [],
      reason: "部分所选接口的速率能力尚未提供；仍可配置其他属性。",
    };
  const complete = selected as Port[];
  const first = complete[0].port_modes!;
  const oneCage =
    new Set(complete.map((port) => port.cage_id || port.name)).size === 1;
  const breakoutAdvertised = relatedActions.some(
    (action) => action.id === "port.breakout",
  );
  const modes: PortMode[] = [];
  for (const mode of first) {
    if (mode.kind === "breakout" && !oneCage) continue;
    const matches = complete.map((port) =>
      port.port_modes!.find(
        (item) => item.value === mode.value && item.kind === mode.kind,
      ),
    );
    if (matches.some((item) => !item)) continue;
    const reasons = matches
      .filter((item) => item!.available !== true)
      .map((item) => item!.reason || "设备尚未确认此模式可用");
    if (mode.kind === "breakout") {
      if (!breakoutAdvertised) reasons.push("设备未提供拆分配置操作");
      if (
        !mode.parent_interface ||
        matches.some((item) => item!.parent_interface !== mode.parent_interface)
      )
        reasons.push("物理大口映射未确认");
    }
    modes.push({
      ...mode,
      available: reasons.length === 0,
      reason: [...new Set(reasons)].join("；") || mode.reason,
    });
  }
  return {
    modes,
    reason: !oneCage
      ? "批量配置仅显示所选接口共同支持的速率；拆分须一次选择一个物理大口。"
      : modes.length
        ? "选项来自当前设备能力；拆分会单独生成变更计划。"
        : "所选接口没有共同可用的速率或拆分模式。",
  };
}

export function configurationRequest(
  action: Action,
  values: Record<string, unknown>,
  ports: Port[],
  relatedActions: Action[],
  control?: FanControl,
): { action: string; values: Record<string, unknown> } {
  if (action.id === "port.configure") {
    const names = Array.isArray(values.interfaces)
      ? values.interfaces.map(String)
      : [];
    const next = { ...values };
    delete next.speed;
    if (values.port_mode) {
      const selected = selectedPortModes(
        ports,
        names,
        relatedActions,
      ).modes.find((mode) => mode.value === values.port_mode);
      if (!selected || selected.available !== true)
        throw new Error(
          selected?.reason || "该模式不适用于当前所选接口，请重新选择。",
        );
      if (selected.kind === "breakout") {
        if (!selected.breakout_mode || !selected.parent_interface)
          throw new Error("拆分模式缺少物理接口信息。");
        return {
          action: "port.breakout",
          values: {
            interface: selected.parent_interface,
            mode: selected.breakout_mode,
          },
        };
      }
      if (!Number.isFinite(selected.speed) || selected.speed! <= 0)
        throw new Error("设备未提供有效的端口速率。");
      next.speed = selected.speed;
    }
    return { action: action.id, values: formPayload(action, next) };
  }
  if (action.id === "fan.control") {
    const next = { ...values };
    const mode = values.mode;
    const advertised = action.fields.find(
      (field) => field.name === "mode",
    )?.options;
    if (
      !advertised?.some((option) => option.value === mode && !option.disabled)
    )
      throw new Error("请选择设备支持的风扇控制模式。");
    if (mode === "auto") delete next.duty_percent;
    if (mode === "fixed") {
      const unavailable = fixedFanReason(control);
      if (unavailable) throw new Error(unavailable);
      const min = control?.min_percent,
        max = control?.max_percent;
      const duty = Number(values.duty_percent);
      if (
        min === null ||
        min === undefined ||
        max === null ||
        max === undefined ||
        !Number.isFinite(min) ||
        !Number.isFinite(max)
      )
        throw new Error("设备未提供固定占空比的可调范围。");
      if (
        values.duty_percent === "" ||
        values.duty_percent === null ||
        values.duty_percent === undefined ||
        !Number.isFinite(duty) ||
        duty < min ||
        duty > max
      )
        throw new Error(`占空比须在 ${min}%–${max}% 范围内。`);
      const step = control?.step_percent;
      if (
        step != null &&
        step > 0 &&
        Number.isFinite(step) &&
        Math.abs((duty - min) / step - Math.round((duty - min) / step)) > 1e-7
      )
        throw new Error(`占空比须按 ${step}% 的步长调节。`);
      next.duty_percent = duty;
    }
    const conditionalAction = {
      ...action,
      fields: action.fields.filter(
        (field) => mode === "fixed" || field.name !== "duty_percent",
      ),
    };
    return { action: action.id, values: formPayload(conditionalAction, next) };
  }
  return { action: action.id, values: formPayload(action, values) };
}

export function fixedFanReason(control: FanControl | undefined): string {
  if (control?.manual_available === false)
    return (
      control.manual_reason || control.reason || "设备不支持手动占空比控制。"
    );
  if (
    control?.min_percent == null ||
    control?.max_percent == null ||
    !Number.isFinite(control.min_percent) ||
    !Number.isFinite(control.max_percent) ||
    control.min_percent > control.max_percent
  )
    return "设备未提供固定占空比的可调范围。";
  return "";
}

export function deferredOperation(job: Job | undefined): Operation | undefined {
  const details = job?.result?.details;
  if (
    !job?.result?.success ||
    !details ||
    typeof details !== "object" ||
    Array.isArray(details)
  )
    return;
  const record = details as Record<string, unknown>;
  if (
    record.reboot_required !== true ||
    typeof record.operation_id !== "string"
  )
    return;
  return {
    id: record.operation_id,
    status: typeof record.status === "string" ? record.status : "queued",
    message: "拆分任务已提交，等待设备重启后验证。",
    verified: false,
  };
}

export function operationPending(operation: Operation | undefined): boolean {
  return (
    !!operation &&
    operation.verified !== true &&
    !["failed", "rolled_back", "cancelled"].includes(operation.status)
  );
}

export function operationTitle(operation: Operation): string {
  if (operation.status === "failed") return "拆分任务未通过验证";
  if (operation.status === "rolled_back")
    return operation.verified === true
      ? "原端口模式已恢复并验证"
      : "端口回退已结束，请检查设备状态";
  if (operation.status === "cancelled") return "拆分任务已取消";
  if (operation.verified === true) return "拆分已验证完成";
  return (
    (
      {
        queued: "拆分任务已提交",
        applying: "正在准备拆分配置",
        pending_reboot: "等待设备重启",
        verifying: "正在验证端口与 ASIC",
      } as Record<string, string>
    )[operation.status] || "等待设备验证"
  );
}

export function display(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
export function speed(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string" && /[a-z]/i.test(value)) return value;
  const n = Number(value);
  return Number.isFinite(n)
    ? n >= 1000
      ? `${n / 1000}G`
      : `${n}M`
    : display(value);
}
export function rate(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  let n = Number(value);
  if (!Number.isFinite(n)) return display(value);
  const units = ["bit/s", "Kbit/s", "Mbit/s", "Gbit/s", "Tbit/s"];
  let i = 0;
  while (Math.abs(n) >= 1000 && i < units.length - 1) {
    n /= 1000;
    i++;
  }
  return `${n.toLocaleString("zh-CN", { maximumFractionDigits: i ? 2 : 0 })} ${units[i]}`;
}
export function timestamp(value: unknown): string {
  if (!value) return "尚未采样";
  const date = new Date(
    typeof value === "number"
      ? value < 1e12
        ? value * 1000
        : value
      : String(value),
  );
  return Number.isNaN(date.valueOf())
    ? display(value)
    : date.toLocaleString("zh-CN", { hour12: false });
}
export function uptime(value: unknown): string {
  if (typeof value !== "number") return display(value);
  if (!Number.isFinite(value) || value < 0) return "未知";
  const seconds = Math.floor(value);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return `${days} 天 ${hours} 小时 ${minutes} 分钟`;
  if (hours) return `${hours} 小时 ${minutes} 分钟`;
  if (minutes) return `${minutes} 分钟`;
  return `${seconds} 秒`;
}
export function pendingSaveState(
  pageValue: boolean | null | undefined,
  overviewValue: boolean | null | undefined,
): boolean | null {
  return pageValue !== undefined ? pageValue : (overviewValue ?? null);
}
export function tone(value: unknown): "good" | "bad" | "warn" | "neutral" {
  const s = String(value ?? "").toLowerCase();
  if (
    [
      "up",
      "ok",
      "healthy",
      "active",
      "enabled",
      "present",
      "running",
      "success",
      "succeeded",
    ].includes(s)
  )
    return "good";
  if (["failed", "error", "critical", "unhealthy", "fault"].includes(s))
    return "bad";
  if (["warning", "degraded", "initializing", "queued", "pending"].includes(s))
    return "warn";
  return "neutral";
}
export function filterSort(
  rows: Row[],
  query: string,
  key: string,
  ascending: boolean,
): Row[] {
  const q = query.trim().toLowerCase();
  return rows
    .filter(
      (row) =>
        !q ||
        Object.values(row).some((v) => display(v).toLowerCase().includes(q)),
    )
    .map((row, order) => ({ row, order }))
    .sort((a, b) => {
      const av = a.row[key],
        bv = b.row[key];
      const compare =
        typeof av === "number" && typeof bv === "number"
          ? av - bv
          : display(av).localeCompare(display(bv), "zh-CN", { numeric: true });
      return (ascending ? compare : -compare) || a.order - b.order;
    })
    .map((x) => x.row);
}
export function groupCages(ports: Port[]): { id: string; ports: Port[] }[] {
  const groups = new Map<string, Port[]>();
  for (const port of ports) {
    const key = port.cage_id || port.name;
    groups.set(key, [...(groups.get(key) ?? []), port]);
  }
  return [...groups]
    .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true }))
    .map(([id, items]) => ({ id, ports: items }));
}
export function initialValues(
  action: Action,
  selection: string[],
): Record<string, unknown> {
  return Object.fromEntries(
    action.fields.map((f) => [
      f.name,
      f.name === "interfaces" && selection.length
        ? [...selection]
        : (f.default ??
          (f.type === "multiselect" ? [] : f.type === "checkbox" ? false : "")),
    ]),
  );
}
export function formPayload(
  action: Action,
  values: Record<string, unknown>,
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const field of action.fields) {
    const v = values[field.name];
    if (
      field.required &&
      (v === "" ||
        v === undefined ||
        v === null ||
        (Array.isArray(v) && !v.length))
    )
      throw new Error(`请填写「${field.label}」`);
    if (v === "" || v === undefined || v === null) continue;
    if (field.type === "number") {
      if (!Number.isFinite(Number(v)))
        throw new Error(`「${field.label}」需要有效数字`);
      result[field.name] = Number(v);
    } else result[field.name] = v;
  }
  return result;
}
