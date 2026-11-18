import { test } from "node:test";
import assert from "node:assert/strict";
import { api, ApiError } from "../src/api.ts";

test("logout POST sends JSON even with no explicit body, plus CSRF and cookies", async (t) => {
  const original = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  let request: { url: unknown; options?: RequestInit } | undefined;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return new Response(JSON.stringify({ authenticated: false }), {
      headers: { "Content-Type": "application/json" },
    });
  };
  const result = await api<{ authenticated: boolean }>("/logout", {
    method: "POST",
    csrf: "test-csrf",
  });
  assert.equal(result.authenticated, false);
  assert.equal(request?.url, "/api/logout");
  assert.equal(request?.options?.method, "POST");
  assert.equal(request?.options?.body, "{}");
  assert.equal(request?.options?.credentials, "same-origin");
  const headers = new Headers(request?.options?.headers);
  assert.equal(headers.get("Content-Type"), "application/json");
  assert.equal(headers.get("X-CSRF-Token"), "test-csrf");
});

test("GET has no body and non-JSON responses remain explicit failures", async (t) => {
  const original = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  let request: RequestInit | undefined;
  globalThis.fetch = async (_url, options) => {
    request = options;
    return new Response("<html>unavailable</html>", {
      status: 503,
      headers: { "Content-Type": "text/html" },
    });
  };
  await assert.rejects(
    () => api("/overview"),
    (error: unknown) => error instanceof ApiError && error.status === 503,
  );
  assert.equal(request?.method, "GET");
  assert.equal(request?.body, undefined);
  assert.equal(new Headers(request?.headers).get("Content-Type"), null);
});

test("terminal close retains same-origin JSON and CSRF with keepalive cleanup", async (t) => {
  const original = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = original;
  });
  let request: RequestInit | undefined;
  globalThis.fetch = async (_url, options) => {
    request = options;
    return new Response("{}", {
      headers: { "Content-Type": "application/json" },
    });
  };
  await api("/consoles/test-session/close", {
    method: "POST",
    body: {},
    csrf: "test-csrf",
    keepalive: true,
  });
  assert.equal(request?.keepalive, true);
  assert.equal(request?.credentials, "same-origin");
  assert.equal(request?.body, "{}");
  assert.equal(new Headers(request?.headers).get("X-CSRF-Token"), "test-csrf");
});
