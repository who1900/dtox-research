# x402 Gateway + Attestor Payment Release Gate

**Decision: NO-GO for paid mainnet.** The current gateway is configured for Solana devnet only. One authorized current devnet payment and signed write passed independent public RPC readback on 2026-10-03. This verifies the minimum tested devnet path only. Mainnet remains disabled; EVM payment support is unverified.

Live, post-deploy and chain observations below are main-attributed; exact timestamps are shown where supplied. This is evidence, not a production certificate.

Scope is limited to `x402-gateway` and `attestor` as the signed-verdict write path and its payment boundary. This is a bounded code/test review, not a penetration test or production re-verification. The offline review and tests did not use secrets, keys, signing, network/RPC calls, payment, or chain writes. The separately authorized live run is attributed below and was not repeated for this document update.

## Checks run

On 2026-10-03, using `C:\Program Files\nodejs\node.exe` and the existing local dependencies:

| Check | Result |
|---|---:|
| `x402-gateway`: full unit suite after script freeze | 38/38 passed in 2.25 s |
| `x402-gateway`: `tsc -p tsconfig.json --noEmit` | Passed |
| `attestor`: `tsx/dist/cli.mjs --test test/*.test.ts` | 47/47 passed, 0 skipped |
| `attestor`: `tsc -p tsconfig.json --noEmit` | Passed |

Gateway payment tests use the installed x402 SDK with an offline mock facilitator/MCP transport; attestor chain tests use mock RPC/chain state. They are local contract tests, not real settlement, RPC, or mainnet evidence.

The latest main-reported full gateway suite is 38/38, including the helper 3/3 subset; the subset overlaps and is not additive. This supersedes the earlier 35/35 result. Standalone CLI TypeScript compilation passed. Attestor tests were 47/47 with TypeScript compilation passing. Exact wall-clock timestamps for these local checks were not supplied. The live RPC verification below is a separate evidence class.

## Evidence by boundary

| Boundary | Verified in code/tests | Limit |
|---|---|---|
| Authentication and authorization | In live mode, paid tools are wrapped by x402. Tests confirm failed upfront settlement makes zero signed-write handler calls; successful upfront settlement precedes that handler. Ordinary paid reads use authorization: handler failure does not settle, success settles afterward. The gateway does not hold a caller key or perform the verdict signature check itself. Attestor requires `X-Internal-Token` on protected routes, compares it timing-safely, and validates the reviewer's Ed25519 signature over the canonical message before chain operations. | Fixtures do not prove facilitator production behavior, deployed token isolation/rotation, or a live caller's settlement. Payment authorizes an attempt; it does not establish reviewer identity beyond the separately verified wallet signature. `/healthz` is intentionally outside the internal-token check. |
| Settlement-before-write | `x402-gateway/src/payments.ts` assigns `paymentFlow: "upfront"` to `record_signed_verdict`; offline tests verify event order and zero handler calls on failed settlement. The authorized live devnet call returned `isError=false`, `receipt.success=true`, and a signed-write response; independent public RPC verified the payment and attestation records. Payment finalized at slot 506956512; attestation at slot 506956519. | This verifies one devnet path only. Upfront settlement is not atomic with the later attestor write: a paid handler failure retains the payment receipt and has no automatic refund. |
| Retry and ambiguous outcomes | `x402-gateway/src/upstream.ts` calls signed writes with retry disabled. Tests cover timeout/no autoretry and that only transient reads retry once. | A timeout can leave the caller uncertain whether downstream work committed. The caller must inspect the payment receipt and attestation state before a deliberate retry; a retry may be another paid attempt. |
| Batch and errors | Tests distinguish all-rejected batches (tool error with full item diagnostics and paid receipt) from partial batches (every item status preserved with receipt); empty results and upstream protocol errors are covered. | No live paid partial/all-invalid batch was exercised. Price semantics are per signed-write attempt/batch, not per accepted item. |
| Replay and idempotency | Attestor derives a stable nonce from `(claim, paper, reviewer)`, serializes same-PDA writes in a bounded process-local queue, reuses an exact existing record without another transaction, and rejects equal/older conflicting records. Tests cover exact and concurrent repeats, rollback, timestamp conflicts, malformed persisted timestamps, and lock release on RPC failure. The request timestamp is checked against a 10-minute age window and one-minute future skew. | These are mock-chain tests. Locking is process-local; no multi-instance or live-chain race test was run. Idempotent attestation reuse does not undo or refund a repeated upfront payment. |
| Provenance and quorum boundary | Gateway forwards the research bundle response; existing product contract qualifies evidence/provenance and does not treat retrieval as proof. Attestor records the reviewer's key and signature, proving authorship of that canonical verdict. Payment and multiple wallets alone are explicitly not scientific quorum; quorum depends on trusted model identities established upstream. | Provenance is not an immutable corpus/index snapshot. Attestor is a signed-record writer, not the scientific quorum adjudicator. No claim of independent reviewers or scientific confirmation follows from payment or an on-chain record. |
| Network/asset boundary | Gateway config and docs identify current live mode as Solana devnet only. Attestor checks the RPC genesis hash against Solana devnet on startup unless the explicit mainnet override is set. | Ethereum Sepolia has no default asset mapping in SDK `@x402/*` 2.24.0; no asset should be guessed. Configurable EVM networks are not evidence of supported live settlement. Mainnet remains a separate, unapproved gate. |

