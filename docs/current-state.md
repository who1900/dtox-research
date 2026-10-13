# Current source and corpus repair — 13 October 2026

This is an observed status, not a new product specification or proof that every local change is deployed. Historical reports retain their dates and scope. The active product is DTOX Research; Browser is pivot history.

## Observed repair status

Oracle read-only snapshot: **2026-10-13 16:33:20 UTC**.

| Scope | Observed result |
|---|---:|
| DONE documents in the frozen census | 183,854 |
| Label-repaired documents, including partial repairs | 36,393 |
| Label failures awaiting source-heading resolution | 17 |
| Restored missing points, all passes | 32,673 |
| Pending point candidates | 60,463 |
| Points being prepared at the snapshot | 7 |
| Deferred point candidates | 110 |
| Failed point candidates | 54 |

The one-time point worker and coordinator were running. The fulltext collection count advanced during the observation; its final sampled count was 11,096,890. Coarse contained 183,854 points. Both Qdrant optimizer statuses were `ok`; fulltext was `yellow` during active updates. Pipeline ingestion services were inactive. API health recovered after reader-maintenance transitions and returned five consecutive successful samples. No fatal worker marker was present.

FTS ownership was fully reconciled for the 11,091,418 points present at migration completion. Later accepted additions maintain ownership through their own transaction receipts. This establishes index identity/ownership, not semantic correctness of every passage.

## Implemented repair

- A shared nullable bibliography contract accepts unknown `venue`, `citation_count` and `url` as null or absent. Known values, text, identity and vector comparisons remain independent.
- Source-bound label mapping uses ordered unsplit source sections, rather than concatenating overlapping retrieval chunks.
- Missing cached points require a unique exact source interval in the appropriate typed source element. Existing text and IDs remain unchanged; only missing vectors are generated.
- Published additions use the existing snapshot replacement journal, with FTS ownership receipts, recovery and reader maintenance.
- [Operational scripts](../ops/corpus_quality/README.md) preserve the durable full-corpus ledgers and repeat diagnosed classes once.

The 531 eligible label failures from the prior audit were repaired. This does not close 1,758 unresolved labels inside 403 earlier partially repaired documents.

## Outstanding quality work

The 54 failed point candidates comprise 23 baseline/cache disagreements across five whitepapers, 21 source/label guard rejections and 10 parent/live/cache mismatches. Two representative journals from the latter classes did not exist: those sampled failures happened during preparation, before apply. They remain explicit failures.

Seventeen oversized-heading documents, damaged or unproven source text, 11 empty caches, historical edition/provenance gaps and two orphan SIMD documents remain open. Pinned source candidates reproduce all 23 existing SIMD points; coherent state/cache/coarse adoption has not been applied. A retained HTML source provides a substantive v1 candidate for one empty-cache document; it has not replaced the working projection.

Content changes need a coherent per-document publication from a selected verified source, including cache, section manifest, FTS and Qdrant. The existing managed rollback-mode writer is not active over the current WAL databases. The accepted 12 October backup still represents the pre-repair state. Whole-corpus quality acceptance and a refreshed single backup are pending.

## Source publication and checks

This Git publication includes accumulated API/retrieval, provenance, extraction/publication, repair, evaluation, MCP, attestor and gateway changes. It does not deploy the complete checkout. The primary API remained on its previously recorded deployed version; only the repair helpers were staged for this repair.

Windows/Node 22 publication checks: all 225 initially changed Python files parsed; moved operational helpers also passed syntax and workspace-path checks. 69 targeted API/publication unittest cases passed; MCP discovery passed 62 cases; attestor passed 58 tests and TypeScript build; gateway passed 63 tests and TypeScript build. The two PDF candidate/memory pytest files passed 60 cases; `requirements-test.txt` supplies their dependencies in CI. Earlier corpus-repair runs passed their focused 67, 55 and 24-test groups. These are bounded offline results, not full-suite, payment, chain-write or semantic-quality acceptance. GitHub CI supplies the Linux checkout checks.
