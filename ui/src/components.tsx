import { useEffect, useState, type ReactNode } from "react";
import type { Chart, Metric, PageData, Port, PortsData, Row } from "./types";
import {
  display,
  filterSort,
  groupCages,
  rate,
  speed,
  timestamp,
  tone,
  formatCellValue,
} from "./model";

export function Icon({ name, size = 16 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    refresh: (
      <>
        <path d="M20 7v5h-5M4 17v-5h5" />
        <path d="M6.1 7a7 7 0 0 1 11.6-1L20 9M4 15l2.3 3A7 7 0 0 0 18 17" />
      </>
    ),
    search: (
      <>
        <circle cx="10.5" cy="10.5" r="6.5" />
        <path d="m16 16 4 4" />
      </>
    ),
    close: <path d="m6 6 12 12M6 18 18 6" />,
    chevron: <path d="m9 5 7 7-7 7" />,
    user: (
      <>
        <circle cx="12" cy="8" r="3.5" />
        <path d="M5 21v-3a7 7 0 0 1 14 0v3" />
      </>
    ),
    save: (
      <>
        <path d="M4 3h13l3 3v15H4z" />
        <path d="M8 3v6h8V3M8 21v-8h8v8" />
      </>
    ),
    box: (
      <>
        <path d="m3 7 9-4 9 4v11l-9 4-9-4zM3 7l9 4 9-4M12 11v11" />
      </>
    ),
    info: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v6M12 7v1" />
      </>
    ),
    download: (
      <>
        <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />
      </>
    ),
    logout: (
      <>
        <path d="M10 4H4v16h6M9 12h12m-4-4 4 4-4 4" />
      </>
    ),
    check: <path d="m5 12 5 5L20 7" />,
    lock: (
      <>
        <rect x="5" y="10" width="14" height="11" rx="2" />
        <path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3" />
      </>
    ),
    network: (
      <>
        <rect x="7" y="2" width="10" height="6" rx="1" />
        <rect x="1" y="17" width="6" height="5" rx="1" />
        <rect x="17" y="17" width="6" height="5" rx="1" />
        <path d="M12 8v5M4 17v-4h16v4" />
      </>
    ),
    warn: (
      <>
        <path d="m12 3 10 18H2zM12 9v5M12 17v1" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] ?? paths.box}
    </svg>
  );
}

