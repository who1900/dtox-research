import { readFileSync } from "node:fs";
import {
  address,
  airdropFactory,
  createSolanaRpc,
  createSolanaRpcSubscriptions,
  createTransactionMessage,
  appendTransactionMessageInstructions,
  setTransactionMessageFeePayerSigner,
  setTransactionMessageLifetimeUsingBlockhash,
  signTransactionMessageWithSigners,
  sendAndConfirmTransactionFactory,
  getSignatureFromTransaction,
  createKeyPairSignerFromBytes,
  lamports,
  type Address,
  type KeyPairSigner,
  type Rpc,
  type SolanaRpcApi,
  type RpcSubscriptions,
  type SolanaRpcSubscriptionsApi,
  type Instruction,
  assertIsTransactionWithBlockhashLifetime,
} from "@solana/kit";
import {
  SOLANA_ATTESTATION_SERVICE_PROGRAM_ADDRESS,
  deriveCredentialPda,
  deriveSchemaPda,
  deriveAttestationPda,
  getCreateCredentialInstruction,
  getCreateSchemaInstruction,
  getCreateAttestationInstruction,
  getCloseAttestationInstruction,
  fetchMaybeCredential,
  fetchMaybeSchema,
  fetchMaybeAttestation,
  fetchSchema,
  serializeAttestationData,
  deserializeAttestationData,
} from "sas-lib";
import bs58 from "bs58";
import { sha256Hex } from "./message.js";

export const CREDENTIAL_NAME = "dtox-research";
export const SCHEMA_NAME = "claim_verdict_v1";
export const SCHEMA_VERSION = 1;

/** Field order fixed by the schema. Every field is a Borsh string (layout code 12). */
export const FIELD_NAMES = [
  "claim_id",
  "claim_sha256",
  "paper_id",
  "verdict",
  "evidence_sha256",
  "reviewer",
  "reviewer_sig",
  "issued_at",
] as const;

const BORSH_STRING_LAYOUT_CODE = 12;
export const FIELD_LAYOUT = new Uint8Array(FIELD_NAMES.length).fill(BORSH_STRING_LAYOUT_CODE);

const SCHEMA_DESCRIPTION =
  "dtox research claim verdict, ed25519-signed by the reviewer wallet named in the record";

export interface AttestationRecord {
  claimId: string;
  claimSha256: string;
  paperId: string;
  verdict: string;
  evidenceSha256: string;
  reviewer: string;
  reviewerSig: string;
  issuedAt: string;
}

interface ChainContext {
  rpc: Rpc<SolanaRpcApi>;
  rpcSubscriptions: RpcSubscriptions<SolanaRpcSubscriptionsApi>;
  authority: KeyPairSigner;
}

let cached: ChainContext | undefined;

function toWsUrl(httpUrl: string): string {
  const explicit = process.env.SOLANA_WS_URL;
  if (explicit) return explicit;
  return httpUrl.replace(/^http/, "ws");
}

function isMainnetUrl(url: string): boolean {
  return /mainnet/i.test(url);
}

/** Genesis hash of Solana devnet. Networks cannot lie about this the way a URL string can. */
export const DEVNET_GENESIS_HASH = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG";

/**
 * Confirms the RPC endpoint is actually devnet by asking it for its genesis
 * hash, rather than pattern-matching the URL (an RPC provider's URL for
 * mainnet does not have to contain the word "mainnet" at all — this is the
 * check that cannot be fooled by a URL). Skipped entirely when
 * ATTESTOR_ALLOW_MAINNET=1 is set.
 */
export async function assertDevnetGenesis(rpc: Pick<Rpc<SolanaRpcApi>, "getGenesisHash">): Promise<void> {
  if (process.env.ATTESTOR_ALLOW_MAINNET === "1") return;
  const genesisHash = await rpc.getGenesisHash().send();
  if (genesisHash !== DEVNET_GENESIS_HASH) {
    throw new Error(
      `refusing to start: RPC genesis hash (${genesisHash}) does not match Solana devnet ` +
        `(${DEVNET_GENESIS_HASH}). Set ATTESTOR_ALLOW_MAINNET=1 to override (not recommended).`
    );
  }
}

export function loadAuthorityFromFile(path: string): Promise<KeyPairSigner> {
  const raw = JSON.parse(readFileSync(path, "utf8"));
  if (!Array.isArray(raw)) {
    throw new Error(`${path} must contain a JSON array (solana-keygen format)`);
  }
  return createKeyPairSignerFromBytes(Uint8Array.from(raw as number[]));
}

