/**
 * Idempotently creates the recipient's USDC associated token account (ATA) on Solana devnet.
 *
 * Why this exists: the x402 SVM "exact" scheme's client just builds a plain SPL
 * TransferChecked instruction straight to the recipient's ATA (see
 * @x402/svm/dist/cjs/exact/client/index.js) -- it never creates that account. If the
 * gateway's X402_SVM_PAY_TO wallet has never held USDC before, its ATA does not exist yet
 * and the very first paid tool call will fail on-chain with an "account not found" error,
 * even though the default x402.org facilitator sponsors the transaction fee (feePayer in
 * its /supported response) and the buyer never needs SOL. Someone -- typically the
 * recipient/operator, not a buyer -- has to pre-create that ATA once.
 *
 * Uses getCreateAssociatedTokenIdempotentInstruction, so re-running this script against an
 * ATA that already exists is a harmless no-op (the instruction succeeds either way).
 *
 * Usage:
 *   npx tsx scripts/ensure-recipient-ata.ts <payer-keypair.json> [recipient-address] [mint]
 *
 * <payer-keypair.json>  solana-keygen JSON keypair (64-byte array). Pays the devnet
 *                       transaction fee and the ATA's rent-exempt deposit. This is an
 *                       admin/operator action, run once per recipient wallet -- it is
 *                       never something a buyer needs to do.
 * [recipient-address]   Wallet to create the ATA for. Defaults to X402_SVM_PAY_TO from the
 *                       environment, or to the payer's own address if neither is given.
 * [mint]                SPL token mint. Defaults to the devnet USDC mint
 *                       4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU.
 *
 * Network: Solana devnet only (SOLANA_RPC_URL may override the RPC endpoint, but this
 * script does not touch mainnet and does not accept a mainnet override).
 */
import { readFileSync } from "node:fs";
import {
  address,
  createKeyPairSignerFromBytes,
  createSolanaRpc,
  createTransactionMessage,
  getSignatureFromTransaction,
  pipe,
  sendAndConfirmTransactionFactory,
  createSolanaRpcSubscriptions,
  setTransactionMessageFeePayerSigner,
  setTransactionMessageLifetimeUsingBlockhash,
  signTransactionMessageWithSigners,
  appendTransactionMessageInstructions
} from "@solana/kit";
import { findAssociatedTokenPda, getCreateAssociatedTokenIdempotentInstructionAsync, TOKEN_PROGRAM_ADDRESS } from "@solana-program/token";

const DEVNET_USDC_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU";
const DEVNET_RPC = process.env.SOLANA_RPC_URL ?? "https://api.devnet.solana.com";
const DEVNET_WS = DEVNET_RPC.replace(/^http/, "ws");
const DEVNET_GENESIS_HASH = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";

async function assertDevnet(rpc: ReturnType<typeof createSolanaRpc>): Promise<void> {
  const genesisHash = await rpc.getGenesisHash().send();
  if (genesisHash !== DEVNET_GENESIS_HASH) {
    throw new Error(
      `refusing to run against a non-devnet RPC (genesis hash ${genesisHash} != devnet's ${DEVNET_GENESIS_HASH}). ` +
      "This script is devnet-only."
    );
  }
}

async function main(): Promise<void> {
  const [keypairPath, recipientArg, mintArg] = process.argv.slice(2);
  if (!keypairPath) {
    console.error("usage: ensure-recipient-ata.ts <payer-keypair.json> [recipient-address] [mint]");
    process.exit(1);
  }

  const secretKeyBytes = Uint8Array.from(JSON.parse(readFileSync(keypairPath, "utf8")) as number[]);
  const payer = await createKeyPairSignerFromBytes(secretKeyBytes);

  const recipient = address(recipientArg ?? process.env.X402_SVM_PAY_TO ?? payer.address);
  const mint = address(mintArg ?? DEVNET_USDC_MINT);

  const rpc = createSolanaRpc(DEVNET_RPC);
  await assertDevnet(rpc);

  const [ata] = await findAssociatedTokenPda({ mint, owner: recipient, tokenProgram: TOKEN_PROGRAM_ADDRESS });

  const existing = await rpc.getAccountInfo(ata, { encoding: "base64" }).send();
  if (existing.value) {
    console.log(JSON.stringify({ status: "already_exists", ata: ata.toString(), owner: recipient.toString(), mint: mint.toString() }, null, 2));
    return;
  }

  const instruction = await getCreateAssociatedTokenIdempotentInstructionAsync({
    payer,
    owner: recipient,
    mint
  });

  const { value: latestBlockhash } = await rpc.getLatestBlockhash({ commitment: "confirmed" }).send();

  const transactionMessage = pipe(
    createTransactionMessage({ version: 0 }),
    (tx) => setTransactionMessageFeePayerSigner(payer, tx),
    (tx) => setTransactionMessageLifetimeUsingBlockhash(latestBlockhash, tx),
    (tx) => appendTransactionMessageInstructions([instruction], tx)
  );

  const signedTransaction = await signTransactionMessageWithSigners(transactionMessage);
  const rpcSubscriptions = createSolanaRpcSubscriptions(DEVNET_WS);
  const sendAndConfirm = sendAndConfirmTransactionFactory({ rpc, rpcSubscriptions });
  await sendAndConfirm(signedTransaction as Parameters<typeof sendAndConfirm>[0], { commitment: "confirmed" });

  console.log(JSON.stringify({
    status: "created",
    ata: ata.toString(),
    owner: recipient.toString(),
    mint: mint.toString(),
    signature: getSignatureFromTransaction(signedTransaction),
    explorer_url: `https://explorer.solana.com/tx/${getSignatureFromTransaction(signedTransaction)}?cluster=devnet`
  }, null, 2));
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
