import assert from "node:assert/strict";
import test from "node:test";
import type { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { loadConfig } from "../src/config.js";
import { createResearchUpstream, decodeUpstreamResult, UpstreamToolError } from "../src/upstream.js";
import { offlineGateway, payload, textResult } from "./offline.js";

test("bundle routing preserves strict budget and limit defaults and explicit overrides", async (t) => {
  const gateway = await offlineGateway(t, { mode: "disabled", response: textResult({ evidence: [] }) });
  for (const args of [{ query: "fixture" }, { query: "fixture", layer: "builder-tech", limit: 1, max_chars: 7000, strict: false }]) {
    const result = await gateway.client.callTool({ name: "get_evidence_bundle", arguments: args });
    assert.ok(!result.isError);
    assert.deepEqual(payload(result), { evidence: [] });
    assert.deepEqual(gateway.calls.at(-1), { name: "get_research_bundle",
      arguments: { limit: 3, max_chars: 12000, strict: true, ...args } });
  }
});

test("unsupported bundle filters are rejected before payment or upstream calls", async (t) => {
  const gateway = await offlineGateway(t);
  for (const name of ["section_type", "element_type", "year_from", "terms", "unknown_filter"]) {
    const result = await gateway.client.callTool({ name: "get_evidence_bundle", arguments: { query: "fixture", [name]: "fixture" } });
    assert.equal(result.isError, true);
  }
  assert.deepEqual(gateway.events, []);
  assert.deepEqual(gateway.calls, []);
});

test("upstream protocol errors preserve JSON statuses and rate limit diagnostics including SDK prefixes", () => {
  const body = { error: "rate limit", status: 429, retry_after: 12, rate_limit_headers: { "Retry-After": "12" } };
  for (const response of [textResult(body, true), { isError: true, content: [{ type: "text", text: "Error executing tool: " + JSON.stringify(body) }] }]) {
    assert.throws(() => decodeUpstreamResult("get_research_bundle", response), (error) => {
      assert.ok(error instanceof UpstreamToolError);
      assert.deepEqual(error.payload, body);
      return true;
    });
  }
});

test("upstream all-failed versus partial signed batches and empty batches", () => {
  const failed = { results: [{ status: 400 }, { status: 503 }] };
  assert.throws(() => decodeUpstreamResult("record_signed_verdict", textResult(failed)), UpstreamToolError);
  for (const body of [{ results: [{ status: 200 }, { status: 400 }] }, { results: [] }]) {
    assert.deepEqual(decodeUpstreamResult("record_signed_verdict", textResult(body)), body);
  }
});

test("only transient reads retry once; signed writes and preparation never autoretry", async () => {
  for (const operation of ["search", "bundle", "recordSignedVerdict", "verdictMessage"] as const) {
    let attempts = 0;
    let closes = 0;
    const api = createResearchUpstream(loadConfig({}), () => ({
      connect: async () => {}, close: async () => { closes++; },
      callTool: async () => { attempts++; throw new Error("request timeout"); }
    } as unknown as Pick<Client, "connect" | "callTool" | "close">));
    await assert.rejects(api[operation]({ query: "fixture" }), /timeout/);
    const expected = operation === "search" || operation === "bundle" ? 2 : 1;
    assert.equal(attempts, expected);
    assert.equal(closes, expected);
  }
});

test("non-transient read errors do not retry and successful UTF-8 responses remain intact", async () => {
  let attempts = 0;
  const body = { title: "東京 café" };
  const api = createResearchUpstream(loadConfig({}), () => ({
    connect: async () => {}, close: async () => {},
    callTool: async () => { attempts++; return attempts === 1 ? textResult({ error: "paper not found", status: 404 }, true) : textResult(body); }
  } as unknown as Pick<Client, "connect" | "callTool" | "close">));
  await assert.rejects(api.search({ query: "fixture" }), /paper not found/);
  assert.equal(attempts, 1);
  assert.deepEqual(await api.search({ query: "fixture" }), body);
});