/**
 * Builds (or returns the cached) RPC + authority context. Refuses to start
 * against anything that looks like mainnet unless explicitly overridden,
 * since this service is devnet-only by design.
 */
export async function getChainContext(): Promise<ChainContext> {
  if (cached) return cached;

  const rpcUrl = process.env.SOLANA_RPC_URL ?? "https://api.devnet.solana.com";

  const keypairPath = process.env.ATTESTOR_KEYPAIR_PATH;
  if (!keypairPath) {
    throw new Error("ATTESTOR_KEYPAIR_PATH is required (solana-keygen JSON keypair file)");
  }

  const rpc = createSolanaRpc(rpcUrl);
  const rpcSubscriptions = createSolanaRpcSubscriptions(toWsUrl(rpcUrl));
  await assertDevnetGenesis(rpc);
  const authority = await loadAuthorityFromFile(keypairPath);

  cached = { rpc, rpcSubscriptions, authority };
  return cached;
}

/** Test/CLI helper: reset the cached context so a fresh keypair/RPC can be used. */
export function resetChainContext(): void {
  cached = undefined;
}

async function sendInstructions(ctx: ChainContext, instructions: readonly Instruction[]) {
  const { rpc, rpcSubscriptions, authority } = ctx;
  const { value: latestBlockhash } = await rpc.getLatestBlockhash().send();
  const sendAndConfirm = sendAndConfirmTransactionFactory({ rpc, rpcSubscriptions });

  const withFeePayer = setTransactionMessageFeePayerSigner(authority, createTransactionMessage({ version: 0 }));
  const withLifetime = setTransactionMessageLifetimeUsingBlockhash(latestBlockhash, withFeePayer);
  const transactionMessage = appendTransactionMessageInstructions(instructions, withLifetime);

  const signedTransaction = await signTransactionMessageWithSigners(transactionMessage);
  assertIsTransactionWithBlockhashLifetime(signedTransaction);
  await sendAndConfirm(signedTransaction, { commitment: "confirmed" });
  return getSignatureFromTransaction(signedTransaction);
}

const chainOperations = {
  getChainContext,
  fetchMaybeCredential,
  fetchMaybeSchema,
  fetchMaybeAttestation,
  fetchSchema,
  sendInstructions,
};

export class AttestationConflictError extends Error {}
export class AttestationBusyError extends Error {}

const MAX_LOCKED_PDAS = 256;
const MAX_PENDING_PER_PDA = 32;
const READ_CONFIG = { commitment: "confirmed" } as const;
const attestationLocks = new Map<string, { tail: Promise<void>; pending: number }>();

async function withAttestationLock<T>(pda: Address, run: () => Promise<T>): Promise<T> {
  let lock = attestationLocks.get(pda);
  if (!lock) {
    if (attestationLocks.size >= MAX_LOCKED_PDAS) {
      throw new AttestationBusyError("too many attestation updates in flight");
    }
    lock = { tail: Promise.resolve(), pending: 0 };
    attestationLocks.set(pda, lock);
  }
  if (lock.pending >= MAX_PENDING_PER_PDA) {
    throw new AttestationBusyError("too many pending updates for this attestation");
  }
  const previous = lock.tail;
  let release!: () => void;
  lock.tail = new Promise<void>((resolve) => { release = resolve; });
  lock.pending++;
  try {
    await previous;
    return await run();
  } finally {
    release();
    if (--lock.pending === 0) attestationLocks.delete(pda);
  }
}

export function parseIssuedAtTimestamp(value: string): number {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  const timestamp = Date.parse(value);
  if (!match || !Number.isFinite(timestamp)) return NaN;
  const localIso = `${match[1]}.${(match[2] ?? "").slice(0, 3).padEnd(3, "0")}Z`;
  const localTimestamp = Date.parse(localIso);
  return Number.isFinite(localTimestamp) && new Date(localTimestamp).toISOString() === localIso ? timestamp : NaN;
}

export async function requestDevnetAirdrop(solAmount = 1): Promise<string> {
  const ctx = await getChainContext();
  const rpcUrl = process.env.SOLANA_RPC_URL ?? "https://api.devnet.solana.com";
  if (isMainnetUrl(rpcUrl)) throw new Error("refusing to airdrop against a mainnet-looking RPC");
  const airdrop = airdropFactory({ rpc: ctx.rpc, rpcSubscriptions: ctx.rpcSubscriptions });
  const signature = await airdrop({
    commitment: "confirmed",
    recipientAddress: ctx.authority.address,
    lamports: lamports(BigInt(Math.round(solAmount * 1_000_000_000))),
  });
  return signature;
}

