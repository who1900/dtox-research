# Post-resize release evidence: 2026-10-03

Offline release QA plus main-supplied, read-only live observations. No network, SSH, payment, chain write, live anonymous-write authorization probe, service mutation, installation, or configuration/code change was performed by this QA. One SSH command containing negative legacy-tool probes was rejected by safety review before execution; it was not retried or reconstructed. The signed-write MCP tool was not invoked. Public inventory did not contain legacy anonymous-write tools; inventory absence is not an authorization test.

## Local tests

Each suite ran once against the existing checkout and installed dependencies. Python used `C:/Python313/python.exe`; Node used existing `node_modules`. No installs were performed.

| Suite | Result | Runtime |
|---|---:|---:|
| API unittest discovery | 532 tests, 1 skipped; exit 0 (531 passed) | 9.642 s |
| MCP unittest discovery | 23 passed; exit 0 | 0.519 s |
| Pipeline unittest discovery | 56 tests, 1 skipped; exit 0 (55 passed) | 9.834 s |
| Ops unittest discovery | 11 passed; exit 0 | 0.246 s |
| Eval unittest discovery | 40 passed; exit 0 | 0.046 s |
| x402 gateway `npm test` | 38 passed, 0 skipped; exit 0 | 4.259 s test-reported |
| x402 gateway `npm run build` | TypeScript passed; exit 0 | 8.017 s command wall |
| Attestor `npm test` | 47 passed, 0 skipped; exit 0 | 6.160 s test-reported |
| Attestor `npm run build` | TypeScript passed; exit 0 | 8.913 s command wall |

Python total: 662 tests, 660 passed and 2 skipped. Combined test total: 747 tests, 745 passed and 2 skipped. The API skip is platform-specific; pipeline's optional parser test skipped because `pylatexenc` is unavailable. Warnings/resource warnings appeared during API tests; the suite still exited successfully. The first Node attempt was blocked before execution by sandbox `EPERM` resolving `C:\Users\User`; the identical local commands then passed with approved filesystem access. No test/build failure remains.

## Live observations supplied for this release check

These are main-supplied measurements, not independently repeated here. They are bounded observations, not an SLO or production certification.

- Public tool inventory: 13 tools; 12 read/preview paths invoked; `record_signed_verdict` not invoked. Legacy anonymous-write tools were absent from the public inventory. This does not establish live authorization behavior.
- All 7 Contabo units were active.
- Registry counts after the paid devnet write: 54 nodes, 16 judgments, 2 links. The earlier baseline was 53/15/2. This report performed no write; the previously authorized 0.01 USDC devnet payment and signed write from October 3 were already verified and were not repeated.
- Counts remained 54/16/2 after all 12 read/preview calls.
- Oracle observation at 13:25 UTC: 23,972 MiB RAM total, 13,867 MiB available; Qdrant cgroup 13.47/18 GiB; OOM false; restarts 0. Host disk free: 120 GiB. These are point-in-time values.
- Follow-up Oracle observation at 13:33:25 UTC: 13,944 MiB available, no swap use. Qdrant process RSS was 16,878,384 kB (about 16.10 GiB), split into 9,281,988 kB anonymous (about 8.85 GiB) and 7,596,396 kB file-backed (about 7.24 GiB). Docker/process RSS includes reclaimable mapped file pages; it must not be added to host available RAM or interpreted as anonymous working set. These figures do not prove that a 10 GiB restore fits. Restore may OOM depending on anonymous working set and loader behavior; require fresh measurements, a controlled window and a resource/rollback plan before any separately approved test.
- Contabo read-only preflight: 391 GiB total disk, 336 GiB used, 38 GiB available (91%); RAM 24,031 MiB total, 8,230 MiB available. Swap occupied: 9,645 MiB; this alone is not evidence of current memory thrashing. The 32.49 GB snapshot plus unpacked/index and temporary restore requirements do not safely fit the reported 38 GiB. No transfer/download or container was launched.
- All three Qdrant collections were green and optimizer activity was none. Counts were unchanged since resize: `papers_fulltext` 10,744,313 points / 11 segments; `papers_coarse_base` 180,181 / 2; `papers_coarse` 162,328 / 2. The small embedding engine was false. Docker memory limit changed; no collection retrieval/HNSW setting changed.

### Read/preview calls

All times are individual end-to-end observations, including MCP/tool overhead. They are not service-only timings or SLO evidence.

