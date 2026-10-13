# Source generation: build, acceptance and activation

Status: the three search-code steps are implemented and offline-checked. The
recoverable export of the frozen 183,170-source snapshot is in progress; the
latest supplied verified checkpoint covers 37,250 sources. A completed partial
1,000-source sizing build is not a complete populated generation. No
production activation has been performed. This file is an operator checklist,
not proof that the semantic gate has passed.

## Observed constraints

Read-only checks on 2026-10-07 found:

- Contabo: 34.37 GiB free disk, about 9.48 GiB available RAM, load about 15.5.
- Oracle: 83.21 GiB free disk, about 13.15 GiB available RAM, load about 0.1.
- Full-text Qdrant: green, 10,931,328 points; coarse index: green, 182,332 points.
- SQLite: 182,333 `done` documents; 233 published snapshot-journal entries.
- A later read counted 182,338 `done` sources, 231,159,143 abstract characters,
  165,901 abstracts longer than 800 characters, and 3,934 empty abstracts.
- A separate metadata-addressed file-size scan covered 182,345 chunk-cache
  files, 12,149,135,127 bytes, with no missing files in that scan.

These are separate live observations while ingestion continues, not one frozen
snapshot or a count of unique scientific papers with verified complete text.
The free disk on Contabo does not describe the primary Qdrant disk on Oracle.

A bounded inventory of the first 100 sorted `done` IDs found 100 recoverable
sources and abstracts, 5,592 valid owned passages, 42 raw LaTeX sources and 14
raw sources without a recoverable bibliography. It diagnosed 1,629 unresolved
citation mentions. This is a deterministic operational sample, not a random
sample or an estimate of corpus-wide bibliography coverage. Inventory was
explicitly partial and was not indexed or activated as a complete generation.

## Build contract

1. Capture a read-only metadata snapshot. Export exact identifiers and current
   metadata with immutable-publication provenance kept separately.
2. Export original owned passages and citation-bearing LaTeX artifacts without
   guessing missing references. Missing bindings remain a coverage diagnostic.
3. Build into a new directory. Input, catalog, checkpoint and manifest paths
   must be distinct. Never replace the primary SQLite database, old Qdrant
   collections or published original chunks.
4. Inventory and plan first. Account for input plus catalog plus temporary disk,
   vector views, payloads and indexing. A planning heuristic is not a certified
   upper bound or a measured memory requirement.
5. Large embedding batches must not hold the live query encoder's lock. Use an
   isolated low-priority process with already cached approved model assets,
   bounded CPU/RAM and no model downloads or paid services.
   Reuse the existing 384-dimensional original-passage index read-only. Match
   the entire original payload text hash, canonical owner and publication
   revision; never attach a paragraph vector to a different citation sentence.
   Only new summary/citation descriptions are embedded into the derived index.
6. Checkpoint acknowledged point IDs and hashes. Resume only the same input,
   catalog, model/tokenizer descriptor and owned derived collection.
7. Publish a complete manifest only after verifying the intended ID set,
   payload hashes and counts. A bounded subset is partial and cannot activate
   the production source-selection path.

All source statuses, original acquisition modes and bibliography-binding
coverage must stay explicit. A corpus generation can include legacy unmarked
documents without pretending they have a new publication certificate.

The exporter first spools only finite metadata, identifier-collision data and
snapshot-journal projections into an owned temporary SQLite file. It closes the
primary database read transaction before opening document artifacts. This avoids
pinning the ingestion WAL for the full artifact scan. Full export does not parse
citations twice: it retains the raw bounded source and the producer parses it.
Progress contains phase/count/elapsed values, not document text or credentials.

The first full-export attempt was deliberately stopped during metadata capture:
review found that legacy chunk-cache records omit the document-level
`papers.matched_terms` annotation used by the original pipeline. The temporary
snapshot files from that attempt were removed; primary files were untouched.
Do not reuse that incomplete attempt or infer term tags from foreign citations.

