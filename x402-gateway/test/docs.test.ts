import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const readme = readFileSync(new URL("../../README.md", import.meta.url), "utf8");
const site = readFileSync(new URL("../../site/index.html", import.meta.url), "utf8");
const ci = readFileSync(new URL("../../.github/workflows/ci.yml", import.meta.url), "utf8");
const plan = readFileSync(new URL("../../docs/2026-10-02-qdrant-maintenance-plan.md", import.meta.url), "utf8");

test("self-host docs explicitly export local paths and port 8005 without claiming automatic env loading", () => {
  assert.ok(readme.includes("codex mcp add dtox --url https://read.whoim.space/mcp"));
  for (const variable of ["RESEARCH_KEYS_PATH", "DTOX_DATA_DIR", "STATE_DB_PATH", "QDRANT_URL"]) {
    assert.match(readme, new RegExp(`export ${variable}=`));
  }
  assert.ok(readme.includes('export EMBED_URL="http://127.0.0.1:8005/embed"'));
  assert.ok(readme.includes('export EMBED_BATCH_URL="http://127.0.0.1:8005/embed_batch"'));
  assert.ok(readme.includes("A .env file is not automatically loaded"));
  assert.ok(readme.includes("restore verification not established"));
  assert.ok(!readme.includes("backups with verified restores"));
});

test("self-host outline separates blocking services and persists local Qdrant storage", () => {
  assert.ok(readme.includes("mkdir -p data"));
  assert.ok(readme.includes('export QDRANT_URL="http://127.0.0.1:6333"'));
  assert.ok(readme.includes("docker volume create dtox-qdrant-data"));
  assert.ok(readme.includes("-p 127.0.0.1:6333:6333"));
  assert.ok(readme.includes("-v dtox-qdrant-data:/qdrant/storage"));
  assert.ok(readme.includes("qdrant/qdrant:<reviewed-version>"));
  assert.ok(readme.includes("shell exports do not propagate to other terminals"));
  for (const command of ["python3 pipeline/service.py", "uvicorn api.main:app --port 8010", "python3 mcp/server.py"]) {
    const block = [...readme.matchAll(/```bash\n([\s\S]*?)```/g)].find((match) => match[1].includes(command));
    assert.ok(block, command);
    assert.equal(["python3 pipeline/service.py", "uvicorn api.main:app --port 8010", "python3 mcp/server.py"]
      .filter((service) => block[1].includes(service)).length, 1);
  }
  assert.ok(readme.includes("this is a setup outline, not a turnkey verification claim"));
});

test("public docs distinguish retrieval limits, observation timestamps, payment and scientific quorum", () => {
  assert.ok(readme.includes("not invariant to wording"));
  assert.ok(readme.includes("arbitrary paraphrases are not guaranteed"));
  assert.ok(readme.includes("observation time, not a corpus snapshot identifier"));
  assert.ok(readme.includes("no automatic refund"));
  assert.ok(readme.includes("per-batch-attempt fee"));
  assert.ok(readme.includes("Payment is not scientific quorum"));
  assert.ok(readme.includes("Solana devnet only"));
  assert.ok(readme.includes("no default asset mapping"));
  assert.ok(!site.includes("when independent wallets agree"));
  assert.ok(!site.includes("The registry reports this claim as confirmed_prior_art"));
  assert.ok(site.includes("observation time, not a corpus snapshot"));
  assert.ok(site.includes("graph traversal is seed-dependent"));
  assert.ok(site.includes("changing query wording can change the retrieved seeds"));
});

test("site lists thirteen public tools including the real research bundle", () => {
  const section = site.slice(site.indexOf('<section id="returns">'), site.indexOf('<section id="verdicts">'));
  assert.ok(section.includes("Thirteen public MCP tools"));
  const tools = [...section.matchAll(/<code>([a-z_]+)<\/code>/g)].map((match) => match[1]);
  assert.equal(new Set(tools).size, 13);
  assert.ok(tools.includes("get_research_bundle"));
});

test("CI includes bounded offline pipeline and operations suites", () => {
  for (const directory of ["pipeline/tests", "ops/tests"]) {
    assert.ok(ci.includes(`python -m unittest discover -s ${directory} -p 'test_*.py'`));
  }
  assert.ok(ci.includes("Fast offline pipeline regressions"));
  assert.ok(ci.includes("Fast offline operations regressions"));
});

test("HNSW document is a gated plan, not an executed migration or verified restore", () => {
  for (const fact of ["10.737", "14 segments", "9.882", "10 GiB", "1.6 GB", "swap 0", "121 GB"]) {
    assert.ok(plan.includes(fact));
  }
  assert.ok(plan.includes("NOT\nstart a rebuild now"));
  assert.ok(plan.includes('"hnsw_config": {"on_disk": true}'));
  assert.ok(plan.includes("Restore verification is NOT"));
  assert.ok(plan.includes("not an instantaneous or resource-free rollback"));
  assert.ok(plan.includes("No exact freed\nRAM"));
  assert.ok(plan.includes("no rebuild"));
});
