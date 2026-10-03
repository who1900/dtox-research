import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";
import * as ed from "@noble/ed25519";
import bs58 from "bs58";
import { buildVerdictMessage, claimTextSha256 } from "../src/message.js";
import { AttestationConflictError, AttestationBusyError, getInitializationState, getAttestation } from "../src/sas.js";
import { mockChain, TEST_ADDRESS, verdictRecord } from "./mock-chain.js";

process.env.ATTESTOR_INTERNAL_TOKEN = "test-internal-token";

const { buildApp, createReviewerRateLimiter } = await import("../src/server.js");

async function withServer(
  run: (baseUrl: string) => Promise<void>,
  dependencies: Parameters<typeof buildApp>[0] = {},
): Promise<void> {
  const app = buildApp(dependencies);
  const server = createServer(app);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  try {
    await run(`http://127.0.0.1:${port}`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

test("POST /message rejects requests without a valid X-Internal-Token", async () => {
  await withServer(async (base) => {
    const res = await fetch(`${base}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    assert.equal(res.status, 403);
  });
});

test("POST /message returns the exact canonical message /attest will verify against", async () => {
  await withServer(async (base) => {
    const res = await fetch(`${base}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Token": "test-internal-token" },
      body: JSON.stringify({
        claim_id: "claim-1",
        claim_text: "Reed-Solomon codes achieve the Singleton bound",
        paper_id: "2401.12345",
        verdict: "asserts",
        issued_at: "2026-09-26T03:00:00Z",
      }),
    });
    assert.equal(res.status, 200);
    const body = (await res.json()) as { message: string; claim_sha256: string; issued_at: string };
    assert.equal(
      body.message,
      [
        "dtox-claim-verdict:v1",
        "claim_id:claim-1",
        `claim_sha256:${body.claim_sha256}`,
        "paper_id:2401.12345",
        "verdict:asserts",
        "evidence_sha256:-",
        "issued_at:2026-09-26T03:00:00Z",
      ].join("\n")
    );
  });
});

test("POST /message stamps issued_at server-side when the caller omits it", async () => {
  await withServer(async (base) => {
    const before = Date.now();
    const res = await fetch(`${base}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Token": "test-internal-token" },
      body: JSON.stringify({
        claim_id: "claim-1",
        claim_text: "some claim text",
        paper_id: "2401.12345",
        verdict: "asserts",
      }),
    });
    assert.equal(res.status, 200);
    const body = (await res.json()) as { issued_at: string };
    const issuedAtMs = Date.parse(body.issued_at);
    assert.ok(issuedAtMs >= before && issuedAtMs <= Date.now());
  });
});

test("POST /message rejects an invalid verdict", async () => {
  await withServer(async (base) => {
    const res = await fetch(`${base}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Token": "test-internal-token" },
      body: JSON.stringify({
        claim_id: "claim-1",
        claim_text: "some claim text",
        paper_id: "2401.12345",
        verdict: "definitely_true",
      }),
    });
    assert.equal(res.status, 400);
  });
});

test("X-Internal-Token check rejects a token of a different length than the real one", async () => {
  await withServer(async (base) => {
    const res = await fetch(`${base}/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Internal-Token": "short" },
      body: JSON.stringify({}),
    });
    assert.equal(res.status, 403);
  });
});

async function signedBody(issuedAt = new Date().toISOString().replace("Z", "+00:00"), secret = ed.utils.randomSecretKey()) {
  const body = {
    claim_id: "claim-1",
    claim_text: "some claim text",
    paper_id: "2401.12345",
    verdict: "asserts" as const,
    evidence_sha256: "-",
    reviewer: bs58.encode(await ed.getPublicKeyAsync(secret)),
    issued_at: issuedAt,
    signature: "",
  };
  const message = buildVerdictMessage({
    claimId: body.claim_id,
    claimSha256: claimTextSha256(body.claim_text),
    paperId: body.paper_id,
    verdict: body.verdict,
    evidenceSha256: body.evidence_sha256,
    issuedAt: body.issued_at,
  });
  body.signature = bs58.encode(await ed.signAsync(new TextEncoder().encode(message), secret));
  return body;
}

function postAttest(base: string, body: unknown) {
  return fetch(`${base}/attest`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Internal-Token": "test-internal-token" },
    body: JSON.stringify(body),
  });
}

test("invalid signatures cannot poison reviewer quota; 30 authenticated attempts succeed and the 31st is limited", { timeout: 10_000 }, async () => {
  const body = await signedBody();
  const other = await signedBody();
  let writes = 0;
  await withServer(async (base) => {
    for (let i = 0; i < 40; i++) {
      const invalid = { ...body, signature: i % 2 ? other.signature : "not-base58!!" };
      assert.equal((await postAttest(base, invalid)).status, 400);
    }
    assert.equal(writes, 0);
    for (let i = 0; i < 30; i++) {
      const response = await postAttest(base, body);
      assert.equal(response.status, 200);
      const result = await response.json() as { issued_at_epoch_ms: number };
      assert.equal(result.issued_at_epoch_ms, Date.parse(body.issued_at));
      assert.ok(Number.isInteger(result.issued_at_epoch_ms));
    }
    assert.equal((await postAttest(base, body)).status, 429);
    assert.equal((await postAttest(base, { ...body, signature: "invalid!" })).status, 400);
    assert.equal((await postAttest(base, other)).status, 200);
    assert.equal(writes, 31);
  }, {
    attestVerdict: async (record) => {
      writes++;
      assert.ok(record.reviewer === body.reviewer || record.reviewer === other.reviewer);
      return { pda: TEST_ADDRESS, signature: null, reused: true };
    },
  });
});

test("authenticated reviewer limiter bounds memory and evicts expired identities", () => {
  let now = 1000;
  const limit = createReviewerRateLimiter(2, () => now);
  assert.equal(limit("verified-a"), "allowed");
  assert.equal(limit("verified-b"), "allowed");
  assert.equal(limit("verified-c"), "capacity");
  assert.equal(limit("verified-a"), "allowed");
  now += 60 * 60 * 1000;
  assert.equal(limit("verified-c"), "allowed");
  assert.equal(limit("verified-d"), "allowed");
  assert.equal(limit("verified-e"), "capacity");
});

test("signed non-ISO issued_at returns 400 before reviewer quota or chain access", { timeout: 10_000 }, async () => {
  const secret = ed.utils.randomSecretKey();
  const body = await signedBody(new Date().toUTCString(), secret);
  const valid = await signedBody(new Date().toISOString(), secret);
  let writes = 0;
  await withServer(async (base) => {
    for (let i = 0; i < 31; i++) {
      const response = await postAttest(base, body);
      assert.equal(response.status, 400);
      assert.match((await response.json() as { error: string }).error, /valid ISO 8601/);
    }
    assert.equal(writes, 0);
    for (let i = 0; i < 30; i++) assert.equal((await postAttest(base, valid)).status, 200);
    assert.equal((await postAttest(base, valid)).status, 429);
  }, { attestVerdict: async () => {
    writes++;
    return { pda: TEST_ADDRESS, signature: null, reused: true };
  } });
});

test("reviewer quota uses a rolling hour rather than retaining expired hits", () => {
  let now = 0;
  const limit = createReviewerRateLimiter(1, () => now);
  for (let i = 0; i < 30; i++) assert.equal(limit("verified"), "allowed");
  assert.equal(limit("verified"), "quota");
  now = 60 * 60 * 1000 - 1;
  assert.equal(limit("verified"), "quota");
  now++;
  assert.equal(limit("verified"), "allowed");
});

test("stale/equal-time conflicts map to HTTP 409, capacity to 503 and RPC failures to 502", async () => {
  const body = await signedBody();
  for (const [error, status] of [
    [new AttestationConflictError("stale verdict"), 409],
    [new AttestationBusyError("queue full"), 503],
    [new Error("RPC unavailable"), 502],
  ] as const) {
    await withServer(async (base) => {
      const response = await postAttest(base, body);
      assert.equal(response.status, status);
      assert.deepEqual(await response.json(), { error: error.message });
    }, { attestVerdict: async () => { throw error; } });
  }
});

test("GET /healthz is read-only and missing accounts return 503/not_initialized", async () => {
  for (const [credentialExists, schemaExists] of [[false, false], [true, false], [false, true], [true, true]]) {
    const chain = mockChain();
    chain.state.credentialExists = credentialExists;
    chain.state.schemaExists = schemaExists;
    await withServer(async (base) => {
      const response = await fetch(`${base}/healthz`);
      const initialized = credentialExists && schemaExists;
      assert.equal(response.status, initialized ? 200 : 503);
      const body = await response.json() as Record<string, unknown>;
      assert.equal(body.status, initialized ? "ok" : "not_initialized");
      assert.equal(body.credential_exists, credentialExists);
      assert.equal(body.schema_exists, schemaExists);
      assert.equal(typeof body.schema, "string");
      assert.equal(body.balance_lamports, "42");
      assert.equal(chain.state.writes, 0);
    }, { getInitializationState: () => getInitializationState(chain.operations) });
  }
});

test("GET /healthz reports RPC read failures without provisioning", async () => {
  const chain = mockChain();
  chain.operations.fetchMaybeCredential = async () => { throw new Error("mock RPC unavailable"); };
  await withServer(async (base) => {
    const response = await fetch(`${base}/healthz`);
    assert.equal(response.status, 502);
    assert.equal(chain.state.writes, 0);
  }, { getInitializationState: () => getInitializationState(chain.operations) });
});

test("GET /attestation is read-only for both missing and existing records", async () => {
  const chain = mockChain();
  chain.state.credentialExists = false;
  await withServer(async (base) => {
    const headers = { "X-Internal-Token": "test-internal-token" };
    assert.equal((await fetch(`${base}/attestation/${TEST_ADDRESS}`, { headers })).status, 404);
    chain.seed(TEST_ADDRESS, verdictRecord());
    const response = await fetch(`${base}/attestation/${TEST_ADDRESS}`, { headers });
    assert.equal(response.status, 200);
    const body = await response.json() as { record: unknown };
    assert.deepEqual(body.record, verdictRecord());
    assert.equal(chain.state.credentialReads, 0);
    assert.equal(chain.state.writes, 0);
  }, { getAttestation: (pda) => getAttestation(pda, chain.operations) });
});