The corrected full-export attempt froze 182,393 `done` metadata rows. Its
primary read transaction was released after 79.681 seconds, before artifact
processing. The last progress event reported 15,000 visited rows, 14,999 exported
sources and 1,154,103 owned-passage packets after 1,699.186 seconds. The service
then hit its 30-minute runtime limit and failed with `timeout`, consuming
14 min 42.558 s CPU with a 2.0 GiB cgroup memory peak and no swap. It did not
publish an export/report bundle or a manifest. An inactive, collected transient
unit's later `Result=success` is not evidence of job success; the journal records
the timeout.

The interrupted attempt left an unsealed 435,946,437-byte packet gzip and a
431,067,136-byte metadata snapshot inside the owned isolated build root. They
are not a resumable checkpoint and must not be renamed into a completed export.
Continuation uses an explicit durable work directory; it must verify the frozen
snapshot, code/options identity, sealed packet prefix and counters before
discarding only an uncommitted tail. The 3-hour cap is a bound on an individual
recoverable export run, not a promise that the full generation will finish in
3 hours. CPU remains capped at 0.5 CPU, RAM at 2 GiB with no swap, compressed
output at 8 GiB, and the disk reserve at 10 GiB. No production search is selected
from an unfinished run.

A read-only validation of all 182,393 frozen metadata rows found no oversized
rows or canonical/filename collisions. Folding ordinary formatting whitespace
in derived display titles changed 59 titles; 182,341 rows then validated and
52 failed because of `source_url`. A separate full URL validation counted
exactly 52 invalid optional URLs. These are input-format defects, not evidence
that the sources are invalid scientific work. Raw metadata and its provenance
remain unchanged: derived display whitespace is normalized explicitly, and an
invalid optional link is null with diagnostics rather than guessed or used to
discard the source. Other invalid controls and required-field failures remain
errors. These checks did not attest live publication visibility or scan full
text artifacts.

The isolated offline encoder was started once on Oracle with the already cached
approved model: one input, 768 finite vector components, L2 norm
1.00000000188419, 848 ms process runtime, 202.3 MiB reported memory peak, no swap.
This verifies the worker/runtime contract only. It is not a throughput, batch
memory, ranking-quality or production-latency benchmark. No model was downloaded
and no Qdrant point was written by that check.

## Runtime contract

- One selected manifest identifies the source-representation generation.
- Partial export coverage must survive the producer/catalog/indexer boundary.
  A successfully built SQLite file alone does not certify a complete generation.
- Dense and lexical rankings aggregate canonical sources before result cuts.
  Representation kinds and repeated mentions are not independent votes.
- The runtime lexical reader streams FTS5 rank-ordered eligible source heads,
  including deterministic boundary ties, rather than materializing all matching
  chunks and sorting them repeatedly. Large-corpus latency remains unmeasured.
- A normal bounded top-K window is reported in diagnostics, not classified as
  a runtime outage. It never implies exhaustive literature coverage. Native
  sources outside the frozen generation are explicit as-of exclusions; changed
  text/revisions inside that generation still invalidate the affected retrieval.
- Original evidence is read separately. Foreign citation context retains its
  actual author, artifact hash and location; it cannot become the cited paper's
  original snippet, verified claim or full-text completeness label.
- Check current publication visibility of both owner and target. For legacy
  documents, an explicit live map entry with null revision differs from absence.
  A staged or conflicting snapshot must not leak through the legacy path.
- Strict filters apply to authoritative target metadata and qualifying owned
  passages. Foreign descriptions cannot satisfy original-section filters.
- Model/tokenizer hashes, dimensions, pooling, maximum token length and query
  instruction must agree between indexing and query embedding.
  Every actual query-vector response also carries the loaded-asset descriptor;
  a cached health response alone cannot certify a vector after a runtime change.
- An invalid configured generation fails explicitly. It is not silently treated
  as a successful empty result or mixed with the old promotion algorithm.
- Generation identity participates in query caches. One absolute deadline covers
  discovery, publication checks and original-evidence retrieval.
- Unknown topic scope is a separate state, not a transport error or evidence of
  novelty. With a ready generation, positive originals/registry readings remain
  available; unverified scope and unread originals prohibit absence conclusions.
  Real runtime/transport failure remains an availability error.
