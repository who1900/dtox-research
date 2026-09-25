# attestor

Anchors dtox's claim-verdict registry on chain using the Solana Attestation
Service (SAS), so a claim judgment is backed by more than "trust the API key
that filed it."

## Why this exists

dtox's `/v1/adjudicate` records an agent's verdict on whether a paper asserts,
does not assert, or partially asserts a claim. Today the only identity behind
a verdict is the caller's dtox API key. A public, keyless MCP endpoint shares
one identity across every anonymous caller, so nothing stops that shared
identity from filing contradictory or bad-faith verdicts — the registry has
no way to tell readers apart.

This service moves the trust anchor from "the API key" to "the agent's own
wallet." An agent signs its verdict with an ed25519 keypair it controls;
`attestor` verifies that signature and writes the verdict on devnet as a SAS
attestation, with the reviewer's public key and signature embedded in the
attested data. Anyone — dtox included — can later verify that a specific
wallet signed a specific verdict, without trusting dtox's own honesty about
who said what. dtox is the *issuer* of the attestation (it holds the
Credential and pays for the transaction); the reviewer's signature inside the
attestation is what a third party actually verifies.

Network: **devnet only**. On startup the service calls `getGenesisHash` on
the configured RPC and compares it against Solana devnet's known genesis
hash (`EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG`) — not a pattern match
on the URL, since an RPC provider's mainnet endpoint doesn't have to contain
the word "mainnet". It refuses to start on a mismatch unless
`ATTESTOR_ALLOW_MAINNET=1` is set.

## How it fits together

```
agent wallet --sign--> canonical message --POST /attest--> attestor --SAS tx--> devnet
                                                              |
                                                   verifies signature first,
                                                   only then touches the chain
```

1. The agent builds the canonical message (see below) and signs it with its
   own ed25519 keypair — the same key it would use for any Solana wallet.
2. It POSTs the verdict, the claim text, its public key, and the signature to
   `attestor`'s internal `/attest` endpoint.
3. `attestor` recomputes `claim_sha256` from the claim text itself (never
   trusts a caller-supplied hash), rebuilds the canonical message, and
   verifies the signature against the given public key.
4. On success it creates (or reuses) a SAS Credential + Schema, derives a
   deterministic PDA for `(claim_id, paper_id, reviewer)`, and writes a SAS
   attestation containing every field plus the reviewer's own signature.

## Trust model

- **dtox is the issuer**, not the reviewer. dtox's authority keypair pays for
  and signs the on-chain *transaction*; it does not vouch for the verdict's
  content.
- **The reviewer's signature is inside the attestation data**, not just used
  to gate the HTTP call. Anyone who reads the attestation can independently
  verify `reviewer_sig` against `reviewer` over the canonical message,
  without trusting dtox at all. dtox could not have forged that signature.
- **One attestation per `(claim_id, paper_id, reviewer)`**, enforced by a
  deterministic nonce, not by application-level bookkeeping. A changed
  verdict from the same reviewer for the same claim/paper closes the old
  attestation and creates a new one — you can't quietly have two live
  verdicts from the same identity.
- **Replay is bounded**: `issued_at` must be within the last 10 minutes (and
  no more than 1 minute in the future), so a captured request body can't be
  replayed indefinitely — though since attestation is idempotent on the same
  triple, replaying a stale-but-still-valid request only ever reproduces the
  same on-chain state.

## Canonical message format

```
dtox-claim-verdict:v1
claim_id:<id>
claim_sha256:<hex sha256 of the normalized claim text>
paper_id:<id>
verdict:<asserts|does_not_assert|partial>
evidence_sha256:<hex|->
issued_at:<ISO 8601 UTC>
```

Claim text normalization (`normalizeClaimText` in `src/message.ts`):
lowercase, trim, collapse all whitespace runs to a single space. This makes
`claim_sha256` stable across cosmetic differences (extra spaces, case,
trailing newlines) without changing what is actually being asserted.

The signature is a standard ed25519 signature over the UTF-8 bytes of this
message, base58-encoded — the same primitive any Solana wallet already uses.

## On-chain schema

- Program: `22zoJMtdu4tQc2PzL74ZUT7FrwgB1Udec8DdW4yw4BdG` (Solana Attestation
  Service, devnet and mainnet).
- Credential name: `dtox-research`.
- Schema name: `claim_verdict_v1`, version `1`.
- Fields (all Borsh strings): `claim_id`, `claim_sha256`, `paper_id`,
  `verdict`, `evidence_sha256`, `reviewer`, `reviewer_sig`, `issued_at`.
