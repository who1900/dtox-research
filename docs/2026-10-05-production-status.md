# Production status - 5 October 2026

**Status: Deployed beta; production readiness not established.** Main confirms that exactly three functions shipped in API revision `8f95e9c9a22107c8094cd1311008d42be0c6e518c8c09ac533a3ba1b444a8d16`, based on `f9c...`. A code backup was created and only the API was restarted. The infrastructure snapshot below is the 17:37 UTC pre-deployment baseline. Final frozen fixture diagnostics are complete; the quality blockers below remain open.

## Snapshot

The API, MCP, x402, attestor, and tunnel are reported active after deployment. Health reports reranking disabled with **0 requests**. The pre-deployment health snapshot had only five latency samples: p50 **1.723 s** and p95 **4.433 s**. Neither snapshot establishes an SLO.

| Signal | Reported state |
|---|---|
| Oracle full-text service | Green; optimizer OK; 10,811,708 points |
| Oracle coarse collection | Green; 180,939 points |
| Oracle Qdrant | OOM=false; restarts=0; container/cgroup memory 9.844 / 18 GiB |
| Oracle host | 23,972 MiB total RAM; 13,662 MiB available; 84 GiB disk |
| Contabo | 38 GiB free; disk 90% utilized; 7,042 MiB available RAM |
| Contabo swap / paging | 9,624 MiB swap reported; `si/so=0` in two `vmstat` samples |

These are point-in-time observations, not a restore test or capacity/SLO proof.

## API failure and verification

The confirmed HTTP 500 root cause was a SQLite BM25 budget `TimeoutError` escaping `_rank_papers`; Main reports the fix is deployed. The final `unittest.TestCase` version passed **11 candidate-ranking regression tests**. The original eight tests were undiscovered and are not counted as passing. The full local API unit run reported **792 passed, 1 skipped (793 total, 8.778 s)**; MCP unit tests reported **28 passed (0.318 s)**; operations probe unit tests reported **10 passed**. These are local/scoped results, not a production or full-restore proof.

The probe parses the JSON object after a leading 404 response prefix and is capped at six HTTP calls, including positive and missing-resource cases. Passing the probe does not establish overall production readiness.

## Staging history and public probes

The first server-candidate staging attempt failed only because the development test dependency `httpx` was missing; this failure is retained in the record. No production libraries were installed. The native staged run later discovered **11 tests: 9 passed and 2 explicitly skipped** because the HTTP test dependency was unavailable. Local candidate tests passed **11/11**.

The public probe passed **6/6 HTTP checks** before and after deployment. Before deployment it took **2.882 s**. The fresh post-deployment probe took **3.048 s** total; `find/read` took **0.461 s**, and spec retrieval **0.398 s**. Both runs reported tool inventory **13**, correct missing-paper/resource `404` / `isError`, errors=0, and partial=false; the fresh run also reported no truncation. These were warm, bounded smoke probes, not SLO or speed-gain evidence. The fresh probe confirms the deployed beta responds; it does not establish production readiness.

## Evidence limits and open gates

- Historical fixture results: **39/93** and **6/31** for the “all-three” measure. The fixture is anchor-biased and historical; these are not accuracy or independent-relevance claims.
- Full restore/recovery validation, SLO attainment, live-write authorization, independent relevance evaluation, overall production readiness, and pilot outcomes remain open.
- The proposed pilot is **three teams over two weeks**; these are not confirmed participants or a commissioned launch.
- The 3 October devnet activity is historical and is not fresh post-deployment evidence.
- No payment, chain write, reindex, or authorization change was made for this deployment.

## Final frozen Web3 fixture diagnostics

Six existing fixture queries were run across two Web3 groups, three wordings per group. All six returned without errors or partial replies.

| Group | Frozen outcomes | Interpretation |
|---|---|---|
| DAG | Literal-anchor ranks: **3, absent, 1**; targets `2105.11827` and `2201.05677` | One wording lost known anchors. Group invariance **failed**. |
| Availability | Ranks: **9, declared twin at 1, declared twin at 1**; targets `1905.09274` and `1809.09044` | Every wording surfaced a known source, including server-declared aliases. |

Across the six queries, literal-ID hits were **3/6** and hits including server-declared twins were **5/6**. For `1809.09044`, OpenAlex `W4289494000` has a server-declared twin mapping to `1809.09044` plus matching source URL and title; this is not independent alias verification. Distractor IDs were absent in all six queries, which is not broad precision evidence. Do not label every miss irrelevant or treat source retrieval as verification that the source asserts the claim.

Connector round trips were **8,336, 13,473, 7,083, 8,154, 10,825, and 4,713 ms**. The first two ran in parallel and the rest serially. These are not p95 data or a before/after performance comparison. No ranking-code change was deployed.

## Proposed global source-quality follow-up - not implemented

Use a rank ledger to record source discovery, candidate admission, bounded evidence retrieval, best qualifying chunk, floor disposition, and final rank. Pair it with a bounded, provenance-preserving evidence shelf so source-level discovery loss can be separated from chunk-floor loss. Do not blindly tune the floor from known anchors. This is a proposal only; no ranking change is implemented. The frozen fixture shows a group-invariance blocker, not a validated global fix.

**Local follow-up (2026-10-06, not deployed):** the API working tree now has a bounded, provenance-preserving related-source candidate fallback for complete empty primary searches, plus structured exact-section 404 guidance from already indexed section metadata. Primary ranking/floor, exact-title matching, and production status are unchanged. These local changes have not been deployed or externally evaluated.

## Release boundary

This report records a beta deployment, not production readiness or launch authorization. Overall retrieval relevance has not been claimed fixed. Keep recovery, SLO, live-write authorization, independent relevance, and pilot gates open until separately evidenced. No private host addresses or secret paths are included.
