# Corpus maintenance

These utilities coordinate catalog reconciliation, source-bound label updates and recovery of missing cached points. The repair modules and offline tests live in `ops/` and `ops/tests/`.

- `remote.py`: stage helpers, dispatch one-time jobs and inspect status.
- `run_sequence.py`: coordinate the existing repair ledgers.
- `run_label_worker.py`: apply bounded operations and recover unfinished journals.
- `run_catalog_reconciliation.py`: reconcile FTS ownership.

Jobs retain durable ledgers, source hashes and before/after journals. Published revision transitions use reader maintenance; preparation runs before that transition.

Remote helpers contain installation-specific defaults. Configure them for the target installation before use. Runtime databases, credentials, source caches and repair journals are kept outside Git.
