import assert from "node:assert/strict";
import test from "node:test";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";
import {
  buildVerdictMessage,
  checkIssuedAtWindow,
  claimTextSha256,
  normalizeClaimText,
  sha256Hex,
  verifyVerdictSignature,
  type VerdictFields,
} from "../src/message.js";

const baseFields = (): VerdictFields => ({
  claimId: "claim-1",
  claimSha256: claimTextSha256("Reed-Solomon codes achieve the Singleton bound"),
  paperId: "2401.12345",
  verdict: "asserts",
  evidenceSha256: "-",
  issuedAt: "2026-09-26T03:00:00Z",
});

test("normalizeClaimText lowercases, trims and collapses whitespace", () => {
  assert.equal(normalizeClaimText("  Foo   BAR\n\tBaz  "), "foo bar baz");
});

test("claimTextSha256 is stable across cosmetic differences", () => {
  const a = claimTextSha256("The protocol IS   byzantine fault tolerant.");
  const b = claimTextSha256("the protocol is byzantine fault tolerant.  ");
  assert.equal(a, b);
});

test("sha256Hex matches known RFC test vectors", () => {
  assert.equal(sha256Hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(sha256Hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
});

test("buildVerdictMessage produces the fixed canonical layout", () => {
  const fields = baseFields();
  const message = buildVerdictMessage(fields);
  assert.equal(
    message,
    [
      "dtox-claim-verdict:v1",
      `claim_id:${fields.claimId}`,
      `claim_sha256:${fields.claimSha256}`,
      `paper_id:${fields.paperId}`,
      `verdict:${fields.verdict}`,
      `evidence_sha256:${fields.evidenceSha256}`,
      `issued_at:${fields.issuedAt}`,
    ].join("\n")
  );
});

test("buildVerdictMessage rejects an invalid verdict", () => {
  const fields = baseFields();
  // @ts-expect-error intentionally invalid at the type level too
  fields.verdict = "definitely_true";
  assert.throws(() => buildVerdictMessage(fields), /verdict must be one of/);
});

test("buildVerdictMessage rejects a malformed claim_sha256", () => {
  const fields = baseFields();
  fields.claimSha256 = "not-hex";
  assert.throws(() => buildVerdictMessage(fields), /lowercase hex|32-byte/);
});

test("checkIssuedAtWindow accepts a fresh timestamp", () => {
  const now = new Date("2026-09-26T03:05:00Z");
  assert.doesNotThrow(() => checkIssuedAtWindow(new Date("2026-09-26T03:00:00Z").toISOString(), now));
});

test("checkIssuedAtWindow rejects a timestamp older than 10 minutes", () => {
  const now = new Date("2026-09-26T03:20:01Z");
  assert.throws(() => checkIssuedAtWindow(new Date("2026-09-26T03:00:00Z").toISOString(), now), /too old/);
});

test("checkIssuedAtWindow rejects a timestamp more than 1 minute in the future", () => {
  const now = new Date("2026-09-26T03:00:00Z");
  assert.throws(() => checkIssuedAtWindow(new Date("2026-09-26T03:01:30Z").toISOString(), now), /future/);
});

test("checkIssuedAtWindow allows small future clock skew", () => {
  const now = new Date("2026-09-26T03:00:00Z");
  assert.doesNotThrow(() => checkIssuedAtWindow(new Date("2026-09-26T03:00:30Z").toISOString(), now));
});

async function makeSignedMessage(fields: VerdictFields) {
  const secretKey = ed.utils.randomSecretKey();
  const publicKey = await ed.getPublicKeyAsync(secretKey);
  const message = buildVerdictMessage(fields);
  const signature = await ed.signAsync(new TextEncoder().encode(message), secretKey);
  return { message, signature: bs58.encode(signature), pubkey: bs58.encode(publicKey) };
}

test("verifyVerdictSignature accepts a genuine signature", async () => {
  const fields = baseFields();
  const { message, signature, pubkey } = await makeSignedMessage(fields);
  assert.equal(await verifyVerdictSignature(message, signature, pubkey), true);
});

test("verifyVerdictSignature rejects a signature after the verdict is swapped", async () => {
  const fields = baseFields();
  const { signature, pubkey } = await makeSignedMessage(fields);
  const tamperedMessage = buildVerdictMessage({ ...fields, verdict: "does_not_assert" });
  assert.equal(await verifyVerdictSignature(tamperedMessage, signature, pubkey), false);
});

test("verifyVerdictSignature rejects a signature from a different key", async () => {
  const fields = baseFields();
  const { message, signature } = await makeSignedMessage(fields);
  const otherSecret = ed.utils.randomSecretKey();
  const otherPublic = bs58.encode(await ed.getPublicKeyAsync(otherSecret));
  assert.equal(await verifyVerdictSignature(message, signature, otherPublic), false);
});

test("verifyVerdictSignature returns false (not throw) on malformed base58", () => {
  return verifyVerdictSignature("anything", "not-base58!!", "also-not-base58!!").then((result) => {
    assert.equal(result, false);
  });
});
