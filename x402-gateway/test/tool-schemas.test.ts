import assert from "node:assert/strict";
import test from "node:test";
import { AjvJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/ajv";
import { paidToolSchemas, type PaidToolName } from "../src/tool-schemas.js";
import { offlineGateway, verdictArgs } from "./offline.js";

const inputs: Record<PaidToolName, Record<string, unknown>> = {
  get_evidence_bundle: { query: "fixture" },
  get_code_or_math_spec: { paper_id: "paper:1" },
  compare_methods: { paper_id_a: "paper:1", paper_id_b: "paper:2" },
  research_trends: { about: "fixture" },
  validate_project: { idea: "fixture project", claims: ["fixture claim"] },
  record_signed_verdict: verdictArgs
};

const layers = ["llm-slm", "ai-agents", "web3"];
const budget = { type: "integer", minimum: 500, maximum: 30000, default: 12000 };
const paperId = { type: "string", minLength: 3, maxLength: 100 };
const fields: Record<PaidToolName, Record<string, Record<string, unknown>>> = {
  get_evidence_bundle: {
    query: { type: "string", minLength: 3, maxLength: 500 },
    layer: { enum: [...layers, "builder-tech"] },
    limit: { type: "integer", minimum: 1, maximum: 5, default: 3 },
    max_chars: { ...budget, maximum: 20000 }, strict: { type: "boolean", default: true }
  },
  get_code_or_math_spec: {
    paper_id: paperId,
    target_elements: { type: "string", maxLength: 100, default: "algorithm,equation,code,table" },
    include_prose: { type: "boolean", default: false }, max_chars: budget,
    cursor: { type: "string", minLength: 1, maxLength: 1024 }
  },
  compare_methods: {
    paper_id_a: paperId, paper_id_b: paperId,
    tables_only: { type: "boolean", default: false }, max_chars: budget
  },
  research_trends: {
    about: { type: "string", minLength: 3, maxLength: 500 }, layer: { enum: layers },
    year_from: { type: "integer", minimum: 1990, maximum: 2100, default: 2023 },
    year_to: { type: "integer", minimum: 1990, maximum: 2100 },
    top: { type: "integer", minimum: 1, maximum: 50, default: 20 }
  },
  validate_project: {
    idea: { type: "string", minLength: 10, maxLength: 2000 },
    claims: { type: "array", minItems: 1, maxItems: 8 }, layer: { enum: layers },
    evidence_per_claim: { type: "integer", minimum: 1, maximum: 8, default: 4 },
    deep: { type: "boolean", default: true }
  },
  record_signed_verdict: {
    claim: { type: "string", minLength: 3, maxLength: 1000 },
    judgments: { type: "array", minItems: 1, maxItems: 20 }, layer: { enum: layers }
  }
};

const required: Record<PaidToolName, string[]> = {
  get_evidence_bundle: ["query"], get_code_or_math_spec: ["paper_id"],
  compare_methods: ["paper_id_a", "paper_id_b"], research_trends: ["about"],
  validate_project: ["idea", "claims"], record_signed_verdict: ["claim", "judgments"]
};

function boundaryValues(rule: Record<string, any>, sample: unknown): Array<[unknown, boolean]> {
  const cases: Array<[unknown, boolean]> = [[null, false]];
  if (rule.type === "string") {
    const matches = (value: string) => !rule.pattern || new RegExp(rule.pattern).test(value);
    cases.push([123, false], ["x".repeat(rule.minLength ?? 0), matches("x".repeat(rule.minLength ?? 0))]);
    if (rule.minLength) cases.push(["x".repeat(rule.minLength - 1), false]);
    if (rule.maxLength) cases.push(["x".repeat(rule.maxLength), matches("x".repeat(rule.maxLength))], ["x".repeat(rule.maxLength + 1), false]);
  }
  if (rule.type === "integer") {
    cases.push([rule.minimum, true], [rule.maximum, true], [rule.minimum - 1, false],
      [rule.maximum + 1, false], [rule.minimum + 0.5, false], [String(rule.minimum), false]);
  }
  if (rule.type === "boolean") cases.push([true, true], [false, true], ["false", false]);
  if (rule.type === "array") {
    const item = (sample as unknown[])[0];
    cases.push([[], false], [Array(rule.minItems).fill(item), true],
      [Array(rule.maxItems).fill(item), true], [Array(rule.maxItems + 1).fill(item), false], ["array", false]);
  }
  if (rule.enum) cases.push(...rule.enum.map((value: unknown): [unknown, boolean] => [value, true]), ["other", false]);
  return cases;
}

test("all paid MCP and actual discovery schemas agree, including every field and boundary", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("network forbidden in schema tests"); });
  const gateway = await offlineGateway(t);
  const catalog = await gateway.client.listTools();
  const validator = new AjvJsonSchemaValidator();
  for (const name of Object.keys(inputs) as PaidToolName[]) {
    const challenge = await gateway.challenge(name, inputs[name]);
    const discovery = challenge.extensions!.bazaar as { info: { input: { inputSchema: any } } };
    const schema = discovery.info.input.inputSchema;
    assert.deepEqual(schema, catalog.tools.find((tool) => tool.name === name)!.inputSchema, name);
    assert.deepEqual(Object.keys(schema.properties), Object.keys(fields[name]), name);
    assert.deepEqual(schema.required, required[name], name);
    const validate = validator.getValidator(schema);
    assert.equal(validate(inputs[name]).valid, true, name);
    for (const [field, rule] of Object.entries(fields[name])) {
      for (const [key, value] of Object.entries(rule)) {
        assert.deepEqual(schema.properties[field][key], value, `${name}.${field}.${key}`);
      }
      for (const [value, accepted] of boundaryValues(rule, inputs[name][field])) {
        const input = { ...inputs[name], [field]: value };
        assert.equal(validate(input).valid, accepted, `${name}.${field}: JSON boundary`);
        assert.equal(paidToolSchemas[name].safeParse(input).success, accepted, `${name}.${field}: Zod boundary`);
      }
    }
    for (const field of required[name]) {
      const input = { ...inputs[name] };
      delete input[field];
      assert.equal(validate(input).valid, false, `${name}.${field}: JSON required`);
      assert.equal(paidToolSchemas[name].safeParse(input).success, false, `${name}.${field}: Zod required`);
    }
  }
  assert.deepEqual(gateway.events, []);
  assert.deepEqual(gateway.calls, []);
});

