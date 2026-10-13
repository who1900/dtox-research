# Corpus repair operations

These are the operational scripts used for the 13 October 2026 existing-corpus repair. They were moved from the audit workspace's `operations/corpus-quality-20261013/` into Git without duplicate copies. Runtime ledgers, journals, source candidates and database files remain outside Git.

`remote.py` stages the repository's repair modules and dispatches the existing one-time Oracle workers. Its defaults identify the audited installation and local SSH key path; review them before using a different host or checkout. The audit workspace layout is `source/research/` beneath the workspace root, with local receipts in `evidence/corpus-quality-20261013/`.

Read status from the audit workspace:

```powershell
python -B source/research/ops/corpus_quality/remote.py status
```

`run_sequence.py --remainder` uses the existing ledgers to finish catalog reconciliation, label retries and the full missing-point pass. Successful journaled operations are retained. One-time retry classes save their previous ledger records. Workers use reader maintenance for published revision transitions and inspect unfinished journals before resuming. A completed dispatch is not whole-corpus text-quality acceptance.

The preparation and recovery code is in `ops/repair_corpus_labels.py`, `ops/replace_published_labels.py`, `ops/restore_corpus_points.py`, `ops/restore_cached_points.py` and `ops/reconcile_fts_catalog.py`. See [current state](../../docs/current-state.md) for results and outstanding classes. GitHub publication does not dispatch these scripts or deploy code.
