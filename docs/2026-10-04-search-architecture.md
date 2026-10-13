# Search architecture release record - 2026-10-04

**Status:** Narrow architecture update deployed and validated.

## Deployment

- User-approved normal code change deployed on Contabo. Deployed code SHA-256: `208228fd1a5c928c163b30b92e13624d6bd0318c8989f4d7c77670fe495b9b60`.
- Backup: `/opt/dtox-research-api/release-backups/dtox-architecture-20261004.4MmaWRFZ`.
- Only MCP production `INSTRUCTIONS` changed. Existing unrelated local changes were preserved and not deployed.
- No paid calls, model, reindex, schema, auth, registry, or wallet changes.

## Search architecture and runtime

- One unified canonical candidate ledger incorporates late exact and quota results and preserves raw hydrated sources. It does not change final primary results or their ordering. Hard filters remain in force.
- `retrieval_summary` reports locally observed run counts only. It does not measure corpus coverage or global recall.
- `SOURCE_DISCOVERY=0` and `RERANK_ENABLED=0` remain unchanged. The optional reranker retains per-process single admission, 60-second cooldown, maximum 8 pairs, cache of 128 entries with 300-second TTL, and a 3-second request timeout. The timeout uses read-idle plus elapsed-result rejection, not a hard network deadline.
- Health reports the optional reranker as disabled with 0 requests. Four services remain active: API, MCP, x402, and attestor. Registry remains unchanged at 54 nodes, 16 judgments, and 2 links.

## Validation

- Offline API suite: 752 tests, 1 skipped, 751 passed; 8.479 seconds.
- MCP suite: 26 passed; 0.399 seconds. Total: 778 tests, including 1 skipped; 777 passed.
- An earlier intermediate run completed 741 tests with 94 errors while an edit was unfinished and a `copy` import was missing. This was not the final result. The subsequent 752-test run had 1 error due to a finite-clock mock; its fixture was updated for identity canonicalization, and the final run passed.
- Three live MCP smoke queries completed. Two returned identical exact IDs and ordering. One returned the same canonical paper: `1809.09044` and `oa:W2945678139`, *Fraud Data Availability Proofs*, confirmed twins. These were smoke queries, not a paired benchmark; they establish no quality gain or full-production-readiness claim.

## Remaining limitation

- The narrow alias extraction source fix is deployed; see [alias extraction release and smoke evidence](2026-10-04-alias-extraction.md). Manual MCP smoke verified the OA and bundle paths, but this does not establish global production readiness.
- Algorithms remain missing. Alias fallback is bounded to at most 2 reads, and measured post-deploy latency was higher than the supplied pre-deploy observations (OA retry 10.567 s vs 5.499 s; canonical 6.379 s vs 4.296 s). No claim of no latency regression is made.
