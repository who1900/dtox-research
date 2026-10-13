# DTOX full tool test | 2026-10-02

Status: Completed bounded assessment; release not signed off. The only unrun read-only golden case is `mev-sequencer`; write-dependent `registry-survives-rewording` is skipped separately. Local checks were executed by the sidecar. Live findings, infrastructure and CI status were supplied by the main agent.

Report date: 2026-10-02, Asia/Qyzylorda. Main live snapshot date: 2026-10-01 UTC.

## Local tests and CI

| Suite | Passed | Skipped | Failures / errors |
|---|---:|---:|---:|
| API unittest | 497 | 1 POSIX-only | 0 |
| MCP unittest | 11 | 0 | 0 |
| Eval unittest | 40 | 0 | 0 |
| Python subtotal | 548 | 1 | 0 |
| Attestor npm tests | 27 | 0 | 0 |
| x402 npm tests | 15 | 0 | 0 |
| Total: 591 tests | 590 | 1 | 0 |

Final Python suites ran unmodified without guards or artificial skips. API: 7.979 s; MCP: 0.028 s; eval: 0.032 s. Both normal npm builds passed: attestor 7.00 s, x402 6.24 s. Final parallel rerun: 21 s. Python compile: 82 files, zero errors, three SyntaxWarnings. Earlier guard-induced skips are superseded.

