import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { MCP_PAYMENT_RESPONSE_META_KEY } from "@x402/mcp";
import { PendingPayments } from "../src/pending-payments.js";
import type { SettlementCheck } from "../src/settlement.js";
import { offlineGateway, payload, verdictArgs } from "./offline.js";

function directory(t: Parameters<typeof offlineGateway>[0]) {
  const dir = mkdtempSync(join(tmpdir(), "dtox-pending-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}
const payment = { transaction: "fixture-wire" };

for (const write of [false, true]) test(`pending ${write ? "verdict" : "read"} recovers automatically, returns receipt and never repeats work`, async (t) => {
  let confirmed = false;
  const pending = new PendingPayments(directory(t), async (_wire, receipt) => ({
    status: confirmed ? "confirmed" : "pending", receipt: { ...receipt, success: confirmed }
  }));
  const g = await offlineGateway(t, { pending, settlePending: true });
  const name = write ? "record_signed_verdict" : "get_evidence_bundle";
  const args = write ? verdictArgs : { query: "fixture" };
  const challenge = await g.challenge(name, args);
  const accepted = challenge.accepts.at(-1)!;
  const [first, duplicate] = await Promise.all([g.pay(name, args, accepted, payment), g.pay(name, args, accepted, payment)]);
  assert.deepEqual(first, duplicate);
  const id = payload(first).operation_id;
  assert.equal(payload(first).status, "settlement_pending");
  assert.ok(!first.isError);
  assert.equal((first._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { transaction: string }).transaction, "fixture-payment-tx");
  assert.equal(g.calls.length, write ? 0 : 1);
  assert.equal(g.events.filter((e) => e === "settle").length, 2); // SDK's initial same-payload retry
  await pending.tick();
  assert.equal(payload(await g.client.callTool({ name: "get_payment_status", arguments: { operation_id: id } })).status, "settlement_pending");
  confirmed = true;
  await Promise.all([pending.tick(), pending.tick()]);
  const done = await g.client.callTool({ name: "get_payment_status", arguments: { operation_id: id } });
  assert.ok(!done.isError);
  assert.equal((done._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { success: boolean }).success, true);
  assert.equal(g.calls.length, 1);
  assert.equal(g.events.filter((e) => e === "settle").length, 2);
  assert.equal(payload(done).status, "completed");
  assert.equal(payload(done).payment_response.success, true);
  const replay = await g.pay(name, args, accepted, payment);
  assert.deepEqual(JSON.parse(JSON.stringify(replay)), payload(done).result); // JSON wire omits undefined optionals
  const changed = await g.pay(name, { ...args, ...(write ? { claim: "different claim" } : { query: "different query" }) }, accepted, payment);
  assert.equal(changed.isError, true);
  assert.equal(g.calls.length, 1);
});

test("restart resumes a pending verdict and another restart returns its saved response without execution", async (t) => {
  const dir = directory(t);
  const first = new PendingPayments(dir, async (_wire, receipt) => ({ status: "pending", receipt }));
  const g = await offlineGateway(t, { pending: first, settlePending: true });
  const challenge = await g.challenge("record_signed_verdict", verdictArgs);
  const started = await g.pay("record_signed_verdict", verdictArgs, challenge.accepts.at(-1)!, payment);
  const id = payload(started).operation_id;
  const inspect = async (_wire: string, receipt: SettlementCheck["receipt"]): Promise<SettlementCheck> => ({ status: "confirmed", receipt: { ...receipt, success: true } });
  const restarted = new PendingPayments(dir, inspect);
  const g2 = await offlineGateway(t, { pending: restarted, settlePending: true });
  restarted.start();
  t.after(() => restarted.close());
  for (let i = 0; i < 100 && payload(restarted.status(id)).status === "settlement_pending"; i++) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.equal(g.calls.length, 0);
  assert.equal(g2.calls.length, 1);
  assert.ok(!g2.events.includes("settle"));
  const result = restarted.status(id);
  const again = new PendingPayments(dir, inspect);
  const g3 = await offlineGateway(t, { pending: again, settlePending: true });
  await again.tick();
  assert.deepEqual(again.status(id), result);
  assert.deepEqual(g3.events, []);
});

test("crash after handler response persistence never invokes that handler on restart", async (t) => {
  const dir = directory(t);
  const pending = new PendingPayments(dir, async (_wire, receipt) => ({ status: "pending", receipt }));
  const g = await offlineGateway(t, { pending, settlePending: true });
  const c = await g.challenge("record_signed_verdict", verdictArgs);
  const started = await g.pay("record_signed_verdict", verdictArgs, c.accepts.at(-1)!, payment);
  // Crash fixture at the persisted response boundary, before final SDK decoration.
  const file = join(dir, readdirSync(dir).find((n) => n.endsWith(".json"))!);
  const entry = JSON.parse(readFileSync(file, "utf8"));
  entry.state = "executing"; entry.receipt.success = true;
  entry.handlerResult = { content: [{ type: "text", text: JSON.stringify({ results: [{ status: 200, attestation_pda: "existing-pda" }] }) }] };
  writeFileSync(file, JSON.stringify(entry));
  const restarted = new PendingPayments(dir, async () => { throw new Error("already paid; no chain lookup required"); });
  const g2 = await offlineGateway(t, { pending: restarted });
  await restarted.tick();
  assert.equal(payload(payload(restarted.status(payload(started).operation_id)).result).results[0].attestation_pda, "existing-pda");
  assert.deepEqual(g2.events, []);
});

test("definitive failed payment returns its receipt and never writes a verdict", async (t) => {
  const pending = new PendingPayments(directory(t), async (_wire, receipt) => ({ status: "failed",
    receipt: { ...receipt, success: false, errorReason: "transaction_failed" } }));
  const g = await offlineGateway(t, { pending, settlePending: true });
  const c = await g.challenge("record_signed_verdict", verdictArgs);
  const started = await g.pay("record_signed_verdict", verdictArgs, c.accepts.at(-1)!, payment);
  await pending.tick();
  const result = pending.status(payload(started).operation_id);
  assert.equal(result.isError, true);
  assert.equal((result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { errorReason: string }).errorReason, "transaction_failed");
  assert.equal(g.calls.length, 0);
});