test("nested claims and signed judgments retain all validation constraints in discovery", async (t) => {
  const gateway = await offlineGateway(t);
  const validator = new AjvJsonSchemaValidator();
  for (const name of ["validate_project", "record_signed_verdict"] as const) {
    const challenge = await gateway.challenge(name, inputs[name]);
    const discovery = challenge.extensions!.bazaar as { info: { input: { inputSchema: any } } };
    const schema = discovery.info.input.inputSchema;
    const validate = validator.getValidator(schema);
    const check = (input: Record<string, unknown>, expected: boolean) => {
      assert.equal(validate(input).valid, expected);
      assert.equal(paidToolSchemas[name].safeParse(input).success, expected);
    };
    if (name === "validate_project") {
      const variants = schema.properties.claims.items.anyOf;
      assert.deepEqual(variants[0], { type: "string", minLength: 3, maxLength: 1000 });
      assert.deepEqual(variants[1].required, ["claim"]);
      assert.deepEqual(variants[1].properties.claim, variants[0]);
      assert.deepEqual(variants[1].properties.phrasings, { type: "array", items: variants[0], maxItems: 4 });
      for (const [claim, accepted] of boundaryValues(variants[0], "fixture")) {
        check({ ...inputs[name], claims: [claim] }, accepted);
        check({ ...inputs[name], claims: [{ claim }] }, accepted);
        check({ ...inputs[name], claims: [{ claim: "fixture", phrasings: [claim] }] }, accepted);
      }
      check({ ...inputs[name], claims: [{}] }, false);
      check({ ...inputs[name], claims: [{ claim: "fixture", phrasings: Array(4).fill("fixture") }] }, true);
      check({ ...inputs[name], claims: [{ claim: "fixture", phrasings: Array(5).fill("fixture") }] }, false);
    } else {
      const item = schema.properties.judgments.items;
      const constraints = {
        id: paperId, verdict: { enum: ["asserts", "does_not_assert", "partial"] },
        reason: { type: "string", maxLength: 2000 }, evidence_sha256: { type: "string", maxLength: 64 },
        reviewer: { type: "string", minLength: 32, maxLength: 44 },
        signature: { type: "string", minLength: 1 }, issued_at: { type: "string", minLength: 1 }
      };
      assert.deepEqual(Object.keys(item.properties), Object.keys(constraints));
      assert.deepEqual(item.required, ["id", "verdict", "reviewer", "signature", "issued_at"]);
      for (const [field, rule] of Object.entries(constraints)) {
        for (const [key, value] of Object.entries(rule)) assert.deepEqual(item.properties[field][key], value);
        for (const [value, accepted] of boundaryValues({ ...rule, pattern: item.properties[field].pattern }, undefined)) {
          check({ ...inputs[name], judgments: [{ ...verdictArgs.judgments[0], [field]: value }] }, accepted);
        }
      }
      for (const field of item.required) {
        const judgment: Record<string, unknown> = { ...verdictArgs.judgments[0] };
        delete judgment[field];
        check({ ...inputs[name], judgments: [judgment] }, false);
      }
    }
  }
});

test("MCP forwards every paid default and preserves aliases and unknown-key compatibility", async (t) => {
  const gateway = await offlineGateway(t, { mode: "disabled" });
  for (const name of Object.keys(inputs) as PaidToolName[]) {
    const result = await gateway.client.callTool({ name, arguments: inputs[name] });
    assert.ok(!result.isError, name);
    const forwarded = gateway.calls.at(-1)!.arguments!;
    for (const [field, rule] of Object.entries(fields[name])) {
      if ("default" in rule) assert.deepEqual(forwarded[field === "tables_only" ? "extract_tables_only" : field], rule.default, `${name}.${field}`);
    }
    if (name === "get_code_or_math_spec") assert.equal(forwarded.arxiv_id, "paper:1");
    if (name === "compare_methods") {
      assert.equal(forwarded.arxiv_id_a, "paper:1");
      assert.equal(forwarded.arxiv_id_b, "paper:2");
    }
    const extra = await gateway.client.callTool({ name, arguments: { ...inputs[name], extra: true } });
    assert.equal(Boolean(extra.isError), name === "get_evidence_bundle", name);
    assert.equal(gateway.calls.at(-1)!.arguments!.extra, undefined);
  }
});
