export type Value = unknown;
export type Row = Record<string, Value>;
export interface Session {
  authenticated: boolean;
  user?: string;
  role?: "admin" | "viewer";
  csrf?: string;
  mode: "live" | "demo";
}
export interface Catalog {
  version: string;
  product: string;
  groups: {
    id: string;
    label: string;
    english: string;
    pages: { id: string; label: string }[];
  }[];
}
export interface Metric {
  label: string;
  value: Value;
}
export interface Capability {
  read: boolean;
  write: boolean;
  reason: string;
}
export interface Field {
  name: string;
  label: string;
  type: "text" | "number" | "select" | "multiselect" | "textarea" | "checkbox";
  required?: boolean;
  options?: {
    label: string;
    value: string;
    disabled?: boolean;
    reason?: string;
  }[];
  default?: Value;
  help?: string;
  min?: number;
  max?: number;
  step?: number;
}
export interface Action {
  id: string;
  label: string;
  description?: string;
  dangerous?: boolean;
  fields: Field[];
}
export interface Chart {
  name: string;
  unit: string;
  points: { time: string | number; value: number }[];
}
export interface FanControl {
  scope?: string;
  scope_label?: string;
  mode?: "auto" | "fixed" | "unknown";
  duty_percent?: number | null;
  min_percent?: number | null;
  max_percent?: number | null;
  step_percent?: number | null;
  protection?: {
    active?: boolean | null;
    reason?: string;
    description?: string;
  };
  temperatures?: Metric[];
  manual_available?: boolean;
  manual_reason?: string;
  reason?: string;
}
export interface Operation {
  id: string | null;
  status: string;
  message: string;
  verified?: boolean;
}
export interface PortMode {
  value: string;
  label: string;
  kind: "speed" | "breakout";
  available: boolean;
  reason?: string;
  speed?: number;
  breakout_mode?: string;
  parent_interface?: string;
  disruptive?: boolean;
  method?: "dynamic" | "static-reboot";
  requires_reboot?: boolean;
  verified?: boolean;
}
export interface PageData {
  id: string;
  title: string;
  description: string;
  columns: { key: string; label: string; type?: string; unit?: string }[];
  rows: Row[];
  summary: Metric[];
  charts?: Chart[];
  actions: Action[];
  capability: Capability;
  sampled_at?: string;
  revision?: string;
  pending_save?: boolean | null;
  pending_save_reason?: string;
  control?: FanControl;
  operation?: Operation;
  bmc?: BmcSnapshot;
  metrics?: SystemMetrics;
}
export interface SystemMetrics {
  cpu_percent?: number | null;
  memory_percent?: number | null;
  uptime?: Value;
  cpu_state?: "warming_up" | "ok" | "unknown" | "stale" | "stopped";
  cpu_warning?: string | null;
  cpu_sample_seconds?: number | null;
  metrics_sampled_at?: string | null;
  metrics_age_seconds?: number | null;
}
export interface BmcSection {
  state: "ok" | "unknown" | "partial";
  rows: { field: string; value: Value }[];
  error: string | null;
}
export interface BmcSnapshot {
  mode: "live" | "demo";
  identity: BmcSection;
  status: BmcSection;
  network: BmcSection;
  console: BmcSection & {
    supported: boolean | null;
    available: false;
    reason: string;
  };
  events: {
    state: "ok" | "unknown" | "partial";
    rows: {
      id: string;
      timestamp: Value;
      sensor: string;
      event: string;
      direction: string;
    }[];
    error: string | null;
  };
  collection: {
    sampled_at: string | null;
    elapsed_ms: number | null;
    cache_seconds: number | null;
    commands: {
      section: string;
      command_id: string;
      ok: boolean;
      elapsed_ms: number | null;
      error: string | null;
    }[];
  };
  sensors?: {
    rows: {
      category: string;
      name: string;
      value: Value;
      unit: string;
      status: string;
      timestamp: Value;
    }[];
    error?: string | null;
  };
}
export interface Overview {
  device: SystemMetrics & {
    hostname?: string;
    model?: string;
    platform?: string;
    hwsku?: string;
    version?: string;
    uptime?: Value;
    cpu_percent?: number | null;
    memory_percent?: number | null;
  };
  health: { status: string; message: string };
  summary: Metric[];
  mode: "live" | "demo";
  sampled_at?: string;
  revision?: string;
  pending_save: boolean | null;
  pending_save_reason?: string;
}
export interface Port extends Row {
  name: string;
  alias?: string;
  cage_id?: string;
  index?: number;
  lanes?: number[];
  speed?: string | number;
  admin_status?: string;
  oper_status?: string;
  mtu?: number;
  fec?: string;
  rx_bps?: number | null;
  tx_bps?: number | null;
  rx_errors?: number | null;
  tx_errors?: number | null;
  description?: string;
  connector?: string;
  breakout_mode?: string;
  port_modes?: PortMode[];
}
export interface PortsData {
  items: Port[];
  layout: { kind: string; verified: boolean; message: string };
  sampled_at?: string;
  revision?: string;
  operation?: Operation;
}
export interface PortDetail {
  port: Port;
  properties: Metric[];
  counters: Metric[];
  transceiver: Metric[];
  relationships: Metric[];
  capabilities: Record<string, Value>;
  sampled_at?: string;
  revision?: string;
}
export interface Plan {
  id: string;
  action: string;
  title: string;
  summary: string;
  changes: { target: string; before: Value; after: Value }[];
  warnings: string[];
  requires_confirmation: boolean;
  revision: string;
  expires_at: string;
  reboot_required?: boolean;
}
export interface Job {
  id: string;
  status: "queued" | "running" | "succeeded" | "failed";
  result?: {
    success: boolean;
    message: string;
    changed: boolean;
    pending_save: boolean | null;
    pending_save_reason?: string;
    details?: Value;
  };
  error?: Value;
}
