import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const readme = readFileSync(new URL("../../README.md", import.meta.url), "utf8");
const technical = readFileSync(new URL("../../docs/technical-reference.md", import.meta.url), "utf8");
const site = readFileSync(new URL("../../site/index.html", import.meta.url), "utf8");
const ci = readFileSync(new URL("../../.github/workflows/ci.yml", import.meta.url), "utf8");
const mcpSource = readFileSync(new URL("../../mcp/server.py", import.meta.url), "utf8");

function siteSection(id: string): string {
  const section = site.match(new RegExp(`<section\\b[^>]*\\bid="${id}"[^>]*>([\\s\\S]*?)</section>`));
  assert.ok(section, `missing site section: ${id}`);
  return section[1];
}

test("self-host docs explicitly export local paths and port 8005 without claiming automatic env loading", () => {
  assert.ok(readme.includes("codex mcp add dtox --url https://read.whoim.space/mcp"));
  assert.ok(readme.includes("(docs/technical-reference.md)"));
  for (const variable of ["RESEARCH_KEYS_PATH", "DTOX_DATA_DIR", "STATE_DB_PATH", "QDRANT_URL"]) {
    assert.match(technical, new RegExp(`export ${variable}=`));
  }
  assert.ok(technical.includes('export EMBED_URL="http://127.0.0.1:8005/embed"'));
  assert.ok(technical.includes('export EMBED_BATCH_URL="http://127.0.0.1:8005/embed_batch"'));
  assert.ok(technical.includes("A .env file is not automatically loaded"));
});

test("self-host outline separates blocking services and persists local Qdrant storage", () => {
  assert.ok(technical.includes("mkdir -p data"));
  assert.ok(technical.includes('export QDRANT_URL="http://127.0.0.1:6333"'));
  assert.ok(technical.includes("docker volume create dtox-qdrant-data"));
  assert.ok(technical.includes("-p 127.0.0.1:6333:6333"));
  assert.ok(technical.includes("-v dtox-qdrant-data:/qdrant/storage"));
  assert.ok(technical.includes("qdrant/qdrant:<reviewed-version>"));
  assert.ok(technical.includes("shell exports do not propagate to other terminals"));
  for (const command of ["python3 pipeline/service.py", "uvicorn api.main:app --port 8010", "python3 mcp/server.py"]) {
    const block = [...technical.matchAll(/```bash\n([\s\S]*?)```/g)].find((match) => match[1].includes(command));
    assert.ok(block, command);
    assert.equal(["python3 pipeline/service.py", "uvicorn api.main:app --port 8010", "python3 mcp/server.py"]
      .filter((service) => block[1].includes(service)).length, 1);
  }
});

test("public guide documents source metadata, attributed readings and payment terms", () => {
  assert.ok(technical.includes("acquisition, extraction and coverage metadata"));
  assert.ok(technical.includes("caller's wallet signature"));
  assert.ok(technical.includes("no automatic refund"));
  assert.ok(technical.includes("per-batch-attempt fee"));
  assert.ok(technical.includes("reviewer attribution and registry status are separate fields"));
  assert.ok(technical.includes("uses Solana devnet"));
  assert.ok(technical.includes("no default asset mapping"));
  assert.ok(!site.includes("when independent wallets agree"));
  assert.ok(!site.includes("The registry reports this claim as confirmed_prior_art"));
  const coverage = siteSection("returns");
  assert.match(coverage, /\bfull text\b/i);
  assert.match(coverage, /\babstract-only records\b/i);
  const solana = siteSection("solana");
  assert.match(solana, /signature identifies who signed/i);
  assert.match(solana, /Solana devnet/);
  assert.match(solana, /free endpoint/);
  for (const guide of ["attestor", "x402-gateway"]) {
    assert.ok(solana.includes(`href="https://github.com/who1900/dtox-research/tree/main/${guide}"`));
  }
});

test("README and site catalogs match the default public MCP registrations", () => {
  // Opt-in unsigned registry mutations register separately, without decorators.
  const expected = [...mcpSource.matchAll(/^@mcp\.tool\(\)\r?\n(?:async )?def ([a-z_]+)\(/gm)]
    .map((match) => match[1]).sort();
  assert.ok(expected.length > 0);
  const section = siteSection("tools");
  assert.ok(section.includes(`${expected.length} public MCP tools`));
  const tools = [...section.matchAll(/<code>([a-z_]+)<\/code>/g)].map((match) => match[1]);
  assert.deepEqual(tools.sort(), expected);
  const catalog = readme.match(/## Public tool catalog\r?\n([\s\S]*?)(?:\r?\n## |$)/);
  assert.ok(catalog, "missing README tool catalog");
  const readmeTools = [...catalog[1].matchAll(/^\| `([a-z_]+)` \|/gm)].map((match) => match[1]);
  assert.deepEqual(readmeTools.sort(), expected);
});

test("CI includes bounded offline pipeline and operations suites", () => {
  for (const directory of ["pipeline/tests", "ops/tests"]) {
    assert.ok(ci.includes(`python -m unittest discover -s ${directory} -p 'test_*.py'`));
  }
  assert.ok(ci.includes("Fast offline pipeline regressions"));
  assert.ok(ci.includes("Fast offline operations regressions"));
});
