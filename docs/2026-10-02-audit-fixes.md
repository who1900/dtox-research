# Audit fixes: scoped closeout

Date: 2026-10-02. Updated: 2026-10-03. Basis: [independent audit](2026-10-02-independent-audit.md), local regression fixtures and the main coordinator's deployment/live-check reports. [Compact HTML](2026-10-02-audit-fixes.html). This is a scoped closeout, not a completeness or production-readiness approval.

Results below are scoped reports from main, not fresh independent checks by this document author. Local/server suites and SDK variants overlap; do not sum them into a unique test total. No server, network, configuration, resource-cap or application-code changes were performed for this document update.

## Scoped deployment: 2026-10-03

Main confirms API, MCP, pipeline, extractor, backup, monitor, gateway and site deployed on October 3 after backups; production/local source or compiled-build hash parity confirmed. Supplied hash prefixes: API `468b2103b4ab...`; MCP `90c0bd2325dc...`; gateway server.js `800bc2608c6e...`, payments.js `9a64a15f31a1...`, tools.js `f3adc5497499...`, upstream.js `e77e763ab1af...`; site `d98d2366eb15...`. These are abbreviated parity references, not full release identifiers or an immutable index snapshot.

All six services are reported active and four new completion/sync tables exist. This confirms scoped deployment/schema availability, not whole-corpus repair, an SLO or a verified restore. Gateway final checks: 35/35 PASS including docs, no skip, 1.746 s; tsc and git diff checks PASS. Main reports no environment, price or network-setting changes, live payment or chain write.

## Scoped test results: 2026-10-03

| Suite / environment | Reported result | Boundary |
|---|---|---|
| Local API | 532 total: 531 PASS, 1 skip; 8.631 s. | Do not count the skip as a pass. |
| Local pipeline | 56 total: 55 PASS, 1 skip. | Overlaps the server pipeline suite. |
| Server pipeline | 56 PASS, no skip; 5.026 s. | Environment-specific run, not 56 additional unique tests. |
| Server ops | 11 PASS; 0.163 s. | Does not prove restore RTO/RPO. |
| MCP, local SDK 2.2 | 23 PASS. | Same scoped suite as the server SDK variant. |
| MCP, server SDK 1.28.1 | 23 PASS. | Not paid settlement or chain E2E. |
| Evaluation | 40 PASS. | Not a global recall benchmark. |
| Attestor | 47 PASS; tsc PASS. | Offline/compiler checks, not fresh chain E2E. |
| Gateway | Final: 35/35 PASS including docs, no skip; 1.746 s; tsc and git diff checks PASS. | Supersedes the earlier 29-test report; deployed, no fresh paid E2E. |

## Scoped live checks: 2026-10-03

These results were not independently rerun for this document. Earlier staging observations remain separate from the newly reported production MCP checks; no staging latency is attributed to production.

| Check | Reported result | Scope / limitation |
|---|---|---|
| Live staging topic trends: `zero knowledge`, `web3` | 39 canonical papers; `status=bounded_sample`; 200 dense chunk hits and 40 lexical candidates, both caps reached; 38 papers with matching metadata; 14.712 s cold. | Bounded candidate sample, not exhaustive coverage, an SLO or a latency distribution. |
| Live staging nonce probe | 3 returned IDs; 4.309 s. | Single scoped observation; exact query parameters were not supplied. |
| Production MCP `get_paper("9999.99999")` | `isError=true`; diagnostic `status=404`. | Actual live error-contract observation reported after API/MCP deployment. |
| Production MCP topic trends: `zero knowledge`, `web3` | 39 papers; `status=bounded_sample`; years 2023-2026. | Actual live bounded-sample observation reported after deployment; no production latency supplied. |
| Production MCP GRPO query | Current canonical read 0, settled 0; 1 similar-node paper requires claim verification; `verification_required=true`; `thin_retrieval_with_registry_evidence`. | F04 live behavior confirmed; similar wallet readings do not settle the current claim. |
| Production `get_paper`, `eip:7702` | DONE; extractor_version 4; six nested Security headings now `section_type=limitations`. | One repaired paper, not complete legacy-corpus recovery; headings listed below. |
| Production gateway health | `status=ok`; `payment_mode=live`; Solana devnet only. | Health/configuration observation, not successful settlement or mainnet readiness. |
| Production unpaid `record_signed` challenge | Dummy invalid signature, no `_meta` payment payload: `isError=true`, error `Payment required`; `accepted.extra.paymentFlow=upfront`. | Challenge only: no upstream write called, no charge; not paid/testnet E2E or signature-verifier proof. |

