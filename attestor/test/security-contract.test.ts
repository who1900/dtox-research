import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import test, { type TestContext } from "node:test";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";
import { buildVerdictMessage, claimTextSha256, checkIssuedAtWindow, parseIssuedAtTimestamp,
  sha256Hex, verifyVerdictSignature, MAX_AGE_MS, MAX_FUTURE_SKEW_MS } from "../src/message.js";
import { attestVerdict, deriveVerdictNonce, getAttestation } from "../src/sas.js";
import { mockChain, TEST_ADDRESS, verdictRecord } from "./mock-chain.js";
import { address } from "@solana/kit";
import { deriveAttestationPda } from "sas-lib";
import { deriveCredentialAddress, deriveSchemaAddress } from "../src/sas.js";

process.env.ATTESTOR_INTERNAL_TOKEN = "offline-contract-token";
const { buildApp } = await import("../src/server.js");
const headers = { "Content-Type": "application/json", "X-Internal-Token": "offline-contract-token" };

function fields(issuedAt = new Date().toISOString()) {
  return { claimId: "claim|東京", claimSha256: claimTextSha256("Protocol has safety"),
    paperId: "iacr:2026/321", verdict: "asserts" as const, evidenceSha256: sha256Hex("evidence"), issuedAt };
}

async function signedBody() {
  const values = fields();
  const fixture = new Uint8Array(32).fill(7);
  return { claim_id: values.claimId, claim_text: "Protocol has safety", paper_id: values.paperId,
    verdict: values.verdict, evidence_sha256: values.evidenceSha256, issued_at: values.issuedAt,
    reviewer: bs58.encode(await ed.getPublicKeyAsync(fixture)),
    signature: bs58.encode(await ed.signAsync(new TextEncoder().encode(buildVerdictMessage(values)), fixture)) };
}

async function withServer(t: TestContext,
  run: (base: string) => Promise<void>, dependencies: Parameters<typeof buildApp>[0] = {}) {
  const originalFetch = globalThis.fetch;
  t.mock.method(globalThis, "fetch", (input: string | URL, init?: RequestInit) => {
    assert.equal(new URL(String(input)).hostname, "127.0.0.1", "external network forbidden");
    return originalFetch(input, init);
  });
  const server = createServer(buildApp(dependencies));
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  try { await run(`http://127.0.0.1:${(server.address() as AddressInfo).port}`); }
  finally { await new Promise<void>((resolve) => server.close(() => resolve())); }
}

test("delimiter-bearing IDs have distinct nonces; ordinary v1 PDA seeds remain compatible", () => {
  assert.notEqual(deriveVerdictNonce("a|b", "c", TEST_ADDRESS), deriveVerdictNonce("a", "b|c", TEST_ADDRESS));
  assert.equal(deriveVerdictNonce("a", "b", TEST_ADDRESS), bs58.encode(Buffer.from(sha256Hex(`dtox:v1:a|b|${TEST_ADDRESS}`), "hex")));
  assert.equal(deriveVerdictNonce("a|b", "c", TEST_ADDRESS), deriveVerdictNonce("a|b", "c", TEST_ADDRESS));
});

test("canonical signatures bind every claim/paper/verdict/evidence/timestamp field and wallet", async () => {
  const values = fields();
  const fixture = new Uint8Array(32).fill(7);
  const reviewer = bs58.encode(await ed.getPublicKeyAsync(fixture));
  const signature = bs58.encode(await ed.signAsync(new TextEncoder().encode(buildVerdictMessage(values)), fixture));
  assert.equal(await verifyVerdictSignature(buildVerdictMessage(values), signature, reviewer), true);
  for (const change of [{ claimId: "other" }, { claimSha256: claimTextSha256("other claim") },
    { paperId: "eip:7702" }, { verdict: "partial" as const }, { evidenceSha256: sha256Hex("other") },
    { issuedAt: new Date(Date.parse(values.issuedAt) + 1).toISOString() }]) {
    assert.equal(await verifyVerdictSignature(buildVerdictMessage({ ...values, ...change }), signature, reviewer), false);
  }
  const other = bs58.encode(await ed.getPublicKeyAsync(new Uint8Array(32).fill(8)));
  assert.equal(await verifyVerdictSignature(buildVerdictMessage(values), signature, other), false);
});

test("legacy delimiter-bearing records remain readable/idempotent and collisions never overwrite them", async () => {
  const chain = mockChain();
  const record = verdictRecord({ claimId: "a|b", paperId: "c" });
  const nonce = address(bs58.encode(Buffer.from(sha256Hex(`dtox:v1:a|b|c|${record.reviewer}`), "hex")));
  const credential = await deriveCredentialAddress(TEST_ADDRESS);
  const schema = await deriveSchemaAddress(credential);
  const [pda] = await deriveAttestationPda({ credential, schema, nonce });
  chain.seed(pda, record);
  chain.state.nonces.set(pda, nonce);
  assert.deepEqual(await getAttestation(pda, chain.operations), record);
  assert.deepEqual(await attestVerdict(record, chain.operations), { pda, signature: null, reused: true });
  const newer = { ...record, verdict: "partial", issuedAt: "2026-09-26T03:01:00Z" };
  assert.equal((await attestVerdict(newer, chain.operations)).pda, pda);
  const collision = verdictRecord({ claimId: "a", paperId: "b|c" });
  assert.notEqual((await attestVerdict(collision, chain.operations)).pda, pda);
  assert.deepEqual(await getAttestation(pda, chain.operations), newer);
  assert.equal(chain.state.writes, 2);
});

