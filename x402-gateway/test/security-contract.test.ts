import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { MCP_PAYMENT_RESPONSE_META_KEY } from "@x402/mcp";
import type { FacilitatorClient } from "@x402/core/server";
import { loadConfig } from "../src/config.js";
import { createWrappers } from "../src/payments.js";
import { paidToolSchemas } from "../src/tool-schemas.js";
import { decodeUpstreamResult, UpstreamToolError } from "../src/upstream.js";
import { offlineGateway, payload, textResult, verdictArgs } from "./offline.js";

const batchCases = JSON.parse(readFileSync(new URL("../../mcp/tests/signed_batch_cases.json", import.meta.url), "utf8")) as Array<{
  name: string; payload: Record<string, unknown>; outcome: "accepted" | "rejected" | "malformed";
}>;

const recipient = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
const live = { X402_MODE: "live", X402_SVM_PAY_TO: recipient };

test("configuration rejects invalid decoded recipients, networks, URLs and lossy USDC prices", () => {
  for (const X402_SVM_PAY_TO of ["z".repeat(44), "1".repeat(33), "0".repeat(32)]) {
    assert.throws(() => loadConfig({ ...live, X402_SVM_PAY_TO }), /Solana address/);
  }
  for (const X402_SVM_NETWORK of ["solana-devnet", "solana:", "eip155:1", "solana:" + "0".repeat(32)]) {
    assert.throws(() => loadConfig({ X402_SVM_NETWORK }), /CAIP-2/);
  }
  for (const name of ["DTOX_UPSTREAM_MCP_URL", "X402_FACILITATOR_URL"]) {
    for (const value of ["ftp://host.invalid", "https://user:pass@host.invalid", "https://host.invalid/#token", "not a URL"]) {
      assert.throws(() => loadConfig({ [name]: value }), /HTTP\(S\)/);
    }
  }
  for (const name of ["EVIDENCE", "SPEC", "COMPARE", "TRENDS", "AUDIT", "VERDICT"]) {
    for (const price of ["$0", "$-1", "$0.0000001", "$0.0100001", "NaN", "1e-2", "$18446744073709.551616", "$1 ETH"]) {
      assert.throws(() => loadConfig({ [`X402_PRICE_${name}`]: price }), /invalid X402_PRICE/);
    }
  }
  for (const price of ["$0.000001", "0.01", "$0.0100000", "0.01 USD", "0.01 USDC"]) {
    assert.equal(loadConfig({ X402_PRICE_VERDICT: price }).prices.verdict, price);
  }
});

for (const network of ["eip155:84532", "eip155:11155111", "eip155:421614", "eip155:8453", "eip155:1", "eip155:42161"]) {
  test(`OFFLINE network ${network}: requirement construction only, mainnet live UNKNOWN`, { timeout: 10000 }, async (t) => {
    t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
    const config = loadConfig({ ...live, X402_EVM_NETWORKS: network,
      X402_EVM_PAY_TO: "0x1234567890123456789012345678901234567890" });
    const networks = [network, config.svmNetwork];
    let payments = 0;
    const facilitator: FacilitatorClient = {
      getSupported: async () => ({ kinds: networks.map((network) => ({ x402Version: 2,
        scheme: "exact", network: network as `${string}:${string}`, extra: { feePayer: recipient } })), extensions: [], signers: {} }),
      verify: async () => { payments++; throw new Error("payments forbidden"); },
      settle: async () => { payments++; throw new Error("payments forbidden"); },
    };
    if (network === "eip155:11155111") {
      await assert.rejects(() => createWrappers(config, facilitator), /No default asset configured/);
      t.diagnostic("KNOWN BLOCKER: installed SDK has no Ethereum Sepolia asset mapping; network was not disabled");
    } else {
      const wrappers = await createWrappers(config, facilitator);
      assert.deepEqual(Object.keys(wrappers), ["evidence", "spec", "compare", "trends", "audit", "verdict"]);
      assert.ok(Object.values(wrappers).every((wrapper) => typeof wrapper === "function"));
      t.diagnostic("SIMULATED: real SDK, mocked facilitator; no settlement/transaction/signature/live E2E evidence");
    }
    assert.equal(payments, 0);
  });
}

test("cross-network payment requirements are rejected before handler or settlement", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  const gateway = await offlineGateway(t);
  const challenge = await gateway.challenge("record_signed_verdict", verdictArgs);
  const result = await gateway.pay("record_signed_verdict", verdictArgs,
    { ...challenge.accepts[0], network: "eip155:1" });
  assert.equal(result.isError, true);
  assert.deepEqual(gateway.events, []);
});

