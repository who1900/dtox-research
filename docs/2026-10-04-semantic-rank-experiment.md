# Semantic rank experiment: 2026-10-04

## Decision

**NO DEPLOY.** The bounded source-candidate prototype did not pass the quality gate: on 93 complete source comparisons, the equal-budget source@10 list found fewer known anchors than the baseline primary@10 list (34/93 vs 39/93). The larger source@20 list found 37/93 by rank 20, but that is a larger result budget, not a semantic-quality win. The actual cross-encoder effect remains unmeasured: zero of 93 source runs completed a real CE rerank. Do not describe this as proof that the broader source-first principle makes the product worse. Production remained unchanged.

## Runs and evidence

The raw runner files are [pilot](../../.scratch_dtox/semantic-rank-20261004/pilot.json), [source-dev](../../.scratch_dtox/semantic-rank-20261004/source-dev.json), and [source-fresh](../../.scratch_dtox/semantic-rank-20261004/source-fresh.json). Independent aggregation used [the analyzer](../eval/analyze_semantic_rank_probe.py); source-only aggregation intentionally excludes CE quality. Saved reports: [source summary](../eval/results/2026-10-04-semantic-rank-source-summary.json) and [pilot summary](../eval/results/2026-10-04-semantic-rank-pilot-summary.json).

SHA-256 provenance:

| Artifact | SHA-256 |
| --- | --- |
| [pilot raw](../../.scratch_dtox/semantic-rank-20261004/pilot.json) | `97445a388530e0d45030677037b9160315f998356fe3d67d627a8b22066d269e` |
| [source-dev raw](../../.scratch_dtox/semantic-rank-20261004/source-dev.json) | `dcf2c15185201c109a73c53f65373b818a357dfd417642e7af4d69d676d45cde` |
| [source-fresh raw](../../.scratch_dtox/semantic-rank-20261004/source-fresh.json) | `8be2a85d2f2d88586bb3728b2d05d65e85ecdc374e2df8a88e4a7e42dfcb7bdf` |
| [source summary](../eval/results/2026-10-04-semantic-rank-source-summary.json) | `ee4151445170939b618eff69015bee4d969beb50e759b51a0681b5c5eb85544f` |
| [pilot summary](../eval/results/2026-10-04-semantic-rank-pilot-summary.json) | `13431b82d5ba43a1a7093ef72afb8d266482b46258077ab45352350444f12e80` |
| [probe runner](../eval/semantic_rank_probe.py) | `10c0779b5757bbe3d2683d1bec9e3e975f768f419192e46e39f106a3e2663c8d` |
| [candidate/rerank module](../api/semantic_rank_experiment.py) | `6e080af0e472210534e905edce45b5d727a3b5be776450e33ada7699d5bbb0f2` |
| [summary analyzer](../eval/analyze_semantic_rank_probe.py) | `a07dab68d65dc8f94efb95616938683d79d915e68f6e357e5fee504010c67c80` |

| Cohort | Queries | Groups | Search/source complete | Known-anchor hit@10: baseline / source@10 | Source@20 hit@20 | All-three-phrase groups: baseline / source@10 / source@20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Dev | 33 | 11 | 33/33 | 16 / 15 | 16 | 3 / 3 / 3 |
| Fresh | 60 | 20 | 60/60 | 23 / 19 | 21 | 3 / 3 / 4 |
| Combined | 93 | 31 | 93/93 | 39 / 34 | 37 | 6 / 6 / 7 |

The all-three-group counts above were recomputed independently from each raw cohort, requiring exactly three distinct phrases and a hit on all three complete rows. Their combined totals are baseline 6/31, source@10 6/31, and source@20 7/31. Do not infer cohort counts by distributing those combined totals. The source@20 column is hit@20. Its hit@10 count was 15/33 dev and 19/60 fresh, equal to source@10 because both use the same ordered candidates; source@20 only adds ranks 11 through 20. The fresh fixture has nine builder-tech queries; its fixture filter mismatch was retained and reported, not edited or used to tune the experiment.

Across the 93 paired, complete baseline/source rows, source@10 vs baseline@10 had **1 binary hit win, 6 losses, and 86 ties**. Mean MRR@10 was **0.294398 baseline** and **0.288202 source@10** (analyzer values 0.2943975081 and 0.2882019116). These are known-anchor retrieval checks, not independent judgments of relevance or precision. Known anchors are incomplete, and a comparator result is not thereby wholly irrelevant.

### Cross-encoder pilot

