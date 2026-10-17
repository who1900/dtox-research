import { HTTPFacilitatorClient, x402ResourceServer, type FacilitatorClient, type OnSettleFailureHook } from "@x402/core/server";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { createPaymentWrapper } from "@x402/mcp";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import type { GatewayConfig } from "./config.js";
import type { PaidWrappers } from "./tools.js";
import { paidToolJsonSchema, type PaidToolName } from "./tool-schemas.js";
import { devnetSettlementRecovery } from "./settlement.js";

export async function createWrappers(config: GatewayConfig, facilitator?: FacilitatorClient,
  recoverSettlement: OnSettleFailureHook = devnetSettlementRecovery()): Promise<PaidWrappers> {
  const empty: PaidWrappers = { evidence: null, spec: null, compare: null, trends: null, audit: null, verdict: null };
  if (config.mode !== "live") return empty;
  const evmEnabled = config.evmNetworks.length > 0 && Boolean(config.evmPayTo);
  const resourceServer = new x402ResourceServer(facilitator ?? new HTTPFacilitatorClient({ url: config.facilitatorUrl }));
  if (evmEnabled) {
    for (const network of config.evmNetworks) resourceServer.register(network, new ExactEvmScheme());
  }
  resourceServer.register(config.svmNetwork, new ExactSvmScheme());
  resourceServer.onSettleFailure(recoverSettlement);
  await resourceServer.initialize();

  async function wrapper(
    toolName: PaidToolName,
    price: string,
    description: string,
    example: Record<string, unknown>
  ) {
    const paymentFlow = toolName === "record_signed_verdict" ? "upfront" : "authorization";
    const requirements = await Promise.all([
      ...(evmEnabled ? config.evmNetworks.map((network) => resourceServer.buildPaymentRequirements({
        scheme: "exact", network, payTo: config.evmPayTo, price,
        extra: { name: "USDC", version: "2", paymentFlow }
      })) : []),
      resourceServer.buildPaymentRequirements({
        scheme: "exact", network: config.svmNetwork, payTo: config.svmPayTo, price,
        extra: { paymentFlow }
      })
    ]);
    return createPaymentWrapper(resourceServer, {
      accepts: requirements.flat(),
      resource: {
        url: `mcp://tool/${toolName}`, serviceName: "dtox research", description,
        mimeType: "application/json", tags: ["research", "ai", "web3", "evidence"]
      },
      extensions: declareDiscoveryExtension({ toolName, description, transport: "streamable-http", inputSchema: paidToolJsonSchema(toolName), example })
    });
  }

  const [evidence, spec, compare, trends, audit, verdict] = await Promise.all([
    wrapper("get_evidence_bundle", config.prices.evidence,
      "A bounded research bundle with evidence, sources and provenance; also available through the free read endpoint.",
      { query: "verifiable delay functions for decentralized sequencing", layer: "web3", limit: 3, max_chars: 12000, strict: true }),
    wrapper("get_code_or_math_spec", config.prices.spec,
      "Extract equations, algorithms, code and tables from one indexed paper.",
      { paper_id: "2401.12345", target_elements: "algorithm,equation" }),
    wrapper("compare_methods", config.prices.compare,
      "Compare results, limitations and benchmark tables from two indexed papers.",
      { paper_id_a: "2401.12345", paper_id_b: "2402.12345", tables_only: true }),
    wrapper("research_trends", config.prices.trends,
      "Measure topic-normalized research trends by year.",
      { about: "zero knowledge proof systems", layer: "web3" }),
    wrapper("validate_project", config.prices.audit,
      "Audit technical claims against source-backed literature evidence.",
      { idea: "An autonomous payment agent", claims: ["uses zero knowledge proofs to aggregate state-channel settlements"], layer: "web3" }),
    wrapper("record_signed_verdict", config.prices.verdict,
      "A wallet-signed verdict attempt on Solana devnet. Payment settles before the write handler; handler failure keeps a receipt, with no automatic refund. Payment is not scientific quorum.",
      { claim: "the protocol tolerates byzantine faults under partial synchrony", judgments: [{ id: "2401.12345", verdict: "asserts", reviewer: "<base58 pubkey>", signature: "<base58 signature>", issued_at: "2026-09-26T00:00:00Z" }] })
  ]);
  return { evidence, spec, compare, trends, audit, verdict };
}
