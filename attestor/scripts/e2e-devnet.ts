// Manual devnet smoke test (not part of CI): airdrops SOL to the authority,
// creates credential+schema, attests one verdict from a fresh reviewer
// keypair, and reads it back. Run with ATTESTOR_KEYPAIR_PATH pointing at a
// throwaway devnet keypair OUTSIDE the repo.
import * as ed from "@noble/ed25519";
import bs58 from "bs58";
import { buildVerdictMessage, claimTextSha256 } from "../src/message.js";
import { attestVerdict, getAttestation, requestDevnetAirdrop, getChainContext, explorerUrl } from "../src/sas.js";
import { readFileSync } from "node:fs";

async function main() {
  const reviewerPath = process.argv[2];
  if (!reviewerPath) {
    console.error("usage: tsx scripts/e2e-devnet.ts <reviewer-keypair.json>");
    process.exit(1);
  }
  const ctx = await getChainContext();
  console.log("authority:", ctx.authority.address);

  const { value: balanceBefore } = await ctx.rpc.getBalance(ctx.authority.address).send();
  console.log("balance before:", balanceBefore.toString());
  if (balanceBefore < 500_000_000n) {
    console.log("requesting devnet airdrop of 1 SOL...");
    const sig = await requestDevnetAirdrop(1);
    console.log("airdrop tx:", explorerUrl("tx", sig));
  }

  const reviewerRaw = JSON.parse(readFileSync(reviewerPath, "utf8")) as number[];
  const reviewerSecret = Uint8Array.from(reviewerRaw).slice(0, 32);
  const reviewerPublic = Uint8Array.from(reviewerRaw).slice(32, 64);
  const reviewer = bs58.encode(reviewerPublic);

  const claimId = "e2e-demo-claim-1";
  const paperId = "2401.00000";
  const claimText = "This paper proves the protocol is byzantine fault tolerant under partial synchrony.";
  const claimSha256 = claimTextSha256(claimText);
  const issuedAt = new Date().toISOString();

  const message = buildVerdictMessage({
    claimId,
    claimSha256,
    paperId,
    verdict: "asserts",
    evidenceSha256: "-",
    issuedAt,
  });
  const signature = bs58.encode(await ed.signAsync(new TextEncoder().encode(message), reviewerSecret));

  console.log("attesting...");
  const result = await attestVerdict({
    claimId,
    claimSha256,
    paperId,
    verdict: "asserts",
    evidenceSha256: "-",
    reviewer,
    reviewerSig: signature,
    issuedAt,
  });
  console.log("attestation PDA:", result.pda, explorerUrl("address", result.pda));
  if (result.signature) console.log("tx:", explorerUrl("tx", result.signature));
  console.log("reused:", result.reused);

  const readBack = await getAttestation(result.pda);
  console.log("read back:", JSON.stringify(readBack, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