[CI run 36922278149](https://github.com/who1900/dtox-research/actions/runs/36922278149) completed successfully at head `7e22e68`, including dependency audit, Python units, TS tests/builds and Docker health/embed-384/rerank smoke.

## Live coverage and payment boundary

Main MCP discovery: 13 tools, all invoked. Twelve ordinary calls: `search_research_paper`, `get_research_bundle`, `find_papers`, `get_paper`, `read_paper_section`, `count_papers`, `similar_papers`, `get_code_or_math_spec`, `compare_methods`, `get_verdict_message`, `research_trends`, `validate_project`. The thirteenth, `record_signed_verdict`, received only a negative live call with `judgments=[]`; successful signed writes are covered only by local mocks.

`get_verdict_message` is not pure read: called once on main MCP and also passed as a free gateway call. Its API path uses `_judgments_conn` / `_canonical_claim` and can INSERT a new `claim_nodes` metadata row and commit. No judgment, verdict or attestation writes and no live wallet transaction. No rollback or cleanup performed; zero registry mutation is not claimed.

Empty judgments returned an internal HTTP 422 error in content with MCP `isError=false`; no DB callback reached. Clients must inspect payload errors. Hidden `record_claim_judgment` and `link_claim_nodes` returned UnknownTool with `isError=true`; these probes are outside the 13 listed tools.

x402: all nine tools invoked; three free ordinary calls passed, six paid tools returned unpaid challenges with `isError=true`, not successful paid executions. Live mode and all accepted payment requirements were Solana devnet only. TS RPC calls were mocked or used fake upstreams.

| x402 tool | Quoted USDC base units | Actual gateway coverage |
|---|---|---|
| `dtox_service_info` | Free | Called |
| `search_research_preview` | Free | Ordinary call passed |
| `get_verdict_message` | Free | Ordinary preparation call passed |
| `get_evidence_bundle` | 10000 ($0.01) | Payment required, isError=true |
| `get_code_or_math_spec` | 30000 ($0.03) | Payment required, isError=true |
| `compare_methods` | 50000 ($0.05) | Payment required, isError=true |
| `research_trends` | 30000 ($0.03) | Payment required, isError=true |
| `validate_project` | 250000 ($0.25) | Payment required, isError=true |
| `record_signed_verdict` | 10000 ($0.01) | Payment required, isError=true; no write |

Preliminary signed request with reviewer shorter than 32 characters returned MCP `-32602`. Correctly shaped dummy zero-public-key/invalid-signature request reached only the payment gate before downstream authentication/attestation. No wallet key, valid signed verdict, charge or settlement. Paid E2E not run.

## Retrieval and extraction

Main Web3 golden: nine searches, seven passes, two failures. Chainlink v2 and descriptive EIP-7702 missed the expected top-10 target. Observed ranks: exact EIP-7702 1, ERC-4337 1, blob 9, VDF 4, nonce 3, named SIMD-0085 1, named SIMD-0297 1. Exact-ID success does not cancel descriptive-query failure.

Separate live quality batch: 15 LLM/agent rank cases, hit@3 9/15, hit@10 12/15, MRR@10 0.4163; no HTTP/tool errors. Adapted golden: 15/16 read-only cases executed, 13 PASS / 2 FAIL: `entropy-kv-budget` missing MEDA `2502.17599` despite paraphrase recall 2/3, and `onchain-oracle` missing `wp:chainlink-v2`. Only `mev-sequencer` was unrun; write-dependent `registry-survives-rewording` was skipped separately. These live batches are separate from local unit totals.

Golden execution is an adapted public-MCP fast run with `deep=False`, not the original REST harness at default depth. Fixture criteria are unchanged, but depth may explain the MEDA miss. No new regression is established without an equal-depth comparison. Most of the latest six before/after Web3 ranks were unchanged.

SIMD-0085/0297 returned zero code/math/table elements despite `fulltext=true` and prose outlines. Fixture expectations are questionable; do not call this a parser bug without source evidence that the expected elements exist.

D2O: 22/39 equations, 3978 characters, raw LaTeX verified. Tables-only comparison: D2O two numeric tables/2574 characters; CAKE one table/1633 characters. Both passed 3000-character per-paper budgets.

Deep Web3 ERC-4337 audit: pure-read generic claim plus paraphrase, 12.59 s, `error=null`, `best_score=0.833` matching maximum evidence score. Returned `strong_candidates`, zero read/three unread candidates, six graph neighbors with hub weights, three limitations, zero supporting math, coverage 42 (moderate). Canonical EIP-4337 was absent from top-three evidence despite the ERC-4337/bundlers query: another quality gap, not a functional failure or contradiction of search ranks. No automatic prior-art conclusion.

## Confirmed findings and validation

- Pagination: normal 500 + 453 characters continued correctly. Valid title with `offset=-1` returned `chars=0`, `next_offset=-1`, confirming a bad-input continuation loop. Recommend `offset: int = Query(0, ge=0)` plus a regression test; not implemented.
- Bundle budget PASS: query `LLM agent tool use failures`, layer `ai-agents`, limit 3, budget 3000 returned exactly 3000 actual/declared characters after `r.content.decode('utf-8')`; all fragment lengths matched. ACL: 1000 characters, 1003 UTF-8 bytes. Earlier 3003/3000 overrun is withdrawn, not a server bug. SSE `text/event-stream` lacked a charset; the client's `r.text` defaulted to ISO-8859-1 and misdecoded accented text. Testing-client pitfall only.
- Other bundle cases: budget 500 returned exactly 500/`partial=true`; budget 6000 returned 3866/`partial=false`.
- Strict wrong layer returned zero; unknown term rejected 400; bundle budget 499 rejected 422; missing paper returned proper error.
- Fresh 2026 queries returned 2026 IACR/arXiv papers; `builder-tech` static analysis returned Slither at rank 1.
- Bad UTF-8 and malformed JSON each returned HTTP 400 / JSON-RPC `-32700`; REST `/v1/search` without a key returned 401.

Maximum observed main spec latency: CAKE 13.146 s, cold-ish and uncontrolled. Most observed reads: 0.1-3.2 s. No service-wide SLO, p95, load or stability claim.

## Bounded infrastructure snapshot

- Completed documents: 179670, unchanged from 20:37:28 to 20:41:36 UTC (4 min 8 s); last completion 20:25:33 UTC. Fourteen stage heartbeats plus reporter fresh; no hung stages; services healthy, Qdrant green. Growth rate not confirmed.
- Earlier queue: 13 quality_checked (11 ACL, two old PMLR missing abstracts); other active states 0. Deferred 29183, rejected 40469, off-niche 1038886. SQLite total 1288242; FTS 179670. Harvest-cursor progress does not prove new docs or source exhaustion.
- Raw completed-layer overlap: llm 148133, agents 18551, web3 17228, builder 16381. MCP web3 count 16384 is the expected canonical-paper count: `paper_facets` uses `canonical_id` plus a seen set for twins-dedup. SQL 17228 counts raw records. Not a mismatch bug.
- Qdrant points 10693340; coarse-base 179669 metadata-only records. Memory 9.639/10 GiB (96.39% cgroup cap); disk 89% used/latest 41 GB free (earlier 42 GB); swap 9 GB. Capacity risk remains despite OOM-score protection.
- No new Qdrant/embed OOM errors in bounded logs; S2/refs 429s. OOM guard timer active, actual score -900. Nginx streaming buffering/gzip off; ten code hashes matched.
- Final monitor with the verified endpoint: ops availability PASS. API/embed health OK, 13-tool MCP schema OK, Qdrant green, backup age 19 h. Fourteen stages plus reporter fresh in a two-minute-old snapshot; idle except fulltext busy.
- Latest done 179670, queue 14, no completion-update activity for 21 min. First-completion/reprocessing rates unknown without journal evidence. Monitor progress OK by its 90-minute threshold, not proof of healthy ingest; growth unproven and queue nearly empty.
- Public health: latest 30 samples, window-observed p95 2.594 s, not a service SLO. Reverse-tunnel/old-benchmark failures pre-existing, not an active outage. No production code/config changes or restarts.
- Public GET `/research/` and `/research/stats.json`: 200; cached snapshot as_of `2026-10-01T21:00Z`: documents 179671, chunks 10693370, citation_edges 3372827, web3_documents 17228 (raw records). Earlier SQLite 179670 at 20:41 UTC versus latest public 179671 indicates a small net count change of +1, not a growth-rate measurement or first-ever ingestion journal evidence; the earlier window delta of zero remains true, but zero growth throughout the entire test is not claimed.

## Safety and assessment limits

Git was clean before report creation. Hygiene scan: 165 public files plus index versions, no staged files, no matches for checked secret patterns. Not an exhaustive history audit. Only the HTML and Markdown reports were authored. No production source/config changes, no live verdict/attestation writes; claim-node metadata may be created by message preparation. No installation, commit, push or paid calls. Final unittest fixtures, pycache and build dist were permitted.

x402 nine-tool coverage and deep ERC-4337 audit complete; final monitor confirms availability, not growth, soak or capacity. Paid x402 E2E not run. Only read-only golden case `mev-sequencer` was unrun; write-dependent `registry-survives-rewording` was skipped separately. Release not signed off.

## Recommended priorities; no fixes implemented

1. Reject negative offsets with `Query(0, ge=0)` and a regression test. Correct the MCP error envelope with `ToolError` or an explicit error flag; clients must inspect payload errors meanwhile.
2. Expand the retrieval candidate pool and validate fixed failing queries, with equal-depth golden comparisons. Separate recall from score calibration; no blind threshold lowering.
3. Investigate source yield and queue policy: earlier queue 13 included two PMLR items missing abstracts; latest queue 14. Availability and cursor progress prove neither growth nor source exhaustion.
4. Address 96.39% cgroup memory usage and disk headroom. OOM score -900 does not remove the hard memory-cap risk.
