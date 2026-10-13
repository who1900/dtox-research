# Large-search evaluation: 2026-10-04

## Executive readout

This was a bounded, read-only retrieval experiment, not a release qualification. Source discovery remains **OFF**. The test found unchanged primary results in the paired 10-vs-10 comparison, more known-anchor reach when candidates were included, measurable tail-latency cost, and source-URL integrity defects. Do not describe this as production-ready or as a broad accuracy result.

The independent summary was checked against all 93 planned fixture queries: 93 raw records, no unattempted queries, no duplicate raw-key conflicts, and 822 boolean metric spot-checks with zero mismatches against fixture target IDs/aliases. Metrics below are known-source anchor hits, not precision, answer correctness, or exhaustive recall.

## Test design and scope

- Three fixture files: two previous files totaling 33 queries and one newly authored/frozen file with 60 queries; 31 groups total, exactly three phrasings per group. These are curated tests, not a random population sample or independent relevance judgments.
- Each query planned three serial, rotating variants: primary top 10 with discovery OFF; primary top 20 with discovery OFF; primary top 10 with discovery ON and up to 10 source candidates. That is 279 planned retrieval calls.
- Queries used one-time embeddings followed by frozen vectors for variant comparisons. Development run: 84.772 s; fresh run: 154.388 s; 239.160 s combined, including embedding generation. No hard timeout or runner error. Three queries had at least one partial variant, leaving 90 complete triplets.
- The original 33 and fresh 60 remain separate cohorts. The frozen fresh set is reported in full; its nine builder-tech cases are not removed after seeing results. A separate layer-compatible fresh subset has 51 queries and is diagnostic only.
- The runner’s preflight coverage map was not used as an exclusion filter: it includes hard-negative identifiers. Missing/unknown coverage does not remove a planned query or turn a miss into a hit.

## Retrieval results

All denominators below are explicit. “Hit” means at least one declared target or target alias appeared in the specified result window.

| Measure | Primary 10, discovery OFF | Primary 20, discovery OFF | Discovery ON, primary 10 + candidates |
| --- | ---: | ---: | ---: |
| Complete variants / 93 | 92 | 92 | 90 |
| Failure-inclusive primary hit@10 | 39/93 | 36/93 | 39/93 |
| Failure-inclusive primary hit@20 | 39/93 | 44/93 | 39/93 |
| Failure-inclusive discovery reach hit@20 | 39/93 | 44/93 | 62/93 |

On the 90-query common-complete set, primary-10 hit@10 was 39/90, primary-20 hit@20 was 43/90, and discovery reach hit@20 was 62/90. These are known-anchor retrieval observations; candidates are unverified and must not be counted as relevant answers or precision. Known hard-negative candidates appeared in 8 development and 9 fresh query observations (17 total); this is an inspection signal, not evidence that those works are false or irrelevant.

The discovery-10 primary compact result objects were byte-for-byte equivalent to primary-10 in all 90 common-complete pairs (same IDs and objects, 90/90). The equal-budget primary-20 comparison had 21 wins, 2 losses, and 67 ties on target reach. At group level, all three phrasings hit for 6/31 groups with primary-10, 7/31 with primary-20, and 14/31 with discovery reach. Broad queries may legitimately retrieve alternative anchors; that does not establish answer accuracy.

The all-three-miss list includes a broad Zcash query whose declared anchor is arXiv:2008.00881, a Zcash tutorial. The returned original Zerocash whitepaper may be a correct mechanism match despite missing that named anchor. This is a known-anchor miss, not a verdict on scientific correctness.

## Latency and response size

Paired on the same 90 complete queries, discovery-minus-primary-10 median latency was **+12.695 ms**; the p95 of paired deltas was **+543.452 ms**. Cluster bootstrap intervals (31 fixture/group clusters; exploratory, not an SLO) were [-12.960, 34.958] ms for the median and [371.887, 950.468] ms for p95. The uncertainty and positive tail do not support a “no latency penalty” claim.

For those paired complete responses, p95 latency was 1,234.438 ms (primary-10), 1,236.768 ms (primary-20), and 1,492.158 ms (discovery-10). These timings exclude query embedding generation and are not public-client end-to-end latency. Average response sizes were 15,778, 28,968, and 27,329 **JSON characters**, respectively, not tokens. The larger top-20 and discovery response bodies have real transport/storage cost.

