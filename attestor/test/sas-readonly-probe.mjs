import { pathToFileURL } from "node:url";
import { address, createSolanaRpc } from "@solana/kit";
import { fetchMaybeCredential, fetchMaybeSchema } from "sas-lib";
import { DEVNET_GENESIS_HASH, FIELD_LAYOUT, FIELD_NAMES, getInitializationState,
  SCHEMA_VERSION } from "../src/sas.ts";

const DEVNET_URL = "https://api.devnet.solana.com";
const READ_METHODS = new Set(["getGenesisHash", "getBalance", "getAccountInfo", "getMultipleAccounts"]);

export async function runReadOnlyProbe(publicAuthority, {
  createRpc = createSolanaRpc,
  fetchCredential = fetchMaybeCredential,
  fetchSchema = fetchMaybeSchema,
} = {}) {
  const authority = Object.freeze({ address: address(publicAuthority) });
  const underlying = createRpc(DEVNET_URL);
  const rpc = new Proxy(underlying, {
    get(target, method) {
      if (!READ_METHODS.has(method)) throw new Error(`non-readonly RPC method forbidden: ${String(method)}`);
      return target[method].bind(target);
    },
  });
  const observed = {};
  const report = {
    captured_at_utc: new Date().toISOString(), rpc_url: DEVNET_URL,
    authority: authority.address, compatible: false,
    expected: { version: SCHEMA_VERSION, layout: [...FIELD_LAYOUT], field_names: [...FIELD_NAMES] },
    observed,
  };
  const context = Object.freeze({ authority, rpc });
  const operations = new Proxy({
    getChainContext: async () => context,
    fetchMaybeCredential: async (...args) => {
      const account = await fetchCredential(...args);
      observed.credential = { address: args[1], exists: account.exists };
      if (account.exists) Object.assign(observed.credential, {
        program_address: account.programAddress, authority: account.data.authority,
        authorized_signers: [...account.data.authorizedSigners],
      });
      return account;
    },
    fetchMaybeSchema: async (...args) => {
      const account = await fetchSchema(...args);
      observed.schema = { address: args[1], exists: account.exists };
      if (account.exists) Object.assign(observed.schema, {
        program_address: account.programAddress, credential: account.data.credential,
        version: account.data.version, layout: [...account.data.layout],
        field_names_hex: Buffer.from(account.data.fieldNames).toString("hex"),
      });
      return account;
    },
  }, {
    get(target, operation) {
      if (!Object.hasOwn(target, operation)) {
        throw new Error(`chain operation forbidden: ${String(operation)}`);
      }
      return target[operation];
    },
  });
  try {
    report.genesis_hash = await rpc.getGenesisHash().send();
    if (report.genesis_hash !== DEVNET_GENESIS_HASH) throw new Error("RPC is not Solana devnet");
    report.initialization = await getInitializationState(operations);
    report.compatible = report.initialization.credential_exists && report.initialization.schema_exists;
    if (!report.compatible) report.error = "existing credential/schema missing; provisioning forbidden";
  } catch (error) {
    report.error = error instanceof Error ? error.message : String(error);
  }
  return report;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv.length !== 4 || process.argv[2] !== "--authority") {
    console.error("Usage: node --import tsx test/sas-readonly-probe.mjs --authority <PUBLIC_ADDRESS>");
    process.exitCode = 2;
  } else {
    try {
      const report = await runReadOnlyProbe(process.argv[3]);
      console.log(JSON.stringify(report, null, 2));
      process.exitCode = report.compatible ? 0 : 1;
    } catch (error) {
      console.error(error instanceof Error ? error.message : String(error));
      process.exitCode = 1;
    }
  }
}
