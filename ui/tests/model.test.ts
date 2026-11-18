import { test } from "node:test";
import assert from "node:assert/strict";
import {
  formPayload,
  groupCages,
  initialValues,
  filterSort,
  rate,
  tone,
  uptime,
  pendingSaveState,
  portConfiguration,
  percent,
  formatCellValue,
  selectedPortModes,
  configurationRequest,
  fixedFanReason,
  deferredOperation,
  operationPending,
  operationTitle,
  temperature,
  cpuDisplay,
} from "../src/model.ts";
test("temperature types retain Celsius and unknown values never become zero", () => {
  assert.equal(
    formatCellValue(41.125, "temperature", "temperature", "°C"),
    "41.13 °C",
  );
  assert.equal(temperature(0), "0 °C");
  assert.equal(temperature(-2.5), "-2.5 °C");
  for (const value of [null, undefined, "", "N/A", NaN])
    assert.equal(temperature(value), "未知");
});
test("CPU display distinguishes warmup, unavailable, stale and true zero", () => {
  assert.equal(cpuDisplay(null, "warming_up"), "采样中");
  assert.equal(cpuDisplay(null, "unknown"), "未知");
  assert.equal(cpuDisplay(0, "ok"), "0.0%");
  assert.equal(cpuDisplay(62.3, "stale"), "62.3%");
  assert.equal(cpuDisplay(62.3, "stopped"), "未知");
});
import type {
  Action,
  FanControl,
  Job,
  PageData,
  Port,
  PortMode,
} from "../src/types.ts";

const configurablePortPage: PageData = {
  id: "ports.ports",
  title: "端口",
  description: "",
  columns: [],
  rows: [],
  summary: [],
  capability: { read: true, write: true, reason: "" },
  revision: "current-port-revision",
  actions: [
    {
      id: "port.configure",
      label: "配置端口",
      fields: [
        {
          name: "interfaces",
          label: "接口",
          type: "multiselect",
          required: true,
          default: ["Ethernet0", "Ethernet1"],
        },
      ],
    },
  ],
};

test("drawer config uses the advertised action and selects only its current port", () => {
  const context = portConfiguration(
    configurablePortPage,
    "admin",
    "Ethernet52",
  );
  assert.ok(context);
  assert.equal(context.action, configurablePortPage.actions[0]);
  assert.equal(context.revision, "current-port-revision");
  assert.deepEqual(context.selection, ["Ethernet52"]);
  assert.deepEqual(
    initialValues(context.action, context.selection).interfaces,
    ["Ethernet52"],
  );
  assert.deepEqual(configurablePortPage.actions[0].fields[0].default, [
    "Ethernet0",
    "Ethernet1",
  ]);
});

test("drawer config is absent for viewers, unsupported or untargeted action schemas", () => {
  assert.equal(
    portConfiguration(configurablePortPage, "viewer", "Ethernet52"),
    undefined,
  );
  assert.equal(
    portConfiguration(
      {
        ...configurablePortPage,
        capability: { read: true, write: false, reason: "unsupported" },
      },
      "admin",
      "Ethernet52",
    ),
    undefined,
  );
  assert.equal(
    portConfiguration(
      { ...configurablePortPage, actions: [] },
      "admin",
      "Ethernet52",
    ),
    undefined,
  );
  assert.equal(
    portConfiguration(
      {
        ...configurablePortPage,
        actions: [{ id: "port.configure", label: "配置", fields: [] }],
      },
      "admin",
      "Ethernet52",
    ),
    undefined,
  );
  assert.equal(
    portConfiguration(
      { ...configurablePortPage, id: "system.modules" },
      "admin",
      "Ethernet52",
    ),
    undefined,
  );
});

test("uptime seconds have units and invalid values remain unknown", () => {
  assert.equal(uptime(86320), "23 小时 58 分钟");
  assert.equal(uptime(90061), "1 天 1 小时 1 分钟");
  assert.equal(uptime(0), "0 秒");
  assert.equal(uptime(null), "—");
  assert.equal(uptime(-1), "未知");
});

