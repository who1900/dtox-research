import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { paidToolJsonSchema, paidToolSchemas } from "../src/tool-schemas.js";
import { offlineGateway, verdictArgs } from "./offline.js";

function contract(requests: Array<{ model: string; input: Record<string, unknown> }> = []) {
  const run = spawnSync(process.platform === "win32" ? "python" : "python3",
    ["-B", fileURLToPath(new URL("./api-contract.py", import.meta.url))],
    { input: JSON.stringify(requests), encoding: "utf8", timeout: 15000, maxBuffer: 4 * 1024 * 1024 });
  assert.equal(run.error, undefined);
  assert.equal(run.status, 0, run.stderr);
  return JSON.parse(run.stdout);
}

test("bundle discovery derives its hard bounds from the real offline Pydantic model", () => {
  const actual = contract();
  const api = actual.models.ResearchBundleBody.properties;
  const gateway = paidToolJsonSchema("get_evidence_bundle").properties as Record<string, any>;
  for (const field of ["limit", "max_chars"]) {
    for (const key of ["type", "minimum", "maximum", "default"]) {
      assert.equal(gateway[field][key], api[field][key], `${field}.${key}`);
    }
  }
  assert.equal(gateway.strict.default, api.strict.default);
  assert.equal(actual.handlers["api/main.py:research_bundle"].parameters[0], "body");
  assert.deepEqual(actual.handlers["mcp/server.py:get_research_bundle"].parameters,
    ["query", "layer", "limit", "max_chars", "strict"]);
});

test("every formerly successful bundle range survives; impossible ranges fail before payment", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("network forbidden"); });
  const requests = [];
  const inputs = [];
  for (const limit of [1, 2, 3, 4, 5, 6, 15]) {
    for (const max_chars of [500, 501, 12000, 19999, 20000, 20001, 30000]) {
      for (const layer of [undefined, "llm-slm", "ai-agents", "web3", "builder-tech"]) {
        for (const strict of [false, true]) {
          const input = { query: "fixture", limit, max_chars, strict, ...(layer ? { layer } : {}) };
          inputs.push(input);
          requests.push({ model: "ResearchBundleBody", input });
        }
      }
    }
  }
  const actual = contract(requests);
  for (let index = 0; index < inputs.length; index++) {
    const parsed = paidToolSchemas.get_evidence_bundle.safeParse(inputs[index]);
    assert.equal(parsed.success, actual.results[index].valid, JSON.stringify(inputs[index]));
    if (parsed.success) assert.deepEqual(parsed.data, inputs[index]);
  }
  const gateway = await offlineGateway(t);
  for (const input of [{ query: "fixture", limit: 6 }, { query: "fixture", limit: 15 },
    { query: "fixture", max_chars: 20001 }, { query: "fixture", max_chars: 30000 }]) {
    assert.equal((await gateway.client.callTool({ name: "get_evidence_bundle", arguments: input })).isError, true);
  }
  assert.deepEqual(gateway.events, []);
  assert.deepEqual(gateway.calls, []);
});

test("all nine tools remain; real model subsets, adapter aliases, budgets and cursor survive", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("network forbidden"); });
  const gateway = await offlineGateway(t, { mode: "disabled" });
  const inputs = {
    search_research_preview: { query: "fixture", layer: "web3" },
    get_evidence_bundle: { query: "fixture", limit: 5, max_chars: 20000, strict: false, layer: "builder-tech" },
    get_code_or_math_spec: { paper_id: "iacr:2026/321", max_chars: 30000, cursor: "v1.opaque+/=:東京", include_prose: true },
    compare_methods: { paper_id_a: "paper:1", paper_id_b: "paper:2", max_chars: 30000, tables_only: true },
    research_trends: { about: "fixture", top: 50, year_from: 1990, year_to: 2100 },
    validate_project: { idea: "fixture project", claims: [{ claim: "fixture claim", phrasings: ["fixture alternative"] }], evidence_per_claim: 8, deep: true },
    get_verdict_message: { claim: "fixture claim", paper_id: "paper:1", verdict: "asserts" },
    record_signed_verdict: verdictArgs,
  };
  assert.deepEqual((await gateway.client.listTools()).tools.map(tool => tool.name).sort(),
    ["dtox_service_info", ...Object.keys(inputs)].sort());
  const actual = contract();
  const mapping: Record<string, string> = { search_research_preview: "search_research_paper", get_evidence_bundle: "get_research_bundle" };
  const modelMapping: Record<string, string> = { search_research_preview: "SearchBody", get_evidence_bundle: "ResearchBundleBody",
    validate_project: "ValidateBody", get_verdict_message: "VerdictMessageBody", record_signed_verdict: "AdjudicateSignedBody" };
  const requests = [];
  for (const [name, input] of Object.entries(inputs)) {
    assert.ok(!(await gateway.client.callTool({ name, arguments: input })).isError, name);
    const forwarded = gateway.calls.at(-1)!;
    assert.equal(forwarded.name, mapping[name] ?? name);
    const handler = actual.handlers[`mcp/server.py:${forwarded.name}`];
    for (const field of Object.keys(forwarded.arguments!)) assert.ok(handler.parameters.includes(field), `${name}.${field}`);
    if (name === "get_code_or_math_spec") {
      assert.equal(forwarded.arguments!.cursor, inputs.get_code_or_math_spec.cursor);
      assert.equal(forwarded.arguments!.arxiv_id, input.paper_id);
      assert.equal(forwarded.arguments!.max_chars, 30000);
      assert.ok(actual.handlers["api/main.py:paper_spec"].parameters.includes("cursor"));
    }
    if (name === "compare_methods") {
      assert.equal(forwarded.arguments!.arxiv_id_a, inputs.compare_methods.paper_id_a);
      assert.equal(forwarded.arguments!.arxiv_id_b, inputs.compare_methods.paper_id_b);
      assert.equal(forwarded.arguments!.extract_tables_only, true);
      assert.equal(forwarded.arguments!.max_chars, 30000);
    }
    if (modelMapping[name]) {
      const body = { ...forwarded.arguments };
      if (name === "validate_project") { body.depth = body.deep ? "full" : "fast"; delete body.deep; }
      requests.push({ model: modelMapping[name], input: body });
    }
  }
  assert.ok(contract(requests).results.every((item: any) => item.valid));
});
