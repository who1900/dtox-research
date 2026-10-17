import { getSignatureFromTransaction, getTransactionDecoder } from "@solana/kit";
import type { OnSettleFailureHook } from "@x402/core/server";
import type { SettleResponse } from "@x402/core/types";

const DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
export const DEVNET_NETWORK = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1";
export type SettlementCheck = { status: "pending" | "confirmed" | "failed"; receipt: SettleResponse };

/** Read-only chain proof of the exact buyer-signed transaction, never a rebroadcast. */
export async function inspectDevnetSettlement(wire: string, receipt: SettleResponse,
  fetchRpc: typeof fetch = fetch): Promise<SettlementCheck> {
  const failed = (reason: string): SettlementCheck => ({ status: "failed",
    receipt: { ...receipt, success: false, errorReason: reason } });
  if (receipt.network !== DEVNET_NETWORK) return failed("network_mismatch");
  const rpc = async (method: string, params: unknown[] = []) => {
    const response = await fetchRpc("https://api.devnet.solana.com", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }), signal: AbortSignal.timeout(2_000)
    });
    if (!response.ok) throw new Error(`Solana RPC HTTP ${response.status}`);
    const body = await response.json();
    if (body.error) throw new Error(`Solana RPC ${body.error.code}`);
    return body.result;
  };
  let original;
  try { original = getTransactionDecoder().decode(Buffer.from(wire, "base64")); }
  catch { return failed("invalid_payment_transaction"); }
  try {
    if (await rpc("getGenesisHash") !== DEVNET_GENESIS) return failed("genesis_mismatch");
    const tx = await rpc("getTransaction", [receipt.transaction, {
      encoding: "base64", commitment: "finalized", maxSupportedTransactionVersion: 0
    }]);
    if (tx) {
      if (!tx.meta || tx.meta.err !== null) return failed("transaction_failed");
      if (!Array.isArray(tx.transaction)) throw new Error("Missing transaction wire");
      const confirmed = getTransactionDecoder().decode(Buffer.from(tx.transaction[0], "base64"));
      if (getSignatureFromTransaction(confirmed) !== receipt.transaction ||
          !Buffer.from(original.messageBytes).equals(Buffer.from(confirmed.messageBytes))) return failed("transaction_mismatch");
      return { status: "confirmed", receipt: { ...receipt, success: true, errorReason: undefined, errorMessage: undefined } };
    }
    // A missing RPC transaction is not a failed payment. Only final chain evidence
    // can terminate recovery; RPC outages and indexing lag remain pending.
  } catch { /* retry read-only in the gateway's recovery loop */ }
  return { status: "pending", receipt: { ...receipt, success: false, errorReason: "settlement_pending" } };
}

export function devnetSettlementRecovery(fetchRpc: typeof fetch = fetch): OnSettleFailureHook {
  return async ({ error, requirements, paymentPayload }) => {
    const receipt = error as Error & SettleResponse;
    if (receipt.errorReason !== "settlement_pending" || requirements.scheme !== "exact" || requirements.network !== DEVNET_NETWORK ||
        receipt.network !== DEVNET_NETWORK || !receipt.transaction) return;
    const wire = paymentPayload.payload.transaction;
    if (typeof wire !== "string") return;
    const deadline = Date.now() + 8_000;
    do {
      const check = await inspectDevnetSettlement(wire, { success: false, transaction: receipt.transaction,
        network: receipt.network, payer: receipt.payer, errorReason: "settlement_pending" }, fetchRpc);
      if (check.status === "confirmed") return { recovered: true, result: check.receipt };
      if (check.status === "failed") return;
      await new Promise((resolve) => setTimeout(resolve, 500));
    } while (Date.now() < deadline);
  };
}
