import assert from "node:assert/strict";
import test from "node:test";
import { MCP_PAYMENT_RESPONSE_META_KEY } from "@x402/mcp";
import { offlineGateway, payload, textResult, verdictArgs } from "./offline.js";

test("real SDK advertises upfront only for signed writes across configured SVM and EVM", async (t) => {
  const gateway = await offlineGateway(t);
  const write = await gateway.challenge("record_signed_verdict", verdictArgs);
  assert.equal(write.accepts.length, 2);
  assert.ok(write.accepts.every((requirement) => requirement.extra.paymentFlow === "upfront"));
  const readArgs = {
    get_evidence_bundle: { query: "fixture" }, get_code_or_math_spec: { paper_id: "paper:1" },
    compare_methods: { paper_id_a: "paper:1", paper_id_b: "paper:2" }, research_trends: { about: "fixture" },
    validate_project: { idea: "fixture project", claims: ["fixture claim"] }
  };
  for (const [name, args] of Object.entries(readArgs)) {
    const read = await gateway.challenge(name, args);
    assert.ok(read.accepts.every((requirement) => (requirement.extra.paymentFlow ?? "authorization") === "authorization"));
  }
  assert.deepEqual(gateway.events, []);
});

test("failed upfront settlement results in zero write handler calls", async (t) => {
  const gateway = await offlineGateway(t, { settleSuccess: false });
  const challenge = await gateway.challenge("record_signed_verdict", verdictArgs);
  const result = await gateway.pay("record_signed_verdict", verdictArgs, challenge.accepts.at(-1)!);
  assert.equal(result.isError, true);
  assert.match(payload(result).error, /settlement failed/i);
  assert.deepEqual(gateway.events, ["settle"]);
  assert.equal(gateway.calls.length, 0);
});

test("successful real SDK upfront settlement precedes the write on SVM and EVM", async (t) => {
  const gateway = await offlineGateway(t);
  const challenge = await gateway.challenge("record_signed_verdict", verdictArgs);
  for (const accepted of [challenge.accepts[0], challenge.accepts.at(-1)!]) {
    gateway.events.length = 0;
    const result = await gateway.pay("record_signed_verdict", verdictArgs, accepted);
    assert.ok(!result.isError);
    assert.deepEqual(gateway.events, ["settle", "handler:record_signed_verdict"]);
    assert.equal((result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { transaction: string }).transaction, "fixture-payment-tx");
  }
});

test("all rejected signed items are an error with paid attempt receipt and full diagnostics", async (t) => {
  const body = { results: [{ status: 400, error: "invalid signature" }, { status: 429, retry_after: 12 }], onchain: [] };
  const gateway = await offlineGateway(t, { response: textResult(body) });
  const challenge = await gateway.challenge("record_signed_verdict", verdictArgs);
  const result = await gateway.pay("record_signed_verdict", verdictArgs, challenge.accepts.at(-1)!);
  assert.equal(result.isError, true);
  assert.deepEqual(payload(result).results, body.results);
  assert.equal((result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { success: boolean }).success, true);
  assert.deepEqual(gateway.events, ["settle", "handler:record_signed_verdict"]);
});

test("partial signed batch preserves every status and its successful payment receipt", async (t) => {
  const body = { results: [{ status: 200, attestation_tx: "fixture" }, { status: 400, error: "invalid" }] };
  const gateway = await offlineGateway(t, { response: textResult(body) });
  const challenge = await gateway.challenge("record_signed_verdict", verdictArgs);
  const result = await gateway.pay("record_signed_verdict", verdictArgs, challenge.accepts[0]);
  assert.ok(!result.isError);
  assert.deepEqual(payload(result), body);
  assert.equal((result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { success: boolean }).success, true);
  assert.deepEqual(gateway.events, ["settle", "handler:record_signed_verdict"]);
});

test("failed authorization read handler never settles", async (t) => {
  const gateway = await offlineGateway(t, { response: textResult({ error: "fixture read failure", status: 400 }, true) });
  const args = { query: "fixture" };
  const challenge = await gateway.challenge("get_evidence_bundle", args);
  const result = await gateway.pay("get_evidence_bundle", args, challenge.accepts.at(-1)!);
  assert.equal(result.isError, true);
  assert.equal(result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY], undefined);
  assert.deepEqual(gateway.events, ["verify", "handler:get_research_bundle"]);
});

test("successful authorization read settles only after its bundle handler", async (t) => {
  const gateway = await offlineGateway(t, { response: textResult({ evidence: ["fixture"] }) });
  const args = { query: "fixture" };
  const challenge = await gateway.challenge("get_evidence_bundle", args);
  const result = await gateway.pay("get_evidence_bundle", args, challenge.accepts.at(-1)!);
  assert.ok(!result.isError);
  assert.deepEqual(gateway.events, ["verify", "handler:get_research_bundle", "settle"]);
});

test("write timeout is not retried and retains its upfront receipt", async (t) => {
  const gateway = await offlineGateway(t, { upstreamFailure: new Error("request timed out") });
  const challenge = await gateway.challenge("record_signed_verdict", verdictArgs);
  const result = await gateway.pay("record_signed_verdict", verdictArgs, challenge.accepts.at(-1)!);
  assert.equal(result.isError, true);
  assert.equal(gateway.calls.length, 1);
  assert.equal((result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { success: boolean }).success, true);
  assert.deepEqual(gateway.events, ["settle", "handler:record_signed_verdict"]);
});
