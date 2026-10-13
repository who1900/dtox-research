import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { GatewayConfig } from "./config.js";
import { isTransientUpstreamError, UpstreamToolError } from "./errors.js";

export { UpstreamToolError } from "./errors.js";

function signedBatchOutcome(payload: Record<string, unknown>): "accepted" | "rejected" | "malformed" {
  const results = payload.results;
  if (!Array.isArray(results) || results.length === 0) return "malformed";
  let accepted = false;
  for (const item of results) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return "malformed";
    const status: unknown = item.status;
    if (typeof status !== "number" || !Number.isInteger(status)
        || !((status >= 200 && status < 300) || (status >= 400 && status < 600))) return "malformed";
    accepted ||= status < 300;
  }
  return accepted ? "accepted" : "rejected";
}

export function decodeUpstreamResult(name: string, response: Record<string, unknown>) {
  if (!response || typeof response !== "object" || Array.isArray(response)) {
    throw new UpstreamToolError({ error: `upstream tool ${name} returned a non-object response` });
  }
  const content = Array.isArray(response.content) ? response.content : [];
  const text = content.find((item) => item && typeof item === "object" && item.type === "text"
    && typeof item.text === "string" && item.text.length > 0)?.text as string | undefined;
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
    throw new UpstreamToolError({ error: `upstream tool ${name} returned a non-object result`, upstream_result: parsed });
  }
  if (response.isError || parsed.error) {
    throw new UpstreamToolError({ ...parsed, error: parsed.error ?? `upstream tool ${name} failed` });
  }
  if (name === "record_signed_verdict") {
    const outcome = signedBatchOutcome(parsed);
    if (outcome !== "accepted") {
      throw new UpstreamToolError({ ...parsed, error: outcome === "malformed"
        ? "malformed signed verdict batch result" : "all signed verdict judgments were rejected", error_type: "protocol" });
    }
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
      if (!retryTransient || !isTransientUpstreamError(error)) throw error;
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
