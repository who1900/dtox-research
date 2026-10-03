# Live release evidence: 2026-10-03

Main-attributed production observations supplied on 2026-10-03. This record documents those reports; it did not independently repeat the live MCP calls, payment, or RPC verification. Exact timestamps are included where supplied; wall-clock timestamps for read calls, RPC readback and post-deploy inventory were not supplied. Timings below are individual observations, not latency percentiles or SLOs. This is evidence, not a production certificate. No private IPs, secret paths, keys, or internal credentials are included.

## Public read MCP

The public MCP inventory returned 13 tools. Twelve read/preview paths passed; the signed-write path is the 13th tool and has a separate current devnet test below. Reported main live check 1 and main live check 2 results:

| Read path | Reported result | Observed time |
|---|---|---:|
| `find_papers`, MEV paper `2101.05511` | Known hit | 13.59 s |
| `get_paper` | Successful read | 0.70 s |
| `read_paper_section`, `eip:4337`, limitations | 1,205 characters | 0.29 s |
| Math spec, `2101.05511` | 7 sections | 0.11 s |
| Strict EIP-4337 bundle, `max_chars=2000` | `generated=false`, `search_partial=false`, `partial=true`, `warnings=[]` | 0.75 s |
| `search_research_paper`, `durable_nonce` | 3 hits | 0.83 s |
| `similar_papers` | 3 papers | 0.65 s |
| `compare_methods` | HTTP 200, keys `a` and `b`; no table-count assertion | 0.32 s |
| `get_verdict_message` | Successful preview | 0.24 s |
| Trends, zero-knowledge proofs | 173 canonical results, `status=bounded_sample`, `lower_bound=true` | 8.14 s |
| `validate_project`, ERC-4337 EntryPoint | 1 `strong_candidate`; not confirmed prior art | 10.78 s |
| `count_papers(layer="web3")` | 16,404 canonical papers | 0.69 s |

Canonical paper counts can differ from raw Web3 `done` rows. Do not substitute one measure for the other or infer an exact raw count. The bundle's `partial=true` is recorded alongside `search_partial=false`; neither field has been normalized away. The live latency values are single-call timings, not an SLO or latency distribution.

Registry counts before and after the live reads were unchanged: 53 nodes, 15 judgments, 2 links. The EntryPoint result is a strong candidate only and was not confirmed prior art. The `compare_methods` check establishes its HTTP status and response keys only, not a row/table count.

## Current authorized devnet payment and signed write

Main reports one successful authorized payment plus one signed test record through the actual public endpoint `https://read.whoim.space/x402/mcp` at `2026-10-03T09:55:43.992Z`. This is one attempt, with no autoretry and no follow-up paid or invalid-signature call.