- Target title/year/layers/source URL come from the frozen catalog for both
  filtering and output. Current citation counts do not overwrite that metadata
  authority. Publication visibility is still checked against the live journal.

## Acceptance before activation

1. Short offline API/MCP and generation-contract checks.
2. A staging instance using the complete real generation and the same original
   evidence index. No changes to mainnet, payments, public access or secrets.
3. The frozen release formulations and independent holdout. Keep queries,
   anchors, limits and scoring unchanged. Resolve corpus absence separately.
4. Compare known-source retention, three-formulation stability, hard-negative
   review flags, operational failures and latency under the same conditions.
   The inherited release result is 14/24 hits and 1/8 stable groups, not a score
   assigned to the new mechanism.
5. Confirm exact-ID lookup, strict sections/elements, compact responses, layered
   queries, validation, comparisons and evidence retrieval still work. Count
   missing/partial evidence honestly rather than manufacturing a snippet.
6. Do not activate on a failed gate or an unmeasured corpus subset. Unit-test
   totals do not prove semantic quality or production readiness.

The original passages remain on their native 384-dimensional vectors. New views
use the approved 768-dimensional model, with exact tokenizer-bounded partitions
of at most 384 tokens. A normal abstract that fits is not split into arbitrary
256-byte fragments. The 4,096-byte pre-window is only a bounded preparation
buffer; it does not authorize silent tokenizer truncation.

## Activation and rollback

Only after acceptance: back up the currently deployed code and selected manifest,
deploy matching reviewed modules, select the complete generation and restart the
API in the existing deployment workflow. Check health and a bounded functional
smoke run. Do not alter nginx, access controls, wallets, payment settings or
secret files as part of this migration.

Rollback restores the previous code/manifest selection. It does not delete the
original corpus or rewrite its vectors. New generation files and collections are
not pruned until their exact ownership and lack of active consumers are checked.

Commit/push, hackathon submission and any paid resource expansion are outside
this operation.

## Code and operational boundary

- `ops/export_source_packets.py`: explicit corpus reader and frozen packet export.
- `ops/build_source_representations.py`: immutable catalog producer with input
  hashes, owned annotation provenance and retained partial/full coverage.
- `ops/index_source_generation.py`: read-only `--plan` by default; explicit
  collection allowlist, budgets and resumable checkpoint for indexing new views.
- `api/source_generation.py`: generation reader, typed dense adapters and one
  dense/lexical source fusion. No promotion rule for individual known articles.
- `api/main.py`: opt-in integration shared by search and its consumers, keeping
  original evidence and unread discovery state distinct.
- `embed/embed_service.py`: reports hashes of actual loaded assets; a matching
  model name without this runtime proof is insufficient for activation.

The isolated build roots are `/opt/dtox-source-migration-20261007` on Contabo and
Oracle. They are not production code directories or selected manifests. No new
SSH port forwarding, access setting, secret or primary collection is required by
the export. Moving a final catalog, staging the reviewed API, and selecting its
manifest are separate actions after disk and acceptance checks.

## Final offline acceptance for this implementation

```powershell
python -B -m pytest -q api/tests mcp/tests pipeline/tests/test_citation_contexts.py pipeline/tests/test_bibliography_cache.py ops/tests/test_export_source_packets.py ops/tests/test_build_source_representations.py ops/tests/test_index_source_generation.py embed/tests -p no:cacheprovider --disable-warnings --tb=short
```

Latest main-reported combined result after recoverable export/producer and
indexer safety changes: **1,302 passed, 1 skipped, 3 warnings,
572 subtests passed in 50.16 s**. The preceding combined result was
1,249 passed / 1 skipped / 3 warnings / 572 subtests in 52.12 s.
The earlier implementation-only result was 1,229 passed / 556 subtests.
An earlier combined run exposed a scheduling race in a 20 ms deadline fixture;
the fixture now synchronizes the discovery phase and still checks deadline,
bounded outstanding work and absence of follow-up evidence reads. Production
timeouts were not increased to satisfy the test.

