import assert from "node:assert/strict";
import test from "node:test";
import { loadConfig } from "../src/config.js";

test("defaults to disabled and all supported test networks", () => {
  const config = loadConfig({});
  assert.equal(config.mode, "disabled");
  assert.deepEqual(config.evmNetworks, ["eip155:84532", "eip155:11155111", "eip155:421614"]);
  assert.equal(config.svmNetwork, "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1");
  assert.equal(config.prices.verdict, "$0.01");
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

test("live mode fails closed without an SVM recipient, even solana-only", () => {
  assert.throws(() => loadConfig({ X402_MODE: "live" }), /X402_SVM_PAY_TO/);
});

test("live mode validates both recipient address families when EVM is configured", () => {
  assert.throws(() => loadConfig({
    X402_MODE: "live",
    X402_EVM_PAY_TO: "bad",
    X402_SVM_PAY_TO: "bad"
  }), /EVM address/);
});

test("live mode validates the SVM recipient even when solana-only", () => {
  assert.throws(() => loadConfig({
    X402_MODE: "live",
    X402_SVM_PAY_TO: "bad"
  }), /Solana address/);
});

test("live mode goes solana-only when X402_EVM_PAY_TO is empty", () => {
  const config = loadConfig({
    X402_MODE: "live",
    X402_SVM_PAY_TO: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
  });
  assert.equal(config.mode, "live");
  assert.deepEqual(config.evmNetworks, []);
  assert.equal(config.evmPayTo, "");
  assert.equal(config.svmPayTo, "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG");
});

test("live mode goes solana-only when X402_EVM_NETWORKS is explicitly empty, even with an EVM payTo set", () => {
  const config = loadConfig({
    X402_MODE: "live",
    X402_EVM_NETWORKS: "",
    X402_EVM_PAY_TO: "0x1234567890123456789012345678901234567890",
    X402_SVM_PAY_TO: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
  });
  assert.deepEqual(config.evmNetworks, []);
  assert.equal(config.evmPayTo, "");
});

test("live mode keeps prior EVM + SVM behavior when an EVM payTo is set and networks are not explicitly emptied", () => {
  const config = loadConfig({
    X402_MODE: "live",
    X402_EVM_PAY_TO: "0x1234567890123456789012345678901234567890",
    X402_SVM_PAY_TO: "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
  });
  assert.deepEqual(config.evmNetworks, ["eip155:84532", "eip155:11155111", "eip155:421614"]);
  assert.equal(config.evmPayTo, "0x1234567890123456789012345678901234567890");
});

test("shadow mode never requires credentials", () => {
  const config = loadConfig({ X402_MODE: "shadow" });
  assert.equal(config.mode, "shadow");
});
