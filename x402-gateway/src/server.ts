import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { HTTPFacilitatorClient, x402ResourceServer } from "@x402/core/server";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { declareDiscoveryExtension } from "@x402/extensions/bazaar";
import { createPaymentWrapper } from "@x402/mcp";
import { ExactSvmScheme } from "@x402/svm/exact/server";
import express from "express";
import { loadConfig, type GatewayConfig } from "./config.js";
import { registerTools, type PaidWrappers } from "./tools.js";
import { createResearchUpstream } from "./upstream.js";

async function createWrappers(config: GatewayConfig): Promise<PaidWrappers> {
  const empty: PaidWrappers = { evidence: null, spec: null, compare: null, trends: null, audit: null };
  if (config.mode !== "live") return empty;

  const resourceServer = new x402ResourceServer(new HTTPFacilitatorClient({ url: config.facilitatorUrl }));
  for (const network of config.evmNetworks) resourceServer.register(network, new ExactEvmScheme());
  resourceServer.register(config.svmNetwork, new ExactSvmScheme());
  await resourceServer.initialize();

  async function wrapper(
    toolName: string,
    price: string,
    description: string,
    inputSchema: Record<string, unknown>,
    example: Record<string, unknown>
  ) {
    const requirements = await Promise.all([
      ...config.evmNetworks.map((network) => resourceServer.buildPaymentRequirements({
        scheme: "exact",
        network,
        payTo: config.evmPayTo,
        price,
        extra: { name: "USDC", version: "2" }
      })),
      resourceServer.buildPaymentRequirements({
        scheme: "exact",
        network: config.svmNetwork,
        payTo: config.svmPayTo,
        price
      })
    ]);
    return createPaymentWrapper(resourceServer, {
      accepts: requirements.flat(),
      resource: {
        url: `mcp://tool/${toolName}`,
        serviceName: "dtox research",
        description,
        mimeType: "application/json",
        tags: ["research", "ai", "web3", "evidence"]
      },
      extensions: declareDiscoveryExtension({
        toolName,
        description,
        transport: "streamable-http",
        inputSchema,
        example
      })
    });
  }

  const [evidence, spec, compare, trends, audit] = await Promise.all([
    wrapper("get_evidence_bundle", config.prices.evidence,
      "Search structured full-text research evidence with sources and provenance.",
      { type: "object", properties: { query: { type: "string" }, layer: { enum: ["llm-slm", "ai-agents", "web3"] } }, required: ["query"] },
      { query: "verifiable delay functions for decentralized sequencing", layer: "web3" }),
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
      { idea: "An autonomous payment agent", claims: ["uses zero knowledge proofs to aggregate state-channel settlements"], layer: "web3" })
  ]);
  return { evidence, spec, compare, trends, audit };
}

async function createMcpServer(config: GatewayConfig, wrappers: PaidWrappers): Promise<McpServer> {
  const server = new McpServer({ name: "dtox-research-x402", version: "0.1.0" });
  registerTools(server, createResearchUpstream(config), config, wrappers);
  return server;
}

export async function start(): Promise<void> {
  const config = loadConfig();
  const wrappers = await createWrappers(config);
  const app = express();
  app.disable("x-powered-by");
  app.use(express.json({ limit: "256kb" }));

  app.get("/health", (_req, res) => {
    res.json({
      status: "ok",
      service: "dtox-research-x402",
      payment_mode: config.mode,
      networks: [...config.evmNetworks, config.svmNetwork]
    });
  });

  app.post("/mcp", async (req, res) => {
    const server = await createMcpServer(config, wrappers);
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    res.on("close", () => {
      void transport.close();
      void server.close();
    });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch (error) {
      if (!res.headersSent) res.status(500).json({ error: "MCP request failed" });
      console.error(error);
    }
  });

  app.all("/mcp", (_req, res) => res.status(405).json({ error: "Use POST for stateless MCP" }));

  app.listen(config.port, config.host, () => {
    console.log(JSON.stringify({
      event: "started",
      host: config.host,
      port: config.port,
      payment_mode: config.mode,
      networks: [...config.evmNetworks, config.svmNetwork]
    }));
  });
}

start().catch((error) => {
  console.error(error);
  process.exit(1);
});