A final read-only production check returned API health 200/ok, Qdrant green with
10,938,013 points, and all three API/MCP/pipeline services active. The production
API SHA-256 remained
`886b70d920901059566f532767c5b62b41a9d4c8822eb91fbd14d4ea7fb9dd66`,
matching the saved pre-change baseline. Health reported p50 0.971 s / p95 1.367 s
over 48 existing samples; these are not a controlled before/after benchmark.

Remaining release gates: finish and verify recoverable export/catalog building,
then the complete catalog/derived collection within actual disk/RAM budgets,
deploy the matching vector-response descriptor, and run staging functional,
semantic/holdout and latency acceptance before selecting the new manifest.
The inherited **14/24 / 1/8** semantic result is unchanged and has not been
re-labelled as a pass. No commit/push or production selection was performed.

## Recoverable-build follow-up

The exporter now seals whole-source gzip batches in an explicit owned work
directory. Resume checks the frozen metadata file, input roots, code/runtime and
options, committed compressed prefix, decoded packet hash and counters. It
removes only the verified uncommitted tail. A deadline or signal cannot publish
a partial run as a complete generation. Frozen raw metadata is not edited.

Independent review found two finalization windows before the full run: a signal
could leave a partial report, or arrive immediately after a successful directory
rename. The final report now uses fsync plus atomic replacement; a committed
output is recognized through its owned checkpoint, report, file identity and
compressed SHA-256. An already published owned result is an idempotent resume,
not an unrelated output that may be overwritten. Both interruption windows and
same-size packet corruption have dedicated regression tests.

All 36 exporter tests also passed on Contabo Linux in an isolated unit, with
network, production database and production caches inaccessible. These fixture
tests took 13.681 seconds, not a full-corpus performance test. Two initial unit
launches failed before Python ran because the isolation hid the interpreter and
then named an absent optional path; their setup was corrected, not counted as
successful test runs. The transient unit's tiny memory-peak counter is not a
credible Python RAM measurement and is not used as one.

The producer now keeps metadata, aliases, abstracts, raw packets and duplicate
signatures in disk-backed SQLite rather than whole-corpus Python maps. Its
bounded cache and document-local citation dedup preserve owner identity. Catalog
rows and phase/cursor progress commit together through attached DELETE/FULL
SQLite databases. Explicit `--work-dir --resume` validates input/report/code/
runtime/options/output identity. Deadline pauses return code 3 and retain the
last committed batch; final catalog publication remains immutable/no-clobber.
This requires extra disk for decoded staging packets. Full-corpus RSS and final
catalog size are not yet measured. Disk limits are batch guards, not a certified
prediction of peak usage; hash guards and one bounded packet can overrun a
cooperative deadline. A separate Linux process limit remains necessary.

An actual offline comparison on Oracle loaded the previously deployed and
descriptor-only candidate encoder against the existing cached model revision.
On the checked query and batch inputs, the maximum absolute vector delta was
`0.0`; dimensions, finite values, normalization and loaded asset descriptors
matched. No model was downloaded or deployed and no service restarted. This is
compatibility evidence, not semantic recall or throughput evidence.

The real export was launched as `dtox-source-export-20261007-v3.service`, using
`runs/export-work-v3` and eventual `runs/full-export-v3` inside the isolated build
root. Each run is capped at 3 hours / 0.5 CPU / 2 GiB RAM / no swap, with an
internal cooperative pause at 10,700 seconds. Whole-source checkpoints are
sealed every 250 metadata rows, gzip level is 1, the compressed output budget
is 8 GiB and the disk reserve is 10 GiB. Launch is not completion. Its matching
exporter/imported modules must not be replaced during this frozen run or resume.
The updated producer/catalog modules were staged only in Oracle's isolated build
root; Contabo's exporter dependency versions remain frozen.

The first actual durable checkpoint committed 250 sources, 20,368 original
passages and 99 raw documents, with a 7,942,536-byte sealed gzip prefix at
226.378 seconds. The new frozen snapshot contains 183,170 done rows; its primary
read transaction was released at 112.853 seconds. These are observed progress
values, not a completion forecast. The unit was active, its cgroup memory peak
was 1,213,005,824 bytes and swap was disabled by its limit. API health remained
200/ok, Qdrant was green with 11,010,699 points, and the production API hash
still matched the baseline. No production manifest was selected.