| Call | Result | Time |
|---|---|---:|
| `find_papers` | 3 results; EIP-4337 ranked first | 9.398 s |
| `get_paper` | Success | 2.696 s |
| `read_paper_section` | Success | 3.483 s |
| Math/spec extraction | 7/7 objects, 2,211 chars | 3.431 s |
| Strict bundle | `generated=false`, `search_partial=false`, `partial=true`, warnings empty | 3.689 s |
| `similar_papers` | 3 results | 4.245 s |
| `count_papers` | 16,404 canonical Web3 papers | 3.894 s |
| Preview | Success | 3.436 s |
| `compare_methods` | D2O 2406.13035 and CAKE 2503.12491; tables 2,574 / 1,633 chars | 3.260 s |
| Durable-nonce search | 3 results | 4.009 s |
| Trends | `bounded_sample`, 157 topic papers | 17.007 s |
| `validate_project` | See known miss below | 22.592 s |

For `compare_methods`, “17/60 available” refers to retrieved candidate chunks, not table count. An earlier comparison against ID `2503.02558` returned 404 because the tester supplied the wrong ID; this is a tester error, not a product defect.

The stored API-health window contains 9 observations; reported p95 was 15.215 s. These are stored-window observations, not 9 health calls in this QA run. The window may include a prior outage and is not a fresh, representative SLO. Do not describe it as one.

### Retrieval limitation observed

The exact idea was: `ERC-4337 EntryPoint security design for an execution agent`. The exact claim was: `ERC-4337 EntryPoint is a central trust point whose implementation concentrates account abstraction security risks`. The same idea/claim was checked at two depths, not with two separate formulations. At `deep=false`, validation returned `weak_signal_only` with empty evidence. The `deep=true` recheck took 13.027 s and remained `weak_signal_only`, evidence empty; best candidate score was .765 and canonical `eip:4337` was still missing. Candidates included SuperPaymaster `2605.05774` (.765), OpenZeppelin docs `c16c88338b1c1f53` (.757), and EIP-7769 (.765). The canonical source's section endpoint and bundle did return exact text including the central trust-point warning. This confirms the observed recall gap beyond fast mode. It is a known miss for this idea/claim, not evidence that a new regression was introduced. Absence of a typed error is not a relevant answer and this result is not a retrieval-quality pass. No threshold/code patch was made in this QA turn.

An unauthenticated `POST /v1/search` returned HTTP 401. A previous GET to this endpoint returned 405 because the tester used the wrong method; the corrected POST is the authorization evidence. Anonymous negative writes were not tested: an attempted probe was blocked by safety review before execution. Inventory absence and offline boundary tests are not substitutes for a live negative-write test.

Repeated VDF search succeeded in 4.691 s and returned the same three IDs: `oa:W4394595369`, `iacr:2018/712`, and `2211.08162`.

EIP-7702 search succeeded in 5.428 s and returned exactly the same ordered IDs as the stored pre-resize baseline: `eip:7702`, `2512.12174`, `gh:foundry-book:24db2abcb43d094f`. This is a narrow regression comparison, not a general retrieval guarantee.

Main review noted a structural difference for named-source retrieval: `validator_run_search` uses `_top=False` and intentionally skips exact injection, while ordinary search uses `_top=True` and includes it. This is a review observation relevant to the named-source gap, not a proven root cause for all misses; no code or threshold change was made in this QA. Keep named-source hydration distinct from evidence bands so a matching ID is not presented as an assertion or validation result.

## Restore and release decision

The September 27, 2026 full-text snapshot is 32.49 GB and is already present on Oracle storage. Full restore remains unverified. Contabo's 38 GiB free disk is insufficient to stage a transferred snapshot plus unpacked/index and temporary restore requirements; no transfer occurred. On Oracle, do not count the already-present read-only snapshot twice, but calculate the incremental space for restored data, temporary files and segments, logs, and production reserve. Oracle's 120 GiB free disk makes it only a possible existing-resource candidate, not a ready restore target. At 13:33:25 UTC Oracle showed 13,944 MiB available, but Qdrant RSS was 16.10 GiB including 7.24 GiB file-backed mapped pages and 8.85 GiB anonymous. RSS cannot be treated as entirely unreclaimable or combined with `MemAvailable`; these numbers do not prove a 10 GiB restore fits. Fresh RAM/anonymous-RSS reserve proof, exact incremental disk calculation, a controlled window, and a resource/rollback plan are required before any separately approved test. Do not infer capacity from point readings. Restore readiness also requires source-time manifest comparison. A full-text snapshot alone is not proof of full application recovery.

**Decision:** conditional bounded beta/hackathon demo only, using known exact IDs and with the recall limitation disclosed. Production and mainnet are **NO-GO**, unconditionally for this release, until resource preflight, verified full application restore, and a release-acceptable outcome for the observed ERC-4337 recall gap. No restore SLO, p95 claim, global recall claim, sustained-capacity claim, or new payment/write claim is supported here.
