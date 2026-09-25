import { createHash } from "node:crypto";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";

/** Verdicts the on-chain schema and the API's own /v1/adjudicate accept. */
export const VALID_VERDICTS = ["asserts", "does_not_assert", "partial"] as const;
export type Verdict = (typeof VALID_VERDICTS)[number];

export const DOMAIN_TAG = "dtox-claim-verdict:v1";

/** Anti-replay window: an issued_at older than this, or more than 1 minute in the future, is rejected. */
export const MAX_AGE_MS = 10 * 60 * 1000;
export const MAX_FUTURE_SKEW_MS = 60 * 1000;

const MAX_ID_LEN = 200;
const HEX_RE = /^[0-9a-f]+$/;

export interface VerdictFields {
  claimId: string;
  claimSha256: string;
  paperId: string;
  verdict: Verdict;
  /** Hex sha256 of supporting evidence text, or "-" when there is none. */
  evidenceSha256: string;
  /** ISO 8601 UTC timestamp, e.g. 2026-09-26T03:10:00Z or with an explicit +00:00 offset. */
  issuedAt: string;
}

/**
 * Normalizes claim text before hashing: lowercase, trim, collapse runs of
 * whitespace to a single space. This keeps `claim_sha256` stable across
 * cosmetic differences (extra spaces, trailing newlines, case) that do not
 * change what is being asserted, while still binding the signature to the
 * actual claim text rather than to a mutable claim_id.
 */
export function normalizeClaimText(text: string): string {
  return text.toLowerCase().trim().replace(/\s+/g, " ");
}

export function sha256Hex(input: string | Uint8Array): string {
  const hash = createHash("sha256");
  hash.update(typeof input === "string" ? Buffer.from(input, "utf8") : input);
  return hash.digest("hex");
}

export function claimTextSha256(claimText: string): string {
  return sha256Hex(normalizeClaimText(claimText));
}

function assertFieldLength(name: string, value: string, max: number): void {
  if (value.length === 0 || value.length > max) {
    throw new Error(`${name} must be between 1 and ${max} characters`);
  }
}

function assertHex(name: string, value: string): void {
  if (!HEX_RE.test(value)) {
    throw new Error(`${name} must be lowercase hex`);
  }
}

/** Validates field shapes and lengths before a message is ever built or signed. */
export function assertVerdictFields(fields: VerdictFields): void {
  assertFieldLength("claim_id", fields.claimId, MAX_ID_LEN);
  assertFieldLength("paper_id", fields.paperId, MAX_ID_LEN);
  assertHex("claim_sha256", fields.claimSha256);
  if (fields.claimSha256.length !== 64) {
    throw new Error("claim_sha256 must be a 32-byte sha256 hex digest");
  }
  if (!VALID_VERDICTS.includes(fields.verdict)) {
    throw new Error(`verdict must be one of ${VALID_VERDICTS.join(", ")}`);
  }
  if (fields.evidenceSha256 !== "-") {
    assertHex("evidence_sha256", fields.evidenceSha256);
    if (fields.evidenceSha256.length !== 64) {
      throw new Error("evidence_sha256 must be a 32-byte sha256 hex digest or '-'");
    }
  }
  if (Number.isNaN(Date.parse(fields.issuedAt))) {
    throw new Error("issued_at must be a parseable ISO 8601 timestamp");
  }
}

/**
 * Builds the canonical, deterministic message an agent's wallet signs. Field
 * order and the domain separator are fixed: changing either would silently
 * invalidate every signature already recorded, so this is the one place that
 * defines the wire format.
 */
export function buildVerdictMessage(fields: VerdictFields): string {
  assertVerdictFields(fields);
  return [
    DOMAIN_TAG,
    `claim_id:${fields.claimId}`,
    `claim_sha256:${fields.claimSha256}`,
    `paper_id:${fields.paperId}`,
    `verdict:${fields.verdict}`,
    `evidence_sha256:${fields.evidenceSha256}`,
    `issued_at:${fields.issuedAt}`,
  ].join("\n");
}

/** Rejects timestamps outside the anti-replay window, relative to `now`. */
export function checkIssuedAtWindow(issuedAt: string, now: Date = new Date()): void {
  const ts = Date.parse(issuedAt);
  if (Number.isNaN(ts)) {
    throw new Error("issued_at must be a parseable ISO 8601 timestamp");
  }
  const delta = now.getTime() - ts;
  if (delta > MAX_AGE_MS) {
    throw new Error("issued_at is too old (replay window is 10 minutes)");
  }
  if (delta < -MAX_FUTURE_SKEW_MS) {
    throw new Error("issued_at is in the future");
  }
}

/**
 * Verifies an ed25519 signature over a canonical message. Returns false on
 * any malformed input (bad base58, wrong length) rather than throwing, since
 * callers treat "invalid" and "malformed" the same way: reject the request.
 */
export async function verifyVerdictSignature(
  message: string,
  signatureBase58: string,
  pubkeyBase58: string
): Promise<boolean> {
  try {
    const signature = bs58.decode(signatureBase58);
    const publicKey = bs58.decode(pubkeyBase58);
    if (signature.length !== 64 || publicKey.length !== 32) return false;
    const messageBytes = new TextEncoder().encode(message);
    return await ed.verifyAsync(signature, messageBytes, publicKey);
  } catch {
    return false;
  }
}