- Attestation nonce: `sha256("dtox:v1:" + claim_id + "|" + paper_id + "|" + reviewer)`,
  base58-encoded and used as the PDA seed — deterministic, not random, so the
  same triple always maps to the same attestation address.
- Expiry: none (0).

## Running it

```bash
cd attestor
npm ci
npm run build   # or `npm run dev` for tsx without a build step
```

### Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `ATTESTOR_KEYPAIR_PATH` | yes | — | Path to a solana-keygen JSON keypair file (64-byte array) for the issuer/payer authority. Keep it outside the repo. |
| `SOLANA_RPC_URL` | no | `https://api.devnet.solana.com` | Must be a devnet (or local) RPC. Refuses to start if it looks like mainnet. |
| `SOLANA_WS_URL` | no | derived from `SOLANA_RPC_URL` (http→ws, https→wss) | Override if your RPC provider uses a different websocket host. |
| `ATTESTOR_ALLOW_MAINNET` | no | unset | Set to `1` to bypass the mainnet guard. Not recommended; this service is designed for devnet. |
| `ATTESTOR_PORT` | no | `8013` | The server only ever binds `127.0.0.1` — it is an internal service, not meant to be exposed. |
| `ATTESTOR_INTERNAL_TOKEN` | yes (for any write) | unset | Required value of the `X-Internal-Token` header. If unset, every endpoint except `/healthz` refuses with 403. |

### Endpoints

- `POST /message` — body: `{claim_id, claim_text, paper_id, verdict, evidence_sha256?, issued_at?}`.
  Returns the exact canonical message to sign (`issued_at` is stamped by the
  server when omitted). Lets a caller build the message once here and get
  back exactly what `/attest` will later verify against, instead of
  reimplementing the canonical format itself.
- `POST /attest` — body: `{claim_id, claim_text, paper_id, verdict, evidence_sha256?, reviewer, signature, issued_at}`.
  Returns `{attestation, signature, explorer_url, reused, claim_sha256}`.
  400 on bad fields or a signature that doesn't verify; 429 on rate limit;
  502 on an RPC failure.
- `GET /attestation/:pda` — reads back and decodes an attestation.
- `GET /healthz` — credential/schema PDAs, whether they exist yet, authority
  pubkey, and SOL balance. Does not require the internal token.

Rate limit: 30 verdicts per hour per `reviewer` pubkey, enforced in memory
(resets on restart — this is a basic abuse guard, not a durable ledger).

### Demo signer

```bash
npx tsx scripts/demo-sign.ts /path/to/agent-keypair.json claim-1 2401.12345 asserts \
  "the protocol tolerates byzantine faults under partial synchrony"
```

Generates (or reuses) an ed25519 keypair at the given path and prints a JSON
body ready to `curl -X POST http://127.0.0.1:8013/attest`.

`scripts/gen-keypair.ts` is a smaller helper that only generates a
solana-keygen-format keypair file (useful for the issuer authority itself on
a fresh machine); it takes no claim data.

## Tests

```bash
npm test
```

27 cases, no network access required: canonical message construction and
field validation, signature verification (including that mutating any signed
field invalidates the signature), the replay window, claim-text
normalization and hashing, that the deterministic nonce differs across
`(claim_id, paper_id, reviewer)` and cannot be confused by concatenation
without the `|` separator, the devnet-genesis startup guard (mocked RPC), the
timing-safe `X-Internal-Token` check, and `POST /message` returning the
message `/attest` will verify against.

`scripts/e2e-devnet.ts` is a manual, network-touching smoke test (airdrop,
credential/schema creation, one attestation, read-back) — not run in CI.

## What is not done

- The devnet end-to-end run (airdrop → credential → schema → attestation →
  read-back) is implemented in `scripts/e2e-devnet.ts` but could not be
  executed during development because `api.devnet.solana.com`'s faucet
  returned `429 airdrop limit reached / faucet dry`. The code path itself is
  exercised by the unit tests up to (not including) the network calls; it
  has not been confirmed against a live devnet transaction.
- `api/main.py` now proxies to this service (`POST /v1/verdict/message`,
  `POST /v1/adjudicate/signed`), and the public MCP exposes it as
  `get_verdict_message` / `record_signed_verdict`. A wallet is still free to
  create, so this does not by itself stop sybil registrations — quorum
  requires multiple distinct wallets, and an x402 payment gate on writes is
  the planned next mitigation.
- The in-memory rate limiter is per-process and resets on restart; it is not
  meant to survive a restart-based bypass attempt.
