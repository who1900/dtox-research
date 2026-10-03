import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import {
  DEVNET_NETWORK,
  DEVNET_USDC_MINT,
  RELEASE_CLAIM,
  RELEASE_PAPER_ID,
  RELEASE_PAY_TO,
  RELEASE_VERDICT,
  parseReleaseArgs,
  validateCanonicalMessage,
  validatePaymentRequired,
  validateServiceInfo
} from "../scripts/release-devnet.js";

const evidenceSha256 = "a".repeat(64);
const args = ["buyer.json", "https://read.whoim.space/x402/mcp", RELEASE_CLAIM,
  RELEASE_PAPER_ID, RELEASE_VERDICT, evidenceSha256];

test("release args are locked to the approved case and gateway endpoints", () => {
  assert.equal(parseReleaseArgs(args).evidenceSha256, evidenceSha256);
  assert.throws(() => parseReleaseArgs([...args.slice(0, 2), "other claim", ...args.slice(3)]));
  assert.throws(() => parseReleaseArgs([args[0], "https://attacker.example/x402/mcp", ...args.slice(2)]));
  assert.throws(() => parseReleaseArgs([args[0], "http://127.0.0.1:8012/mcp", ...args.slice(2)]));
  assert.throws(() => parseReleaseArgs([...args.slice(0, 5), "not-a-sha256"]));
  assert.doesNotThrow(() => validateServiceInfo({ service: "dtox research", mode: "live", networks: [DEVNET_NETWORK] }));
  assert.throws(() => validateServiceInfo({ service: "dtox research", mode: "shadow", networks: [DEVNET_NETWORK] }));
  assert.throws(() => validateServiceInfo({ service: "dtox research", mode: "live", networks: [DEVNET_NETWORK, "eip155:11155111"] }));
});

function challenge(amount: string, overrides: Record<string, unknown> = {}) {
  return {
    x402Version: 2,
    accepts: [{ scheme: "exact", network: DEVNET_NETWORK, asset: DEVNET_USDC_MINT,
      payTo: RELEASE_PAY_TO, amount, ...overrides }]
  };
}

test("payment challenge accepts only one exact devnet-USDC option at or below 10000 atomic", () => {
  assert.equal(validatePaymentRequired(challenge("10000")).accepts.length, 1);
  assert.equal(validatePaymentRequired(challenge("1")).accepts.length, 1);
  assert.throws(() => validatePaymentRequired(challenge("10001")));
  assert.throws(() => validatePaymentRequired(challenge("0")));
  assert.throws(() => validatePaymentRequired(challenge("10000", { network: "eip155:11155111" })));
  assert.throws(() => validatePaymentRequired(challenge("10000", { asset: "wrong-mint" })));
  assert.throws(() => validatePaymentRequired(challenge("10000", { payTo: "wrong-recipient" })));
  assert.throws(() => validatePaymentRequired({ ...challenge("10000"), accepts: [
    ...challenge("10000").accepts, ...challenge("10000").accepts
  ] }));
});

test("canonical message must bind the requested claim, evidence, case, and fresh timestamp", () => {
  const now = Date.now();
  const claimSha = "83ac";
  const normalized = RELEASE_CLAIM.toLowerCase().trim().replace(/\s+/g, " ");
  const claimHash = createHash("sha256").update(normalized, "utf8").digest("hex");
  const issuedAt = new Date(now).toISOString();
  const payload = {
    claim_id: claimSha,
    claim_sha256: claimHash,
    paper_id: RELEASE_PAPER_ID,
    verdict: RELEASE_VERDICT,
    evidence_sha256: evidenceSha256,
    issued_at: issuedAt,
    message: ["dtox-claim-verdict:v1", `claim_id:${claimSha}`, `claim_sha256:${claimHash}`,
      `paper_id:${RELEASE_PAPER_ID}`, `verdict:${RELEASE_VERDICT}`,
      `evidence_sha256:${evidenceSha256}`, `issued_at:${issuedAt}`].join("\n")
  };
  assert.equal(validateCanonicalMessage(payload, parseReleaseArgs(args), now).message, payload.message);
  assert.throws(() => validateCanonicalMessage({ ...payload, verdict: "asserts" }, parseReleaseArgs(args), now));
  assert.throws(() => validateCanonicalMessage({ ...payload, issued_at: "2000-01-01T00:00:00Z" }, parseReleaseArgs(args), now));
  assert.throws(() => validateCanonicalMessage({ ...payload, message: `${payload.message}\n` }, parseReleaseArgs(args), now));
});
