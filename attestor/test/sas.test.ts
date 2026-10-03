import assert from "node:assert/strict";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import {
  assertDevnetGenesis, deriveVerdictNonce, DEVNET_GENESIS_HASH, FIELD_LAYOUT, FIELD_NAMES,
  attestVerdict, getAttestation, getInitializationState, AttestationConflictError, AttestationBusyError,
} from "../src/sas.js";
import { deferred, mockChain, TEST_ADDRESS, verdictRecord } from "./mock-chain.js";

function mockRpc(genesisHash: string) {
  return { getGenesisHash: () => ({ send: async () => genesisHash }) };
}

test("deriveVerdictNonce is deterministic for the same (claim, paper, reviewer)", () => {
  const a = deriveVerdictNonce("claim-1", "2401.12345", "ReviewerPubkey111111111111111111111111111");
  const b = deriveVerdictNonce("claim-1", "2401.12345", "ReviewerPubkey111111111111111111111111111");
  assert.equal(a, b);
});

test("deriveVerdictNonce differs for a different reviewer", () => {
  const a = deriveVerdictNonce("claim-1", "2401.12345", "ReviewerPubkey111111111111111111111111111");
  const b = deriveVerdictNonce("claim-1", "2401.12345", "AnotherReviewerPubkey22222222222222222222");
  assert.notEqual(a, b);
});

test("deriveVerdictNonce differs for a different claim or paper", () => {
  const base = deriveVerdictNonce("claim-1", "2401.12345", "ReviewerPubkey111111111111111111111111111");
  const differentClaim = deriveVerdictNonce("claim-2", "2401.12345", "ReviewerPubkey111111111111111111111111111");
  const differentPaper = deriveVerdictNonce("claim-1", "2402.99999", "ReviewerPubkey111111111111111111111111111");
  assert.notEqual(base, differentClaim);
  assert.notEqual(base, differentPaper);
});

test("deriveVerdictNonce is not confused by concatenation without the separator", () => {
  // "claim-1" + "2401" via the "|" join must not collide with "claim-12401" + ""
  const a = deriveVerdictNonce("claim-1", "2401", "R");
  const b = deriveVerdictNonce("claim-12401", "", "R");
  assert.notEqual(a, b);
});

test("assertDevnetGenesis accepts the real devnet genesis hash", async () => {
  await assert.doesNotReject(() => assertDevnetGenesis(mockRpc(DEVNET_GENESIS_HASH)));
});

test("assertDevnetGenesis rejects a non-devnet genesis hash (e.g. mainnet)", async () => {
  await assert.rejects(
    () => assertDevnetGenesis(mockRpc("5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d")),
    /does not match Solana devnet/
  );
});

test("assertDevnetGenesis skips the check when ATTESTOR_ALLOW_MAINNET=1", async () => {
  process.env.ATTESTOR_ALLOW_MAINNET = "1";
  try {
    await assert.doesNotReject(() => assertDevnetGenesis(mockRpc("not-a-real-hash-at-all")));
  } finally {
    delete process.env.ATTESTOR_ALLOW_MAINNET;
  }
});

test("schema field layout is one Borsh String (code 12) per field, matching field names", () => {
  assert.equal(FIELD_LAYOUT.length, FIELD_NAMES.length);
  assert.ok([...FIELD_LAYOUT].every((code) => code === 12));
  assert.deepEqual(
    [...FIELD_NAMES],
    ["claim_id", "claim_sha256", "paper_id", "verdict", "evidence_sha256", "reviewer", "reviewer_sig", "issued_at"]
  );
});

test("A -> B -> captured A rejects rollback and preserves B", async () => {
  const chain = mockChain();
  const a = verdictRecord();
  const b = verdictRecord({ verdict: "partial", issuedAt: "2026-09-26T03:01:00Z", reviewerSig: "test-signature-b" });
  const first = await attestVerdict(a, chain.operations);
  const second = await attestVerdict(b, chain.operations);
  assert.equal(second.pda, first.pda);
  await assert.rejects(() => attestVerdict(a, chain.operations), AttestationConflictError);
  assert.deepEqual(await getAttestation(first.pda, chain.operations), b);
  assert.equal(chain.state.writes, 2);
});

test("exact repeats are idempotent without another transaction", async () => {
  const chain = mockChain();
  const record = verdictRecord();
  const first = await attestVerdict(record, chain.operations);
  assert.deepEqual(await attestVerdict({ ...record }, chain.operations), {
    pda: first.pda, signature: null, reused: true,
  });
  assert.equal(chain.state.writes, 1);
});

test("equal timestamps reject conflicting content, including equivalent timestamp spellings", async () => {
  const chain = mockChain();
  const record = verdictRecord();
  await attestVerdict(record, chain.operations);
  for (const change of [
    { verdict: "partial" },
    { evidenceSha256: "b".repeat(64) },
    { claimSha256: "b".repeat(64) },
    { reviewerSig: "test-signature-b" },
    { issuedAt: "2026-09-26T03:00:00.000+00:00" },
  ]) {
    await assert.rejects(() => attestVerdict({ ...record, ...change }, chain.operations), AttestationConflictError);
  }
  assert.equal(chain.state.writes, 1);
});

test("malformed persisted timestamps fail closed, including parseable non-ISO and invalid dates", async () => {
  const chain = mockChain();
  const record = verdictRecord();
  const { pda } = await attestVerdict(record, chain.operations);
  for (const issuedAt of ["not-a-date", "", "0", "2026-02-30T03:00:00Z", "undefined"]) {
    chain.seed(pda, { ...record, issuedAt });
    await assert.rejects(() => attestVerdict(record, chain.operations), /persisted issued_at is malformed/);
  }
  assert.equal(chain.state.writes, 1);
});

