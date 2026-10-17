/**
 * Demo agent-buyer CLI: calls a paid tool on the dtox x402 gateway over Solana devnet and
 * pays for it with x402, using an @x402/mcp client + @x402/svm client signer.
 *
 * The gateway (src/server.ts) speaks streamable-HTTP MCP at POST /mcp, so this client
 * connects with StreamableHTTPClientTransport rather than SSE.
 *
 * Usage:
 *   npx tsx scripts/demo-buyer.ts <keypair.json> <gateway-url> [tool-name] [args-json]
 *   npx tsx scripts/demo-buyer.ts <keypair.json> <gateway-url> --verdict <claim> <paper_id> <verdict> [evidence_sha256]
 *
 * Examples:
 *   npx tsx scripts/demo-buyer.ts ./buyer-keypair.json http://127.0.0.1:8012/mcp
 *   npx tsx scripts/demo-buyer.ts ./buyer-keypair.json http://127.0.0.1:8012/mcp search_research_paper \
 *     '{"query":"verifiable delay functions"}'
 *   npx tsx scripts/demo-buyer.ts ./buyer-keypair.json http://127.0.0.1:8012/mcp --verdict \
 *     "the protocol tolerates byzantine faults under partial synchrony" 2401.12345 asserts
 *
 * <keypair.json>  solana-keygen JSON keypair (64-byte array) for the buyer's own wallet.
 *                 It must hold devnet USDC to pay for tools, but needs no SOL: the default
 *                 x402.org facilitator is the fee payer for the Solana "exact" scheme (see
 *                 its /supported response, extra.feePayer), so the buyer only ever signs
 *                 the SPL transfer, never the transaction fee.
 * <gateway-url>   The gateway's MCP endpoint, e.g. http://127.0.0.1:8012/mcp.
 * [tool-name]     Which paid tool to call. Defaults to search_research_paper's gateway
 *                 equivalent, get_evidence_bundle (the closest free-form evidence search
 *                 tool the gateway actually exposes -- the upstream dtox tool is named
 *                 search_research_paper, but the gateway's own paid surface calls it
 *                 get_evidence_bundle; see src/tools.ts).
 * [args-json]     JSON object of arguments for that tool. Defaults to a demo query.
 * --verdict       Runs get_verdict_message -> sign with this same wallet -> record_signed_verdict
 *                 (paid) instead of an ordinary tool call, and prints the attestation
 *                 explorer_url on success.
 *
 * Network: this script only ever talks to a Solana devnet RPC (for building/broadcasting
 * the SPL payment via @x402/svm's client scheme, through the facilitator) and to the
 * gateway URL given on the command line. Nothing else.
 */
import { readFileSync } from "node:fs";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CallToolResultSchema } from "@modelcontextprotocol/sdk/types.js";
import { createx402MCPClient, MCP_PAYMENT_RESPONSE_META_KEY } from "@x402/mcp";
import type { SettleResponse } from "@x402/core/types";
import { ExactSvmScheme } from "@x402/svm/exact/client";
import { toClientSvmSigner } from "@x402/svm";
import { createKeyPairSignerFromBytes, getBase58Decoder, signBytes } from "@solana/kit";

const DEFAULT_SVM_NETWORK = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1";

async function loadSigner(keypairPath: string) {
  const secretKeyBytes = Uint8Array.from(JSON.parse(readFileSync(keypairPath, "utf8")) as number[]);
  return createKeyPairSignerFromBytes(secretKeyBytes);
}

