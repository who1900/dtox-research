# Source attribution release: 2026-10-04

## Shipped change

Deployed five API files after local tests, isolated comparison and code-hash checks: `main.py`, `evidence.py`, `research_bundle.py`, `source_attribution.py`, and `whitepaper_sources.json`. The previous code is retained for rollback. API health passed after restart. MCP, payments, environment/secrets, ingestion decisions and Qdrant settings were not changed. Production source discovery remains OFF.

Source resolution now validates HTTP(S) URL syntax and uses valid stored metadata or payload URLs first. Historical whitepapers have a conservative catalog fallback requiring both their ID and normalized title to match. Unknown origins stay explicitly missing rather than receiving invented arXiv links. Syntax validity is not availability or proof that the URL's content matches the indexed revision. No URLs are fetched during result hydration; downstream downloaders still need their own SSRF controls.

The live read-only metadata probe found 29 indexed whitepapers with `source_url=NULL`. Their ingest INSERT did not persist that field. The API fallback resolved all 29 tested records without altering the DB. This fixes citation output, not persistence in the ingest path. The historical `wp:mastercoin` ID remains unchanged; its matched title is Tendermint, and the alias warning is returned. Catalog URLs carry individual verification statuses. Chainlink 2.0 availability remains inconclusive; EigenLayer has a verified primary-source reference, not a claim that its PDF was fetched successfully.

Source URL, origin, verification status and missing-provenance labels survive hydration, compact source candidates, paper passports, extracted sections, comparisons and research bundles. Existing indexed-fragment hashes are preserved. Malformed/missing catalog files, including invalid UTF-8 and invalid JSON schema, fail closed without taking down non-whitepaper retrieval.

## Local QA

- Final API unittest discovery: 688 tests, 687 passed and one platform skip; 10.444 seconds.
- Final combined API/MCP pytest run with a workspace-local temporary directory: 731 passed, one skipped, 235 subtests passed; 15.68 seconds. Three deprecation warnings remain. These totals overlap the unittest run and must not be added.
- Earlier runs encountered a temporary fixture-freeze race, a legacy assertion expecting three metadata reads instead of the new one, and pytest's system-Temp permissions. The stable final runs passed after correcting the assertion and using a writable test directory. No intermediate build was deployed.
- `git diff --check` passed. Pre-existing unrelated checkout changes were preserved; this report does not claim a Git commit or push.

## Controlled retrieval comparison

Fixture: `eval/paraphrase_release_20261004.json`, SHA-256 `9b666344cd3e137e0fe532046b5a13e59c23c5e5c907628e7f1b1e597d0ab5ff`, frozen before the first live query. Eight technical questions, three phrasings each: six Web3 queries, twelve model-method queries and six smart-contract tooling queries. All 18 distinct expected/contrast IDs were present in indexed metadata. Prompts and anchors were not changed after retrieval.

This is an agent-authored known-source holdout, not independent expert-labelled relevance data. A recovered anchor is not precision, exhaustive recall, or proof that unlisted results are wrong. Contrast papers identify passages requiring review, not automatic false positives. The fixture covers no dedicated AI-agent group. It does not establish universal performance.

The isolated old/new imports used shared query vectors, disabled response/vector caches and rotated variant order against the same live, mutable read-only indexes. No index snapshot was frozen. No listener or production feature switch was changed. The run took 91.9 seconds, with a 180-second budget, 1.5-CPU limit and 1536-MiB memory cap. Timings below exclude embedding generation, HTTP and MCP overhead. They are small-sample diagnostics, not an SLO. Percentiles here include only complete responses; partials are counted separately and as failed anchor checks.

| Build / discovery | Complete / attempted | Known-source hit@10 / 24 | Known-source reach@20 / 24 | Complete-response p50 / p95 |
|---|---:|---:|---:|---:|
| Old / OFF | 23/24 | 11/24 | 11/24 | 612 / 1080 ms |
| New / OFF | 22/24 | 11/24 | 11/24 | 630 / 1044 ms |
| Old / ON, isolated only | 22/24 | 11/24 | 16/24 | 738 / 1186 ms |
| New / ON, isolated only | 23/24 | 11/24 | 17/24 | 634 / 1800 ms |

