import assert from "node:assert/strict";
import test from "node:test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPError } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";
import { loadConfig } from "../src/config.js";
import { isTransientUpstreamError, UpstreamToolError } from "../src/errors.js";
import { createResearchUpstream, UpstreamToolError as CompatibleError } from "../src/upstream.js";
import { textResult } from "./offline.js";

const reads = ["search", "bundle", "spec", "compare", "trends", "validate"] as const;
const signed = ["recordSignedVerdict", "verdictMessage"] as const;

function fixture(error: unknown, phase: "connect" | "call" = "call", recover = true) {
  let attempts = 0;
  let closes = 0;
  const api = createResearchUpstream(loadConfig({}), () => {
    const fail = () => { if (++attempts === 1 || !recover) throw error; };
    return {
      connect: async () => { if (phase === "connect") fail(); },
      callTool: async () => { if (phase === "call") fail(); return textResult({ ok: true }); },
      close: async () => { closes++; }
    } as unknown as Pick<Client, "connect" | "callTool" | "close">;
  });
  return { api, counts: () => ({ attempts, closes }) };
}

test("structured read HTTP 503 responses retry once through the actual decoder", async () => {
  for (const operation of reads) {
    let attempts = 0;
    let closes = 0;
    const api = createResearchUpstream(loadConfig({}), () => ({
      connect: async () => {}, close: async () => { closes++; },
      callTool: async () => ++attempts === 1
        ? textResult({ error: "temporarily unavailable", status: 503 }, true)
        : textResult({ ok: true })
    } as unknown as Pick<Client, "connect" | "callTool" | "close">));
    assert.deepEqual(await api[operation]({}), { ok: true });
    assert.equal(attempts, 2);
    assert.equal(closes, 2);
  }
});

test("typed HTTP, SDK timeout and transport causes retry reads exactly once", async () => {
  const errors = [
    new StreamableHTTPError(503, "unavailable"),
    new McpError(ErrorCode.RequestTimeout, "deadline"),
    new McpError(ErrorCode.ConnectionClosed, "closed"),
    Object.assign(new Error("socket"), { code: "ECONNRESET" }),
    new TypeError("fetch failed", { cause: Object.assign(new Error("socket"), { code: "ECONNRESET" }) }),
    { statusCode: 504 }, { status: 502 }
  ];
  for (const operation of reads) for (const error of errors) for (const phase of ["connect", "call"] as const) {
    const { api, counts } = fixture(error, phase);
    assert.deepEqual(await api[operation]({}), { ok: true });
    assert.deepEqual(counts(), { attempts: 2, closes: 2 });
  }
  const error = new StreamableHTTPError(503, "unavailable");
  const { api, counts } = fixture(error, "call", false);
  await assert.rejects(api.bundle({}), (caught) => caught === error);
  assert.deepEqual(counts(), { attempts: 2, closes: 2 });
});

test("signed operations never retry structured timeouts, 503 or transport failures", async () => {
  for (const operation of signed) for (const error of [
    new McpError(ErrorCode.RequestTimeout, "deadline"),
    new StreamableHTTPError(503, "unavailable"),
    new UpstreamToolError({ error: "unavailable", status: 503 }),
    new Error("transport", { cause: { code: "ETIMEDOUT" } })
  ]) for (const phase of ["connect", "call"] as const) {
    const { api, counts } = fixture(error, phase);
    await assert.rejects(api[operation]({}), (caught) => caught === error);
    assert.deepEqual(counts(), { attempts: 1, closes: 1 });
  }
});

test("unknown errors and random messages containing 503 or timeout never retry", async () => {
  for (const error of [
    new Error("random message 503 request timeout connection reset"),
    new UpstreamToolError({ error: "paper 503 is unavailable" }),
    new Error("fetch failed"), { code: "UNKNOWN" }, { status: "503" },
    new StreamableHTTPError(400, "503 timeout"), new UpstreamToolError({ error: "timeout", status: 429 })
  ]) for (const operation of reads) {
    const { api, counts } = fixture(error);
    await assert.rejects(api[operation]({}), (caught) => caught === error);
    assert.deepEqual(counts(), { attempts: 1, closes: 1 });
  }
});

test("cause traversal is bounded, cycle-safe, and preserves the exported error class", () => {
  assert.equal(CompatibleError, UpstreamToolError);
  const cycle: { cause?: unknown } = {};
  cycle.cause = cycle;
  assert.equal(isTransientUpstreamError(cycle), false);
  let chain: unknown = { code: "ECONNRESET" };
  for (let i = 0; i < 20; i++) chain = { cause: chain };
  assert.equal(isTransientUpstreamError(chain), false);
  for (const error of [null, undefined, "503", 503, {}, { status: 500 }]) {
    assert.equal(isTransientUpstreamError(error), false);
  }
});
