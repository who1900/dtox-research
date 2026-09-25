/**
 * Demo / test CLI: signs a claim verdict with an agent keypair and prints the
 * JSON body ready to POST to /attest.
 *
 * Usage:
 *   tsx scripts/demo-sign.ts <keypair.json> <claim_id> <paper_id> <verdict> <claim_text> [evidence_sha256]
 *
 * If <keypair.json> does not exist, a fresh ed25519 keypair is generated and
 * written there (solana-keygen JSON array format), so repeated runs reuse the
 * same demo identity.
 */
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";
import { buildVerdictMessage, claimTextSha256, type Verdict } from "../src/message.js";

async function loadOrCreateKeypair(path: string): Promise<{ secretKey: Uint8Array; publicKey: Uint8Array }> {
  if (existsSync(path)) {
    const raw = JSON.parse(readFileSync(path, "utf8")) as number[];
    const secretKey64 = Uint8Array.from(raw);
    return { secretKey: secretKey64.slice(0, 32), publicKey: secretKey64.slice(32, 64) };
  }
  const secretKey = ed.utils.randomSecretKey();
  const publicKey = await ed.getPublicKeyAsync(secretKey);
  const full64 = new Uint8Array(64);
  full64.set(secretKey, 0);
  full64.set(publicKey, 32);
  writeFileSync(path, JSON.stringify(Array.from(full64)));
  return { secretKey, publicKey };
}

async function main() {
  const [keypairPath, claimId, paperId, verdict, claimText, evidenceSha256 = "-"] = process.argv.slice(2);
  if (!keypairPath || !claimId || !paperId || !verdict || !claimText) {
    console.error(
      "usage: demo-sign.ts <keypair.json> <claim_id> <paper_id> <asserts|does_not_assert|partial> <claim_text> [evidence_sha256]"
    );
    process.exit(1);
  }

  const { secretKey, publicKey } = await loadOrCreateKeypair(keypairPath);
  const reviewer = bs58.encode(publicKey);
  const claimSha256 = claimTextSha256(claimText);
  const issuedAt = new Date().toISOString();

  const message = buildVerdictMessage({
    claimId,
    claimSha256,
    paperId,
    verdict: verdict as Verdict,
    evidenceSha256,
    issuedAt,
  });

  const signatureBytes = await ed.signAsync(new TextEncoder().encode(message), secretKey);
  const signature = bs58.encode(signatureBytes);

  const body = {
    claim_id: claimId,
    claim_text: claimText,
    paper_id: paperId,
    verdict,
    evidence_sha256: evidenceSha256,
    reviewer,
    signature,
    issued_at: issuedAt,
  };

  console.log(JSON.stringify(body, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