test("unknown save state must not become false or fall back to stale known state", () => {
  assert.equal(pendingSaveState(null, false), null);
  assert.equal(pendingSaveState(null, true), null);
  assert.equal(pendingSaveState(false, true), false);
  assert.equal(pendingSaveState(undefined, true), true);
  assert.equal(pendingSaveState(undefined, undefined), null);
});

test("split interfaces share a cage without guessing chassis geometry", () => {
  const cages = groupCages([
    { name: "Ethernet52", cage_id: "QSFP2" },
    { name: "Ethernet53", cage_id: "QSFP2" },
    { name: "Ethernet64", cage_id: "QSFP5" },
  ]);
  assert.equal(cages.length, 2);
  assert.deepEqual(
    cages[0].ports.map((p) => p.name),
    ["Ethernet52", "Ethernet53"],
  );
});
test("unmapped interfaces retain separate identities", () =>
  assert.equal(
    groupCages([{ name: "Ethernet0" }, { name: "Ethernet1" }]).length,
    2,
  ));
test("unknown or missing readings are not healthy or zero", () => {
  assert.equal(tone(undefined), "neutral");
  assert.equal(tone("unknown"), "neutral");
  assert.equal(rate(null), "—");
  assert.equal(rate(0), "0 bit/s");
});
test("selection only populates the advertised batch field and preserves explicit defaults", () => {
  const action: Action = {
    id: "port.configure",
    label: "端口",
    fields: [
      {
        name: "interfaces",
        label: "端口",
        type: "multiselect",
        required: true,
      },
      { name: "mtu", label: "MTU", type: "number" },
      { name: "enabled", label: "启用", type: "checkbox", default: false },
    ],
  };
  const values = initialValues(action, ["Ethernet52"]);
  assert.deepEqual(formPayload(action, values), {
    interfaces: ["Ethernet52"],
    enabled: false,
  });
  assert.throws(
    () => formPayload(action, { ...values, interfaces: [] }),
    /端口/,
  );
  assert.throws(() => formPayload(action, { ...values, mtu: "NaN" }), /MTU/);
  assert.deepEqual(
    formPayload(action, { ...values, mtu: "9100", command: "unexpected" }),
    { interfaces: ["Ethernet52"], mtu: 9100, enabled: false },
  );
});
test("table search and numeric port order are stable and do not mutate data", () => {
  const rows = [
    { name: "Ethernet10", speed: 25000 },
    { name: "Ethernet2", speed: 100000 },
    { name: "Ethernet1", speed: 25000 },
  ];
  assert.deepEqual(
    filterSort(rows, "25000", "name", true).map((r) => r.name),
    ["Ethernet1", "Ethernet10"],
  );
  assert.equal(rows[0].name, "Ethernet10");
});

const portAction: Action = {
  id: "port.configure",
  label: "端口配置",
  fields: [
    { name: "interfaces", label: "接口", type: "multiselect", required: true },
    { name: "speed", label: "速率", type: "number" },
    { name: "mtu", label: "MTU", type: "number" },
    { name: "admin_status", label: "管理状态", type: "select" },
  ],
};
const breakoutAction: Action = {
  id: "port.breakout",
  label: "拆分",
  fields: [],
};
const speed25: PortMode = {
  value: "speed:25000",
  label: "25G",
  kind: "speed",
  speed: 25000,
  available: true,
};
const speed100: PortMode = {
  value: "speed:100000",
  label: "100G",
  kind: "speed",
  speed: 100000,
  available: true,
};
const breakout: PortMode = {
  value: "breakout:4x25G",
  label: "4 × 25G",
  kind: "breakout",
  breakout_mode: "4x25G",
  parent_interface: "Ethernet52",
  available: true,
  requires_reboot: true,
  method: "static-reboot",
};
const portFixtures: Port[] = [
  {
    name: "Ethernet52",
    cage_id: "QSFP2",
    port_modes: [speed25, speed100, breakout],
  },
  { name: "Ethernet53", cage_id: "QSFP2", port_modes: [speed25, breakout] },
  {
    name: "Ethernet64",
    cage_id: "QSFP5",
    port_modes: [speed25, { ...breakout, parent_interface: "Ethernet64" }],
  },
];
const related = [portAction, breakoutAction];