## Authorized devnet proof and response contract

One authorized payment and one signed test record were submitted to the public `https://read.whoim.space/x402/mcp` endpoint at `2026-10-03T09:55:43.992Z`. No second attempt or autoretry was made. The public response reported SDK `isError=false`, `receipt.success=true`, network `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1`, and an attestation transaction/PDA. Independent public Solana RPC readback passed: the full genesis hash was `EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG`; SAS account owner matched; claim, evidence, paper, verdict, reviewer and `issued_at` fields matched; the decoded on-chain reviewer signature passed Ed25519 verification. Both transactions finalized with `err=null`. Payment slot was `506956512`; attestation slot was `506956519`, seven slots later.

- Payment transaction: [Solana devnet explorer](https://explorer.solana.com/tx/31iL5AZXhVpBFVDF2Je42C3uZAirzk2Hg9LpLtLBXd3mw5w8inWYwbvgoYVCW3t4zxMrbTVZVsHcoxsukf8adu8J?cluster=devnet).
- Attestation transaction: [Solana devnet explorer](https://explorer.solana.com/tx/4wwnu49FV5gSshoXyfBteZZhEod5wcNSuQsyBx5e148ESpupQyPQQ47Upzip1tLgjoVQ1AaEyahtpXRK8qysJ3x3?cluster=devnet). PDA: `G1BBNygRmArfr4Ut7s3D84SBBwsgSQPRxv6uy6dnFH6b`.
- Reviewer: `HCTag5BBmexuDNsY6QAE9kkiNev3Xe7bgZQ4eAmE2jfc`.
- Claim: “Release smoke 2026-10-03: ERC-4337 prevents every possible denial-of-service attack”; paper `eip:4337`; verdict `does_not_assert`; evidence SHA-256 `335696d9c582a3285a719928621789cf8ce38438f51d39b8905ed124b0d0978a`; claim SHA-256 `f22e64c9c301e383abeb45419ae4b2d36708d4d42f38f65c1b24585b0289eab4`.
- Registry result: `read_once`, one reader, one `does_not_assert` reading, not quorum.
- Script scope: exact devnet mint and recipient, cap `10000` atomic units, charged amount `10000` atomic units (exactly `0.01` devnet USDC), no automatic retry.

The public MCP response does **not** expose the complete attestation record. The independent public RPC readback matched the on-chain fields and validated the decoded signature. Attestor's internal `GET /attestation/:pda` route is behind `X-Internal-Token`; do not expose that token or use an unapproved access path. Post-write DB verification found one unique PDA row with the exact claim, reviewer and verdict. `judged_by_model=unspecified` is not a trusted model identity. Global counts changed from 53 nodes/15 judgments/2 links before this single write to 54/16/2 after it; registry status is `read_once`, one reader, not quorum. No invalid-signature call, paid read, replay, batch, additional payment or write was made; offline invalid-signature tests remain the evidence for that case.

This result verifies the tested public devnet payment-and-write path. It does not prove paid-read behavior, failure/replay cases, production readiness, scientific quorum, or mainnet safety. Mainnet still requires separate explicit authorization, verified mainnet asset/facilitator configuration, independent review of deployment controls, and a new release decision. Retain **NO-GO for paid mainnet**; EVM support is unverified.

## Post-deploy MCP description verification

Main reports HTTP 200 for the 13-tool public inventory after the MCP description-only deployment. The preview description was corrected and no longer promises automatic aliasing. The service is active. Only the MCP description was deployed; no API restart, index, RAM, price, or network changes were made. The exact wall-clock time of this verification was not supplied. This does not change the NO-GO decisions for production or paid mainnet.
