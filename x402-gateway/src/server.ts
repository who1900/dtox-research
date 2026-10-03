import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import express from "express";
import { loadConfig, type GatewayConfig } from "./config.js";
import { registerTools, type PaidWrappers } from "./tools.js";
import { createResearchUpstream } from "./upstream.js";
import { createWrappers } from "./payments.js";


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
