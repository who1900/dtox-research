// One-off helper (not part of the shipped CLI surface) to generate a solana-keygen
// format keypair file at an arbitrary path, for local/devnet testing only.
import { writeFileSync } from "node:fs";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";

const outPath = process.argv[2];
if (!outPath) {
  console.error("usage: tsx scripts/gen-keypair.ts <out.json>");
  process.exit(1);
}
const secretKey = ed.utils.randomSecretKey();
const publicKey = await ed.getPublicKeyAsync(secretKey);
const full = new Uint8Array(64);
full.set(secretKey, 0);
full.set(publicKey, 32);
writeFileSync(outPath, JSON.stringify(Array.from(full)));
console.log(bs58.encode(publicKey));
