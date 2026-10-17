import { getSignatureFromTransaction, getTransactionDecoder } from "@solana/kit";
import type { OnSettleFailureHook } from "@x402/core/server";

const DEVNET_GENESIS = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";
const DEVNET_NETWORK = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1";

/** Reconcile a facilitator's broadcast receipt. Never submit or recreate a payment. */
export function devnetSettlementRecovery(fetchRpc: typeof fetch = fetch): OnSettleFailureHook {
  return async ({ error, requirements, paymentPayload }) => {
    const receipt = error as Error & { errorReason?: string; transaction?: string; network?: string; payer?: string };
    if (receipt.errorReason !== "settlement_pending" || requirements.scheme !== "exact" || requirements.network !== DEVNET_NETWORK ||
        receipt.network !== DEVNET_NETWORK || !receipt.transaction) return;
    const wire = paymentPayload.payload.transaction;
    if (typeof wire !== "string") return;
    const deadline = Date.now() + 8_000;
    const rpc = async (method: string, params: unknown[] = []) => {
      const response = await fetchRpc("https://api.devnet.solana.com", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
        signal: AbortSignal.timeout(Math.max(1, Math.min(2_000, deadline - Date.now())))
      });
      if (!response.ok) throw new Error(`Solana RPC HTTP ${response.status}`);
      const body = await response.json();
      if (body.error) throw new Error(`Solana RPC ${body.error.code}`);
      return body.result;
    };
    try {
      if (await rpc("getGenesisHash") !== DEVNET_GENESIS) throw new Error("Solana RPC genesis mismatch");
      const decoder = getTransactionDecoder();
      const original = decoder.decode(Buffer.from(wire, "base64"));
      while (Date.now() < deadline) {
        const tx = await rpc("getTransaction", [receipt.transaction, {
          encoding: "base64", commitment: "confirmed", maxSupportedTransactionVersion: 0
        }]);
        if (tx) {
          if (!tx.meta || tx.meta.err !== null || !Array.isArray(tx.transaction)) return;
          const confirmed = decoder.decode(Buffer.from(tx.transaction[0], "base64"));
          if (getSignatureFromTransaction(confirmed) !== receipt.transaction) return;
          // The facilitator adds its fee-payer signature, but cannot change the
          // buyer-signed message (recipient, mint, amount, nonce and instructions).
          if (!Buffer.from(original.messageBytes).equals(Buffer.from(confirmed.messageBytes))) return;
          console.info(JSON.stringify({ event: "x402_settlement_confirmed", transaction: receipt.transaction }));
          return { recovered: true, result: { success: true, transaction: receipt.transaction,
            network: requirements.network, payer: receipt.payer } };
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
    } catch (cause) {
      console.warn(JSON.stringify({ event: "x402_settlement_rpc_error", transaction: receipt.transaction,
        error: cause instanceof Error ? cause.message : "RPC error" }));
    }
    console.warn(JSON.stringify({ event: "x402_settlement_unresolved", transaction: receipt.transaction }));
  };
}