const statusLabels: Record<string, string> = {
  up: "Up",
  down: "Down",
  ok: "正常",
  healthy: "正常",
  unhealthy: "异常",
  unknown: "未知",
  degraded: "降级",
  warning: "告警",
  failed: "失败",
  enabled: "启用",
  disabled: "禁用",
  present: "在位",
  absent: "不在位",
  running: "运行中",
  initializing: "初始化",
  queued: "排队中",
  succeeded: "已完成",
};
export function Badge({
  value,
  children,
}: {
  value: unknown;
  children?: ReactNode;
}) {
  return (
    <span className={`badge ${tone(value)}`}>
      <i />
      {children ?? statusLabels[String(value).toLowerCase()] ?? display(value)}
    </span>
  );
}
export function Notice({
  children,
  kind = "info",
}: {
  children: ReactNode;
  kind?: "info" | "warn" | "error" | "success";
}) {
  return (
    <div
      className={`notice ${kind}`}
      role={kind === "error" ? "alert" : undefined}
    >
      <Icon
        name={kind === "success" ? "check" : kind === "info" ? "info" : "warn"}
      />
      <div>{children}</div>
    </div>
  );
}
export function Empty({
  title = "暂无记录",
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Icon name="box" size={25} />
      </span>
      <strong>{title}</strong>
      <p>{children ?? "当前采样未返回数据。"}</p>
    </div>
  );
}
export function Metrics({ items }: { items: Metric[] }) {
  return items.length ? (
    <div className="metrics">
      {items.map((m, i) => (
        <div className="metric" key={i}>
          <span>{m.label}</span>
          <strong title={display(m.value)}>{display(m.value)}</strong>
        </div>
      ))}
    </div>
  ) : null;
}
export function Properties({ items }: { items: Metric[] }) {
  return items.length ? (
    <dl className="properties">
      {items.map((m, i) => (
        <div key={i}>
          <dt>{m.label}</dt>
          <dd>{display(m.value)}</dd>
        </div>
      ))}
    </dl>
  ) : (
    <Empty title="暂无可用信息">设备尚未提供此类数据。</Empty>
  );
}
export function Panel({
  title,
  aside,
  children,
  className = "",
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-heading">
        <h2>{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  );
}

function cell(row: Row, key: string, type?: string, unit?: string) {
  const value = row[key];
  if (
    type === "status" ||
    ["oper_status", "admin_status", "status", "state"].includes(key)
  )
    return <Badge value={value} />;
  if (
    ["percent", "speed", "rate", "temperature"].includes(type ?? "") ||
    key.endsWith("_bps")
  )
    return (
      <span className="mono">{formatCellValue(value, key, type, unit)}</span>
    );
  if (type === "boolean") return display(value);
  return (
    <span
      title={display(value)}
      className={
        ["name", "alias", "address", "prefix", "mac", "ip"].includes(key)
          ? "mono"
          : ""
      }
    >
      {display(value)}
    </span>
  );
}

export function DataTable({
  columns,
  rows,
  selected,
  onSelection,
  onOpen,
  empty,
  label = "记录",
}: {
  columns: PageData["columns"];
  rows: Row[];
  selected?: Set<string>;
  onSelection?: (next: Set<string>) => void;
  onOpen?: (row: Row) => void;
  empty?: string;
  label?: string;
}) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState(columns[0]?.key ?? "name");
  const [asc, setAsc] = useState(true);
  const [page, setPage] = useState(0);
  const [limit, setLimit] = useState(25);
  const filtered = filterSort(rows, query, sort, asc);
  const pages = Math.max(1, Math.ceil(filtered.length / limit));
  const shownPage = Math.min(page, pages - 1);
  const visible = filtered.slice(shownPage * limit, (shownPage + 1) * limit);
  const ids = visible.map((row) => String(row.name));
  const all = ids.length > 0 && ids.every((id) => selected?.has(id));
  useEffect(() => {
    setPage(0);
  }, [query, limit]);
  function toggle(name: string) {
    const next = new Set(selected);
    next.has(name) ? next.delete(name) : next.add(name);
    onSelection?.(next);
  }
  return (
    <div className="table-block">
      <div className="table-tools">
        <div className="search">
          <Icon name="search" />
          <input
            aria-label={`搜索${label}`}
            placeholder={`搜索${label}…`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {query && (
            <button
              className="icon-button"
              aria-label="清除搜索"
              onClick={() => setQuery("")}
            >
              <Icon name="close" size={13} />
            </button>
          )}
        </div>
        <span className="table-count">
          {query ? `${filtered.length} / ${rows.length}` : rows.length} 条
          {selected && selected.size > 0 && (
            <>
              {" "}
              · 已选 <b>{selected.size}</b>
              <button
                className="link-button"
                onClick={() => onSelection?.(new Set())}
              >
                清空
              </button>
            </>
          )}
        </span>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {onSelection && (
                <th className="selection-cell">
                  <input
                    type="checkbox"
                    aria-label="选择当前页全部端口"
                    checked={all}
                    onChange={() => {
                      const next = new Set(selected);
                      ids.forEach((id) =>
                        all ? next.delete(id) : next.add(id),
                      );
                      onSelection(next);
                    }}
                  />
                </th>
              )}
              {columns.map((c) => (
                <th
                  key={c.key}
                  aria-sort={
                    sort === c.key ? (asc ? "ascending" : "descending") : "none"
                  }
                >
                  <button
                    onClick={() => {
                      if (sort === c.key) setAsc(!asc);
                      else {
                        setSort(c.key);
                        setAsc(true);
                      }
                    }}
                  >
                    {c.label}
                    <span
                      className={`sort-marker ${sort === c.key ? "sorted" : ""}`}
                    >
                      {sort === c.key && !asc ? "▾" : "▴"}
                    </span>
                  </button>
                </th>
              ))}
              {onOpen && <th className="detail-cell">详情</th>}
            </tr>
          </thead>
          <tbody>
            {visible.map((row, i) => (
              <tr
                key={`${display(row.name ?? row.id)}-${i}`}
                className={
                  selected?.has(String(row.name)) ? "selected-row" : ""
                }
              >
                {onSelection && (
                  <td className="selection-cell">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${row.name}`}
                      checked={selected?.has(String(row.name)) ?? false}
                      onChange={() => toggle(String(row.name))}
                    />
                  </td>
                )}
                {columns.map((c, ci) => (
                  <td key={c.key}>
                    {onOpen && ci === 0 ? (
                      <button
                        className="link-button port-name"
                        onClick={() => onOpen(row)}
                      >
                        {cell(row, c.key, c.type, c.unit)}
                      </button>
                    ) : (
                      cell(row, c.key, c.type, c.unit)
                    )}
                  </td>
                ))}
                {onOpen && (
                  <td>
                    <button
                      className="icon-button"
                      aria-label={`查看 ${display(row.name)} 详情`}
                      onClick={() => onOpen(row)}
                    >
                      <Icon name="chevron" size={14} />
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
        {!visible.length && (
          <Empty title={query ? "没有匹配的记录" : "暂无记录"}>
            {query
              ? "尝试其他名称、地址或状态。"
              : (empty ?? "当前设备未返回此类记录。")}
          </Empty>
        )}
      </div>
      <div className="table-footer">
        <span>
          显示 {filtered.length ? shownPage * limit + 1 : 0}–
          {Math.min((shownPage + 1) * limit, filtered.length)}，共{" "}
          {filtered.length} 条
        </span>
        <div>
          <label>
            每页{" "}
            <select
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option>25</option>
              <option>50</option>
              <option>100</option>
            </select>
          </label>
          <button
            aria-label="上一页"
            disabled={shownPage === 0}
            onClick={() => setPage(shownPage - 1)}
          >
            ‹
          </button>
          <span>
            {shownPage + 1} / {pages}
          </span>
          <button
            aria-label="下一页"
            disabled={shownPage + 1 >= pages}
            onClick={() => setPage(shownPage + 1)}
          >
            ›
          </button>
        </div>
      </div>
    </div>
  );
}

export function PortMap({
  data,
  onOpen,
  selected,
  onSelection,
}: {
  data: PortsData;
  onOpen: (p: Port) => void;
  selected?: Set<string>;
  onSelection?: (s: Set<string>) => void;
}) {
  const cages = groupCages(data.items);
  const groups = [
    { title: "SFP / SFP28", cages: cages.filter((c) => /^SFP/i.test(c.id)) },
    { title: "QSFP / QSFP28", cages: cages.filter((c) => /^QSFP/i.test(c.id)) },
    { title: "其他接口", cages: cages.filter((c) => !/^(Q?SFP)/i.test(c.id)) },
  ].filter((g) => g.cages.length);
  return (
    <div className="port-map">
      <Notice>
        {data.layout.verified
          ? "已验证设备布局。"
          : "逻辑端口示意 · 不代表机箱实际插孔的行列位置。"}{" "}
        {data.layout.message}
      </Notice>
      <div className="map-legend">
        <span>
          <i className="link-light up" />
          链路 Up
        </span>
        <span>
          <i className="link-light down" />
          链路 Down
        </span>
        <span>
          <i className="link-light unknown" />
          状态未知
        </span>
        <span>点击端口查看详情{onSelection ? "；复选框用于批量配置" : ""}</span>
      </div>
      {groups.map((g) => (
        <section className="cage-group" key={g.title}>
          <div className="cage-group-title">
            <h3>{g.title}</h3>
            <span>{g.cages.length} 个物理模块 / 插槽</span>
          </div>
          <div className="cages">
            {g.cages.map((c) => (
              <div
                className={`cage ${c.ports.length > 1 ? "split" : ""}`}
                key={c.id}
              >
                <div className="cage-title">
                  {c.id}
                  <span>
                    {c.ports.length > 1
                      ? `${c.ports.length} 路`
                      : speed(c.ports[0].speed)}
                  </span>
                </div>
                <div className="cage-ports">
                  {c.ports.map((p) => (
                    <div
                      className={`map-port ${selected?.has(p.name) ? "selected" : ""}`}
                      key={p.name}
                    >
                      <button
                        title={`${p.name} · ${display(p.alias)} · ${speed(p.speed)} · ${display(p.oper_status)}`}
                        onClick={() => onOpen(p)}
                      >
                        <i
                          className={`link-light ${p.oper_status?.toLowerCase() === "up" ? "up" : p.oper_status?.toLowerCase() === "down" ? "down" : "unknown"}`}
                        />
                        <span>{p.name}</span>
                        <span className="port-contact" />
                      </button>
                      {onSelection && (
                        <input
                          type="checkbox"
                          aria-label={`选择 ${p.name}`}
                          checked={selected?.has(p.name) ?? false}
                          onChange={() => {
                            const n = new Set(selected);
                            n.has(p.name) ? n.delete(p.name) : n.add(p.name);
                            onSelection(n);
                          }}
                        />
                      )}
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </section>
      ))}
      {!cages.length && <Empty title="尚无端口数据" />}
    </div>
  );
}

export function LineChart({ chart }: { chart: Chart }) {
  const points = chart.points.filter((p) => Number.isFinite(p.value));
  if (points.length < 2)
    return (
      <Panel title={chart.name}>
        <Empty title="历史采样不足">需要至少两条有效采样才能显示趋势。</Empty>
      </Panel>
    );
  const low = Math.min(...points.map((p) => p.value)),
    high = Math.max(...points.map((p) => p.value));
  const range = high - low || 1;
  const d = points
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${36 + (i / (points.length - 1)) * 564},${140 - ((p.value - low) / range) * 112}`,
    )
    .join(" ");
  return (
    <Panel
      title={chart.name}
      aside={
        <span className="muted">
          {points.length} 次采样 · {chart.unit}
        </span>
      }
    >
      <div className="chart">
        <svg
          viewBox="0 0 620 170"
          role="img"
          aria-label={`${chart.name}，最小 ${low}，最大 ${high} ${chart.unit}`}
        >
          <line x1="36" y1="28" x2="600" y2="28" className="gridline" />
          <line x1="36" y1="84" x2="600" y2="84" className="gridline" />
          <line x1="36" y1="140" x2="600" y2="140" className="gridline" />
          <text x="2" y="33">
            {high.toFixed(1)}
          </text>
          <text x="2" y="145">
            {low.toFixed(1)}
          </text>
          <path d={d} fill="none" stroke="#1685b8" strokeWidth="2" />
          <text x="36" y="164">
            {timestamp(points[0].time)}
          </text>
          <text x="600" y="164" textAnchor="end">
            {timestamp(points.at(-1)!.time)}
          </text>
        </svg>
      </div>
    </Panel>
  );
}
