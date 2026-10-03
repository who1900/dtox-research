import { HTTPFacilitatorClient, x402ResourceServer, type FacilitatorClient } from "@x402/core/server";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { createPaymentWrapper } from "@x402/mcp";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import type { GatewayConfig } from "./config.js";
import type { PaidWrappers } from "./tools.js";

export async function createWrappers(config: GatewayConfig, facilitator?: FacilitatorClient): Promise<PaidWrappers> {
  const empty: PaidWrappers = { evidence: null, spec: null, compare: null, trends: null, audit: null, verdict: null };
  if (config.mode !== "live") return empty;
  const evmEnabled = config.evmNetworks.length > 0 && Boolean(config.evmPayTo);
  const resourceServer = new x402ResourceServer(facilitator ?? new HTTPFacilitatorClient({ url: config.facilitatorUrl }));
  if (evmEnabled) {
    for (const network of config.evmNetworks) resourceServer.register(network, new ExactEvmScheme());
  }
  resourceServer.register(config.svmNetwork, new ExactSvmScheme());
  await resourceServer.initialize();

  async function wrapper(
    toolName: string,
    price: string,
    description: string,
    inputSchema: Record<string, unknown>,
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
      extensions: declareDiscoveryExtension({ toolName, description, transport: "streamable-http", inputSchema, example })
    });
  }

  const [evidence, spec, compare, trends, audit, verdict] = await Promise.all([
    wrapper("get_evidence_bundle", config.prices.evidence,
      "A bounded research bundle with evidence, sources and provenance; also available through the free read endpoint.",
      { type: "object", additionalProperties: false, properties: {
        query: { type: "string", minLength: 3, maxLength: 500 },
        layer: { enum: ["llm-slm", "ai-agents", "web3", "builder-tech"] },
        limit: { type: "integer", minimum: 1, maximum: 15, default: 3 },
        max_chars: { type: "integer", minimum: 500, maximum: 30000, default: 12000 },
        strict: { type: "boolean", default: true }
      }, required: ["query"] },
      { query: "verifiable delay functions for decentralized sequencing", layer: "web3", limit: 3, max_chars: 12000, strict: true }),
    wrapper("get_code_or_math_spec", config.prices.spec,
      "Extract equations, algorithms, code and tables from one indexed paper.",
      { type: "object", properties: { paper_id: { type: "string" }, target_elements: { type: "string" } }, required: ["paper_id"] },
      { paper_id: "2401.12345", target_elements: "algorithm,equation" }),
    wrapper("compare_methods", config.prices.compare,
      "Compare results, limitations and benchmark tables from two indexed papers.",
      { type: "object", properties: { paper_id_a: { type: "string" }, paper_id_b: { type: "string" }, tables_only: { type: "boolean" } }, required: ["paper_id_a", "paper_id_b"] },
      { paper_id_a: "2401.12345", paper_id_b: "2402.12345", tables_only: true }),
    wrapper("research_trends", config.prices.trends,
      "Measure topic-normalized research trends by year.",
      { type: "object", properties: { about: { type: "string" }, layer: { enum: ["llm-slm", "ai-agents", "web3"] } }, required: ["about"] },
      { about: "zero knowledge proof systems", layer: "web3" }),
    wrapper("validate_project", config.prices.audit,
      "Audit technical claims against source-backed literature evidence.",
      { type: "object", properties: { idea: { type: "string" }, claims: { type: "array", items: { type: "string" } }, layer: { enum: ["llm-slm", "ai-agents", "web3"] } }, required: ["idea", "claims"] },
      { idea: "An autonomous payment agent", claims: ["uses zero knowledge proofs to aggregate state-channel settlements"], layer: "web3" }),
    wrapper("record_signed_verdict", config.prices.verdict,
      "A wallet-signed verdict attempt on Solana devnet. Payment settles before the write handler; handler failure keeps a receipt, with no automatic refund. Payment is not scientific quorum.",
      { type: "object", properties: { claim: { type: "string" }, judgments: { type: "array", items: { type: "object" } }, layer: { enum: ["llm-slm", "ai-agents", "web3"] } }, required: ["claim", "judgments"] },
      { claim: "the protocol tolerates byzantine faults under partial synchrony", judgments: [{ id: "2401.12345", verdict: "asserts", reviewer: "<base58 pubkey>", signature: "<base58 signature>", issued_at: "2026-09-26T00:00:00Z" }] })
  ]);
  return { evidence, spec, compare, trends, audit, verdict };
}
