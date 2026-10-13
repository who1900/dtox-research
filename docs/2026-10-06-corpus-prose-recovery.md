> **SUPERSEDED / CURRENT PAUSE: 2026-10-06.** Recovery worker остановлен, journals сохранены; старый `reprocess.timer` остановлен и disabled. Reprocess и recovery writer retired/read-only; legacy FTS baseline не доказан. Native pipeline 115, ops 112 и API candidate 8/8 PASS являются отдельными прогонами. Deployment отклонён auto-review до выполнения из-за отсутствия отдельного согласования live restart: production copy/restart/schema changes не было, pipeline/API/MCP активны на старом коде; not deployed at writing. Production backups не созданы. Будущее recovery блокируют publication owner, exact mapping migration и missing inputs; корпус не объявлен восстановленным. Ниже сохранена историческая запись, не текущий running-статус. Текущие факты и требуемое согласование: [архитектурные исправления](2026-10-06-architecture-fixes.md).

# Corpus prose recovery: running, not complete

The earlier SnapKV repair verified the mechanism, but did not repair the affected corpus. This follow-up adds a general, resumable census and additions-only recovery job. It does not certify production readiness.

## Scope and evidence

- Read-only census: 182,076 `done` records, not 182,076 verified complete papers.
- 126,527 records had both cached LaTeX and cached chunks. These are inspectable, not necessarily damaged.
- 3,428 `latex` records had neither cached source directory nor chunk cache. They cannot be repaired from the available cache.
- PDF, specification and abstract-only records are distinguished from this LaTeX converter defect.
- The recovery job froze a universe of 182,079 `done` records at `2026-10-06T10:37:06.054226+00:00`. Newly completed records are outside that snapshot.
- The repaired parser's `pylatexenc_recovered` diagnostic is not evidence that a whole paragraph was lost. Formatting changes alone must not create additions.

## General recovery rule

Inspect uniquely bounded source sections. Restore substantive missing prose, including conservative partial gaps, only when retained-word coverage and source boundaries support an additive repair. No paper-specific selection rules are used by the corpus worker.

Independent safe sections may be repaired even when another section is ambiguous. Such a paper is `applied_partial`, with unresolved sections retained in the ledger. Global equation, algorithm or code sequence mismatches block automatic repair. Unsupported IDs, absent inputs and parser timeouts have explicit outcomes.

The worker preserves existing cache objects, point UUIDs, vectors and payloads. It embeds only new prose with the existing 384-dimensional model. It inserts new FTS rows without whole-paper replacement or deletion. A private per-paper journal contains the original cache and Qdrant snapshot; stage markers and FTS receipts support crash recovery and idempotent resume. Legacy whole-paper FTS mapping is not declared complete.

## Verification

Final native suite: **98 tests passed** in 26.086 seconds. The local integration suite passed 97 tests before the final one-message timeout fix; that fix's runner suite separately passed 27 tests locally.

Independent code review confirmed closure of seven earlier runner findings: transient errors, unfinished journals, scan/apply starvation, corpus snapshot scope, HTTP deadlines, cache path checks and early ID validation. The exact engine error `timed out` is covered by a regression test, not only a synthetic `timeout` spelling.

Limited live pilot:

| Paper | Added chunks / FTS rows | Old Qdrant points verified exactly unchanged | Outcome |
|---|---:|---:|---|
| [1703.06322](https://arxiv.org/abs/1703.06322) | 16 | 24 | Applied partial; unresolved sections retained |
| [1803.00646](https://arxiv.org/abs/1803.00646) | 7 | 49 | Applied partial; unresolved sections retained |
| [1710.10377](https://arxiv.org/abs/1710.10377) | 6 | 66 | Applied within supported recovery scope |

All **139 old points**, including vectors and payloads, compared exactly equal after recovery. Every old cache object remained an unchanged prefix of its appended cache. All three journals reached `done`; 29 new FTS rows were confirmed by receipts. A transient embedding/transport failure on the first paper was retried, not reported as success.

Public MCP `read_paper_section` returned the recovered `Address clustering` section (3,544 characters) and Bitcoin hash-rate/difficulty section (1,801 characters), with source attribution. Internal anonymous API reads returned 401; no credentials were read to bypass authentication. Two MCP reads are not a latency benchmark or p95/SLO proof.

## Running job

The first six-hour background launch was rejected by safety review before execution. After the remaining error-message mismatch was fixed, independently checked, tested natively and verified in the bounded live pilot, the launch was approved and executed.

Service: `dtox-prose-corpus-recovery-20261006.service`.

- One worker, CPU quota 50% of one core, memory maximum 768 MiB, nice 15, idle I/O priority.
- Cycles are bounded to 60 seconds, at most 20 census records and 3 apply attempts; scan has only half the cycle budget and large repair backlogs are drained first.
- Parser children have hard time/memory limits. Low RAM, insufficient disk, high host load or unhealthy Qdrant pauses work without consuming paper attempts.
- Private ledger/journal footprint caps stop expansion before exhausting disk.
- Six-hour worker time slices exit with a dedicated restart status and resume from the ledger. Unresolved completion exits separately from clean completion.
- This is a transient systemd job. Its files survive reboot, but automatic boot startup was not configured. A host reboot requires restarting the same worker with the same ledger and journal directory.

First post-start observation: **active/running**, memory approximately 33 MiB, 60 census records recorded (54 outside LaTeX scope, 5 blocked, 1 retry), cursor `0704.2667`. These are early scan outcomes, not 60 repaired papers. Pilot results are in a separate ledger and are not counted again as corpus-job successes.

Final turn observation: all four services were active. The corpus ledger had advanced to 85 records (55 outside LaTeX scope, 28 blocked, 2 retry), cursor `0706.0252`. No corpus-worker apply success had yet been recorded at that early frontier. `blocked` can mean unsupported/ambiguous structure without any proven missing prose; it is not a damaged-paper count. The three confirmed live repairs above belong to the separate pilot. The full pass is genuinely running, but further corpus recoveries are not being claimed in advance.

API health was `ok`, Qdrant collection `green`, optimizer `ok`. Product API, MCP, extractor and pipeline service hashes remained at their pre-job values. No product services were restarted, no secrets or payments changed, no commit or push performed.

## Remaining limits

The corpus pass has not finished. The total affected population is not yet known. Missing inputs, ambiguous structures, mismatched structured content and partial repairs require follow-up, not a success label. Existing `parse_completeness` remains unknown. A full FTS audit, semantic quality comparison and live production SLO certification are not established by this repair.

Final utility SHA-256:

```text
planner  9c7b34c3532ec2b257660c2abb4b316fedd37e196c88af0afde4973720cbc989
engine   117da7cbea8ff2c9a52944fd94dcc034e43a67e60cfd5d6a02c027f8902c759a
runner   bd0075a018ebe9dac1f14677b39feb4c3e3bd742b60dd94c942a006570e93fc3
```
