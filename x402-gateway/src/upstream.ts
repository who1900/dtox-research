import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { GatewayConfig } from "./config.js";

type ToolContent = { type: string; text?: string };

export class UpstreamToolError extends Error {
  constructor(readonly payload: Record<string, unknown>) {
    super(typeof payload.error === "string" ? payload.error : JSON.stringify(payload));
    this.name = "UpstreamToolError";
  }
}

export function decodeUpstreamResult(name: string, response: Record<string, unknown>) {
  const content = response.content as ToolContent[] | undefined;
  const text = content?.find((item) => item.type === "text" && item.text)?.text;
  let parsed: Record<string, unknown>;
  if (response.structuredContent && typeof response.structuredContent === "object") {
    parsed = response.structuredContent as Record<string, unknown>;
  } else {
    if (!text) throw new UpstreamToolError({ error: `upstream tool ${name} returned no text` });
    try {
      parsed = JSON.parse(text) as Record<string, unknown>;
    } catch {
      if (!response.isError) throw new UpstreamToolError({ error: `upstream tool ${name} returned invalid JSON` });
      try {
        parsed = JSON.parse(text.slice(text.indexOf("{"))) as Record<string, unknown>;
      } catch {
        parsed = { error: text };
      }
    }
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new UpstreamToolError({ error: `upstream tool ${name} returned a non-object result` });
  }
  if (response.isError || parsed.error) {
    throw new UpstreamToolError({ ...parsed, error: parsed.error ?? `upstream tool ${name} failed` });
  }
  const results = parsed.results;
  if (name === "record_signed_verdict" && Array.isArray(results) && results.length > 0
      && results.every((item) => item && typeof item.status === "number" && item.status >= 400)) {
    throw new UpstreamToolError({ ...parsed, error: "all signed verdict judgments were rejected" });
  }
  return parsed;
}

export function createResearchUpstream(
  config: GatewayConfig,
  clientFactory: () => Pick<Client, "connect" | "callTool" | "close"> = () => new Client({ name: "dtox-x402-gateway", version: "0.1.0" })
) {
  async function callOnce(name: string, args: Record<string, unknown>): Promise<unknown> {
    const client = clientFactory();
    const transport = new StreamableHTTPClientTransport(new URL(config.upstreamMcpUrl));
    try {
      await client.connect(transport);
      const response = await client.callTool({ name, arguments: args }, undefined, { timeout: 180_000 });
      return decodeUpstreamResult(name, response);
    } finally {
      await client.close().catch(() => undefined);
    }
  }

  async function call(name: string, args: Record<string, unknown>, retryTransient = true): Promise<unknown> {
    try {
      return await callOnce(name, args);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const transient = /timed?\s*out|timeout|could not reach|\b50[234]\b|connection reset/i.test(message);
      if (!retryTransient || !transient) throw error;
      return callOnce(name, args);
    }
  }

  return {
    search: (body: Record<string, unknown>) => call("search_research_paper", body),
    bundle: (body: Record<string, unknown>) => call("get_research_bundle", body),
    spec: (body: Record<string, unknown>) => call("get_code_or_math_spec", body),
    compare: (body: Record<string, unknown>) => call("compare_methods", body),
    trends: (body: Record<string, unknown>) => call("research_trends", body),
    validate: (body: Record<string, unknown>) => call("validate_project", body),
    verdictMessage: (body: Record<string, unknown>) => call("get_verdict_message", body, false),
    recordSignedVerdict: (body: Record<string, unknown>) => call("record_signed_verdict", body, false)
  };
}