All 37 producer fixture tests then passed on Oracle Linux in 10.680 seconds,
inside a separate 0.5 CPU / 2 GiB / no-network unit. They exercise actual SQLite
phase/resume/publication behavior on Linux, not the full production corpus or
semantic quality. No full catalog or derived dense collection has yet been built.

## Current verified checkpoint and finite continuation

The latest main-supplied verified durable checkpoint of the same 183,170-source
snapshot contains **37,250 sources, 2,612,434 original-passage packets,
2,595 raw documents and 923,433,436 sealed gzip bytes**. These committed counts
supersede the initial 250-source observation above; they do not certify a
complete export, catalog or generation. The earlier checkpoint elapsed time and
memory peak above belong to that earlier observation, not this checkpoint.

At the supplied continuation observation,
`dtox-source-export-continuation-20261007.service` was actually active. Its helper
was at attempt 0, waiting for rows 39,250, with reported memory 8,843,264 bytes.
The waiting threshold is not a verified committed checkpoint; helper memory is
not the exporter's peak or a full-build RAM estimate. Continuation remains finite,
and neither an active unit nor a successful partial build means completion.
No unit was launched or changed by this documentation update.

## Partial sizing and provisional capacity gate

`dtox-source-catalog-sizing-20261007.service` completed the explicitly partial
1,000-source sizing run: runtime **99.506 s**, CPU **49.713 s**, memory peak
**366.1 MiB** (main-reported service measurements, not full-corpus measurements).
Read-only SQLite inspection found:

| Measurement | Observed value |
|---|---:|
| Published catalog | 149,065,728 bytes |
| Retained `work.sqlite` | 191,221,760 bytes |
| Unique catalog + work file bytes | 340,287,488 bytes |
| Sources / own passages / summaries / citation contexts | 1,000 / 83,116 / 1,000 / 70 |
| Representation text | 52,393,232 bytes |
| `representations` table pages (`dbstat`) | 101,842,944 bytes |
| FTS shadow-table pages (`dbstat`) | 22,257,664 bytes |
| Catalog freelist pages | 0 |

Output is `runs/sizing-1000-catalog.sqlite`, with work in
`runs/sizing-1000-work` under the isolated Oracle build root. The published
catalog and `work/catalog.sqlite` share one inode through hardlinks: do not count
them twice. Coverage remains `partial=true`, `explicit_input_only`.

Scaling by 11 million native passages, not by the source count, gives about
**19.73 GB catalog + 25.31 GB work = 45.04 GB**. Sensitivity scenarios of +10%
and +25% give 49.54 GB and 56.29 GB combined (catalog 21.70 GB and 24.66 GB).
GB here is decimal; 10 GiB reserve is 10,737,418,240 bytes. These are provisional
scenarios, not confidence intervals or certified peak bounds: the sorted prefix
is not random, full-source/citation composition can differ, and journals,
compressed input and other temporary files need separate allowance.

Oracle's read-only free-space observation was **88,603,963,392 bytes**; retaining
the same 10 GiB reserve leaves about 77.87 GB. The sampled catalog/work projection
fits provisionally, not as unconditional full-build authorization. Contabo is
borderline: if the reported 32.37 GB free precedes an additional ~4 GB gzip copy,
keeping that copy leaves only about 17.63 GB for the catalog above reserve.
After a separately approved removal of only that newly created secondary copy,
the corresponding catalog allowance would be about 21.63 GB, just 1.90 GB above
the linear catalog estimate; the +10% scenario already slightly exceeds it.
Any such removal requires confirmed size/SHA-256-matching transfer to Oracle
and proof that the Contabo copy is neither primary nor active nor needed by a
consumer. No deletion has occurred. Keep the existing active FTS database
(about 16.65 GB) and all native vectors. Final transfer gate uses the actual
finished catalog and fresh free bytes: `free - incremental_catalog >= 10 GiB`,
including any additional staging allocation rather than assuming it away.

## New-only indexer safety