test("port mode choices intersect actual speeds and restrict breakout to one cage", () => {
  assert.deepEqual(
    selectedPortModes(
      portFixtures,
      ["Ethernet52", "Ethernet64"],
      related,
    ).modes.map((m) => m.value),
    ["speed:25000"],
  );
  assert.deepEqual(
    selectedPortModes(
      portFixtures,
      ["Ethernet52", "Ethernet53"],
      related,
    ).modes.map((m) => m.value),
    ["speed:25000", "breakout:4x25G"],
  );
  assert.equal(
    selectedPortModes(portFixtures, ["Ethernet52", "unknown"], related).modes
      .length,
    0,
  );
  assert.equal(
    selectedPortModes([{ name: "Ethernet0" }], ["Ethernet0"], related).modes
      .length,
    0,
  );
  assert.equal(selectedPortModes(portFixtures, [], related).modes.length, 0);
});

test("unavailable or unadvertised breakout cannot generate a request", () => {
  const noAction = selectedPortModes(
    portFixtures,
    ["Ethernet52"],
    [portAction],
  ).modes.find((m) => m.kind === "breakout");
  assert.equal(noAction?.available, false);
  assert.match(noAction?.reason || "", /未提供/);
  const disabled = [
    {
      ...portFixtures[0],
      port_modes: [{ ...breakout, available: false, reason: "平台未验证" }],
    },
  ];
  assert.throws(
    () =>
      configurationRequest(
        portAction,
        { interfaces: ["Ethernet52"], port_mode: breakout.value },
        disabled,
        related,
      ),
    /平台未验证/,
  );
  assert.throws(
    () =>
      configurationRequest(
        portAction,
        { interfaces: ["Ethernet52"], port_mode: "speed:400000" },
        portFixtures,
        related,
      ),
    /不适用/,
  );
  const ambiguous = [
    {
      ...portFixtures[1],
      port_modes: [{ ...breakout, parent_interface: "Ethernet99" }],
    },
  ];
  assert.equal(
    selectedPortModes(
      [portFixtures[0], ...ambiguous],
      ["Ethernet52", "Ethernet53"],
      related,
    ).modes[0].available,
    false,
  );
});

test("normal speed configuration and breakout produce separate exact payloads", () => {
  assert.deepEqual(
    configurationRequest(
      portAction,
      {
        interfaces: ["Ethernet52", "Ethernet64"],
        port_mode: speed25.value,
        speed: 999,
        mtu: "9100",
      },
      portFixtures,
      related,
    ),
    {
      action: "port.configure",
      values: {
        interfaces: ["Ethernet52", "Ethernet64"],
        speed: 25000,
        mtu: 9100,
      },
    },
  );
  assert.deepEqual(
    configurationRequest(
      portAction,
      {
        interfaces: ["Ethernet52", "Ethernet53"],
        port_mode: breakout.value,
        mtu: "9100",
        admin_status: "down",
        speed: 999,
      },
      portFixtures,
      related,
    ),
    {
      action: "port.breakout",
      values: { interface: "Ethernet52", mode: "4x25G" },
    },
  );
  assert.deepEqual(
    configurationRequest(
      portAction,
      { interfaces: ["Ethernet52"], speed: 999, mtu: "9100" },
      portFixtures,
      related,
    ),
    {
      action: "port.configure",
      values: { interfaces: ["Ethernet52"], mtu: 9100 },
    },
  );
});

test("fan percentage columns never use port megabit units", () => {
  assert.equal(formatCellValue(39, "speed", "percent"), "39%");
  assert.equal(formatCellValue("45%", "speed", "percent"), "45%");
  assert.equal(formatCellValue(100000, "speed", "speed"), "100G");
  assert.equal(formatCellValue(39, "speed"), "39");
  assert.equal(percent(null), "—");
  assert.equal(percent("unknown"), "未知");
});

