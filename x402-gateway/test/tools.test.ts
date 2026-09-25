import assert from "node:assert/strict";
import test from "node:test";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { loadConfig } from "../src/config.js";
import { registerTools, type PaidWrappers } from "../src/tools.js";

type Handler = (args: Record<string, unknown>) => Promise<{ content: Array<{ type: "text"; text: string }>; isError?: boolean }>;

function fakeServer() {
  const tools = new Map<string, Handler>();
  const server = {
    tool: (name: string, _description: string, _schema: unknown, handler: Handler) => {
      tools.set(name, handler);
    }
  } as unknown as McpServer;
  return { server, tools };
}

function fakeApi(overrides: Record<string, (args: Record<string, unknown>) => Promise<unknown>> = {}) {
  const base = {
    search: async () => ({ papers: [] }),
    spec: async () => ({}),
    compare: async () => ({}),
    trends: async () => ({}),
    validate: async () => ({}),
    verdictMessage: async (args: Record<string, unknown>) => ({ message: "canonical", ...args }),
    recordSignedVerdict: async (args: Record<string, unknown>) => ({ results: [{ status: 200 }], ...args })
  };
  return { ...base, ...overrides };
}

const noWrappers: PaidWrappers = { evidence: null, spec: null, compare: null, trends: null, audit: null, verdict: null };

test("get_verdict_message is free in every mode: no payment wrapper, proxies straight to upstream", async () => {
  for (const mode of ["disabled", "shadow", "live"] as const) {
    const config = loadConfig({
      X402_MODE: mode,
      X402_SVM_PAY_TO: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
    });
    const { server, tools } = fakeServer();
    const api = fakeApi();
    registerTools(server, api as any, config, noWrappers);
    const response = await tools.get("get_verdict_message")!({
      claim: "byzantine fault tolerance",
      paper_id: "2401.12345",
      verdict: "asserts"
    });
    assert.equal(response.isError, undefined);
    const parsed = JSON.parse(response.content[0].text);
    assert.equal(parsed.message, "canonical");
    assert.equal(parsed.paper_id, "2401.12345");
    // Free tool: no payment_preview is ever attached, even in shadow mode.
    assert.equal(parsed.payment_preview, undefined);
  }
});

test("record_signed_verdict in shadow mode previews the verdict price without charging", async () => {
  const config = loadConfig({ X402_MODE: "shadow", X402_PRICE_VERDICT: "$0.02" });
  const { server, tools } = fakeServer();
  const api = fakeApi();
  registerTools(server, api as any, config, noWrappers);
  const response = await tools.get("record_signed_verdict")!({
    claim: "byzantine fault tolerance",
    judgments: [{
      id: "2401.12345",
      verdict: "asserts",
      reviewer: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG",
      signature: "sig",
      issued_at: "2026-09-26T00:00:00Z"
    }]
  });
  assert.equal(response.isError, undefined);
  const parsed = JSON.parse(response.content[0].text);
  assert.equal(parsed.payment_preview.price, "$0.02");
  assert.equal(parsed.payment_preview.charged, false);
});

test("record_signed_verdict in disabled mode calls upstream directly with no payment metadata", async () => {
  const config = loadConfig({ X402_MODE: "disabled" });
  const { server, tools } = fakeServer();
  let received: Record<string, unknown> | undefined;
  const api = fakeApi({
    recordSignedVerdict: async (args) => {
      received = args;
      return { results: [{ status: 200, attestation_pda: "fake" }] };
    }
  });
  registerTools(server, api as any, config, noWrappers);
  const response = await tools.get("record_signed_verdict")!({
    claim: "byzantine fault tolerance",
    judgments: [{ id: "2401.12345", verdict: "asserts", reviewer: "r", signature: "s", issued_at: "t" }]
  });
  assert.equal(response.isError, undefined);
  const parsed = JSON.parse(response.content[0].text);
  assert.equal(parsed.payment_preview, undefined);
  assert.equal(received?.claim, "byzantine fault tolerance");
});

test("dtox_service_info lists get_verdict_message as free and record_signed_verdict as the only paid write", async () => {
  const config = loadConfig({ X402_MODE: "disabled", X402_PRICE_VERDICT: "$0.02" });
  const { server, tools } = fakeServer();
  registerTools(server, fakeApi() as any, config, noWrappers);
  const response = await tools.get("dtox_service_info")!({});
  const parsed = JSON.parse(response.content[0].text);
  assert.ok(parsed.free_tools.includes("get_verdict_message"));
  assert.equal(parsed.paid_tools.record_signed_verdict, "$0.02");
});
