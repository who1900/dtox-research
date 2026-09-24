import assert from "node:assert/strict";
import test from "node:test";
import { loadConfig } from "../src/config.js";

test("defaults to disabled and all supported test networks", () => {
  const config = loadConfig({});
  assert.equal(config.mode, "disabled");
  assert.deepEqual(config.evmNetworks, ["eip155:84532", "eip155:11155111", "eip155:421614"]);
  assert.equal(config.svmNetwork, "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1");
});

test("accepts multiple EVM networks and removes duplicates", () => {
  const config = loadConfig({ X402_EVM_NETWORKS: "eip155:8453,eip155:1,eip155:42161,eip155:8453" });
  assert.deepEqual(config.evmNetworks, ["eip155:8453", "eip155:1", "eip155:42161"]);
});

test("keeps the legacy single-network variable compatible", () => {
  const config = loadConfig({ X402_EVM_NETWORK: "eip155:8453" });
  assert.deepEqual(config.evmNetworks, ["eip155:8453"]);
});

test("rejects malformed EVM network identifiers", () => {
  assert.throws(() => loadConfig({ X402_EVM_NETWORKS: "base-sepolia" }), /eip155/);
});

test("live mode fails closed without both recipients", () => {
  assert.throws(() => loadConfig({ X402_MODE: "live" }), /X402_EVM_PAY_TO.*X402_SVM_PAY_TO/);
});

test("live mode validates both recipient address families", () => {
  assert.throws(() => loadConfig({
    X402_MODE: "live",
    X402_EVM_PAY_TO: "bad",
    X402_SVM_PAY_TO: "bad"
  }), /EVM address/);
});

test("shadow mode never requires credentials", () => {
  const config = loadConfig({ X402_MODE: "shadow" });
  assert.equal(config.mode, "shadow");
});