No query retuning or optimization was performed. Targeted EIP repair and gateway/site deployment are confirmed. Ethereum Sepolia has no default asset mapping in SDK 2.24.0: this is a documented gate, not an invitation to guess an asset. The unpaid challenge does not exercise paid settlement or downstream signature verification.

## Observed resources and targeted repair

October 3 Oracle observation: Qdrant green, optimizer OK, 10,743,556 points and 14 segments; memory 9.683 GiB / 10 GiB (96.83%). Oracle host available memory: 1439 MiB; swap 0. Oracle storage free: 129,152,708,608 B (120.29 GiB). Oracle is primary vector storage; Contabo has separate storage/metadata roles. Spare Oracle disk does not resolve Contabo headroom. No resource caps were changed. The HNSW plan is ready but NOT executed; execution requires explicit human approval.

Raw done before EIP repair: 180174. Final bounded SQL observation reported by main: done 180176; `completion_totals` has `first_completed=2`, `reprocessed=1`, `unknown=0`, unchanged; six services active. Counters are observed since the new deployed baseline. Keep first completions separate from reprocessing; these are not historical rates or canonical-paper counts. The resource and completion observations are separate samples, not a transactional snapshot.

Only `eip:7702` v3 -> v4 was repaired from cached fulltext, with old chunks copied and prior done/v3 history recorded. Main confirms live SQL DONE / extractor_version 4 and actual `get_paper` output. It is reprocessing, not first ingestion. The six nested headings now classified as limitations are: Implementation of secure delegate contracts; Front running initialization; Storage management; Setting code as `tx.origin`; Sponsored transaction relayers; Transaction propagation. This single-paper result does not establish whole-corpus repair or complete extraction.

Latest reported `paper_index_sync` queue count: 102. The earlier sample had 14 durable pending entries, mostly unmapped legacy FTS; that classification was not re-established for all 102 entries. Durable retries do not block embedding or Qdrant/coarse completion; queued entries are not repaired legacy metadata/content. Legacy FTS sidecatalog bootstrap remains pending and planned. These samples do not establish a queue growth rate.

## Findings

