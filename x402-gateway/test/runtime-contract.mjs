import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";

export async function verifyRuntimeContract(output, beforePath) {
  const before = JSON.parse(readFileSync(beforePath, "utf8"));
  const { registerTools } = await import(pathToFileURL(path.join(output, "tools.js")).href);
  const { loadConfig } = await import(pathToFileURL(path.join(output, "config.js")).href);
  const info = before.service_info;
  const names = { evidence: "get_evidence_bundle", spec: "get_code_or_math_spec", compare: "compare_methods",
    trends: "research_trends", audit: "validate_project", verdict: "record_signed_verdict" };
  const config = { ...loadConfig({}), mode: info.mode,
    evmNetworks: info.networks.filter(network => network.startsWith("eip155:")),
    svmNetwork: info.networks.find(network => network.startsWith("solana:")),
    prices: Object.fromEntries(Object.entries(names).map(([key, name]) => [key, info.paid_tools[name]])) };
  const server = new McpServer({ name: "runtime-guard", version: "fixture" });
  const client = new Client({ name: "runtime-guard-client", version: "fixture" });
  const api = new Proxy({}, { get: () => async () => { throw new Error("upstream forbidden in runtime guard"); } });
  registerTools(server, api, config, Object.fromEntries(Object.keys(names).map(key => [key, null])));
  const [left, right] = InMemoryTransport.createLinkedPair();
  try {
    await Promise.all([client.connect(left), server.connect(right)]);
    const catalog = await client.listTools();
    assert.equal(catalog.tools.length, 9);
    const expected = structuredClone(before.mcp_schemas);
    const bundle = expected.get_evidence_bundle.properties;
    const backend = before.api_schemas.ResearchBundleBody.properties;
    assert.equal(bundle.limit.maximum, 15);
    assert.equal(bundle.max_chars.maximum, 30000);
    bundle.limit.maximum = backend.limit.maximum;
    bundle.max_chars.maximum = backend.max_chars.maximum;
    assert.equal(bundle.limit.maximum, 5);
    assert.equal(bundle.max_chars.maximum, 20000);
    if (!expected.get_code_or_math_spec.properties.cursor) {
      expected.get_code_or_math_spec.properties.cursor = { type: "string", minLength: 1, maxLength: 1024 };
    }
    for (const tool of catalog.tools) {
      assert.equal(tool.inputSchema.additionalProperties, before.mcp_schemas[tool.name].additionalProperties,
        `${tool.name}: deployed additionalProperties changed`);
    }
    assert.deepEqual(Object.fromEntries(catalog.tools.map(tool => [tool.name, tool.inputSchema])), expected,
      "actual nine runtime schemas changed beyond bundle bounds and additive cursor");
    const response = await client.callTool({ name: "dtox_service_info", arguments: {} });
    assert.deepEqual(JSON.parse(response.content[0].text), info, "actual service info/prices/payment policy changed");
    return { captured_utc: before.captured_utc, schemas: catalog.tools.length, financial_fields_unchanged: true };
  } finally { await client.close(); await server.close(); }
}
