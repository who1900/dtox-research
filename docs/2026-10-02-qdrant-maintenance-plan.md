# Qdrant HNSW maintenance plan - 2026-10-02 observations

Status: plan only. The human decision is to prepare a migration plan and NOT
start a rebuild now. No configuration update, snapshot creation, restart,
capacity-limit change or load test was executed for this document.

## Reported baseline, not a new live measurement

The owner supplied this Oracle-host observation: Qdrant green, approximately
10.737 million points across 14 segments; container memory 9.882 GiB of a
10 GiB limit. Host available memory was 1.6 GB, swap 0, and free disk 121 GB.
These are observation-time values, not reserved capacity or a corpus snapshot.

Reported configuration:

| Setting | Reported value | Planned treatment |
| --- | --- | --- |
| HNSW `on_disk` | `false` | Scoped candidate change to `true` |
| int8 quantization `always_ram` | `true` | Unchanged |
| `on_disk_payload` | `true` | Unchanged |

Vector-storage mode, named-vector overrides, installed Qdrant version and
optimizer state must be recorded before deciding on a migration. They are
not inferred from the above settings. Disk-backed HNSW can still use page
cache; int8 quantized vectors explicitly remain RAM-resident. No exact freed
RAM, steady-state memory or latency improvement is promised without measurement.

## Gates before any execution

1. Obtain a separate human approval for the exact collection and maintenance
   window. Resolve its identity privately; do not publish private addresses,
   credentials or production access-file paths. No broad collection updates.
2. Record installed Qdrant/image versions, collection configuration including
   per-vector HNSW overrides, point count, segment count, optimizer status,
   disk footprint and payload/index schema. Save non-secret source revision
   and checksums of the actual deployed ingestion/configuration code, including
   relevant dirty changes, so future collection creation cannot silently
   restore the old setting. Recording is not authorization to edit that code.
3. Create an approved collection snapshot and a consistent backup of the
   corresponding SQLite/state metadata. Coordinate ingestion and record the
   write watermark and any writes after it. Verify artifact completion and
   checksums, retain an independent copy, and account for snapshot disk cost.
4. Restore to an isolated target with adequate spare capacity. Check point
   counts, configuration, payload schema, selected known IDs and representative
   queries against the recorded baseline. Restore verification is NOT
   established until this actually succeeds and is recorded. A backup file
   or a successful snapshot request alone is insufficient.
5. Measure rebuild peaks on that isolated target using the same version and
   representative data/configuration. Budget existing storage, snapshots,
   temporary/rebuilt segments, overlap between old and new indexes, WAL growth
   and page cache. Reserve capacity for concurrent services and ingest traffic.
   Recheck current host/cgroup memory, available disk and ongoing growth.
   The reported 1.6 GB host availability and 9.882/10 GiB container usage do
   not establish sufficient rebuild headroom. With no measured peak and
   operator-approved safety margin, this gate is NO-GO.
6. Agree numeric acceptance and abort thresholds before execution: cgroup/host
   memory, OOM events, disk floor, optimizer progress, error rate and query
   latency under specified concurrency. No thresholds or SLOs are invented
   from this single observation. Do not increase caps or enable swap as an
   implicit part of the plan.

## Smallest scoped candidate migration

Confirm the installed version supports the intended collection update and
how named-vector overrides interact with collection HNSW configuration.
The intended logical diff is only HNSW `on_disk: false -> true` for the
approved collection. An illustrative collection-level update body is:

```json
{"hnsw_config": {"on_disk": true}}
```

This is not an executable request or approval. If per-vector overrides require
a different scoped update, review that exact diff first. Preserve graph
parameters, vector dimensions/distance, quantization, payload configuration,
optimizer settings and capacity limits. Do not apply a global default or
recreate all collections. Do not modify scoring or re-embed/re-harvest sources.

After approval and successful headroom/restore gates, apply the reviewed
single-collection update in the maintenance window. Allow only the measured,
operator-approved ingestion/concurrency profile. Monitor optimizer/rebuild
progress and resource peaks. A config response alone does not prove all
segments have migrated; confirm completion and the effective configuration.
Whether a restart is necessary must be established for the installed version;
no restart is requested or authorized by this plan.

## Acceptance measurements

Record before, peak and after values using the same workload: container RSS
and cgroup memory (including cache), host availability, disk use, segment and
point counts, optimizer status, errors/timeouts and throughput. Separate warm
and cold/page-cache effects; compare idle and concurrent ingestion conditions.
Collect enough timestamped samples for meaningful latency distributions,
with the sample size and concurrency reported. A one-query smoke is not p95.

Check a fixed representative query set and selected known point IDs for
completeness, payload integrity and retrieval differences. Keep query/index
parameters fixed; record any rank/recall differences rather than assuming a
rebuild leaves approximate-search order identical. Accept only against the
thresholds agreed before the window. Publish measured RAM and latency effects,
not an estimate promoted to a guarantee.

## Abort and rollback caveats

If agreed resource or correctness limits are breached, stop the approved
maintenance activity under the operator's version-specific recovery procedure.
Do not improvise process restarts, deletions or capacity changes. Preserve logs,
watermarks and snapshots needed for diagnosis and recovery.

Changing HNSW back to `on_disk: false` may trigger another rebuild and restore
RAM pressure; it is not an instantaneous or resource-free rollback. Snapshot
restore is a separate recovery operation with its own capacity, downtime and
version-compatibility requirements. It may discard post-snapshot writes unless
they are reconciled from the recorded write boundary. Prefer a separately
validated recovery target when available; do not load a duplicate collection
onto the already constrained host without an approved capacity calculation.

Remaining gates: fresh headroom measurements, isolated restore verification,
measured rebuild peaks, workload acceptance thresholds and explicit human
execution approval. Until those gates pass, the decision remains: no rebuild.