const fanAction: Action = {
  id: "fan.control",
  label: "风扇控制",
  fields: [
    {
      name: "mode",
      label: "模式",
      type: "select",
      required: true,
      options: [
        { value: "auto", label: "自动" },
        { value: "fixed", label: "固定" },
      ],
    },
    { name: "duty_percent", label: "占空比", type: "number", required: true },
  ],
};
const fanControl: FanControl = {
  min_percent: 30,
  max_percent: 90,
  step_percent: 5,
  manual_available: true,
};

test("fan auto drops residual duty; fixed obeys actual limits and step", () => {
  assert.deepEqual(
    configurationRequest(
      fanAction,
      { mode: "auto", duty_percent: "10" },
      [],
      [],
      fanControl,
    ),
    { action: "fan.control", values: { mode: "auto" } },
  );
  assert.deepEqual(
    configurationRequest(
      fanAction,
      { mode: "fixed", duty_percent: "45" },
      [],
      [],
      fanControl,
    ),
    { action: "fan.control", values: { mode: "fixed", duty_percent: 45 } },
  );
  for (const duty of ["", "nan", 29, 91])
    assert.throws(
      () =>
        configurationRequest(
          fanAction,
          { mode: "fixed", duty_percent: duty },
          [],
          [],
          fanControl,
        ),
      /范围/,
    );
  assert.throws(
    () =>
      configurationRequest(
        fanAction,
        { mode: "fixed", duty_percent: 44 },
        [],
        [],
        fanControl,
      ),
    /步长/,
  );
});

test("fan controls fail closed for missing limits or unsupported hardware modes", () => {
  assert.throws(
    () =>
      configurationRequest(
        fanAction,
        { mode: "fixed", duty_percent: 45 },
        [],
        [],
      ),
    /范围/,
  );
  assert.throws(
    () =>
      configurationRequest(
        fanAction,
        { mode: "fixed", duty_percent: 45 },
        [],
        [],
        {
          ...fanControl,
          manual_available: false,
          manual_reason: "OEM 接口未验证",
        },
      ),
    /OEM 接口未验证/,
  );
  assert.throws(
    () =>
      configurationRequest(
        {
          ...fanAction,
          fields: [
            { ...fanAction.fields[0], options: [] },
            fanAction.fields[1],
          ],
        },
        { mode: "auto" },
        [],
        [],
        fanControl,
      ),
    /支持/,
  );
  assert.ok(fixedFanReason({ min_percent: 90, max_percent: 30 }));
});

test("submitted reboot job is pending until backend explicitly verifies hardware", () => {
  const job: Job = {
    id: "job1",
    status: "succeeded",
    result: {
      success: true,
      changed: false,
      pending_save: false,
      message: "queued",
      details: { operation_id: "op1", reboot_required: true, status: "queued" },
    },
  };
  const operation = deferredOperation(job);
  assert.equal(operation?.id, "op1");
  assert.equal(operationPending(operation), true);
  assert.equal(
    operationTitle({ ...operation!, status: "succeeded" }),
    "等待设备验证",
  );
  assert.equal(
    operationPending({ ...operation!, status: "succeeded", verified: true }),
    false,
  );
  assert.equal(
    operationTitle({ ...operation!, status: "succeeded", verified: true }),
    "拆分已验证完成",
  );
  assert.equal(operationPending({ ...operation!, status: "failed" }), false);
  assert.equal(
    operationPending({ ...operation!, status: "rolled_back" }),
    false,
  );
  assert.equal(operationPending({ ...operation!, status: "cancelled" }), false);
  assert.equal(
    operationTitle({ ...operation!, status: "rolled_back", verified: true }),
    "原端口模式已恢复并验证",
  );
  assert.equal(
    deferredOperation({ ...job, result: { ...job.result!, success: false } }),
    undefined,
  );
  assert.equal(
    deferredOperation({ ...job, result: { ...job.result!, details: {} } }),
    undefined,
  );
});
