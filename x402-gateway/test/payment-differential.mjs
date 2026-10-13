import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { copyFileSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY } from "@x402/mcp";
import { loadConfig } from "../dist/config.js";
import { registerTools } from "../dist/tools.js";
import { createWrappers as candidateWrappers } from "../dist/payments.js";
import { createResearchUpstream } from "../dist/upstream.js";

const inputs = {
  get_evidence_bundle: { query: "fixture" },
  get_code_or_math_spec: { paper_id: "paper:1" },
  compare_methods: { paper_id_a: "paper:1", paper_id_b: "paper:2" },
  research_trends: { about: "fixture" },
  validate_project: { idea: "fixture project", claims: ["fixture claim"] },
  record_signed_verdict: { claim: "fixture claim", judgments: [{ id: "paper:1", verdict: "asserts",
    reviewer: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG", signature: "1".repeat(64), issued_at: "2026-10-06T00:00:00Z" }] },
};
const hash = file => createHash("sha256").update(readFileSync(file)).digest("hex");
const text = result => JSON.parse(result.content.find(item => item.type === "text").text);

function paymentSemantics(response) {
  const normalized = structuredClone(response);
  for (const item of normalized.content ?? []) {
    if (item.type === "text") {
      try { item.text = JSON.parse(item.text); } catch {}
    }
  }
  // SDK failure responses repeat the intentionally corrected discovery schema.
  for (const body of [normalized.structuredContent, ...(normalized.content ?? []).map(item => item.text)]) {
    const input = body?.extensions?.bazaar?.info?.input;
    if (input) delete input.inputSchema;
  }
  return normalized;
}

async function gateway(factory, config, scenario = "success") {
  const events = [];
  const calls = [];
  // Same injected facilitator contract and synthetic identities as test/offline.ts.
  const facilitator = {
    getSupported: async () => ({ kinds: [...config.evmNetworks, config.svmNetwork].map(network => ({
      x402Version: 2, scheme: "exact", network, extra: { feePayer: config.svmPayTo },
    })), extensions: [], signers: {} }),
    verify: async () => { events.push("verify"); return { isValid: scenario !== "verify-failure", invalidReason: scenario === "verify-failure" ? "fixture_verify_failed" : undefined }; },
    settle: async (_payment, requirements) => {
      events.push("settle");
      return { success: scenario !== "settle-failure", transaction: "fixture-payment-tx", network: requirements.network,
        errorReason: scenario === "settle-failure" ? "fixture_settlement_failed" : undefined };
    },
  };
  const wrappers = await factory(config, facilitator);
  const api = createResearchUpstream(config, () => ({
    connect: async () => {}, close: async () => {}, callTool: async request => {
      events.push(`handler:${request.name}`);
      calls.push(request);
      if (scenario === "timeout") throw new Error("request timed out");
      const body = scenario === "handler-failure" ? { error: "fixture handler failure", status: 400 }
        : scenario === "rejected-batch" ? { results: [{ status: 400, error: "invalid signature" }] }
        : scenario === "mixed-batch" ? { results: [{ status: 200, attestation_tx: "fixture" }, { status: 400, error: "invalid" }] }
        : { results: [{ status: 200 }] };
      return { content: [{ type: "text", text: JSON.stringify(body) }], isError: scenario === "handler-failure" };
    },
  }));
  const server = new McpServer({ name: "differential-gateway", version: "fixture" });
  const client = new Client({ name: "differential-client", version: "fixture" });
  registerTools(server, api, config, wrappers);
  const [left, right] = InMemoryTransport.createLinkedPair();
  await Promise.all([client.connect(left), server.connect(right)]);
  return { client, events, calls,
    challenge: async name => text(await client.callTool({ name, arguments: inputs[name] })),
    pay: (name, accepted) => client.callTool({ name, arguments: inputs[name],
      _meta: { [MCP_PAYMENT_META_KEY]: { x402Version: 2, accepted, payload: { fixture: true } } } }),
    close: async () => { await client.close(); await server.close(); },
  };
}

async function paired(beforeFactory, config, scenario, inspect) {
  const before = await gateway(beforeFactory, config, scenario);
  let candidate;
  try { candidate = await gateway(candidateWrappers, config, scenario); await inspect(before, candidate); }
  finally { await before.close(); if (candidate) await candidate.close(); }
}

export async function paymentDifferential(beforePath) {
  assert.ok(beforePath, "--before must name the actual private compiled payments.js; no fixture fallback");
  const testRoot = path.resolve(fileURLToPath(new URL("./", import.meta.url)));
  const temporary = mkdtempSync(path.join(testRoot, ".payment-differential-"));
  const tracked = [beforePath, ...["payments.js", "tools.js", "tool-schemas.js", "config.js", "server.js", "upstream.js", "errors.js"]
    .map(name => fileURLToPath(new URL(`../dist/${name}`, import.meta.url)))];
  const initial = Object.fromEntries(tracked.map(file => [file, hash(file)]));
  const originalFetch = globalThis.fetch;
  let networkAttempts = 0;
  globalThis.fetch = async () => { networkAttempts++; throw new Error("fetch forbidden in payment differential proof"); };
  try {
    const beforeCopy = path.join(temporary, "actual-before.mjs");
    copyFileSync(beforePath, beforeCopy);
    assert.equal(hash(beforeCopy), initial[beforePath], "private before must be copied byte-for-byte, never rewritten");
    const { createWrappers: beforeWrappers } = await import(pathToFileURL(beforeCopy).href);
    const reports = [];
    let requirementChecks = 0;
    let discoveryChecks = 0;
    let flowChecks = 0;
    for (const profile of ["svm-only", "evm-and-svm"]) {
      const config = loadConfig({ X402_MODE: "live", X402_EVM_NETWORKS: profile === "svm-only" ? "" : "eip155:84532",
        X402_EVM_PAY_TO: "0x1234567890123456789012345678901234567890",
        X402_SVM_PAY_TO: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG" });
      const requirements = {};
      await paired(beforeWrappers, config, "success", async (before, candidate) => {
        const catalog = await candidate.client.listTools();
        assert.equal(catalog.tools.length, 9);
        for (const name of Object.keys(inputs)) {
          const old = await before.challenge(name);
          const fresh = await candidate.challenge(name);
          assert.deepEqual(fresh.accepts, old.accepts, `${profile}/${name}: FULL payment requirements changed`);
          assert.equal(fresh.accepts.length, profile === "svm-only" ? 1 : 2);
          for (const requirement of fresh.accepts) {
            for (const field of ["scheme", "amount", "asset", "network", "payTo", "maxTimeoutSeconds", "extra"]) {
              assert.ok(Object.hasOwn(requirement, field), `${name}: missing ${field}`);
            }
            assert.equal(requirement.extra.paymentFlow, name === "record_signed_verdict" ? "upfront" : "authorization");
          }
          const oldRest = structuredClone(old);
          const freshRest = structuredClone(fresh);
          delete oldRest.extensions.bazaar.info.input.inputSchema;
          delete freshRest.extensions.bazaar.info.input.inputSchema;
          assert.deepEqual(freshRest, oldRest, `${name}: challenge changed outside discovery schema`);
          assert.deepEqual(fresh.extensions.bazaar.info.input.inputSchema,
            catalog.tools.find(tool => tool.name === name).inputSchema, `${name}: Bazaar != actual compiled tools/list`);
          requirements[name] = fresh.accepts;
          requirementChecks++;
          discoveryChecks++;
        }
        assert.deepEqual(before.events, []);
        assert.deepEqual(candidate.events, []);
      });
      for (const name of Object.keys(inputs)) {
        const signed = name === "record_signed_verdict";
        const upstreamName = name === "get_evidence_bundle" ? "get_research_bundle" : name;
        const scenarios = signed ? ["success", "handler-failure", "timeout", "rejected-batch", "mixed-batch", "settle-failure"]
          : ["success", "handler-failure", "settle-failure", "verify-failure"];
        for (const accepted of requirements[name]) {
          for (const scenario of scenarios) {
            await paired(beforeWrappers, config, scenario, async (before, candidate) => {
              const old = await before.pay(name, accepted);
              const fresh = await candidate.pay(name, accepted);
              assert.deepEqual(paymentSemantics(fresh), paymentSemantics(old), `${profile}/${name}/${scenario}: result/receipt changed outside discovery`);
              assert.deepEqual(candidate.events, before.events, `${name}/${scenario}: payment flow changed`);
              assert.deepEqual(candidate.calls, before.calls, `${name}/${scenario}: calls changed`);
              const handler = `handler:${upstreamName}`;
              const expected = signed ? (scenario === "settle-failure" ? ["settle"] : ["settle", handler])
                : scenario === "verify-failure" ? ["verify"]
                : scenario === "handler-failure" ? ["verify", handler] : ["verify", handler, "settle"];
              assert.deepEqual(candidate.events, expected, `${name}/${scenario}: wrong semantic ordering`);
              assert.equal(candidate.calls.length, expected.includes(handler) ? 1 : 0, `${name}/${scenario}: retry or unexpected handler`);
              assert.equal(Boolean(fresh.isError), !["success", "mixed-batch"].includes(scenario));
              const receipt = fresh._meta?.[MCP_PAYMENT_RESPONSE_META_KEY];
              if (signed && scenario !== "settle-failure") {
                assert.equal(receipt.success, true, `${scenario}: failed write lost upfront receipt`);
                assert.equal(receipt.transaction, "fixture-payment-tx");
              }
              if (!signed && ["handler-failure", "verify-failure"].includes(scenario)) assert.equal(receipt, undefined);
              flowChecks++;
            });
          }
        }
      }
      reports.push({ profile, tools: 6, networks: [...config.evmNetworks, config.svmNetwork] });
    }
    assert.equal(networkAttempts, 0);
    assert.deepEqual(Object.fromEntries(tracked.map(file => [file, hash(file)])), initial, "private before or current compiled product mutated");
    return { status: "PASS", sdk: "same installed gateway SDK for both modules", before_sha256: initial[beforePath],
      candidate_sha256: initial[tracked[1]], requirementChecks, discoveryChecks, flowChecks, networkAttempts,
      profiles: reports, limitation: "offline SDK with injected facilitator; no wallets, network, real payments or deployment" };
  } finally {
    globalThis.fetch = originalFetch;
    assert.equal(path.dirname(temporary), testRoot);
    assert.ok(path.basename(temporary).startsWith(".payment-differential-"));
    rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  try {
    const index = process.argv.indexOf("--before");
    assert.ok(index !== -1 && process.argv[index + 1], "usage: node test/payment-differential.mjs --before /absolute/private/dist/payments.js");
    console.log(JSON.stringify(await paymentDifferential(path.resolve(process.argv[index + 1])), null, 2));
  } catch (error) { console.error(error.stack); process.exitCode = 1; }
}
