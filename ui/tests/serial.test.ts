import { test } from "node:test";
import assert from "node:assert/strict";
import {
  SerialController,
  SerialTextBuffer,
  SERIAL_OUTPUT_LIMIT,
  serialUnavailableReason,
  type SerialApiLike,
  type SerialPortLike,
} from "../src/serial.ts";

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
async function eventually(check: () => boolean) {
  for (let i = 0; i < 30; i++) {
    if (check()) return;
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.ok(check(), "state did not settle");
}
class FakePort implements SerialPortLike {
  readable: ReadableStream<Uint8Array> | null = null;
  writable: WritableStream<Uint8Array> | null = null;
  source!: ReadableStreamDefaultController<Uint8Array>;
  originalReadable: ReadableStream<Uint8Array> | null = null;
  originalWritable: WritableStream<Uint8Array> | null = null;
  writes: Uint8Array[] = [];
  opens: Parameters<SerialPortLike["open"]>[0][] = [];
  trace: string[] = [];
  openGate: Promise<void> | undefined;
  writeGate: Promise<void> | undefined;
  failOpen = false;
  failWrite = false;
  failClose = false;
  closes = 0;
  async open(options: Parameters<SerialPortLike["open"]>[0]) {
    this.opens.push(options);
    if (this.openGate) await this.openGate;
    this.readable = this.originalReadable = new ReadableStream({
      start: (controller) => {
        this.source = controller;
      },
      cancel: () => {
        this.trace.push("cancel-reader");
      },
    });
    this.writable = this.originalWritable = new WritableStream({
      write: async (bytes) => {
        this.trace.push("write");
        if (this.writeGate) await this.writeGate;
        if (this.failWrite) throw new Error("simulated write failure");
        this.writes.push(Uint8Array.from(bytes));
      },
      abort: () => {
        this.trace.push("abort-writer");
      },
    });
    if (this.failOpen) throw new Error("simulated partial open failure");
  }
  async close() {
    this.closes++;
    assert.equal(
      this.originalReadable?.locked,
      false,
      "reader must release before close",
    );
    assert.equal(
      this.originalWritable?.locked,
      false,
      "writer must release before close",
    );
    this.trace.push("close");
    if (this.failClose) throw new Error("simulated close failure");
    this.readable = this.writable = null;
  }
  emit(text: string) {
    this.source.enqueue(new TextEncoder().encode(text));
  }
}
function setup(api?: SerialApiLike) {
  const port = new FakePort();
  let prompts = 0;
  const outputs: string[] = [];
  const controller = new SerialController(
    api || {
      requestPort: async () => {
        prompts++;
        return port;
      },
    },
    { onState: () => {}, onOutput: (text) => outputs.push(text) },
  );
  return { port, controller, outputs, prompts: () => prompts };
}

test("serial capability gates require live admin and secure supported browser", () => {
  assert.match(serialUnavailableReason("demo", "admin", true, true), /DEMO/);
  assert.match(serialUnavailableReason("live", "viewer", true, true), /管理员/);
  assert.match(serialUnavailableReason("live", "admin", false, true), /安全/);
  assert.match(
    serialUnavailableReason("live", "admin", true, false),
    /Web Serial/,
  );
  assert.equal(serialUnavailableReason("live", "admin", true, true), "");
});
test("serial text strips split ANSI/OSC controls and handles CRLF/backspace", () => {
  const buffer = new SerialTextBuffer();
  buffer.append("hello\x1b[");
  buffer.append("31m red\x1b[0m\x1b]0;invisible-title");
  buffer.append("\x1b");
  buffer.append("\\\r");
  buffer.append("\n😀\bworld\x00\x07");
  assert.equal(buffer.value, "hello red\nworld");
  buffer.append("\x1bPdiscarded");
  buffer.append("more\x1b\\!\u009b32m!");
  assert.equal(buffer.value, "hello red\nworld!!");
});
test("serial text retention is bounded by UTF-8 bytes and has no partial characters", () => {
  const buffer = new SerialTextBuffer();
  buffer.append("中😀".repeat(18000));
  assert.ok(
    new TextEncoder().encode(buffer.value).length <= SERIAL_OUTPUT_LIMIT,
  );
  assert.equal(buffer.value.includes("�"), false);
  assert.ok(buffer.value.endsWith("中😀"));
  buffer.clear();
  assert.equal(buffer.value, "");
});
test("connect sends nothing; explicit line/Enter/Ctrl-C do not echo; disconnect unlocks", async () => {
  const { port, controller, prompts } = setup();
  await controller.connect(115200);
  assert.equal(prompts(), 1);
  assert.deepEqual(port.opens, [
    {
      baudRate: 115200,
      dataBits: 8,
      stopBits: 1,
      parity: "none",
      flowControl: "none",
      bufferSize: 4096,
    },
  ]);
  assert.equal(port.writes.length, 0);
  await controller.sendLine("synthetic-hidden-input");
  await controller.sendLine("");
  await controller.sendInterrupt();
  assert.equal(controller.output, "");
  assert.deepEqual(
    port.writes.map((bytes) => Array.from(bytes)),
    [
      Array.from(new TextEncoder().encode("synthetic-hidden-input\r")),
      [13],
      [3],
    ],
  );
  await assert.rejects(controller.sendLine("two\nlines"), /一行/);
  port.emit("device-output");
  await eventually(() => controller.output === "device-output");
  await controller.disconnect();
  assert.equal(controller.state.phase, "idle");
  assert.equal(controller.state.hasPort, false);
  assert.equal(port.closes, 1);
  assert.ok(port.trace.indexOf("cancel-reader") < port.trace.indexOf("close"));
  await controller.dispose();
});
test("cancelled chooser returns idle; duplicate connect cannot show second chooser", async () => {
  const choice = deferred<SerialPortLike>();
  let prompts = 0;
  const { controller } = setup({
    requestPort: () => {
      prompts++;
      return choice.promise;
    },
  });
  const pending = controller.connect(115200);
  assert.equal(controller.state.phase, "selecting");
  await assert.rejects(controller.connect(9600), /先断开/);
  choice.reject(new DOMException("cancelled", "NotFoundError"));
  await pending;
  assert.equal(prompts, 1);
  assert.deepEqual(controller.state, {
    phase: "idle",
    error: "",
    hasPort: false,
  });
  await controller.dispose();
});
test("partial open failure closes acquired streams and allows explicit retry", async () => {
  const { port, controller } = setup();
  port.failOpen = true;
  await controller.connect(115200);
  assert.equal(controller.state.phase, "error");
  assert.equal(controller.state.hasPort, false);
  assert.equal(port.closes, 1);
  port.failOpen = false;
  await controller.connect(115200);
  assert.equal(controller.state.phase, "connected");
  await controller.dispose();
  assert.equal(port.closes, 2);
});
test("dispose while chooser pending never opens the subsequently selected port", async () => {
  const choice = deferred<SerialPortLike>();
  const port = new FakePort();
  const { controller } = setup({ requestPort: () => choice.promise });
  const connecting = controller.connect(115200);
  const disposing = controller.dispose();
  choice.resolve(port);
  await Promise.all([connecting, disposing]);
  assert.equal(port.opens.length, 0);
  await assert.rejects(controller.connect(115200), /关闭/);
});
test("dispose during open waits for acquisition then closes without reading or writing", async () => {
  const { port, controller } = setup();
  const gate = deferred();
  port.openGate = gate.promise;
  const connecting = controller.connect(115200);
  await eventually(() => controller.state.phase === "opening");
  const disposing = controller.dispose();
  gate.resolve();
  await Promise.all([connecting, disposing]);
  assert.equal(port.closes, 1);
  assert.equal(port.writes.length, 0);
  assert.equal(port.trace.includes("cancel-reader"), false);
  assert.equal(controller.state.hasPort, false);
});
test("read failure/unplug releases reader before closing and reports unknown connection", async () => {
  const { port, controller } = setup();
  await controller.connect(115200);
  port.source.error(new Error("unplugged"));
  await eventually(() => port.closes === 1);
  assert.equal(controller.state.phase, "error");
  assert.equal(controller.state.hasPort, false);
  assert.equal(port.originalReadable?.locked, false);
  await controller.dispose();
});
test("write failure releases writer and closes without self-await deadlock", async () => {
  const { port, controller } = setup();
  await controller.connect(115200);
  port.failWrite = true;
  await assert.rejects(controller.sendLine("synthetic-input"), /发送失败/);
  await eventually(() => port.closes === 1);
  assert.equal(controller.state.hasPort, false);
  assert.equal(port.originalWritable?.locked, false);
  await controller.dispose();
});
test("disconnect waits for in-flight write and rejects new send/connect attempts", async () => {
  const { port, controller } = setup();
  await controller.connect(115200);
  const gate = deferred();
  port.writeGate = gate.promise;
  const writing = controller.sendLine("synthetic-input");
  await eventually(() => port.trace.includes("write"));
  const disconnecting = controller.disconnect();
  await assert.rejects(controller.sendLine("later"), /未连接/);
  await assert.rejects(controller.connect(115200), /先断开/);
  assert.equal(port.closes, 0);
  gate.resolve();
  await Promise.all([writing, disconnecting]);
  assert.equal(port.closes, 1);
  assert.equal(port.originalWritable?.locked, false);
  await controller.dispose();
});
test("failed port close retains the handle for an explicit close retry", async () => {
  const { port, controller } = setup();
  await controller.connect(115200);
  port.failClose = true;
  await controller.disconnect();
  assert.equal(controller.state.hasPort, true);
  assert.match(controller.state.error, /释放失败/);
  await assert.rejects(controller.connect(115200), /先断开/);
  port.failClose = false;
  await controller.disconnect();
  assert.equal(controller.state.hasPort, false);
  assert.equal(controller.state.phase, "idle");
  assert.equal(port.closes, 2);
  await controller.dispose();
});