test("spec opaque cursor is additive and forwarded unchanged with existing paper aliases", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  const gateway = await offlineGateway(t, { mode: "disabled" });
  for (const cursor of [undefined, "v1.opaque+/=:東京", "x".repeat(1024)]) {
    const args = { paper_id: "iacr:2026/321", ...(cursor === undefined ? {} : { cursor }) };
    const result = await gateway.client.callTool({ name: "get_code_or_math_spec", arguments: args });
    assert.ok(!result.isError);
    assert.deepEqual(gateway.calls.at(-1), { name: "get_code_or_math_spec", arguments: {
      arxiv_id: args.paper_id, target_elements: "algorithm,equation,code,table", include_prose: false,
      max_chars: 12000, cursor,
    } });
  }
});

test("invalid spec cursor is rejected before payment and upstream calls", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  const gateway = await offlineGateway(t);
  for (const cursor of ["", "x".repeat(1025), null, 1, [], {}]) {
    const args = { paper_id: "paper:1", cursor };
    assert.equal(paidToolSchemas.get_code_or_math_spec.safeParse(args).success, false);
    const result = await gateway.client.callTool({ name: "get_code_or_math_spec", arguments: args });
    assert.equal(result.isError, true);
  }
  assert.deepEqual(gateway.events, []);
  assert.deepEqual(gateway.calls, []);
});

test("signed wire fields retain deployed structural rejection before payment", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  const gateway = await offlineGateway(t);
  assert.equal(paidToolSchemas.record_signed_verdict.safeParse(verdictArgs).success, true);
  for (const change of [{ signature: "" }, { signature: 1 }, { reviewer: "x".repeat(31) },
    { reviewer: "x".repeat(45) }, { issued_at: "" }, { issued_at: null },
    { evidence_sha256: "x".repeat(65) }]) {
    const args = { ...verdictArgs, judgments: [{ ...verdictArgs.judgments[0], ...change }] };
    assert.equal(paidToolSchemas.record_signed_verdict.safeParse(args).success, false);
    const result = await gateway.client.callTool({ name: "record_signed_verdict", arguments: args });
    assert.equal(result.isError, true);
  }
  assert.deepEqual(gateway.events, []);
  assert.deepEqual(gateway.calls, []);
});

test("shared signed outcome contract rejects every malformed item and preserves mixed diagnostics", () => {
  for (const fixture of batchCases) {
    const response = textResult(fixture.payload);
    if (fixture.outcome === "accepted") {
      assert.deepEqual(decodeUpstreamResult("record_signed_verdict", response), fixture.payload, fixture.name);
    } else {
      assert.throws(() => decodeUpstreamResult("record_signed_verdict", response), (error) => {
        assert.ok(error instanceof UpstreamToolError, fixture.name);
        if (fixture.payload && typeof fixture.payload === "object" && !Array.isArray(fixture.payload)) {
          for (const [key, value] of Object.entries(fixture.payload)) assert.deepEqual(error.payload[key], value, fixture.name);
          assert.match(error.message, fixture.outcome === "malformed" ? /malformed/ : /all signed/, fixture.name);
        } else {
          assert.deepEqual(error.payload.upstream_result, fixture.payload, fixture.name);
        }
        return true;
      });
    }
  }
  for (const content of [null, {}, [null, {}, { type: "text", text: 200 }]]) {
    assert.throws(() => decodeUpstreamResult("record_signed_verdict", { content }), UpstreamToolError);
  }
  assert.deepEqual(decodeUpstreamResult("get_research_bundle", textResult({ results: [] })), { results: [] });
});

test("all signed outcome fixtures retain upfront receipts, never retry, and never report malformed success", { timeout: 10000 }, async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  for (const fixture of batchCases) {
    const gateway = await offlineGateway(t, { response: textResult(fixture.payload) });
    const challenge = await gateway.challenge("record_signed_verdict", verdictArgs);
    const response = await gateway.pay("record_signed_verdict", verdictArgs, challenge.accepts.at(-1)!);
    assert.equal(Boolean(response.isError), fixture.outcome !== "accepted", fixture.name);
    assert.equal((response._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { success: boolean }).success, true, fixture.name);
    assert.deepEqual(gateway.events, ["settle", "handler:record_signed_verdict"], fixture.name);
    assert.equal(gateway.calls.length, 1, fixture.name);
    if (fixture.outcome === "accepted") assert.deepEqual(payload(response), fixture.payload, fixture.name);
    else if (fixture.payload && typeof fixture.payload === "object" && !Array.isArray(fixture.payload)) {
      for (const [key, value] of Object.entries(fixture.payload)) assert.deepEqual(payload(response)[key], value, fixture.name);
    }
  }
});
