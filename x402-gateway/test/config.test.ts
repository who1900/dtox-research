import assert from "node:assert/strict";
import test from "node:test";
import { loadConfig } from "../src/config.js";

test("defaults to disabled and both official test networks", () => {
  const config = loadConfig({});
  assert.equal(config.mode, "disabled");
  assert.equal(config.evmNetwork, "eip155:84532");
  assert.equal(config.svmNetwork, "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1");
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
