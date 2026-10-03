import { timingSafeEqual } from "node:crypto";
import { fileURLToPath } from "node:url";
import express from "express";
import {
  buildVerdictMessage,
  checkIssuedAtWindow,
  claimTextSha256,
  normalizeClaimText,
  verifyVerdictSignature,
  VALID_VERDICTS,
  type Verdict,
} from "./message.js";
import {
  attestVerdict,
  getAttestation,
  getInitializationState,
  AttestationConflictError,
  AttestationBusyError,
  parseIssuedAtTimestamp,
  explorerUrl,
} from "./sas.js";
import { address } from "@solana/kit";

const PORT = Number(process.env.ATTESTOR_PORT ?? "8013");
const HOST = "127.0.0.1"; // internal service, loopback only
const INTERNAL_TOKEN = process.env.ATTESTOR_INTERNAL_TOKEN ?? "";

const MAX_CLAIM_TEXT_LEN = 20_000;
const MAX_REVIEWER_REQUESTS_PER_HOUR = 30;
const rateLimitWindowMs = 60 * 60 * 1000;
const MAX_CONCURRENT_SIGNATURE_CHECKS = 32;

export function createReviewerRateLimiter(maxReviewers = 10_000, clock = Date.now) {
  const reviewerHits = new Map<string, number[]>();
  let nextCleanup = 0;
  return (reviewer: string): "allowed" | "quota" | "capacity" => {
    const now = clock();
    if (now >= nextCleanup) {
      for (const [key, hits] of reviewerHits) {
        if (now - hits[hits.length - 1] >= rateLimitWindowMs) reviewerHits.delete(key);
      }
      nextCleanup = now + 60_000;
    }
    if (!reviewerHits.has(reviewer) && reviewerHits.size >= maxReviewers) return "capacity";
    const hits = (reviewerHits.get(reviewer) ?? []).filter((t) => now - t < rateLimitWindowMs);
    if (hits.length >= MAX_REVIEWER_REQUESTS_PER_HOUR) return "quota";
    hits.push(now);
    reviewerHits.set(reviewer, hits);
    return "allowed";
  };
}

interface AttestBody {
  claim_id?: string;
  claim_text?: string;
  paper_id?: string;
  verdict?: string;
  evidence_sha256?: string;
  reviewer?: string;
  signature?: string;
  issued_at?: string;
}

function badRequest(res: express.Response, detail: string) {
  res.status(400).json({ error: detail });
}

/** Constant-time string comparison: a length check first (timingSafeEqual
 * throws on mismatched lengths), then a byte-for-byte compare so a wrong
 * token can't be brute-forced by timing how far the mismatch got. */
function timingSafeStringEqual(a: string, b: string): boolean {
  const bufA = Buffer.from(a, "utf8");
  const bufB = Buffer.from(b, "utf8");
  if (bufA.length !== bufB.length) return false;
  return timingSafeEqual(bufA, bufB);
}

function requireInternalToken(req: express.Request, res: express.Response, next: express.NextFunction) {
  if (req.path === "/healthz") return next();
  if (!INTERNAL_TOKEN) {
    res.status(403).json({ error: "ATTESTOR_INTERNAL_TOKEN is not configured; refusing all requests" });
    return;
  }
  if (!timingSafeStringEqual(req.header("X-Internal-Token") ?? "", INTERNAL_TOKEN)) {
    res.status(403).json({ error: "missing or invalid X-Internal-Token" });
    return;
  }
  next();
}