| Finding | Current code / deployment status | Remaining boundary |
|---|---|---|
| F01: pipeline transaction recovery | Deployed rollback/connection recovery and separate success/progress/error tracking; pipeline suites passed as scoped above. | No claim that this caused historical slow growth. |
| F02: literal code extraction | Deployed literal protection and ordered containers; pipeline regression suites passed. | Legacy code listings not backfilled; completeness unproven. |
| F03: resource headroom | Capacity risk remains unresolved; latest Oracle resource observation recorded above. | HNSW plan ready, NOT executed; no cap changes; explicit human approval required. |
| F04: registry report consistency | Deployed; actual public MCP GRPO confirms separated current/similar counts, required verification and qualified thin-retrieval scope. | Wallet agreement without trusted model diversity is not scientific quorum. |
| F05: coverage counts and truncation | Deployed API code counts canonical twins once and follows chunk caps for truncation; layer/probe semantics exposed; scoped API tests PASS. | Bounded counts are not an exhaustive census; overlapping layers are not summed. |
| F06: topic trends false empty | Deployed API code uses bounded dense plus lexical candidates and existing layer bands; actual production MCP returned the reported 39-paper bounded sample. | No global recall benchmark; empty selection does not establish absent literature. |
| F07: Markdown taxonomy/order | Deployed; EIP-7702 DONE v4 and actual paper output confirm six nested Security headings classified as limitations. | Only one paper repaired; legacy taxonomy/content not globally backfilled. |
| F08: HAL cursor ownership/refresh | Deployed source isolation and bounded cursor reopening; pipeline suites passed. | Historical source omissions not measured or backfilled. |
| F09: rediscovery reconciliation | Deployed placeholder transitions and durable revision-aware reconciliation; pipeline suites passed. | Latest sync queue count 102; earlier 14-entry sample mostly legacy. No blanket repair, queue-rate claim or completion blocking. |
| F10: chunk FTS replay | Deployed atomic paper-scoped replacement, deduplication and indexed sidecatalog addressing; pipeline suites passed. | Legacy sidecatalog bootstrap planned; unmapped legacy work deferred without full scans. |
| F11: signing side effects | Deployed API code keeps preview/failed signature free of embeddings and node creation; valid attestor response precedes vector preparation outside the writer transaction; scoped tests PASS. | Explicit canonical links and monotonic signed ordering retained; no automatic claim merge or fresh live chain-write test. |
| F12: MCP error contract | Deployed MCP code raises SDK tool errors and classifies fully rejected signed batches; suite PASS and actual production `isError=true` / `status=404` reported above. | Scoped checks do not prove paid settlement correctness or full regression coverage. |
| F13: write/settlement atomicity | Upfront SDK gate fixed, deployed and offline-tested; gateway 35/35 PASS and tsc PASS; unpaid live challenge advertises upfront flow without charge/upstream write. | Real paid/testnet E2E remains unverified; challenge is not atomicity proof. Sepolia's separate default-asset gate remains explicit. |
| F14: paid/free bundle parity | Deployed real bundle proxy target, budget/limit/strict handling and unsupported-filter rejection; scoped gateway checks PASS. | No exclusive paid-value claim; paid E2E and willingness to pay remain unproven. |
| F15: public promises | Gateway/site deployed after backups with confirmed source/build parity; scoped contract and documentation checks PASS. | No wallet-only scientific quorum, exclusive paid value, revenue or paid production-readiness claim. |
| F16: self-host setup | API runtime path overrides deployed with legacy compatibility; scoped tests PASS; README self-host instructions corrected; no automatic dotenv loader added. | Clean self-host smoke remains unverified; documentation/runtime fixes do not prove reproducible deployment. |
| F17: timestamp contract | Deployed API code labels `as_of` an observed timestamp, not an immutable snapshot or reproducibility guarantee; scoped test PASS. | Immutable source/index versions are not established by this field. |
| F18: backup verification | Backup/monitor fixes deployed; required schema/integrity and modification-time selection covered by server ops tests. | Fresh restore, full recovery coverage and restore RTO/RPO not verified. |
| F19: wording dependence | Deployed explicit claim memory and bounded recall; linked/similar scoped regressions PASS. | Seeds and manual links still matter; no universal paraphrase-invariance claim. |

## GRPO: actual post-deployment query

Exact tested query: `GRPO removes the separate value critic model`. Main reports the actual public MCP verdict `prior_art_reported_on_similar_claim`; registry paper `2402.03300`, readers 2, models `[]`, current-claim total 0, similar total 1. Headline: 0 current canonical read / 0 settled; 1 similar-node paper requires claim verification. `verification_required=true`; scope `thin_retrieval_with_registry_evidence`. F04 live behavior is confirmed. The older registry claim remains a separate unlinked record, not proof that the two claims are identical or scientifically settled.

## Closeout limits

- Deployed fixes do not repair the whole legacy indexed corpus. Only the single cached EIP repair is confirmed done. No mass reindex, global source scan or whole-corpus backfill was performed.
- Legacy FTS sidecatalog bootstrap is a planned, separately authorized maintenance task. The live path must not perform a per-paper full scan of roughly 10 million chunks to reconstruct missing catalog entries.
- Deployed operational first-completion/reprocessing counters describe observations since deployment/baseline, not a historical ingestion rate or a throughput forecast. No historical backfill exists; updates to existing done rows are not newly indexed papers. Unknown history stays unknown.
- Real paid/testnet E2E was not performed; the unpaid upfront challenge had no charge or upstream write. Ethereum Sepolia's SDK 2.24.0 default-asset gate remains unresolved; no asset is guessed. Restore RTO/RPO and recovery SLO were not verified. No production, mainnet or revenue-readiness conclusion follows from local fixtures.
- Main confirms all reported component deployments, source/build parity and the targeted EIP done result. RAM pressure, legacy FTS bootstrap, real paid E2E, Ethereum asset configuration and restore SLO remain open. Do not sum overlapping test runs or present scoped observations as full regression proof or whole-audit closure.