async function main(): Promise<void> {
  const [keypairPath, gatewayUrl, ...rest] = process.argv.slice(2);
  if (!keypairPath || !gatewayUrl) {
    console.error(
      "usage: demo-buyer.ts <keypair.json> <gateway-url> [tool-name] [args-json]\n" +
      "       demo-buyer.ts <keypair.json> <gateway-url> --verdict <claim> <paper_id> <verdict> [evidence_sha256]"
    );
    process.exit(1);
  }

  const keyPairSigner = await loadSigner(keypairPath);
  const svmClientSigner = toClientSvmSigner(keyPairSigner);

  const x402Client = createx402MCPClient({
    name: "dtox-x402-demo-buyer",
    version: "0.1.0",
    schemes: [{ network: DEFAULT_SVM_NETWORK, client: new ExactSvmScheme(svmClientSigner) }],
    autoPayment: true,
    onPaymentRequested: async ({ paymentRequired }) => {
      const accept = paymentRequired.accepts[0];
      console.error(`payment requested: ${accept?.amount ?? "?"} atomic units on ${accept?.network ?? "?"}`);
      return true;
    }
  });

  const transport = new StreamableHTTPClientTransport(new URL(gatewayUrl));
  await x402Client.connect(transport);

  async function callAndWait(name: string, args: Record<string, unknown>) {
    // The SDK's free-call convenience method drops payment response metadata.
    // Use its existing raw transport for status, retaining the recovered receipt.
    async function status(operation_id: string): Promise<Awaited<ReturnType<typeof x402Client.callTool>>> {
      const raw = CallToolResultSchema.parse(await x402Client.client.callTool({ name: "get_payment_status", arguments: { operation_id } }));
      const body = raw.structuredContent ?? JSON.parse((raw.content.find((item) => item.type === "text") as { text: string }).text);
      const result = body.status === "completed" ? CallToolResultSchema.parse(body.result) : raw;
      return { content: result.content, isError: result.isError, paymentMade: false,
        paymentResponse: (body.payment_response ?? result._meta?.[MCP_PAYMENT_RESPONSE_META_KEY]) as SettleResponse | undefined };
    }
    let response = name === "get_payment_status" ? await status(String(args.operation_id)) : await x402Client.callTool(name, args);
    while (true) {
      const text = response.content.find((item) => item.type === "text")?.text;
      if (typeof text !== "string") throw new Error("Missing tool result text");
      const body = JSON.parse(text);
      if (!["settlement_pending", "processing"].includes(body.status) || !body.operation_id) {
        if (response.isError) process.exitCode = 1;
        return response;
      }
      console.error(`payment operation ${body.operation_id}: ${body.status}; checking automatically`);
      await new Promise((resolve) => setTimeout(resolve, 5_000));
      response = await status(body.operation_id);
    }
  }

  if (rest[0] === "--payment-status") {
    console.log(JSON.stringify(await callAndWait("get_payment_status", { operation_id: rest[1] }), null, 2));
    return;
  }

  if (rest[0] === "--verdict") {
    const [, claim, paperId, verdict, evidenceSha256] = rest;
    if (!claim || !paperId || !verdict) {
      console.error("usage: demo-buyer.ts <keypair.json> <gateway-url> --verdict <claim> <paper_id> <verdict> [evidence_sha256]");
      process.exit(1);
    }

    const messageResult = await x402Client.callTool("get_verdict_message", {
      claim,
      paper_id: paperId,
      verdict,
      ...(evidenceSha256 ? { evidence_sha256: evidenceSha256 } : {})
    });
    const messageText = (messageResult.content[0] as { text?: string }).text ?? "{}";
    const messagePayload = JSON.parse(messageText) as { message: string; issued_at: string };

    const signatureBytes = await signBytes(keyPairSigner.keyPair.privateKey, new TextEncoder().encode(messagePayload.message));
    const signature = getBase58Decoder().decode(signatureBytes);

    const recordResult = await callAndWait("record_signed_verdict", {
      claim,
      judgments: [{
        id: paperId,
        verdict,
        reviewer: keyPairSigner.address,
        signature,
        issued_at: messagePayload.issued_at,
        ...(evidenceSha256 ? { evidence_sha256: evidenceSha256 } : {})
      }]
    });

    const recordText = (recordResult.content[0] as { text?: string }).text ?? "{}";
    console.log(JSON.stringify({
      paid: recordResult.paymentResponse?.success === true,
      network: recordResult.paymentResponse?.network,
      settlement_tx: recordResult.paymentResponse?.transaction,
      result: JSON.parse(recordText)
    }, null, 2));
    return;
  }

  const toolName = rest[0] ?? "get_evidence_bundle";
  const args = rest[1] ? JSON.parse(rest[1]) : { query: "verifiable delay functions for decentralized sequencing" };

  const result = await callAndWait(toolName, args);
  const resultText = (result.content[0] as { text?: string }).text ?? "{}";
  const payload = JSON.parse(resultText);

  console.log(JSON.stringify({
    tool: toolName,
    paid: result.paymentResponse?.success === true,
    amount: result.paymentResponse?.amount,
    network: result.paymentResponse?.network,
    settlement_tx: result.paymentResponse?.transaction,
    result_preview: payload
  }, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
