# Lexical contention final report, 2026-10-04

Historical strict-gate decision: NO DEPLOY. Subsequent rollout decision is below.

Decision: NO DEPLOY. The cooperative title-build priority prototype failed the strict warm latency gate in the initial sample and after one final fixed confirmation. The prototype stays off, is not shipped, and is not production-ready. No further tests or threshold changes are planned.

## Scope and controls

All runs used the frozen 24-query fixture, SHA256 `9b666344cd3e137e0fe532046b5a13e59c23c5e5c907628e7f1b1e597d0ab5ff`. Saved query vectors replaced embedding requests; response, Qdrant-result, and embedding caches were disabled. Retrieval accessed live data read-only, not a frozen database snapshot. Source discovery and reranking were disabled. Cold traces retained their first requests without prewarming.

Diagnostics used a two-CPU quota and 1536 MB memory; production has unlimited CPU. The baseline recorded zero throttling. These observations support investigating title-build contention but do not establish universal behavior or statistical proof.

Exact weighted FTS5 SQL profiling covered eight first phrases across fresh, reused, and session-only mmap connections: 24 calls in 10.797 seconds. The database contained 465674240 bytes and 20 index segments. All eight groups returned identical IDs and BM25 ranks. Reuse and mmap provided no useful speed benefit. A 100000-operation progress-handler control took 26.484 seconds, still completed only 22/24 responses, and retained timeout overshoot. These controls were rejected without production tuning.

## Cold prototype results

The baseline trace took 20.412 seconds and completed 22/24 responses. Two lexical timeouts overlapped title construction, taking approximately 2.0 seconds wall time despite only 0.40 to 0.55 seconds worker CPU.

The staged priority prototype eliminated those two partial responses, completing 24/24. All 22 previously complete pairs retained identical ID lists. Only the formerly partial responses changed ID ordering. Known-source hit@10 increased from 11/24 to 12/24 because of completion. This measures coverage of known sources, not precision, independently assessed relevance, or better semantic ranking.

Title construction slowed from 7.943 to 14.281 seconds wall time, while CPU stayed approximately 7.337 versus 7.339 seconds. More requests overlapped background construction. Candidate query median latency was higher, although maximum query latency fell from 2.622 to 1.308 seconds. No overall cold median speed improvement is claimed.

## Warm gate: FAILED

The initial warm sample completed all 24 pairs in both variants with identical IDs. Its paired median was -1.552 ms and paired p95 was 139.760 ms, failing the original 100 ms p95 limit. This failure remains reported.

One preregistered confirmation ran two 24-query blocks with alternating pair order: reversed in the first block, restored in the second. One shared TitleIndex was built before query timing. All 48 new pairs completed in both variants with identical IDs; their paired p95 was 118.338 ms.

Pooling the unchanged initial 24 with all 48 new observations gives 72 pairs. All 72 completed in both variants with identical IDs. No failed, partial, missing, or mismatched observations were discarded. Candidate-minus-baseline paired median was -6.224 ms, but p95 was 142.321 ms. The unchanged gate required median no greater than 20 ms and p95 no greater than 100 ms, plus complete identical-ID pairs. The pooled p95 fails, so the final decision is NO DEPLOY.

## Validation and disposition

Local validation reported 744 passes, one skip, and 254 subtests in 15.22 seconds. Two public production queries completed in approximately 4.7 and 4.4 seconds with exact ID lists verified; these are observations, not SLO evidence.

Cleanup is complete. All four services are active and health is `ok`. Production `main.py` (`89ed3d917bb1...`) and `exact_match.py` (`d43cfac8c0c7...`) remain unchanged, as do registry counts 54/16/2. The repository prototype flag defaults to zero, and the production title-build priority drop-in is absent. No deployment, LLM calls, reindexing, additional RAM, or new service cost is authorized by these results.

## Accepted tradeoff and limited rollout, 2026-10-04

The original 100 ms warm p95 gate FAILED: pooled paired p95 was 142.321 ms. The user accepts this measured tradeoff and authorizes limited activation behind a flag with rollback available. The observed benefit is cold completeness only, improving from 22/24 to 24/24; ranking quality did not improve. This authorization and result are not proof of production SLO compliance or production readiness. Deployment has not been asserted here; production rollout status will be recorded separately.

## Current rollout status, 2026-10-04

The rollout procedure was prepared and reviewed, and its artifacts were validated. Execution was blocked by automatic permission review, which required explicit confirmation for replacing two production files, changing the flag, and restarting only the API service. No activation occurred. Production `main.py` (`89ed3d917bb1...`) and `exact_match.py` (`d43cfac8c0c7...`) were retained unchanged; the production priority drop-in and flag activation remain absent. The precheck and post-verification both reported all four services active and health `ok`. A temporary staging area exists; no private production backup was created.

Two fresh public-query observations show that production timeouts remain: Hyena took 8.712 seconds and returned 4 results, with the lexical channel skipped entirely; ColBERT took 5.930 seconds and returned 10 results, with the lexical channel skipped entirely. These are observations, not SLO evidence. They do not establish that the cold prototype benefit is present in production.

## Current rollout status, COMPLETED, 2026-10-04

The user explicitly approved this rollout this turn. The limited production rollout is complete: exactly two API files were deployed, `main.py` SHA `5c3df936534b...` and `exact_match.py` SHA `727f3d7611fe...`, matching the approved pins, plus one priority drop-in. A backup was created with the guard in place. Only the API service was restarted. The prior blocked status above remains historical and records the state before this approval and execution.

Post-rollout guarded verification: all four services are active and health is `ok`; the running priority flag is `1`; `source_discovery` is `0`; registry counts remain `54/16/2`. Paid-contour resources and databases were untouched. The repository flag default remains `0` deliberately; the production drop-in sets the runtime flag to `1`. This is not a bulk auto-deploy.

Local protective tests this turn: 14 passed, 24 subtests. The earlier 744-test result remains historical and is not replaced by this focused run.

Fresh public MCP observations: Hyena completed in 3.749 seconds with 6 results, lexical_channel=paper and no partial flag. ColBERT completed in 3.513 seconds with 10 results, lexical_channel=paper, `partial=false`, and `2004.12832` first. Before this production rollout, Hyena took 8.712 seconds with 4 results and `lex_skipped`; ColBERT took 5.930 seconds with 10 results and `lex_skipped`. These two live observations are not an SLO, independently assessed relevance data, or evidence for a universal acceleration claim. The historical 20/100 warm gate failure remains: pooled p95 was 142.321 ms. The user accepts the completeness tradeoff; this does not establish production SLO compliance or production readiness.
