import assert from "node:assert/strict";
import test from "node:test";
import { assertDevnetGenesis, deriveVerdictNonce, DEVNET_GENESIS_HASH, FIELD_LAYOUT, FIELD_NAMES } from "../src/sas.js";

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
