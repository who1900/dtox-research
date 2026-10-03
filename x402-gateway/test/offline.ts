import type { TestContext } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import type { FacilitatorClient } from "@x402/core/server";
import type { PaymentRequired, PaymentRequirements } from "@x402/core/types";
import { MCP_PAYMENT_META_KEY } from "@x402/mcp";
import { loadConfig } from "../src/config.js";
import { createWrappers } from "../src/payments.js";
import { registerTools } from "../src/tools.js";
import { createResearchUpstream } from "../src/upstream.js";

export const verdictArgs = {
  claim: "fixture claim", judgments: [{ id: "paper:1", verdict: "asserts",
    reviewer: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG", signature: "fixture", issued_at: "fixture" }]
};

export function textResult(payload: Record<string, unknown>, isError = false): CallToolResult {
  return { content: [{ type: "text", text: JSON.stringify(payload) }], isError };
}

export function payload(result: { content?: unknown }) {
  const content = result.content as Array<{ type: string; text?: string }>;
  return JSON.parse(content.find((item) => item.type === "text")!.text!);
}

export async function offlineGateway(t: TestContext, options: {
  mode?: "live" | "disabled";
  settleSuccess?: boolean;
  response?: CallToolResult;
  upstreamFailure?: Error;
} = {}) {
  const config = loadConfig({ X402_MODE: options.mode ?? "live",
    X402_EVM_NETWORKS: "eip155:84532",
    X402_EVM_PAY_TO: "0x1234567890123456789012345678901234567890",
    X402_SVM_PAY_TO: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG" });
  const events: string[] = [];
  const calls: Array<{ name: string; arguments?: Record<string, unknown> }> = [];
  const facilitator: FacilitatorClient = {
    getSupported: async () => ({ kinds: [...config.evmNetworks, config.svmNetwork].map((network) => ({
      x402Version: 2, scheme: "exact", network, extra: { feePayer: config.svmPayTo }
    })), extensions: [], signers: {} }),
    verify: async () => { events.push("verify"); return { isValid: true }; },
    settle: async (_payment, requirements) => {
      events.push("settle");
      return { success: options.settleSuccess ?? true, transaction: "fixture-payment-tx",
        network: requirements.network, errorReason: options.settleSuccess === false ? "fixture_settlement_failed" : undefined };
    }
  };
  const wrappers = await createWrappers(config, facilitator);
  const api = createResearchUpstream(config, () => ({
    connect: async () => {}, close: async () => {},
    callTool: async (request: { name: string; arguments?: Record<string, unknown> }) => {
      events.push(`handler:${request.name}`);
      calls.push(request);
      if (options.upstreamFailure) throw options.upstreamFailure;
      return options.response ?? textResult({ results: [{ status: 200 }] });
    }
  } as unknown as Pick<Client, "connect" | "callTool" | "close">));
  const server = new McpServer({ name: "offline-gateway", version: "fixture" });
  registerTools(server, api, config, wrappers);
  const client = new Client({ name: "offline-client", version: "fixture" });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
  t.after(async () => { await client.close(); await server.close(); });
  const challenge = async (name: string, args: Record<string, unknown>) => {
    const result = await client.callTool({ name, arguments: args });
    return payload(result) as PaymentRequired;
  };
  const pay = async (name: string, args: Record<string, unknown>, accepted: PaymentRequirements) => {
    return client.callTool({ name, arguments: args,
      _meta: { [MCP_PAYMENT_META_KEY]: { x402Version: 2, accepted, payload: { fixture: true } } } });
  };
  return { config, client, events, calls, challenge, pay };
}