export function buildApp(dependencies: Partial<{
  attestVerdict: typeof attestVerdict;
  getAttestation: typeof getAttestation;
  getInitializationState: typeof getInitializationState;
}> = {}): express.Express {
  const operations = { attestVerdict, getAttestation, getInitializationState, ...dependencies };
  const checkRateLimit = createReviewerRateLimiter();
  let activeSignatureChecks = 0;
  const app = express();
  app.disable("x-powered-by");
  app.use(express.json({ limit: "64kb" }));
  app.use(requireInternalToken);

  app.get("/healthz", async (_req, res) => {
    try {
      const state = await operations.getInitializationState();
      const initialized = state.credential_exists && state.schema_exists;
      res.status(initialized ? 200 : 503).json({
        status: initialized ? "ok" : "not_initialized",
        ...state,
        rpc_url: process.env.SOLANA_RPC_URL ?? "https://api.devnet.solana.com",
      });
    } catch (error) {
      res.status(502).json({ status: "error", error: error instanceof Error ? error.message : String(error) });
    }
  });

  app.post("/message", (req, res) => {
    const body = req.body as Pick<
      AttestBody,
      "claim_id" | "claim_text" | "paper_id" | "verdict" | "evidence_sha256" | "issued_at"
    >;

    const claimId = (body.claim_id ?? "").trim();
    const claimText = body.claim_text ?? "";
    const paperId = (body.paper_id ?? "").trim();
    const verdict = body.verdict ?? "";
    const evidenceSha256 = (body.evidence_sha256 ?? "-").trim() || "-";
    // issued_at is stamped by the server when the caller doesn't supply one,
    // so the message an agent signs is exactly the message /attest will
    // rebuild and check the signature against.
    const issuedAt = (body.issued_at ?? "").trim() || new Date().toISOString();

    if (!claimId || !claimText || !paperId || !verdict) {
      badRequest(res, "claim_id, claim_text, paper_id and verdict are required");
      return;
    }
    if (claimText.length > MAX_CLAIM_TEXT_LEN) {
      badRequest(res, `claim_text must be at most ${MAX_CLAIM_TEXT_LEN} characters`);
      return;
    }
    if (!(VALID_VERDICTS as readonly string[]).includes(verdict)) {
      badRequest(res, `verdict must be one of ${VALID_VERDICTS.join(", ")}`);
      return;
    }

    const claimSha256 = claimTextSha256(claimText);
    try {
      const message = buildVerdictMessage({
        claimId,
        claimSha256,
        paperId,
        verdict: verdict as Verdict,
        evidenceSha256,
        issuedAt,
      });
      res.json({
        message,
        claim_id: claimId,
        claim_sha256: claimSha256,
        paper_id: paperId,
        verdict,
        evidence_sha256: evidenceSha256,
        issued_at: issuedAt,
      });
    } catch (error) {
      badRequest(res, error instanceof Error ? error.message : "invalid verdict fields");
    }
  });

  app.post("/attest", async (req, res) => {
    const body = req.body as AttestBody;

    const claimId = (body.claim_id ?? "").trim();
    const claimText = body.claim_text ?? "";
    const paperId = (body.paper_id ?? "").trim();
    const verdict = body.verdict ?? "";
    const reviewer = (body.reviewer ?? "").trim();
    const signature = (body.signature ?? "").trim();
    const issuedAt = (body.issued_at ?? "").trim();
    const evidenceSha256 = (body.evidence_sha256 ?? "-").trim() || "-";

    if (!claimId || !claimText || !paperId || !verdict || !reviewer || !signature || !issuedAt) {
      badRequest(res, "claim_id, claim_text, paper_id, verdict, reviewer, signature and issued_at are required");
      return;
    }
    if (claimText.length > MAX_CLAIM_TEXT_LEN) {
      badRequest(res, `claim_text must be at most ${MAX_CLAIM_TEXT_LEN} characters`);
      return;
    }
    if (!(VALID_VERDICTS as readonly string[]).includes(verdict)) {
      badRequest(res, `verdict must be one of ${VALID_VERDICTS.join(", ")}`);
      return;
    }
    try {
      if (!Number.isFinite(parseIssuedAtTimestamp(issuedAt))) {
        throw new Error("issued_at must be a valid ISO 8601 timestamp");
      }
      checkIssuedAtWindow(issuedAt);
    } catch (error) {
      badRequest(res, error instanceof Error ? error.message : "invalid issued_at");
      return;
    }
    try {
      address(reviewer); // validates it decodes as a base58 32-byte pubkey
    } catch {
      badRequest(res, "reviewer must be a base58-encoded ed25519 public key");
      return;
    }

    const claimSha256 = claimTextSha256(claimText);
    let message: string;
    try {
      message = buildVerdictMessage({
        claimId,
        claimSha256,
        paperId,
        verdict: verdict as Verdict,
        evidenceSha256,
        issuedAt,
      });
    } catch (error) {
      badRequest(res, error instanceof Error ? error.message : "invalid verdict fields");
      return;
    }

    if (activeSignatureChecks >= MAX_CONCURRENT_SIGNATURE_CHECKS) {
      res.status(503).json({ error: "signature verification capacity exceeded; retry later" });
      return;
    }
    let valid: boolean;
    activeSignatureChecks++;
    try {
      valid = await verifyVerdictSignature(message, signature, reviewer);
    } finally {
      activeSignatureChecks--;
    }
    if (!valid) {
      badRequest(res, "signature does not verify against reviewer's public key over the canonical message");
      return;
    }
    const rateLimit = checkRateLimit(reviewer);
    if (rateLimit === "capacity") {
      res.status(503).json({ error: "reviewer rate limiter capacity exceeded; retry later" });
      return;
    }
    if (rateLimit === "quota") {
      res.status(429).json({ error: "rate limit exceeded (30 verdicts/hour per reviewer)" });
      return;
    }

    try {
      const result = await operations.attestVerdict({
        claimId,
        claimSha256,
        paperId,
        verdict,
        evidenceSha256,
        reviewer,
        reviewerSig: signature,
        issuedAt,
      });
      res.json({
        attestation: result.pda,
        signature: result.signature,
        explorer_url: explorerUrl(result.signature ? "tx" : "address", result.signature ?? result.pda),
        reused: result.reused,
        claim_sha256: claimSha256,
        issued_at_epoch_ms: Date.parse(issuedAt),
        normalized_claim_preview: normalizeClaimText(claimText).slice(0, 200),
      });
    } catch (error) {
      const status = error instanceof AttestationConflictError ? 409 : error instanceof AttestationBusyError ? 503 : 502;
      res.status(status).json({ error: error instanceof Error ? error.message : "attestation failed" });
    }
  });

  app.get("/attestation/:pda", async (req, res) => {
    try {
      const pda = address(req.params.pda);
      const record = await operations.getAttestation(pda);
      if (!record) {
        res.status(404).json({ error: "no attestation at that address" });
        return;
      }
      res.json({ pda, record, explorer_url: explorerUrl("address", pda) });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (/^Address string/.test(message) || /is not a base/i.test(message)) {
        badRequest(res, "pda must be a base58-encoded address");
        return;
      }
      res.status(502).json({ error: message });
    }
  });

  return app;
}

export async function start(): Promise<void> {
  const app = buildApp();
  app.listen(PORT, HOST, () => {
    console.log(JSON.stringify({ event: "started", host: HOST, port: PORT }));
  });
}

// Only auto-start when this file is run directly (`node dist/server.js` /
// `tsx src/server.ts`), not when it's imported — e.g. by tests importing
// `buildApp` to exercise the routes without binding a real port.
const isMainModule = process.argv[1] !== undefined && fileURLToPath(import.meta.url) === process.argv[1];
if (isMainModule) {
  start().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
