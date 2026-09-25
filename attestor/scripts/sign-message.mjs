// usage: node sign.mjs <keypair.json> <message-file>  -> {"reviewer","signature"}
import { readFileSync, existsSync, writeFileSync } from "node:fs";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";
const [kp, msgFile] = process.argv.slice(2);
let full;
if (existsSync(kp)) full = Uint8Array.from(JSON.parse(readFileSync(kp, "utf8")));
else {
  const sk = ed.utils.randomSecretKey(); const pk = await ed.getPublicKeyAsync(sk);
  full = new Uint8Array(64); full.set(sk, 0); full.set(pk, 32);
  writeFileSync(kp, JSON.stringify(Array.from(full)), { mode: 0o600 });
}
const sig = await ed.signAsync(new TextEncoder().encode(readFileSync(msgFile, "utf8")), full.slice(0, 32));
console.log(JSON.stringify({ reviewer: bs58.encode(full.slice(32)), signature: bs58.encode(sig) }));