The pilot attempted three queries. The first CE request hit its approximately 3,014 ms timeout; the run circuit breaker disabled later CE requests. The first two baseline/source searches were partial, and only the third row had complete retrieval. There were **zero actual CE-complete rows**. Pilot fallback order is not CE evidence and is not pooled into the 93-query source-only cohort. The later source runs intentionally used a closed local scorer URL: all 93 retrieval comparisons completed, but no CE request completed. Therefore no CE quality win or loss can be claimed.

### Runtime

Independent post-checks reported 33/33 dev rows complete in **38.387 s** and 60/60 fresh rows complete in **60.258 s**, run separately under the per-run budget. Candidate-build timing across the 93 rows was **25.21 ms mean, 17.05 ms p50, 54.47 ms p95, 277.21 ms max**. This is candidate-build time only, not end-to-end query latency or a latency guarantee. The 33+60 split respects independent bounded runs; do not imply that all 93 were run under one 180-second budget.

## What was tested

The runner used the in-process hierarchical search path, pinned to main SHA `5c3df936534b2b9f9246e0e20d2453c0b5553496a124d12d975b73f3fde79a1a`. Search configuration explicitly kept `SOURCE_DISCOVERY=0`, set `TITLE_BUILD_PRIORITY=1`, disabled auto-relax, and used cached small-384/base-768 query vectors. Scoring was paired title plus passage, with title capped at 256 characters, passage body at 1,200 characters, at most two passages per paper, 20 candidates, and 40 pairs. The installed ONNX reranker was used; no new model, paid embedding/API call, reindex, or production deployment was involved.

Semantic logits only order unverified retrieval leads. They are not probabilities, identity proof, entailment, evidence, or source verification. Candidate provenance retains score, available measured floor, configured layer floor, channel ranks, and `verification_required`; missing measured floor is reported as unknown, not inferred. The runner's floor-bypass candidates are an offline shadow comparison, not primary evidence.

## Interpretation limits

- This compares whole candidate paths, not the isolated causal effect of lowering or bypassing a relevance floor. The bounded treatment selects up to 20 papers with usable non-boilerplate content from the pre-floor hierarchical pool.
- That pool precedes later exact-match, quota, and other passport/final-selection stages. At least two sources were absent from the bounded top-20 candidate set in the dev cohort; six absences were observed across all 93 queries, but not all causes are confirmed. Their absence cannot be attributed uniquely to the relevance floor; candidate cap, content-only eligibility, and later pipeline stages are entangled.
- Source@20 exposes ten more ranks than primary@10. Its larger-budget recall must not be presented as a like-for-like quality improvement.
- Anchor hit rate and MRR measure fixture-anchor retrieval only. They do not establish general relevance, precision, truth, or user-facing answer quality.
- The experiment says this prototype fails the current quality/speed gate. It does not reject the global source-first principle, and it does not establish a production regression because production was not changed.

## QA and follow-up

Final verification: the API suite passed **734 tests with 1 skip in 8.831 s**. The **22 focused core/probe tests** and **11 analyzer tests** also passed; these are separately reported subsets/checks, not additional tests to add to the API total.

The final operational post-check completed on 2026-10-04: API, MCP, x402, and attestor were active; health checks were OK; main SHA remained `5c3df936534b2b9f9246e0e20d2453c0b5553496a124d12d975b73f3fde79a1a`; exact-match SHA was `727f3d7611fe75133204a24434cb3d62e4261495a5799f26cd8d8d073ced1365`. Registry state remained unchanged at 54 nodes, 16 judgments, and 2 links. Qdrant fulltext was green and optimizer checks were OK. Its point count increased from 10,751,100 to 10,751,224 (+124), consistent with legitimate background ingestion; this report does not attribute those corpus writes to the experiment. Operational health does not establish overall production readiness, which was not proven by this experiment.

Experiment cleanup completed after raw hashes were verified and a local copy plus manifest were retained: the three experiment-owned remote temporary directories and the duplicate local scratch bundle were removed. The production/backup copies were untouched. The reproducible raw bundle, manifest, probe archive, and private preparation materials remain retained. After cleanup, all four services were still active. This is cleanup of experiment resources only, not a claim that unrelated project artifacts were reviewed or removed.

Next global work, without changing this decision: retain all qualified channels, including late exact-match and quota candidates, before applying any bounded rerank; evaluate CE quality and latency separately with an explicit budget and CPU isolation. This is a research direction, not a promise of a new model, production flag, or deployment.