Across the 22 complete old/new pairs in each mode, primary IDs matched exactly. In ON mode the candidate IDs also matched for every complete pair. The 16-to-17 reach difference is therefore not evidence of better ranking; completion status differed. The ON candidate shelf offers additional leads, not verified evidence.

A diagnostic repeat of the two incomplete-query cases used the already-frozen vectors and changed no original scores. All four state-space repeats were complete with identical IDs. The relative-position query showed `lexical retrieval incomplete` in both builds: three of four repeat calls were partial. Cold/variable lexical completion can change the ranking; this remains a release blocker rather than being hidden by reporting only complete pairs.

The soft-layer optimization removes duplicate source-metadata hydration only for explicitly deferred internal children. The outer response hydrates the union once. Direct validator `_top=False` calls still hydrate, and cache keys distinguish deferred entries. Unit tests prove one union read instead of three in the relevant path. This does not prove end-to-end speed improvement: the ON p95 worsened in the controlled run. **Do not enable production discovery or advertise acceleration on this evidence.**

## Public smoke checks after deployment

- `get_paper(wp:chainlink-v2)` returned its historical source URL with `curated_historical_list` and availability `inconclusive`.
- `get_paper(wp:mastercoin)` returned the Tendermint title, correct historical URL and explicit legacy-ID warning.
- Strict Web3 search for durable nonce returned three relevant Solana/Anza documentation records with source URLs and no source-candidate shelf.
- D2O `2406.13035`: 8 equations returned from 39 available objects within 1567 characters; each carried the arXiv URL and `exact_latex=true`.
- Uniswap v4: the source URL was fixed, but the requested equation/algorithm extraction returned zero objects with an explicit missing-elements note. Its acquisition is PDF text and `exact_latex=false`. This is an extraction limitation, not a successful math test.

Observed end-to-end calls were about 3-12 seconds, including client/tool overhead; the durable-nonce call was 12.4 seconds while three smoke calls ran concurrently. Do not substitute subsecond isolated retrieval timing for public agent latency.

## Recovery and readiness

Read-only preflight: fulltext and coarse-base Qdrant collections were green, optimizers OK, with 10,745,312 and 180,191 points respectively. Points are not canonical paper counts. Four API/MCP/payment/attestor units were active before rollout; API health was OK after it.

The newest checked registry backup passed `quick_check` and contains 53 nodes, 15 judgments and two links. The state backup had required tables; full integrity checking of this large file was not run. The September 27 fulltext snapshot exists, with a stored checksum and a 32.49-GB size. Coarse-base had no listed snapshot. Neither snapshot existence nor SQLite schema checks prove full application recovery. Snapshot checksums were read as metadata, not independently recomputed over 32.49 GB.

No new registry writes, wallet access, payment or restore occurred. The earlier October 3 Solana devnet proof is not a fresh test and does not establish mainnet or EVM readiness. Public anonymous-write rejection was not re-tested live; inventory and offline authorization tests are not substitutes for that gate.

Final read-only check confirmed all four API/MCP/payment/attestor services active, API health OK, and the deployed main hash `89ed3d917bb1897c2db098b8ce8d209f5d465dcae2f3d265f5b182d285c47189`. The live registry remains at 54 nodes, 16 judgments and two links, matching the prior release count. The completed temporary server comparison directory was removed after its raw results, vectors and manifests were saved locally. The production code backup remains.

**Decision: bounded beta/hackathon demo, not unconditional production-ready.** Outstanding: reliable lexical completion, independently reviewed paraphrase relevance, a separately approved full recovery exercise and final live authorization evidence. Do not spend on reindexing or enable the experimental shelf before latency and quality gates pass.
