import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { createKeyPairSignerFromBytes, getBase58Decoder, signBytes } from "@solana/kit";
import { MCP_PAYMENT_META_KEY, MCP_PAYMENT_RESPONSE_META_KEY, createx402MCPClient } from "@x402/mcp";
import type { PaymentRequired } from "@x402/core/types";
import { toClientSvmSigner } from "@x402/svm";
import { ExactSvmScheme } from "@x402/svm/exact/client";

export const RELEASE_CLAIM = "Release smoke 2026-10-03: ERC-4337 prevents every possible denial-of-service attack";
export const RELEASE_PAPER_ID = "eip:4337";
export const RELEASE_VERDICT = "does_not_assert";
export const DEVNET_NETWORK = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1";
export const DEVNET_USDC_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
export const RELEASE_PAY_TO = "39ayJ2S5h6VpzxWtAGkKFHTSiHMYt73oJNvpAkTp8Qc8";
export const MAX_PAYMENT_ATOMIC = 10_000n;

export interface ReleaseArgs {
  walletPath: string;
  gatewayUrl: URL;
  claim: string;
  paperId: string;
  verdict: typeof RELEASE_VERDICT;
  evidenceSha256: string;
}

interface CanonicalMessage {
  message: string;
  claim_id: string;
  claim_sha256: string;
  paper_id: string;
  verdict: string;
  evidence_sha256: string;
  issued_at: string;
}

export function parseReleaseArgs(argv: string[]): ReleaseArgs {
  if (argv.length !== 6) throw new Error("invalid arguments");
  const [walletPath, rawUrl, claim, paperId, verdict, evidenceSha256] = argv;
  if (!walletPath || !claim || !paperId || !evidenceSha256) throw new Error("missing argument");
  if (claim !== RELEASE_CLAIM || paperId !== RELEASE_PAPER_ID || verdict !== RELEASE_VERDICT) {
    throw new Error("release case mismatch");
  }
  if (!/^[0-9a-f]{64}$/.test(evidenceSha256)) throw new Error("invalid evidence hash");

  let gatewayUrl: URL;
  try {
    gatewayUrl = new URL(rawUrl);
  } catch {
    throw new Error("invalid gateway URL");
  }
  const productionEndpoint = gatewayUrl.protocol === "https:" && gatewayUrl.hostname === "read.whoim.space"
    && gatewayUrl.pathname === "/x402/mcp";
  if (!productionEndpoint || gatewayUrl.username || gatewayUrl.password || gatewayUrl.search || gatewayUrl.hash) {
    throw new Error("gateway URL is outside the release allowlist");
  }
  return { walletPath, gatewayUrl, claim, paperId, verdict: RELEASE_VERDICT, evidenceSha256 };
}

export function validatePaymentRequired(value: unknown): PaymentRequired {
  if (!value || typeof value !== "object") throw new Error("missing payment challenge");
  const challenge = value as Partial<PaymentRequired>;
  if (challenge.x402Version !== 2 || !Array.isArray(challenge.accepts) || challenge.accepts.length !== 1) {
    throw new Error("challenge must contain exactly one x402 v2 option");
  }
  const accepted = challenge.accepts[0];
  if (!accepted || accepted.scheme !== "exact" || accepted.network !== DEVNET_NETWORK
      || accepted.asset !== DEVNET_USDC_MINT || accepted.payTo !== RELEASE_PAY_TO
      || typeof accepted.amount !== "string" || !/^\d+$/.test(accepted.amount)) {
    throw new Error("challenge does not match the approved devnet payment");
  }
  const amount = BigInt(accepted.amount);
  if (amount <= 0n || amount > MAX_PAYMENT_ATOMIC) throw new Error("challenge amount exceeds the release cap");
  return challenge as PaymentRequired;
}

export function validateServiceInfo(value: unknown): void {
  if (!value || typeof value !== "object") throw new Error("invalid gateway discovery response");
  const info = value as { service?: unknown; mode?: unknown; networks?: unknown };
  if (info.service !== "dtox research" || info.mode !== "live"
      || !Array.isArray(info.networks) || info.networks.length !== 1 || info.networks[0] !== DEVNET_NETWORK) {
    throw new Error("gateway discovery is not live Solana devnet-only");
  }
}

function normalizedClaimHash(claim: string): string {
  const normalized = claim.toLowerCase().trim().replace(/\s+/g, " ");
  return createHash("sha256").update(normalized, "utf8").digest("hex");
}

