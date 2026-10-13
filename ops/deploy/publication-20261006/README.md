# Publication rollout candidate (2026-10-06)

This recipe was reviewed and the six-file release deployed on 2026-10-06 at 15:54 UTC. See [release evidence](../../../docs/2026-10-06-publication-release.md). The script is fail-closed and requires a root invocation for future deploy/rollback. Starting the writer initializes the additive publication journal; no historical migration or reindex ran.

## Native Linux temporary stage

From the repository root on Linux, build a disposable package:

```sh
bash ops/deploy/publication-20261006/guarded-rollout.sh stage /tmp/dtox-publication-20261006.tar.gz
tmpdir="$(mktemp -d /tmp/dtox-native-check.XXXXXXXX)"
echo "Keep this private stage directory for inspection: $tmpdir"
tar -xzf /tmp/dtox-publication-20261006.tar.gz -C "$tmpdir"
python3 - "$tmpdir" <<'PY'
import ast, pathlib, sys
root=pathlib.Path(sys.argv[1])
for p in list((root/'pipeline').glob('*.py')) + list((root/'api').glob('*.py')):
    ast.parse(p.read_text(encoding='utf-8'), filename=str(p))
print('candidate Python syntax: OK')
PY
(cd "$tmpdir" && PYTHONPATH="$tmpdir" python3 -m pytest -q pipeline/tests/test_publication.py pipeline/tests/test_publication_failures.py pipeline/tests/test_publication_preflight.py pipeline/tests/test_fts_catalog.py pipeline/tests/test_build_fts_safety.py pipeline/tests/test_parse_safety.py pipeline/tests/test_whitepaper_staging.py api/tests/test_publication_visibility.py api/tests/test_evidence_integrity.py)
```

The focused test files may import additional repository code; run them in the full Linux checkout when the isolated package lacks a test-only dependency. No DB, Qdrant, scan, or production service is needed for this stage.

## Guarded host invocation (operator-only; do not run until reviewed)

Transfer the stage archive manually after approval, then:

```sh
sudo bash guarded-rollout.sh deploy /path/to/dtox-publication-20261006.tar.gz
```

The script validates candidate hashes, expected live baseline hashes/absence, whitelisted API dependencies, and active units before writing. It backs up explicit code under `/opt/dtox-release-20261006/<UTC timestamp>`, then stops the writer, installs API files only, restarts API and requires `/v1/health` (bounded to about 20 seconds), and only then installs pipeline files and starts the writer. Any API-health failure leaves the writer stopped; do not restore the old reader over potentially marked data. It does not touch config, env, secrets, DB schema, Qdrant data, jobs, or harvesting.

Rollback is intentionally conservative: stop the writer, restore only `service.py` and `build_fts.py`, leave API gate and publication module/journal intact, restart/check API, and leave writer stopped for manual reconciliation. No deletion or automatic migration is performed.

## Checks before a future rollout

- Confirm live target paths and API dependency files on the host; the script refuses if dependency modules are not already installed in `/opt/dtox-research-api`.
- Verify the manually supplied `build_fts.py` baseline hash, the service baseline hash, and the three supplied snapshots still describe the live files immediately before deployment.
- Review native Linux suite output, then explicitly authorize any host invocation. No rollout was performed by preparing this artifact.

Temporary staging/extraction directories are intentionally not deleted automatically. Record the exact paths printed by the script, inspect them after use, and remove only those explicitly verified directories during a separate manual cleanup.
