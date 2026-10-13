import assert from "node:assert/strict";
import test from "node:test";
import { runReadOnlyProbe } from "./sas-readonly-probe.mjs";
import { DEVNET_GENESIS_HASH } from "../src/sas.js";
import { mockChain, TEST_ADDRESS } from "./mock-chain.js";

test("readonly probe invokes candidate strict account checks with public authority only", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  const chain = mockChain();
  const context = await chain.operations.getChainContext();
  const report = await runReadOnlyProbe(TEST_ADDRESS, {
    createRpc: (url: string) => {
      assert.equal(url, "https://api.devnet.solana.com");
      return { getGenesisHash: () => ({ send: async () => DEVNET_GENESIS_HASH }),
        getBalance: context.rpc.getBalance };
    },
    fetchCredential: chain.operations.fetchMaybeCredential,
    fetchSchema: chain.operations.fetchMaybeSchema,
  });
  assert.equal(report.compatible, true);
  assert.equal(report.initialization.authority, TEST_ADDRESS);
  assert.deepEqual(report.observed.schema.layout, report.expected.layout);
  assert.equal(chain.state.credentialReads, 1);
  assert.equal(chain.state.schemaReads, 1);
  assert.equal(chain.state.writes, 0);
});

test("readonly probe reports missing or incompatible existing accounts without provisioning", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  for (const mismatch of ["missing", "layout", "owner", "authority", "fieldNames", "version"]) {
    const chain = mockChain();
    if (mismatch === "missing") chain.state.schemaExists = false;
    const context = await chain.operations.getChainContext();
    const report = await runReadOnlyProbe(TEST_ADDRESS, {
      createRpc: () => ({ getGenesisHash: () => ({ send: async () => DEVNET_GENESIS_HASH }),
        getBalance: context.rpc.getBalance }),
      fetchCredential: async (...args: Parameters<typeof chain.operations.fetchMaybeCredential>) => {
        const account = await chain.operations.fetchMaybeCredential(...args);
        if (mismatch === "authority") account.data.authority = "invalid-authority";
        return account;
      },
      fetchSchema: async (...args: Parameters<typeof chain.operations.fetchMaybeSchema>) => {
        const account = await chain.operations.fetchMaybeSchema(...args);
        if (account.exists) {
          if (mismatch === "layout") account.data.layout = new Uint8Array([12]);
          if (mismatch === "owner") account.programAddress = TEST_ADDRESS;
          if (mismatch === "fieldNames") account.data.fieldNames = new Uint8Array();
          if (mismatch === "version") account.data.version = 2;
        }
        return account;
      },
    });
    assert.equal(report.compatible, false, mismatch);
    assert.match(report.error, /missing|mismatch|not owned/);
    assert.equal(chain.state.writes, 0);
  }
});

test("readonly probe rejects other genesis and any attempt to send a transaction", async (t) => {
  t.mock.method(globalThis, "fetch", async () => { throw new Error("external network forbidden"); });
  let writes = 0;
  let accountReads = 0;
  const chain = mockChain();
  const context = await chain.operations.getChainContext();
  for (const wrongGenesis of [true, false]) {
    const report = await runReadOnlyProbe(TEST_ADDRESS, {
      createRpc: () => ({
        getGenesisHash: () => ({ send: async () => wrongGenesis ? "mainnet" : DEVNET_GENESIS_HASH }),
        getBalance: context.rpc.getBalance,
        sendTransaction: () => { writes++; },
      }),
      fetchCredential: async (rpc: { sendTransaction: () => void }) => {
        accountReads++;
        rpc.sendTransaction();
      },
      fetchSchema: chain.operations.fetchMaybeSchema,
    });
    assert.equal(report.compatible, false);
    assert.match(report.error, wrongGenesis ? /not Solana devnet/ : /non-readonly RPC method forbidden/);
  }
  assert.equal(accountReads, 1);
  assert.equal(writes, 0);
  assert.equal(chain.state.writes, 0);
});