export function validateCanonicalMessage(value: unknown, args: ReleaseArgs, now = Date.now()): CanonicalMessage {
  if (!value || typeof value !== "object") throw new Error("invalid message response");
  const payload = value as Partial<CanonicalMessage>;
  if (typeof payload.message !== "string" || typeof payload.claim_id !== "string" || !payload.claim_id
      || payload.claim_sha256 !== normalizedClaimHash(args.claim) || payload.paper_id !== args.paperId
      || payload.verdict !== args.verdict || payload.evidence_sha256 !== args.evidenceSha256
      || typeof payload.issued_at !== "string") {
    throw new Error("message fields do not match the approved release case");
  }
  const timestamp = Date.parse(payload.issued_at);
  if (!Number.isFinite(timestamp) || now - timestamp > 10 * 60_000 || timestamp - now > 60_000) {
    throw new Error("message timestamp is outside the signing window");
  }
  const expected = [
    "dtox-claim-verdict:v1",
    `claim_id:${payload.claim_id}`,
    `claim_sha256:${payload.claim_sha256}`,
    `paper_id:${payload.paper_id}`,
    `verdict:${payload.verdict}`,
    `evidence_sha256:${payload.evidence_sha256}`,
    `issued_at:${payload.issued_at}`
  ].join("\n");
  if (payload.message !== expected) throw new Error("canonical message layout mismatch");
  return payload as CanonicalMessage;
}

function resultPayload(value: unknown): unknown {
  if (!value || typeof value !== "object") throw new Error("invalid tool response");
  const result = value as { structuredContent?: unknown; content?: Array<{ type: string; text?: string }> };
  if (result.structuredContent && typeof result.structuredContent === "object") return result.structuredContent;
  const text = result.content?.find((item) => item.type === "text")?.text;
  if (!text) throw new Error("missing tool response");
  return JSON.parse(text) as unknown;
}

function safeReceipt(value: unknown): { success?: boolean; network?: string; transaction?: string } | null {
  if (!value || typeof value !== "object") return null;
  const receipt = value as Record<string, unknown>;
  return {
    ...(typeof receipt.success === "boolean" ? { success: receipt.success } : {}),
    ...(typeof receipt.network === "string" ? { network: receipt.network } : {}),
    ...(typeof receipt.transaction === "string" ? { transaction: receipt.transaction } : {})
  };
}

async function loadSigner(walletPath: string) {
  const parsed: unknown = JSON.parse(readFileSync(walletPath, "utf8"));
  if (!Array.isArray(parsed) || parsed.length !== 64
      || parsed.some((item) => !Number.isInteger(item) || item < 0 || item > 255)) {
    throw new Error("wallet file must be a 64-byte solana-keygen array");
  }
  const secretBytes = Uint8Array.from(parsed as number[]);
  try {
    return await createKeyPairSignerFromBytes(secretBytes);
  } finally {
    secretBytes.fill(0);
    parsed.fill(0);
  }
}