export async function deriveCredentialAddress(authorityAddress: Address): Promise<Address> {
  const [pda] = await deriveCredentialPda({ authority: authorityAddress, name: CREDENTIAL_NAME });
  return pda;
}

export async function deriveSchemaAddress(credential: Address): Promise<Address> {
  const [pda] = await deriveSchemaPda({ credential, name: SCHEMA_NAME, version: SCHEMA_VERSION });
  return pda;
}

/** Idempotent: returns the existing Credential PDA if one is already there. */
export async function ensureCredential(operations = chainOperations): Promise<Address> {
  const ctx = await operations.getChainContext();
  const credentialAddress = await deriveCredentialAddress(ctx.authority.address);

  const maybe = await operations.fetchMaybeCredential(ctx.rpc, credentialAddress, READ_CONFIG);
  if (maybe.exists) return credentialAddress;

  const instruction = getCreateCredentialInstruction({
    payer: ctx.authority,
    credential: credentialAddress,
    authority: ctx.authority,
    name: CREDENTIAL_NAME,
    signers: [ctx.authority.address],
  });
  await operations.sendInstructions(ctx, [instruction]);
  return credentialAddress;
}

/** Idempotent: returns the existing Schema PDA if one is already there. */
export async function ensureSchema(credentialAddress: Address, operations = chainOperations): Promise<Address> {
  const ctx = await operations.getChainContext();
  const schemaAddress = await deriveSchemaAddress(credentialAddress);

  const maybe = await operations.fetchMaybeSchema(ctx.rpc, schemaAddress, READ_CONFIG);
  if (maybe.exists) return schemaAddress;

  const instruction = getCreateSchemaInstruction({
    payer: ctx.authority,
    authority: ctx.authority,
    credential: credentialAddress,
    schema: schemaAddress,
    name: SCHEMA_NAME,
    description: SCHEMA_DESCRIPTION,
    layout: FIELD_LAYOUT,
    fieldNames: [...FIELD_NAMES],
  });
  await operations.sendInstructions(ctx, [instruction]);
  return schemaAddress;
}

export async function getInitializationState(operations = chainOperations) {
  const ctx = await operations.getChainContext();
  const credential = await deriveCredentialAddress(ctx.authority.address);
  const schema = await deriveSchemaAddress(credential);
  const [credentialAccount, schemaAccount, { value: balance }] = await Promise.all([
    operations.fetchMaybeCredential(ctx.rpc, credential, READ_CONFIG),
    operations.fetchMaybeSchema(ctx.rpc, schema, READ_CONFIG),
    ctx.rpc.getBalance(ctx.authority.address, READ_CONFIG).send(),
  ]);
  return {
    authority: ctx.authority.address,
    credential,
    schema,
    credential_exists: credentialAccount.exists,
    schema_exists: schemaAccount.exists,
    balance_lamports: balance.toString(),
  };
}

/** One attestation per (claim, paper, reviewer): nonce is derived, never random. */
export function deriveVerdictNonce(claimId: string, paperId: string, reviewer: string): Address {
  const digestHex = sha256Hex(`dtox:v1:${claimId}|${paperId}|${reviewer}`);
  const digestBytes = Uint8Array.from(Buffer.from(digestHex, "hex"));
  return address(bs58.encode(digestBytes));
}

function recordToDataObject(record: AttestationRecord): Record<string, unknown> {
  return {
    claim_id: record.claimId,
    claim_sha256: record.claimSha256,
    paper_id: record.paperId,
    verdict: record.verdict,
    evidence_sha256: record.evidenceSha256,
    reviewer: record.reviewer,
    reviewer_sig: record.reviewerSig,
    issued_at: record.issuedAt,
  };
}

function dataObjectToRecord(obj: Record<string, unknown>): AttestationRecord {
  return {
    claimId: String(obj.claim_id),
    claimSha256: String(obj.claim_sha256),
    paperId: String(obj.paper_id),
    verdict: String(obj.verdict),
    evidenceSha256: String(obj.evidence_sha256),
    reviewer: String(obj.reviewer),
    reviewerSig: String(obj.reviewer_sig),
    issuedAt: String(obj.issued_at),
  };
}

