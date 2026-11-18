import { test } from "node:test";
import assert from "node:assert/strict";
import {
  RemoteConsoleController,
  base64ToBytes,
  bytesToBase64,
  consoleUnavailableReason,
  CONSOLE_INPUT_QUEUE,
  type ConsoleRequest,
  type ConsoleRequestOptions,
  type ConsoleCapabilities,
} from "../src/remoteConsole.ts";

function deferred<T = unknown>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
async function eventually(check: () => boolean, timeout = 1500) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 4));
  }
  assert.ok(check(), "transport did not settle");
}
function created(id = "one", mode = "live") {
  return {
    id,
    target: "sonic",
    mode,
    cursor: 0,
    created_at: "2026-11-18T00:00:00Z",
    expires_at: "2026-11-18T00:30:00Z",
  };
}
function read(
  id: string,
  cursor: number,
  bytes = new Uint8Array(),
  closed = false,
) {
  return {
    id,
    data: bytesToBase64(bytes),
    cursor,
    next_cursor: cursor + bytes.length,
    oldest_cursor: 0,
    truncated: false,
    closed,
    exit_code: closed ? 0 : null,
    reason: closed ? "process_exit" : undefined,
  };
}
function setup(
  handler?: (
    path: string,
    options: ConsoleRequestOptions,
    normal: () => unknown,
  ) => unknown,
) {
  const calls: { path: string; options: ConsoleRequestOptions; at: number }[] =
    [];
  const output: Uint8Array[] = [];
  const notices: string[] = [];
  let count = 0;
  const request: ConsoleRequest = async <T>(
    path: string,
    options: ConsoleRequestOptions = {},
  ) => {
    calls.push({ path, options, at: Date.now() });
    const normal = () => {
      if (path === "/consoles") return created("session" + ++count);
      if (path.endsWith("/input"))
        return {
          accepted_bytes: base64ToBytes((options.body as { data: string }).data)
            .length,
        };
      if (path.endsWith("/close") || path.endsWith("/resize")) return {};
      const match = path.match(/^\/consoles\/([^?]+)\?cursor=(\d+)$/)!;
      return read(match[1], Number(match[2]));
    };
    return (await (handler ? handler(path, options, normal) : normal())) as T;
  };
  const controller = new RemoteConsoleController(
    "sonic",
    request,
    {
      onState: () => {},
      onOutput: (bytes) => output.push(bytes),
      onNotice: (text) => notices.push(text),
    },
    10,
  );
  return { controller, calls, output, notices };
}
test("fixed-target capabilities require live admin and advertised availability", () => {
  const capabilities: ConsoleCapabilities = {
    mode: "live",
    sonic: { available: true, reason: "" },
    bmc: { available: false, reason: "BMC SSH 未验证" },
  };
  assert.equal(
    consoleUnavailableReason("live", "admin", capabilities, "sonic"),
    "",
  );
  assert.match(
    consoleUnavailableReason("demo", "admin", capabilities, "sonic"),
    /DEMO/,
  );
  assert.match(
    consoleUnavailableReason("live", "viewer", capabilities, "sonic"),
    /管理员/,
  );
  assert.match(
    consoleUnavailableReason("live", "admin", capabilities, "bmc"),
    /BMC SSH 未验证/,
  );
  assert.match(
    consoleUnavailableReason("live", "admin", undefined, "sonic"),
    /确认/,
  );
  assert.throws(
    () =>
      new RemoteConsoleController(
        "arbitrary-host" as "sonic",
        async <T>() => ({}) as T,
        { onState: () => {}, onOutput: () => {}, onNotice: () => {} },
      ),
    /无效/,
  );
});
test("base64 preserves raw PTY bytes including fragmented Unicode and terminal controls", () => {
  const bytes = new Uint8Array([
    ...new TextEncoder().encode("中😀\x1b[31m"),
    0,
    255,
  ]);
  assert.deepEqual(base64ToBytes(bytesToBase64(bytes)), bytes);
  assert.throws(() => base64ToBytes("*invalid*"));
});
test("connect sends only fixed target and geometry, never startup input", async () => {
  const { controller, calls } = setup();
  await controller.connect(24, 80);
  assert.deepEqual(calls[0].options.body, {
    target: "sonic",
    rows: 24,
    cols: 80,
  });
  assert.equal(calls.filter((c) => c.path.endsWith("/input")).length, 0);
  await controller.dispose();
  const close = calls.find((c) => c.path.endsWith("/close"));
  assert.deepEqual(close?.options.body, {});
  assert.equal(close?.options.keepalive, true);
});
test("cursor advances by byte range and drains final output before releasing closed process", async () => {
  let reads = 0;
  const source = new TextEncoder().encode("中文末尾");
  const { controller, calls, output } = setup((path, options, normal) => {
    if (path.includes("?cursor=")) {
      const start = Number(path.split("=")[1]);
      const chunk = source.slice(start, start + 2);
      reads++;
      return read("session1", start, chunk, true);
    }
    return normal();
  });
  await controller.connect(24, 80);
  await eventually(() => controller.state.phase === "closed");
  assert.deepEqual(
    Uint8Array.from(output.flatMap((b) => Array.from(b))),
    source,
  );
  assert.equal(reads, Math.ceil(source.length / 2) + 1);
  assert.equal(calls.filter((c) => c.path.endsWith("/close")).length, 1);
  await controller.dispose();
});
test("truncated server ring explicitly reports incomplete output", async () => {
  let first = true;
  const { controller, notices } = setup((path, options, normal) => {
    if (path.includes("?cursor=") && first) {
      first = false;
      return {
        ...read("session1", 100, new Uint8Array([65])),
        truncated: true,
        oldest_cursor: 100,
      };
    }
    return normal();
  });
  await controller.connect(24, 80);
  await eventually(() => notices.length === 1);
  assert.match(notices[0], /不完整/);
  await controller.dispose();
});
test("dispose during create closes late session without reading or sending", async () => {
  const late = deferred();
  const { controller, calls } = setup((path, options, normal) =>
    path === "/consoles" ? late.promise : normal(),
  );
  const connect = controller.connect(24, 80),
    dispose = controller.dispose();
  late.resolve(created());
  await Promise.all([connect, dispose]);
  assert.deepEqual(
    calls.map((c) => c.path),
    ["/consoles", "/consoles/one/close"],
  );
});
test("DEMO session returned to live transport is closed without accepting input", async () => {
  const { controller, calls } = setup((path, options, normal) =>
    path === "/consoles" ? created("demo-session", "demo") : normal(),
  );
  await controller.connect(24, 80);
  assert.equal(controller.state.phase, "error");
  assert.equal(controller.sendText("anything"), false);
  assert.equal(
    calls.some((c) => c.path.includes("?cursor=")),
    false,
  );
  assert.equal(calls.at(-1)?.path, "/consoles/demo-session/close");
  await controller.dispose();
});
test("input chunks stay within 4096 bytes and 16KiB per second without local echo", async () => {
  const { controller, calls, output } = setup();
  await controller.connect(24, 80);
  const text = "x".repeat(5 * 4096);
  assert.equal(controller.sendText(text), true);
  await eventually(
    () => calls.filter((c) => c.path.endsWith("/input")).length === 5,
    2200,
  );
  const input = calls.filter((c) => c.path.endsWith("/input"));
  assert.ok(
    input.every(
      (c) =>
        base64ToBytes((c.options.body as { data: string }).data).length <= 4096,
    ),
  );
  assert.ok(input[4].at - input[0].at >= 1000);
  assert.equal(output.length, 0);
  await controller.dispose();
});
test("oversized paste is rejected atomically and failed input is not replayed", async () => {
  let input = 0;
  const { controller, notices } = setup((path, options, normal) => {
    if (path.endsWith("/input")) {
      input++;
      throw new Error("synthetic transport error");
    }
    return normal();
  });
  await controller.connect(24, 80);
  assert.equal(controller.sendText("x".repeat(CONSOLE_INPUT_QUEUE + 1)), false);
  assert.match(notices[0], /过长/);
  controller.sendText("x".repeat(8192));
  await eventually(
    () => controller.state.phase === "error" && !controller.state.hasSession,
  );
  assert.equal(input, 1);
  await controller.dispose();
});
test("resize coalesces geometry updates and is not terminal input", async () => {
  const { controller, calls } = setup();
  await controller.connect(24, 80);
  controller.resize(25, 90);
  controller.resize(30, 110);
  await eventually(() => calls.some((c) => c.path.endsWith("/resize")));
  assert.deepEqual(
    calls.find((c) => c.path.endsWith("/resize"))?.options.body,
    { rows: 30, cols: 110 },
  );
  assert.equal(
    calls.some((c) => c.path.endsWith("/input")),
    false,
  );
  await controller.dispose();
});
test("late old read rejection cannot disconnect a replacement session", async () => {
  const old = deferred();
  const { controller, calls } = setup((path, options, normal) =>
    path.startsWith("/consoles/session1?") ? old.promise : normal(),
  );
  await controller.connect(24, 80);
  await controller.disconnect();
  await controller.connect(24, 80);
  old.reject(new Error("delayed old read abort"));
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(controller.state.phase, "connected");
  assert.equal(
    calls.some((c) => c.path === "/consoles/session2/close"),
    false,
  );
  await controller.dispose();
});
test("late old input rejection cannot close new session or clear its write lock", async () => {
  const old = deferred(),
    fresh = deferred();
  const { controller, calls } = setup((path, options, normal) =>
    path === "/consoles/session1/input"
      ? old.promise
      : path === "/consoles/session2/input"
        ? fresh.promise
        : normal(),
  );
  await controller.connect(24, 80);
  controller.sendText("old");
  await eventually(() =>
    calls.some((c) => c.path === "/consoles/session1/input"),
  );
  await controller.disconnect();
  await controller.connect(24, 80);
  controller.sendText("new");
  await eventually(() =>
    calls.some((c) => c.path === "/consoles/session2/input"),
  );
  old.reject(new Error("delayed old input abort"));
  controller.sendText("later");
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(controller.state.phase, "connected");
  assert.equal(
    calls.filter((c) => c.path === "/consoles/session2/input").length,
    1,
  );
  assert.equal(
    calls.some((c) => c.path === "/consoles/session2/close"),
    false,
  );
  fresh.resolve({ accepted_bytes: 3 });
  await eventually(
    () =>
      calls.filter((c) => c.path === "/consoles/session2/input").length === 2,
  );
  await controller.dispose();
});
test("close failure can be retried and expired 404 sessions count as released", async () => {
  let closes = 0;
  const { controller } = setup((path, options, normal) => {
    if (path.endsWith("/close")) {
      closes++;
      throw Object.assign(new Error("synthetic"), {
        status: closes === 1 ? 503 : 404,
      });
    }
    return normal();
  });
  await controller.connect(24, 80);
  await controller.disconnect();
  assert.equal(controller.state.hasSession, true);
  await controller.disconnect();
  assert.equal(controller.state.hasSession, false);
  await controller.dispose();
});