async function main(): Promise<void> {
  let args: ReleaseArgs;
  try {
    args = parseReleaseArgs(process.argv.slice(2));
  } catch {
    console.error("usage: release-devnet.ts <wallet-path> <gateway-url> <claim> <paper> <verdict> <evidence-sha256>");
    process.exitCode = 2;
    return;
  }

  let client: ReturnType<typeof createx402MCPClient> | undefined;
  try {
    const signer = await loadSigner(args.walletPath);
    const svmSigner = toClientSvmSigner(signer);
    client = createx402MCPClient({
      name: "dtox-release-devnet",
      version: "0.1.0",
      schemes: [{ network: DEVNET_NETWORK, client: new ExactSvmScheme(svmSigner) }],
      autoPayment: false
    });
    await client.connect(new StreamableHTTPClientTransport(args.gatewayUrl));

    const discoveryResult = await client.callTool("dtox_service_info", {});
    if (discoveryResult.isError) throw new Error("gateway discovery failed");
    validateServiceInfo(resultPayload(discoveryResult));

    const messageResult = await client.callTool("get_verdict_message", {
      claim: args.claim,
      paper_id: args.paperId,
      verdict: args.verdict,
      evidence_sha256: args.evidenceSha256
    });
    if (messageResult.isError) throw new Error("message preparation failed");
    const message = validateCanonicalMessage(resultPayload(messageResult), args);

    const writeArgs = {
      claim: args.claim,
      judgments: [{
        id: args.paperId,
        verdict: args.verdict,
        reviewer: signer.address,
        signature: getBase58Decoder().decode(await signBytes(
          signer.keyPair.privateKey,
          new TextEncoder().encode(message.message)
        )),
        issued_at: message.issued_at,
        evidence_sha256: args.evidenceSha256
      }]
    };

    const paymentRequired = validatePaymentRequired(
      await client.getToolPaymentRequirements("record_signed_verdict", writeArgs)
    );
    const paymentPayload = await client.paymentClient.createPaymentPayload(paymentRequired);
    if (paymentPayload.accepted.network !== DEVNET_NETWORK || paymentPayload.accepted.asset !== DEVNET_USDC_MINT
        || paymentPayload.accepted.payTo !== RELEASE_PAY_TO
        || BigInt(paymentPayload.accepted.amount) <= 0n
        || BigInt(paymentPayload.accepted.amount) > MAX_PAYMENT_ATOMIC) {
      throw new Error("created payment does not match the approved devnet cap");
    }

    // Bypass x402MCPClient.callToolWithPayment recovery: this is exactly one paid transport call.
    const paidResult = await client.client.callTool({
      name: "record_signed_verdict",
      arguments: writeArgs,
      _meta: { [MCP_PAYMENT_META_KEY]: paymentPayload }
    }, undefined, { timeout: 60_000 });
    const receipt = safeReceipt(paidResult._meta?.[MCP_PAYMENT_RESPONSE_META_KEY]);
    const output: Record<string, unknown> = {
      sdkIsError: Boolean(paidResult.isError),
      paymentReceipt: receipt,
      reviewer: signer.address,
      publicMessage: {
        claim_id: message.claim_id,
        claim_sha256: message.claim_sha256,
        paper_id: message.paper_id,
        verdict: message.verdict,
        evidence_sha256: message.evidence_sha256,
        issued_at: message.issued_at
      }
    };

    if (paidResult.isError || receipt?.success !== true || receipt.network !== DEVNET_NETWORK || !receipt.transaction) {
      console.log(JSON.stringify(output, null, 2));
      process.exitCode = 1;
      return;
    }

    try {
      const payload = resultPayload(paidResult) as Record<string, unknown>;
      const results = payload.results;
      const papers = payload.papers as Record<string, unknown> | undefined;
      const paper = papers?.[args.paperId] as Record<string, unknown> | undefined;
      const onchain = paper?.onchain;
      const item = Array.isArray(results) && results[0] && typeof results[0] === "object"
        ? results[0] as Record<string, unknown> : undefined;
      if (item) {
        output.writeResult = {
          id: item.id,
          status: item.status,
          attestation_pda: item.attestation_pda,
          attestation_tx: item.attestation_tx,
          explorer_url: item.explorer_url
        };
      }
      if (!Array.isArray(results) || results.length !== 1 || !results[0] || typeof results[0] !== "object") {
        throw new Error("unexpected result batch");
      }
      if (!item) throw new Error("missing result item");
      if (item.id !== args.paperId || item.status !== 200 || typeof item.attestation_pda !== "string"
          || typeof item.attestation_tx !== "string" || !item.attestation_tx
          || typeof item.explorer_url !== "string" || !Array.isArray(onchain)) {
        throw new Error("attestation result incomplete");
      }
      const publicReadback = onchain.find((entry) => entry && typeof entry === "object"
        && (entry as Record<string, unknown>).reviewer === signer.address
        && (entry as Record<string, unknown>).verdict === args.verdict
        && (entry as Record<string, unknown>).attestation === item.attestation_pda) as Record<string, unknown> | undefined;
      if (!publicReadback) throw new Error("public on-chain summary did not match the write");

      output.record = {
        paper_id: item.id,
        status: item.status,
        attestation_pda: item.attestation_pda,
        attestation_tx: item.attestation_tx,
        explorer_url: item.explorer_url,
        public_readback: {
          reviewer: publicReadback.reviewer,
          verdict: publicReadback.verdict,
          attestation: publicReadback.attestation,
          explorer_url: publicReadback.explorer_url
        },
        registry_status: paper?.status,
        registry_readers: paper?.readers,
        registry_counts: paper?.counts
      };
    } catch {
      output.readback = "failed; paid response retained; inspect before any further action; no retry was attempted";
      console.log(JSON.stringify(output, null, 2));
      process.exitCode = 1;
      return;
    }
    console.log(JSON.stringify(output, null, 2));
  } catch {
    console.error("release devnet check stopped; no automatic retry was attempted");
    process.exitCode = 1;
  } finally {
    if (client) await client.close().catch(() => undefined);
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  void main();
}