The sidecar records versioned local ownership only after a successful collection
PUT with `result=true` and verified returned configuration. Collection creation
is not automatically retried. Error, timeout, conflict, or a crash before the
ownership checkpoint leaves an existing collection unowned; matching config
alone cannot adopt it. Orphans require separate ownership proof, and legacy
`owned=1` is refused rather than migrated automatically. No sentinel point or
new remote metadata API was introduced.

New collection requests set `hnsw_config.max_indexing_threads=1` and
`optimizers_config.max_optimization_threads=1`. Readback uses singular
`optimizer_config`; resume, completed retries and final verification require
both values to be exactly integer 1. Support was checked in the installed Qdrant
model and [official optimizer documentation](https://qdrant.tech/documentation/faq/database-optimization/).
Optimizer concurrency is per shard, not a whole-server CPU quota. The old
collection's observed HNSW value 0 and optimizer value null are not changed.
All **75 focused indexer tests passed in 5.60 s** using mock HTTP; no real Qdrant
write or index build was performed by that implementation/test pass.

## Fresh current-runtime pre-run: partial, not a semantic comparison

Main's fresh MCP pre-run exited 1 after receiving all 24 responses. The first
six LLM formulations (two groups) returned `partial=true`,
`retrieval_warning="lexical retrieval incomplete"`, `lexical_channel="skipped"`
and `error=null`. These are **6 partial responses from current lexical retrieval;
full semantic comparison invalid**, not six transport failures or demonstrated
timeouts. The other 18 responses were valid; their descriptive subtotal was
11 hits, with 1/8 stable groups reported and zero hard-negative flags. Do not
reinterpret this as a semantic decrease from 14 to 11 or as a candidate result.
Main is diagnosing the existing private
`release-live-before-generation-20261007.json`; no cause, including export load
or common-term complexity, is assigned without measurement.

The historical **14/24 hits, 1/8 stable groups** baseline remains unchanged. No
new-generation semantic quality result exists. Production API, live encoder and
selected manifest remain unchanged; no activation is claimed.

Frozen secondary fixtures were not edited; their raw-byte SHA-256 values match
the existing pins:

- `eval/paraphrase_cases_20261003.json`: `852f374aeb3bdfc0160bce9c46706bbac17ec49dc84b667faa24b51dbd60385d`
- `eval/paraphrase_holdout_20261003.json`: `9f30c246aa4202c32a0a5a58a6bf37048d9e89029092f1c6b3fd0829c98ff716`
- `eval/paraphrase_fresh_20261004.json`: `858438e4910280099b173c9cb28bce76eda853d78575e38f6de70a47cb376728`

The release fixture is also unchanged: raw SHA-256
`9b666344cd3e137e0fe532046b5a13e59c23c5e5c907628e7f1b1e597d0ab5ff`.
Its stored semantic fixture digest
`c5d1703f9fa52573156ab38f553bf1d10cefdc3076883684d4f476992595be9e`
is a distinct digest, not the raw file SHA-256. This documentation pass did not
launch another MCP capture, test suite, service or production operation.

## Finite build execution

The main agent subsequently observed 44,250 committed sources, 3,024,173 owned
passages, 2,856 raw documents and 1,066,836,570 compressed bytes. This remains
an unfinished export of the same 183,170-source snapshot.

A private local controller was actually launched at 2026-10-07 10:50 UTC,
with a 20-hour total wall cap. Its first observed phase is `waiting-export`.
It requires the local computer to remain running for the verified SCP relay and
remote control. The separate limited export-continuation unit runs on Contabo
without that dependency. After verified full export, the controller transfers
only the owned public-data bundle, checks compressed and report SHA-256, and
builds the full catalog on Oracle in an isolated unit: 0.5 CPU, 2 GiB RAM,
no swap/network, 64 GiB owned-work ceiling and 10 GiB free-space reserve.
At most two additional producer invocations follow a definitively confirmed
exit-3 pause. Unknown outcomes are not replayed. It then runs the tokenizer-exact
read-only index plan and stops. No Qdrant writes, descriptor deployment,
production activation, deletions or semantic candidate acceptance are authorized
by that controller. A successful catalog/plan still is not a complete searchable
generation or proof of improved quality.