function sameRecord(a: AttestationRecord, b: AttestationRecord): boolean {
  return (
    a.claimId === b.claimId &&
    a.claimSha256 === b.claimSha256 &&
    a.paperId === b.paperId &&
    a.verdict === b.verdict &&
    a.evidenceSha256 === b.evidenceSha256 &&
    a.reviewer === b.reviewer &&
    a.reviewerSig === b.reviewerSig &&
    a.issuedAt === b.issuedAt
  );
}

export interface AttestResult {
  pda: Address;
  signature: string | null;
  reused: boolean;
}

/**
 * Attests a verdict. Idempotent on (claim_id, paper_id, reviewer): a repeat
 * of the exact same record returns the existing attestation untouched. A
 * strictly newer verdict for the same triple closes the old attestation and creates
 * a fresh one in the same transaction, so there is never more than one live
 * attestation per (claim, paper, reviewer).
 */
export async function attestVerdict(record: AttestationRecord, operations = chainOperations): Promise<AttestResult> {
  const issuedAt = parseIssuedAtTimestamp(record.issuedAt);
  if (!Number.isFinite(issuedAt)) throw new Error("issued_at must be a valid ISO 8601 timestamp");
  const ctx = await operations.getChainContext();
  const credential = await deriveCredentialAddress(ctx.authority.address);
  const schemaAddress = await deriveSchemaAddress(credential);
  const nonce = deriveVerdictNonce(record.claimId, record.paperId, record.reviewer);
  const [attestationPda] = await deriveAttestationPda({ credential, schema: schemaAddress, nonce });

  return withAttestationLock(attestationPda, async () => {
    // Process-local only; never expire an active write and let another one overlap.
    await ensureCredential(operations);
    await ensureSchema(credential, operations);
    const schemaAccount = await operations.fetchSchema(ctx.rpc, schemaAddress, READ_CONFIG);
    const existing = await operations.fetchMaybeAttestation(ctx.rpc, attestationPda, READ_CONFIG);
    const instructions: Instruction[] = [];
    if (existing.exists) {
      const existingRecord = dataObjectToRecord(
        deserializeAttestationData<Record<string, unknown>>(schemaAccount.data, Uint8Array.from(existing.data.data))
      );
      const existingIssuedAt = parseIssuedAtTimestamp(existingRecord.issuedAt);
      if (!Number.isFinite(existingIssuedAt)) {
        throw new AttestationConflictError("persisted issued_at is malformed; refusing to replace attestation");
      }
      if (sameRecord(existingRecord, record)) {
        return { pda: attestationPda, signature: null, reused: true };
      }
      if (issuedAt <= existingIssuedAt) {
        throw new AttestationConflictError("verdict is older than or conflicts with the current attestation");
      }
      instructions.push(getCloseAttestationInstruction({
        payer: ctx.authority,
        authority: ctx.authority,
        credential,
        attestation: attestationPda,
      }));
    }
    instructions.push(getCreateAttestationInstruction({
      payer: ctx.authority,
      authority: ctx.authority,
      credential,
      schema: schemaAddress,
      attestation: attestationPda,
      nonce,
      data: serializeAttestationData(schemaAccount.data, recordToDataObject(record)),
      expiry: 0n,
    }));
    const signature = await operations.sendInstructions(ctx, instructions);
    return { pda: attestationPda, signature, reused: false };
  });
}

export async function getAttestation(pda: Address, operations = chainOperations): Promise<AttestationRecord | null> {
  const ctx = await operations.getChainContext();
  const maybe = await operations.fetchMaybeAttestation(ctx.rpc, pda, READ_CONFIG);
  if (!maybe.exists) return null;
  const credential = await deriveCredentialAddress(ctx.authority.address);
  const schemaAddress = await deriveSchemaAddress(credential);
  const schemaAccount = await operations.fetchSchema(ctx.rpc, schemaAddress, READ_CONFIG);
  const decoded = deserializeAttestationData<Record<string, unknown>>(
    schemaAccount.data,
    Uint8Array.from(maybe.data.data)
  );
  return dataObjectToRecord(decoded);
}

export function explorerUrl(kind: "tx" | "address", value: string): string {
  const cluster = isMainnetUrl(process.env.SOLANA_RPC_URL ?? "") ? "" : "?cluster=devnet";
  const path = kind === "tx" ? "tx" : "address";
  return `https://explorer.solana.com/${path}/${value}${cluster}`;
}

export { SOLANA_ATTESTATION_SERVICE_PROGRAM_ADDRESS };
