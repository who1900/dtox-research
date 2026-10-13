import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { verifyRuntimeContract } from "./runtime-contract.mjs";

const gateway = fileURLToPath(new URL("../", import.meta.url));
const testRoot = path.join(gateway, "test");
const digest = bytes => createHash("sha256").update(bytes).digest("hex");
const files = directory => Object.fromEntries(readdirSync(directory).filter(name => name.endsWith(".js"))
  .sort().map(name => [name, digest(readFileSync(path.join(directory, name)))]));

export async function verifyBuild(checkDist = false, inspect = async () => {}, scopedBefore) {
  const temporary = mkdtempSync(path.join(testRoot, ".contract-build-"));
  try {
    for (const run of ["first", "second"]) {
      const build = spawnSync(process.execPath, [path.join(gateway, "node_modules/typescript/bin/tsc"),
        "-p", path.join(gateway, "tsconfig.json"), "--outDir", path.join(temporary, run), "--noEmitOnError"],
        { cwd: gateway, encoding: "utf8", timeout: 30000 });
      assert.equal(build.error, undefined);
      assert.equal(build.status, 0, build.stdout + build.stderr);
    }
    const first = path.join(temporary, "first");
    const emitted = files(first);
    assert.deepEqual(files(path.join(temporary, "second")), emitted, "non-reproducible compiled output");
    const installed = existsSync(path.join(gateway, "dist")) ? files(path.join(gateway, "dist")) : {};
    const drift = [...new Set([...Object.keys(installed), ...Object.keys(emitted)])]
      .filter(name => installed[name] !== emitted[name]).sort();
    const metadata = {
      node: process.version,
      typescript: JSON.parse(readFileSync(path.join(gateway, "node_modules/typescript/package.json"), "utf8")).version,
      compiler_config: digest(readFileSync(path.join(gateway, "tsconfig.json"))),
      package: digest(readFileSync(path.join(gateway, "package.json"))),
      lock: digest(readFileSync(path.join(gateway, "package-lock.json"))),
      sources: Object.fromEntries(readdirSync(path.join(gateway, "src")).filter(name => name.endsWith(".ts"))
        .sort().map(name => [name, digest(readFileSync(path.join(gateway, "src", name)))])),
      emitted,
      dist: installed,
      dist_drift: drift,
      dist_matches: drift.length === 0,
    };
    if (scopedBefore) {
      const scoped = path.join(temporary, "scoped");
      mkdirSync(scoped);
      for (const name of Object.keys(installed)) copyFileSync(path.join(gateway, "dist", name), path.join(scoped, name));
      for (const name of ["tools.js", "tool-schemas.js"]) copyFileSync(path.join(first, name), path.join(scoped, name));
      const overlay = files(scoped);
      const preserved = Object.fromEntries(Object.entries(installed).filter(([name]) =>
        !["tools.js", "tool-schemas.js"].includes(name)));
      for (const name of ["payments.js", "server.js", "config.js", "upstream.js", "errors.js"]) {
        assert.ok(preserved[name], `missing existing compiled dependency ${name}`);
      }
      for (const [name, sha] of Object.entries(preserved)) assert.equal(overlay[name], sha, `${name} must stay byte-identical`);
      metadata.scoped_guard = {
        ...(await verifyRuntimeContract(scoped, scopedBefore)),
        candidate: { "src/tools.ts": metadata.sources["tools.ts"], "src/tool-schemas.ts": metadata.sources["tool-schemas.ts"],
          "dist/tools.js": overlay["tools.js"], "dist/tool-schemas.js": overlay["tool-schemas.js"] },
        preserved_dist: preserved,
      };
      assert.deepEqual(files(path.join(gateway, "dist")), installed, "existing dist mutated during scoped verification");
    }
    await inspect(first, metadata);
    if (checkDist) assert.equal(drift.length, 0,
      `source/dist drift: release blocked (${drift.join(", ")}); rebuild from the reviewed current sources, never restore old payment sources`);
    return metadata;
  } finally {
    assert.equal(path.dirname(temporary), testRoot);
    assert.ok(path.basename(temporary).startsWith(".contract-build-"));
    rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  try {
    const beforeIndex = process.argv.indexOf("--before");
    if (beforeIndex !== -1) assert.ok(process.argv[beforeIndex + 1], "--before requires the captured contract path");
    const scoped = process.argv.includes("--check-scoped");
    if (scoped) assert.ok(beforeIndex !== -1, "--check-scoped requires --before with the actual runtime snapshot");
    const metadata = await verifyBuild(process.argv.includes("--check-dist"), async (output, report) => {
      if (beforeIndex !== -1) report.runtime_guard = await verifyRuntimeContract(output, process.argv[beforeIndex + 1]);
    }, scoped ? process.argv[beforeIndex + 1] : undefined);
    console.log(JSON.stringify(metadata, null, 2));
  }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
