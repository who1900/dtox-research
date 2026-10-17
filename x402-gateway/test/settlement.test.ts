import assert from "node:assert/strict";
import test from "node:test";
import { getBase58Decoder } from "@solana/kit";
import type { OnSettleFailureHook } from "@x402/core/server";
import { MCP_PAYMENT_RESPONSE_META_KEY } from "@x402/mcp";
import { devnetSettlementRecovery } from "../src/settlement.js";
import { offlineGateway, verdictArgs } from "./offline.js";

// Wire framing fixture: one signature followed by message bytes. The RPC mock
// changes the facilitator signature, while the buyer-signed message must match.
const message = [1, 0, 0, 1, ...Array(32).fill(1), ...Array(32).fill(2), 0];
const differentMessage = [...message];
differentMessage[40] = 9;
const transaction = getBase58Decoder().decode(Buffer.alloc(64, 7));
const wire = (messageBytes = message, signatureByte = 0) =>
  Buffer.concat([Buffer.from([1]), Buffer.alloc(64, signatureByte), Buffer.from(messageBytes)]).toString("base64");
const network = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1";
const context = {
  error: Object.assign(new Error("pending"), { errorReason: "settlement_pending", transaction, network }),
  requirements: { network, scheme: "exact" }, paymentPayload: { payload: { transaction: wire() } }
} as Parameters<OnSettleFailureHook>[0];

function rpc(txRecord: unknown, genesis = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG") {
  const calls: string[] = [];
  const fetchRpc = (async (_url, init) => {
    const { method, params } = JSON.parse(init!.body as string);
    calls.push(method);
    if (method === "getTransaction") assert.deepEqual(params, [transaction, {
      encoding: "base64", commitment: "finalized", maxSupportedTransactionVersion: 0
    }]);
    return Response.json({ result: method === "getGenesisHash" ? genesis : txRecord });
  }) as typeof fetch;
  return { hook: devnetSettlementRecovery(fetchRpc), calls };
}

test("pending receipt recovers only a successful confirmed identical signed message", async () => {
  const mock = rpc({ meta: { err: null }, transaction: [wire(message, 7), "base64"] });
  const result = await mock.hook(context);
  assert.equal(result?.result.success, true);
  assert.equal(result?.result.transaction, transaction);
  assert.deepEqual(mock.calls, ["getGenesisHash", "getTransaction"]);
});

test("different message, failed chain execution and wrong genesis never authorize a handler", async () => {
  for (const mock of [
    rpc({ meta: { err: null }, transaction: [wire(differentMessage, 7), "base64"] }),
    rpc({ meta: { err: null }, transaction: [wire(message, 8), "base64"] }),
    rpc({ meta: { err: { InstructionError: [0, "failed"] } }, transaction: [wire(), "base64"] }),
    rpc({ meta: { err: null }, transaction: [wire(), "base64"] }, "wrong-genesis")
  ]) assert.equal(await mock.hook(context), undefined);
});

test("non-pending failures and other networks make no RPC calls", async () => {
  const mock = rpc(null);
  assert.equal(await mock.hook({ ...context, error: new Error("failed") }), undefined);
  assert.equal(await mock.hook({ ...context, requirements: { ...context.requirements, network: "eip155:84532" } }), undefined);
  assert.deepEqual(mock.calls, []);
});

for (const write of [false, true]) test(`SDK pending recovery returns receipt and ${write ? "executes one paid write" : "retains paid read result"}`, async (t) => {
  const mock = rpc({ meta: { err: null }, transaction: [wire(message, 7), "base64"] });
  const gateway = await offlineGateway(t, { settlePending: true, settleTransaction: transaction, recoverSettlement: mock.hook });
  const name = write ? "record_signed_verdict" : "get_evidence_bundle";
  const args = write ? verdictArgs : { query: "fixture" };
  const challenge = await gateway.challenge(name, args);
  const result = await gateway.pay(name, args, challenge.accepts.at(-1)!, { transaction: wire() });
  assert.ok(!result.isError);
  assert.equal((result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY] as { success: boolean }).success, true);
  assert.equal(gateway.calls.length, 1);
  assert.deepEqual(gateway.events, write ? ["settle", "settle", "handler:record_signed_verdict"] :
    ["verify", "handler:get_research_bundle", "settle", "settle"]);
});
