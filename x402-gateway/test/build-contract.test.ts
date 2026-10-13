import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import path from "node:path";
import test from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { loadConfig } from "../src/config.js";
import { registerTools } from "../src/tools.js";
import { paidToolJsonSchema, paidToolSchemas, type PaidToolName } from "../src/tool-schemas.js";
import { verifyBuild } from "./verify-build.mjs";

test("isolated compiled build is reproducible and exposes the same nine contracts as source", { timeout: 60000 }, async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("network forbidden in build verification"); });
  const metadata = await verifyBuild(false, async (output: string) => {
    const compiledSchemas = await import(pathToFileURL(path.join(output, "tool-schemas.js")).href);
    const compiledTools = await import(pathToFileURL(path.join(output, "tools.js")).href);
    for (const name of Object.keys(paidToolSchemas) as PaidToolName[]) {
      assert.deepEqual(compiledSchemas.paidToolJsonSchema(name), paidToolJsonSchema(name), name);
    }
    const catalogs = [];
    for (const register of [registerTools, compiledTools.registerTools]) {
      const server = new McpServer({ name: "build-contract", version: "fixture" });
      const client = new Client({ name: "build-client", version: "fixture" });
      const config = loadConfig({ X402_MODE: "disabled" });
      const api = new Proxy({}, { get: () => async () => { throw new Error("upstream forbidden"); } });
      register(server, api as any, config, { evidence: null, spec: null, compare: null, trends: null, audit: null, verdict: null });
      const [left, right] = InMemoryTransport.createLinkedPair();
      try {
        await Promise.all([client.connect(left), server.connect(right)]);
        catalogs.push(await client.listTools());
        const info = await client.callTool({ name: "dtox_service_info", arguments: {} });
        catalogs.push(info);
      } finally { await client.close(); await server.close(); }
    }
    assert.equal(catalogs[0].tools.length, 9);
    for (const catalog of [catalogs[0], catalogs[2]]) {
      for (const tool of catalog.tools) {
        assert.equal(tool.inputSchema.additionalProperties, tool.name === "dtox_service_info" ? undefined : false,
          `${tool.name}: real tools/list additionalProperties`);
      }
      const signed = catalog.tools.find((tool: any) => tool.name === "record_signed_verdict");
      assert.equal(signed.inputSchema.properties.judgments.items.additionalProperties, false);
      const audit = catalog.tools.find((tool: any) => tool.name === "validate_project");
      assert.equal(audit.inputSchema.properties.claims.items.anyOf[1].additionalProperties, false);
    }
    assert.deepEqual(catalogs[2], catalogs[0]);
    assert.deepEqual(catalogs[3], catalogs[1]);
  });
  t.diagnostic(JSON.stringify(metadata));
  t.diagnostic("Only isolated candidate compilation verified; --check-dist is the separate fail-closed release gate, not a deployment.");
});