test("IDs cannot inject canonical lines; timestamps reject non-ISO and calendar overflow", () => {
  for (const control of ["\n", "\r", "\0", "\t"]) {
    for (const field of ["claimId", "paperId"]) assert.throws(() => buildVerdictMessage({ ...fields(), [field]: `a${control}b` }), /control/);
  }
  for (const issuedAt of ["2026-02-30T00:00:00Z", "2026-10-06", "Tue, 06 Oct 2026 00:00:00 GMT", "2026-10-06T24:00:00Z"]) {
    assert.ok(Number.isNaN(parseIssuedAtTimestamp(issuedAt)));
    assert.throws(() => buildVerdictMessage(fields(issuedAt)), /valid ISO/);
  }
  for (const issuedAt of ["2026-10-06T00:00:00Z", "2026-10-06T05:00:00+05:00"]) {
    assert.equal(parseIssuedAtTimestamp(issuedAt), Date.parse("2026-10-06T00:00:00Z"));
  }
});

test("TTL and future skew boundaries are exact and reject a bad clock", () => {
  const now = new Date("2026-10-06T00:00:00Z");
  for (const delta of [-MAX_AGE_MS, MAX_FUTURE_SKEW_MS]) {
    assert.doesNotThrow(() => checkIssuedAtWindow(new Date(+now + delta).toISOString(), now));
  }
  for (const delta of [-MAX_AGE_MS - 1, MAX_FUTURE_SKEW_MS + 1]) {
    assert.throws(() => checkIssuedAtWindow(new Date(+now + delta).toISOString(), now));
  }
  assert.throws(() => checkIssuedAtWindow(now.toISOString(), new Date(NaN)), /valid/);
});

test("malformed JSON and field types return bounded JSON 400 without writes", { timeout: 10000 }, async (t) => {
  let writes = 0;
  await withServer(t, async (base) => {
    for (const path of ["v2/message", "v2/attest"]) {
      for (const body of ["null", "[]", "1", '"text"', '{"claim_id":12}', '{"claim_text":{}}', '{"paper_id":null}', "{"]) {
        const response = await fetch(`${base}/${path}`, { method: "POST", headers, body });
        assert.equal(response.status, 400, `${path}: ${body}`);
        assert.equal(typeof (await response.json() as { error: unknown }).error, "string");
      }
      const response = await fetch(`${base}/${path}`, { method: "POST", headers,
        body: JSON.stringify({ ...(await signedBody()), claim_text: " \n\t " }) });
      assert.equal(response.status, 400);
    }
  }, { attestVerdict: async () => { writes++; throw new Error("unexpected write"); } });
  assert.equal(writes, 0);
});

test("HTTP rejects tampering before chain access; exact retries/concurrency are idempotent", { timeout: 10000 }, async (t) => {
  const chain = mockChain();
  const body = await signedBody();
  await withServer(t, async (base) => {
    const post = (value: unknown) => fetch(`${base}/v2/attest`, { method: "POST", headers, body: JSON.stringify(value) });
    for (const change of [{ claim_id: "other" }, { claim_text: "Other claim" }, { paper_id: "eip:7702" },
      { verdict: "partial" }, { evidence_sha256: "b".repeat(64) }, { signature: "0" },
      { issued_at: new Date(Date.parse(body.issued_at) + 1).toISOString() }, { reviewer: TEST_ADDRESS }]) {
      assert.equal((await post({ ...body, ...change })).status, 400);
    }
    assert.equal(chain.state.credentialReads, 0);
    const responses = await Promise.all(Array.from({ length: 6 }, () => post(body)));
    assert.ok(responses.every((response) => response.status === 200));
    assert.equal(chain.state.writes, 1);
  }, { attestVerdict: (record) => attestVerdict(record, chain.operations) });
});

test("SAS account ownership, authority, schema and signer fail closed", async () => {
  for (const [operation, changes] of [
    ["fetchMaybeCredential", [{ programAddress: TEST_ADDRESS }, { data: { authority: "foreign" } }, { data: { authorizedSigners: [] } }]],
    ["fetchMaybeSchema", [{ programAddress: TEST_ADDRESS }, { data: { credential: TEST_ADDRESS } }, { data: { version: 2 } }, { data: { layout: [] } }, { data: { fieldNames: [] } }]],
    ["fetchSchema", [{ data: { isPaused: true } }]],
  ] as const) {
    for (const change of changes) {
      const chain = mockChain();
      const operations = chain.operations as any;
      const original = operations[operation];
      operations[operation] = async (...args: any[]) => {
        const account = await original(...args);
        return { ...account, ...change, data: { ...account.data, ...("data" in change ? change.data : {}) } };
      };
      await assert.rejects(() => attestVerdict(verdictRecord(), chain.operations), /owned|authority|schema/);
      assert.equal(chain.state.writes, 0);
    }
  }
  for (const change of [{ programAddress: TEST_ADDRESS }, { data: { signer: "foreign" } },
    { data: { schema: TEST_ADDRESS } }, { data: { credential: TEST_ADDRESS } }, { data: { nonce: TEST_ADDRESS } }]) {
    const chain = mockChain();
    const { pda } = await attestVerdict(verdictRecord(), chain.operations);
    const original = chain.operations.fetchMaybeAttestation;
    chain.operations.fetchMaybeAttestation = (async (...args: Parameters<typeof original>) => {
      const account: any = await original(...args);
      return { ...account, ...change, data: { ...account.data, ...("data" in change ? change.data : {}) } };
    }) as typeof original;
    await assert.rejects(() => getAttestation(pda, chain.operations), /owned|mismatch|nonce/);
    await assert.rejects(() => attestVerdict(verdictRecord(), chain.operations), /owned|mismatch/);
    assert.equal(chain.state.writes, 1);
  }
});
