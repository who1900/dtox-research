import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";

process.env.ATTESTOR_INTERNAL_TOKEN = "test-internal-token";

const { buildApp } = await import("../src/server.js");

async function withServer(run: (baseUrl: string) => Promise<void>): Promise<void> {
  const app = buildApp();
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
