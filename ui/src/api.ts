export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(
  path: string,
  options: {
    method?: "GET" | "POST";
    body?: unknown;
    csrf?: string;
    signal?: AbortSignal;
    keepalive?: boolean;
  } = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const body =
    options.body !== undefined
      ? options.body
      : method === "POST"
        ? {}
        : undefined;
  const response = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    signal: options.signal,
    ...(options.keepalive ? { keepalive: true } : {}),
    headers: {
      Accept: "application/json",
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(options.csrf ? { "X-CSRF-Token": options.csrf } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.headers.get("content-type")?.includes("application/json"))
    throw new ApiError(
      `服务返回了非 JSON 响应（HTTP ${response.status}）。请检查 API 服务。`,
      response.status,
    );
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/login")
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(
      data.message || data.error || `请求失败（HTTP ${response.status}）`,
      response.status,
    );
  }
  return data as T;
}
