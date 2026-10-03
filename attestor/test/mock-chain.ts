import assert from "node:assert/strict";
import { address, type Address, type Instruction } from "@solana/kit";
import {
  getCreateAttestationInstructionDataDecoder,
  serializeAttestationData,
  type Schema,
} from "sas-lib";
import { attestVerdict, FIELD_LAYOUT, FIELD_NAMES, type AttestationRecord } from "../src/sas.js";

export const TEST_ADDRESS = address("11111111111111111111111111111111");

export function verdictRecord(overrides: Partial<AttestationRecord> = {}): AttestationRecord {
  return {
    claimId: "claim-1",
    claimSha256: "a".repeat(64),
    paperId: "2401.12345",
    verdict: "asserts",
    evidenceSha256: "-",
    reviewer: TEST_ADDRESS,
    reviewerSig: "test-signature-a",
    issuedAt: "2026-09-26T03:00:00Z",
    ...overrides,
  };
}

export function mockChain() {
  const fieldNames = Buffer.concat(FIELD_NAMES.map((name) => {
    const text = Buffer.from(name);
    const length = Buffer.alloc(4);
    length.writeUInt32LE(text.length);
    return Buffer.concat([length, text]);
  }));
  const schema: Schema = {
    discriminator: 0,
    credential: TEST_ADDRESS,
    name: new Uint8Array(),
    description: new Uint8Array(),
    fieldNames,
    layout: FIELD_LAYOUT,
    isPaused: false,
    version: 1,
  };
  const state = {
    credentialExists: true,
    schemaExists: true,
    reads: 0,
    writes: 0,
    credentialReads: 0,
    schemaReads: 0,
    failNextWrite: false,
    beforeWrite: async () => {},
    records: new Map<Address, Uint8Array>(),
  };
  const operations = {
    getChainContext: async () => ({
      authority: { address: TEST_ADDRESS },
      rpc: { getBalance: (_address: unknown, config: { commitment: string }) => {
        assert.equal(config.commitment, "confirmed");
        return { send: async () => ({ value: 42n }) };
      } },
      rpcSubscriptions: {},
    }),
    fetchMaybeCredential: async (_rpc: unknown, _pda: unknown, config: { commitment: string }) => {
      assert.equal(config.commitment, "confirmed");
      state.credentialReads++;
      return { exists: state.credentialExists };
    },
    fetchMaybeSchema: async (_rpc: unknown, _pda: unknown, config: { commitment: string }) => {
      assert.equal(config.commitment, "confirmed");
      state.schemaReads++;
      return { exists: state.schemaExists };
    },
    fetchSchema: async (_rpc: unknown, _pda: unknown, config: { commitment: string }) => {
      assert.equal(config.commitment, "confirmed");
      state.schemaReads++;
      if (!state.schemaExists) throw new Error("schema not initialized");
      return { data: schema };
    },
    fetchMaybeAttestation: async (_rpc: unknown, pda: Address, config: { commitment: string }) => {
      assert.equal(config.commitment, "confirmed");
      state.reads++;
      const data = state.records.get(pda);
      return data ? { exists: true, data: { data } } : { exists: false };
    },
    sendInstructions: async (_ctx: unknown, instructions: readonly Instruction[]) => {
      await state.beforeWrite();
      if (state.failNextWrite) {
        state.failNextWrite = false;
        throw new Error("mock RPC failure");
      }
      assert.ok(instructions.length === 1 || instructions.length === 2);
      const create = instructions[instructions.length - 1];
      assert.equal(create.data?.[0], 6, "unexpected credential/schema provisioning");
      const decoded = getCreateAttestationInstructionDataDecoder().decode(create.data!);
      state.records.set(create.accounts![4].address, Uint8Array.from(decoded.data));
      return `mock-transaction-${++state.writes}`;
    },
  } as unknown as NonNullable<Parameters<typeof attestVerdict>[1]>;
  function seed(pda: Address, record: AttestationRecord) {
    state.records.set(pda, serializeAttestationData(schema, {
      claim_id: record.claimId,
      claim_sha256: record.claimSha256,
      paper_id: record.paperId,
      verdict: record.verdict,
      evidence_sha256: record.evidenceSha256,
      reviewer: record.reviewer,
      reviewer_sig: record.reviewerSig,
      issued_at: record.issuedAt,
    }));
  }
  return { state, operations, seed };
}

export function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}