- SDK response: `isError=false`, `receipt.success=true`.
- Network: `solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1`.
- Payment transaction: [Solana devnet explorer](https://explorer.solana.com/tx/31iL5AZXhVpBFVDF2Je42C3uZAirzk2Hg9LpLtLBXd3mw5w8inWYwbvgoYVCW3t4zxMrbTVZVsHcoxsukf8adu8J?cluster=devnet).
- Attestation transaction: [Solana devnet explorer](https://explorer.solana.com/tx/4wwnu49FV5gSshoXyfBteZZhEod5wcNSuQsyBx5e148ESpupQyPQQ47Upzip1tLgjoVQ1AaEyahtpXRK8qysJ3x3?cluster=devnet).
- PDA: `G1BBNygRmArfr4Ut7s3D84SBBwsgSQPRxv6uy6dnFH6b`.
- Reviewer: `HCTag5BBmexuDNsY6QAE9kkiNev3Xe7bgZQ4eAmE2jfc`.
- Script scope: exact devnet mint and recipient, cap `10000` atomic units, charged amount `10000` atomic units (exactly `0.01` devnet USDC), no autoretry.

Test record:

- Claim: `Release smoke 2026-10-03: ERC-4337 prevents every possible denial-of-service attack`.
- Paper: `eip:4337`.
- Verdict: `does_not_assert`.
- Evidence SHA-256: `335696d9c582a3285a719928621789cf8ce38438f51d39b8905ed124b0d0978a`.
- Claim SHA-256: `f22e64c9c301e383abeb45419ae4b2d36708d4d42f38f65c1b24585b0289eab4`.
- Registry readback reported `read_once`, one reader, one `does_not_assert` reading. It is not a quorum or scientific confirmation.

Independent public Solana RPC readback passed. Full genesis hash `EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG` matched devnet; SAS account owner matched; on-chain claim, evidence, paper, verdict, reviewer and `issued_at` fields matched; the decoded on-chain reviewer signature passed Ed25519 verification. Both transactions finalized with `err=null`: payment slot `506956512`, attestation slot `506956519`, seven slots later. For mint `4zMMC9...`, token balance delta was buyer `-10000`, recipient `+10000`, exactly `0.01` devnet USDC. This verifies the minimum current public devnet payment/write path. The public response does not expose the complete attestation record. No internal token, private key, or private endpoint is documented here.

The post-write DB check found one unique PDA row matching the exact claim, reviewer and verdict. `judged_by_model=unspecified` is not trusted-model attribution. Public registry status remains `read_once`, one reader, not quorum. Read-only counts before the payment were 53 nodes, 15 judgments and 2 links; after this single write they were 54 nodes, 16 judgments and 2 links. No extra payment or write was made.

Offline invalid-signature tests remain the evidence for invalid-signature behavior. A live invalid-signature call would exceed the one-payment/one-write authorization and was not made. No paid read, replay, batch, second payment, or retry was performed.

## Backup and recovery observations

Main reports the newest judgments/state backup timestamp as `2026-10-03T01:30:00Z`.

- A real registry backup was restored into an isolated in-memory database. Reported result: PASS, integrity OK, 53 nodes, 15 judgments, 2 links. This small registry restore is not a Qdrant restore and was not a production restore.
- The state backup is 2.42 GB. Its required-table `quick_check` reached the 25-second cap and was interrupted. The check finished incomplete and not verified. This is not evidence of corruption; no full scan is planned.
- The latest weekly FTS backup is dated September 27, 2026.
- The full-text Qdrant snapshot is dated September 27, 2026, size 32.49 GB, checksum recorded.
- No coarse-index snapshot exists; the coarse index is reported rebuildable.
- Full Qdrant/application restore remains unverified and is a production readiness gate. Do not conflate the isolated registry restore with a full restore.

## Qdrant health and memory

Latest reported RAM observation at `2026-10-03 09:48 UTC`: 9.88 GiB of 10 GiB (98.8%), approximately 0.12 GiB available by that reading. It is a point-in-time observation, not a guaranteed peak. Qdrant was green and its optimizer OK. Wrapper PID 321193 and restart count 1 were unchanged. Actual Qdrant process PID 321228 had `oom_score_adj=-900`; wrapper PID 321193 had `oom_score_adj=0`, expected for the wrapper. No OOM guard defect was reported. No RAM increase, rebuild, or restart was performed; those changes require a separate decision.

## Release interpretation

- Current CI for checkout `322ff3a`: success, as reported by main.
- Bounded public read beta: smoke passed for the reported read paths.
- Current devnet payment/write: independent public RPC verification PASS, minimum tested devnet path only. Registry status remains `read_once`, not quorum.
- Paid mainnet and full production: **NO-GO**. RAM headroom and full restore remain known production prerequisites. No mainnet readiness, sustained-capacity SLO, global recall, or independent scientific quorum is established by these observations.

## Post-deploy MCP documentation and test reports

Main reports a post-deploy check after an MCP description-only update; exact wall-clock time was not supplied. HTTP 200 returned for the inventory of 13 public tools. The preview description was corrected and no longer promises automatic claim aliasing. Only the MCP tool description was deployed; the service was active. No API restart, index, RAM, price or network changes were made.

The pre-write read-only registry counts (53 nodes, 15 judgments, 2 links) were unchanged across the two live read checks. After the single paid write, DB verification found one unique PDA row matching the claim, reviewer and verdict, and global counts of 54 nodes, 16 judgments and 2 links. `judged_by_model=unspecified` is not trusted-model attribution. The one reading remains `read_once`, not quorum.

The final main-reported gateway unit suite after script freeze was 38/38 PASS in 2.25 seconds. Standalone CLI TypeScript compilation passed. A helper subset reported 3/3 and overlaps the 38-test suite, so it is not additive. Attestor tests were 47/47 with TypeScript compilation passing. Exact wall-clock timestamps for these local checks were not supplied. These reported local tests are distinct from live RPC evidence and were not rerun for this document.

The MCP description deployment did not restart the API or change the index, RAM allocation, price, or network. The public MCP service remained active; the tool inventory and corrected preview description were rechecked successfully.
