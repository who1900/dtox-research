import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { GatewayConfig } from "./config.js";

type ToolContent = { type: string; text?: string };

export function createResearchUpstream(config: GatewayConfig) {
  async function callOnce(name: string, args: Record<string, unknown>): Promise<unknown> {
    const client = new Client({ name: "dtox-x402-gateway", version: "0.1.0" });
    const transport = new StreamableHTTPClientTransport(new URL(config.upstreamMcpUrl));
    try {
      await client.connect(transport);
      const response = await client.callTool({ name, arguments: args }, undefined, { timeout: 180_000 });
      const content = response.content as ToolContent[] | undefined;
      const text = content?.find((item) => item.type === "text" && item.text)?.text;
      if (!text) throw new Error(`upstream tool ${name} returned no text`);
      const parsed = JSON.parse(text) as Record<string, unknown>;
      if (response.isError || parsed.error) throw new Error(String(parsed.error ?? `upstream tool ${name} failed`));
      return parsed;
    } finally {
      await client.close().catch(() => undefined);
    }
  }

  async function call(name: string, args: Record<string, unknown>): Promise<unknown> {
    try {
      return await callOnce(name, args);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const transient = /timed?\s*out|timeout|could not reach|\b50[234]\b|connection reset/i.test(message);
      if (!transient) throw error;
      return callOnce(name, args);
    }
  }

  return {
    search: (body: Record<string, unknown>) => call("search_research_paper", body),
    spec: (body: Record<string, unknown>) => call("get_code_or_math_spec", body),
    compare: (body: Record<string, unknown>) => call("compare_methods", body),
    trends: (body: Record<string, unknown>) => call("research_trends", body),
    validate: (body: Record<string, unknown>) => call("validate_project", body),
    verdictMessage: (body: Record<string, unknown>) => call("get_verdict_message", body),
    recordSignedVerdict: (body: Record<string, unknown>) => call("record_signed_verdict", body)
  };
}