test("concurrent older update waits for newer write then rejects rollback", { timeout: 5000 }, async () => {
  const chain = mockChain();
  const started = deferred();
  const release = deferred();
  chain.state.beforeWrite = async () => { started.resolve(); await release.promise; };
  const newer = verdictRecord({ issuedAt: "2026-09-26T03:01:00Z", verdict: "partial" });
  const writing = attestVerdict(newer, chain.operations);
  await started.promise;
  const older = attestVerdict(verdictRecord(), chain.operations).then(() => null, (error) => error);
  release.resolve();
  const { pda } = await writing;
  assert.ok(await older instanceof AttestationConflictError);
  assert.deepEqual(await getAttestation(pda, chain.operations), newer);
  assert.equal(chain.state.writes, 1);
});

test("newer concurrent update cannot overtake a blocked older write", { timeout: 5000 }, async () => {
  const chain = mockChain();
  const started = deferred();
  const release = deferred();
  let writeCalls = 0;
  chain.state.beforeWrite = async () => {
    if (++writeCalls === 1) { started.resolve(); await release.promise; }
  };
  const older = attestVerdict(verdictRecord(), chain.operations);
  await started.promise;
  const record = verdictRecord({ issuedAt: "2026-09-26T03:01:00Z", verdict: "partial" });
  const newer = attestVerdict(record, chain.operations);
  try {
    await delay(50);
    assert.equal(chain.state.reads, 1);
    assert.equal(writeCalls, 1);
  } finally {
    release.resolve();
  }
  await older;
  const { pda } = await newer;
  assert.deepEqual(await getAttestation(pda, chain.operations), record);
  assert.equal(chain.state.writes, 2);
});

test("a blocked PDA does not block updates to other PDAs", { timeout: 5000 }, async () => {
  const chain = mockChain();
  const started = deferred();
  const release = deferred();
  let writeCalls = 0;
  chain.state.beforeWrite = async () => {
    if (++writeCalls === 1) { started.resolve(); await release.promise; }
  };
  const blocked = attestVerdict(verdictRecord(), chain.operations);
  await started.promise;
  try {
    const other = await attestVerdict(verdictRecord({ claimId: "claim-2" }), chain.operations);
    assert.equal(other.reused, false);
    assert.equal(chain.state.writes, 1);
  } finally {
    release.resolve();
  }
  await blocked;
  assert.equal(chain.state.writes, 2);
});

test("concurrent exact repeats perform only one write", { timeout: 5000 }, async () => {
  const chain = mockChain();
  const results = await Promise.all(Array.from({ length: 8 }, () => attestVerdict(verdictRecord(), chain.operations)));
  assert.equal(results.filter((result) => !result.reused).length, 1);
  assert.equal(chain.state.writes, 1);
});

test("locks release after RPC failures", async () => {
  const chain = mockChain();
  chain.state.failNextWrite = true;
  await assert.rejects(() => attestVerdict(verdictRecord(), chain.operations), /mock RPC failure/);
  const result = await attestVerdict(verdictRecord(), chain.operations);
  assert.equal(result.reused, false);
  assert.equal(chain.state.writes, 1);
});

test("same-PDA queue is bounded and excess requests fail without unlocking the active write", { timeout: 5000 }, async () => {
  const chain = mockChain();
  const started = deferred();
  const release = deferred();
  const rejected = deferred();
  chain.state.beforeWrite = async () => { started.resolve(); await release.promise; };
  const writing = attestVerdict(verdictRecord(), chain.operations);
  await started.promise;
  const pending = Array.from({ length: 32 }, () => attestVerdict(verdictRecord(), chain.operations).then(
    (result) => result,
    (error) => { rejected.resolve(); return error; },
  ));
  try {
    await rejected.promise;
    assert.equal(chain.state.reads, 1);
  } finally {
    release.resolve();
  }
  const results = await Promise.all(pending);
  await writing;
  assert.equal(results.filter((result) => result instanceof AttestationBusyError).length, 1);
  assert.equal(chain.state.writes, 1);
  assert.equal((await attestVerdict(verdictRecord(), chain.operations)).reused, true);
});

test("health state only reads and reports uninitialized credential/schema", async () => {
  const chain = mockChain();
  chain.state.credentialExists = false;
  chain.state.schemaExists = false;
  const state = await getInitializationState(chain.operations);
  assert.equal(state.credential_exists, false);
  assert.equal(state.schema_exists, false);
  assert.equal(state.balance_lamports, "42");
  assert.equal(chain.state.writes, 0);
  assert.equal(chain.state.credentialReads, 1);
  assert.equal(chain.state.schemaReads, 1);
});

test("lookup never provisions, whether missing or decoding an existing record", async () => {
  const chain = mockChain();
  chain.state.credentialExists = false;
  chain.state.schemaExists = false;
  assert.equal(await getAttestation(TEST_ADDRESS, chain.operations), null);
  chain.seed(TEST_ADDRESS, verdictRecord());
  await assert.rejects(() => getAttestation(TEST_ADDRESS, chain.operations), /not initialized/);
  chain.state.schemaExists = true;
  assert.deepEqual(await getAttestation(TEST_ADDRESS, chain.operations), verdictRecord());
  assert.equal(chain.state.credentialReads, 0);
  assert.equal(chain.state.writes, 0);
});