## Integrity findings and blockers

**Do not enable source discovery.** Candidate integrity passed in 88/93 discovery responses; five query lists contained six cards across five queries with missing or non-HTTP actual source URLs. This is a concrete release blocker for the OFF feature, not a cosmetic score issue.

A broader source-attribution metadata issue was also observed with discovery OFF: missing URLs on nine unique `wp:` paper IDs across the raw primary outputs: `chainlink-v1`, `chainlink-v2`, `ethereum-yellowpaper`, `celestia`, `solana`, `zerocash`, `plasma`, `uniswap-v4`, and `mastercoin`. This observation is limited to returned records; it does not prove the entire corpus is corrupt. It is a higher-priority global metadata/source-attribution audit than the candidate-only defect. One `mastercoin` identifier returned a Tendermint title; flag for metadata audit, do not automatically rename it.

The nine builder-tech cases had a fixture/filter mismatch discovered after the run: their target papers’ actual layers are `llm-slm` / `ai-agents`, not `builder-tech`. They are not fair same-layer retrieval-quality cases, but remain in the frozen 60-query headline and are interpreted separately as filter diagnostics. A separate one-query-per-builder-group diagnostic, with the layer omitted, retrieved the corresponding anchor at rank 1 in 3/3 complete checks (0.716 s, 1.489 s, 1.310 s). Three separate paper reads completed in 0.617 s, 1.035 s, and 1.290 s. This tests three examples only; it does not show that all nine cases are fixed or validate the API’s layer semantics.

## Validation and public API scope

- API test run: 660 discovered; 659 passed and 1 skipped. MCP: 24 passed; evaluation: 3 passed; TypeScript attestor: 47 passed; gateway: 38 passed. Total: 771 passed, 1 skipped across 772 test invocations. The five new runner unit tests were included. These checks do not certify retrieval quality.
- Public SDK exercise: 13 tools listed, 11 unique read-only tools invoked. There were 29 public calls total: 16 initial calls (12 positive, 3 expected negative errors, 1 off-scope biology query), one bundle clarification, six exact-ID fresh cross-checks (0.406 to 0.941 s), and six builder diagnostics above. One bundle response was `partial: true`, `search_partial: false`, and capped at 2,459/2,500 characters: budget-limited output, not incomplete search. Six builder diagnostics are diagnostic examples, not a 9/9 repair claim.
- Deliberately excluded: `get_verdict_message` (metadata-mutating), `record_signed_verdict`, paid x402 end-to-end calls, paid credits, LLM judges, reindexing, deployment, configuration changes, and restarts. No production code changes were in scope. No service was restarted.

## Reproduction and handling

Run only in the approved isolated, read-only evaluation environment; this report does not request a rerun. Supply the already approved hash and fixture paths locally—do not copy private host paths, credentials, or raw records into this report or a public artifact.

```bash
python eval/large_search_eval.py \
  --api-dir "$READONLY_API_CHECKOUT" \
  --fixtures eval/paraphrase_cases_20261003.json eval/paraphrase_holdout_20261003.json \
  --output .scratch_dtox/large-eval-20261004/dev-replay.json \
  --expected-main-hash "$APPROVED_MAIN_SHA256"

python eval/large_search_eval.py \
  --api-dir "$READONLY_API_CHECKOUT" \
  --fixtures eval/paraphrase_fresh_20261004.json \
  --output .scratch_dtox/large-eval-20261004/fresh-replay.json \
  --expected-main-hash "$APPROVED_MAIN_SHA256"
```

The archived raw JSON and generated vectors are private evaluation data; publish only sanitized aggregates. The sanitized summary is `eval/results/2026-10-04-large-search-summary.json`. Local raw-data hashes were verified unchanged and temporary server data was removed; prior API/MCP health checks were active. No commit or push is part of this task.

## Decision

Keep `SOURCE_DISCOVERY` **OFF**. The observed primary identity stability is encouraging but is not a correctness or safety approval; candidate URL integrity fails the release gate, metadata URL defects also affect the existing primary path, builder-tech coverage is filter-mismatched, and tail latency is unresolved. Fix and separately verify the scoped metadata issues before proposing another feature-on evaluation.
