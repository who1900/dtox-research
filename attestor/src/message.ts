import { createHash } from "node:crypto";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";

/** Verdicts the on-chain schema and the API's own /v1/adjudicate accept. */
export const VALID_VERDICTS = ["asserts", "does_not_assert", "partial"] as const;
export type Verdict = (typeof VALID_VERDICTS)[number];

export const DOMAIN_TAG = "dtox-claim-verdict:v2";

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

/** V2 preserves exact text: case, Unicode, operators and whitespace are data. */
export function normalizeClaimText(text: string): string {
  return text;
}

/** Historical verification only; never used to authorize a new write. */
export function legacyClaimTextSha256(text: string): string {
  return sha256Hex(text.toLowerCase().trim().replace(/\s+/g, " "));
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
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    throw new Error(`${name} must be between 1 and ${max} characters`);
  }
  if (/[\u0000-\u001f\u007f]/.test(value)) {
    throw new Error(`${name} must not contain control characters`);
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
  if (!Number.isFinite(parseIssuedAtTimestamp(fields.issuedAt))) {
    throw new Error("issued_at must be a valid ISO 8601 timestamp");
  }
}

export function parseIssuedAtTimestamp(value: string): number {
  if (typeof value !== "string") return NaN;
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  const timestamp = Date.parse(value);
  if (!match || !Number.isFinite(timestamp)) return NaN;
  const localIso = `${match[1]}.${(match[2] ?? "").slice(0, 3).padEnd(3, "0")}Z`;
  const localTimestamp = Date.parse(localIso);
  return Number.isFinite(localTimestamp) && new Date(localTimestamp).toISOString() === localIso ? timestamp : NaN;
}

/**
 * Builds the canonical, deterministic message an agent's wallet signs. Field
 * order and the domain separator are fixed: changing either would silently
 * invalidate every signature already recorded, so this is the one place that
 * defines the wire format.
 */
export function buildVerdictMessage(fields: VerdictFields): string {
  return verdictMessage(fields, DOMAIN_TAG);
}

/** Reconstruct existing v1 receipts for independent read-only verification. */
export function buildLegacyVerdictMessage(fields: VerdictFields): string {
  return verdictMessage(fields, "dtox-claim-verdict:v1");
}

function verdictMessage(fields: VerdictFields, domain: string): string {
  assertVerdictFields(fields);
  return [
    domain,
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
  const ts = parseIssuedAtTimestamp(issuedAt);
  if (!Number.isFinite(ts) || !Number.isFinite(now.getTime())) {
    throw new Error("issued_at and now must be valid ISO 8601 timestamps");
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
