# Release test evidence - 2026-10-03

## Scope

Offline release QA at `HEAD 322ff3a` (`Keep chunk embeddings on Contabo and disable Oracle small engine`). Source files were read-only. No live harness, network request, service operation, data collection, paid call, dependency installation, or chain interaction was performed.

Runtime: `C:/Python313/python.exe` (Python 3.13). Each suite was run once with the repository's unittest discovery command. Results below are separate suite totals; they are not combined or deduplicated across suites.

## Offline unit suites

| Suite | Command | Result |
|---|---|---|
| API | `C:/Python313/python.exe -m unittest discover -s api/tests -p 'test_*.py'` | Exit 0; 532 tests in 8.898 s; 1 skipped (POSIX-only real `flock` test on Windows). |
| MCP | `C:/Python313/python.exe -m unittest discover -s mcp/tests -p 'test_*.py'` | Exit 0; 23 tests in 0.361 s. |
| Pipeline | `C:/Python313/python.exe -m unittest discover -s pipeline/tests -p 'test_*.py'` | Exit 0; 56 tests in 8.834 s; 1 skipped because `pylatexenc` is not installed. No dependencies were installed. |
| Ops | `C:/Python313/python.exe -m unittest discover -s ops/tests -p 'test_*.py'` | Exit 0; 11 tests in 0.237 s. |
| Eval | `C:/Python313/python.exe -m unittest discover -s eval/tests -p 'test_*.py'` | Exit 0; 40 tests in 0.029 s. |

## Contract coverage observed

- **Registry/quorum:** API unit coverage exercises the default quorum, invalid quorum values, contested and partial readings, wallet-only pending status, same-model agreement, and settlement only with distinct trusted model identities (`api/tests/test_registry_policy.py:4-112`; `api/tests/test_audit_regressions.py:105-190`).
- **Errors:** MCP tests cover transport failures, HTTP errors, malformed success bodies, and all request-handler transport-error paths (`mcp/tests/test_audit_errors.py:80-182`). API tests exercise mocked dependency failures and partial results.
- **Extraction:** Pipeline tests cover LaTeX comment/code handling, structural ordering, Markdown section taxonomy, and chunk limits (`pipeline/tests/test_extractor_audit_fixes.py:8-169`). The optional real-parser test did not run because `pylatexenc` is absent.
- **Coverage semantics:** API regressions cover deduplication, lower-bound reporting, incomplete dense retrieval, and partial coverage (`api/tests/test_audit_regressions.py:28-58, 197-278`). These are bounded unit fixtures, not an index-wide measurement.
- **Ops:** Backup integrity/schema and monitor behavior were exercised against temporary fixtures and mocks (`ops/tests/test_ops_audit_fixes.py:1-131`).

## Main-reported evidence (not measured in this run)

The main report says GitHub CI for commits `e6da049` and `322ff3a` is SUCCESS, and reports live read search `200` with two results, unchanged bge-base vector output after disabling the Oracle small engine, and an unchanged/green Qdrant PID. A later main-reported check found the 13-tool inventory endpoint returned HTTP 200 and the first seven read tools passed, with observed individual latencies from 0.11 to 13.59 seconds. These observations do not establish a global p95 latency. All are attributed reports, not measurements made or independently verified by this offline QA.

Main-reported backup metadata: fresh `judgments`/`state` backup at `20261003T0130`; registry backup 512 KB with integrity `ok` and schema 3; weekly FTS backup last dated `2026-09-27`; Qdrant full-text snapshot dated `2026-09-27`, 32.49 GB, checksum recorded; no coarse snapshot (reported rebuildable). A full restore was not performed. These are supplied metadata, not backup/restore checks performed by this QA.

## Uncovered release-critical gaps

1. **Deployment routing is not asserted by a test.** The release change sets the pipeline batch endpoint to Contabo and disables Oracle's small model in systemd drop-ins (`ops/deploy/contabo/dtox-research.service.d/zz-contabo-only-chunks.conf:1-4`; `ops/deploy/oracle/dtox-embed-base.service.d/zz-contabo-only-chunks.conf:1-3`). The CI workflow runs Python suites but contains no assertion of the effective systemd environment or service behavior (`.github/workflows/ci.yml:23-31`). Main-reported live observations above provide limited operational evidence, but not a repeatable config regression test.
2. **Structured extraction evidence is limited.** The local parser-specific test was skipped solely because `pylatexenc` is not installed (`pipeline/tests/test_extractor_audit_fixes.py:96-107`); that skip is an environment limitation, not evidence of an extraction defect. Separately, an eval fixture preserves a lead-reported prior miss (a spec returned zero structured elements; `eval/tests/test_web3_evidence.py:102-108`). This offline run did not reproduce that report against a live service, so its current status is unverified.
3. **No global recall or performance proof.** The eval suite validates offline behavior and mocked responses (`eval/tests/test_web3_evidence.py:13-18, 32-36`); the live API harness was intentionally not run. Unit coverage and the small main-reported live sample do not establish corpus-wide recall, latency, or performance. This is **not global recall/performance proof**.
4. **Full CI was not reproduced locally.** This run did not execute CI dependency audit/compileall or the Node gateway and attestor tests/builds (`.github/workflows/ci.yml:19-45`). The local Python runtime was 3.13 rather than CI's pinned Python 3.12 (`.github/workflows/ci.yml:16-18`).

## Follow-up verification

The `get_verdict_message` docstring discrepancy was confirmed against the API route and its regression test: claim lookup uses a read-only SQLite connection and resolves exact normalized text/existing links (`api/main.py:4010-4037, 4412-4454`); the preview test asserts no database/node creation and no embedding (`api/tests/test_signed_verdicts.py:129-142`). Both the overview and `Args.claim` now avoid promising automatic aliasing and state that previews do not create/register claim nodes (`mcp/server.py:633-655`). This is documentation-only; the endpoint may still make its normal request/limiting bookkeeping, so the wording is scoped to claim-node and chain effects. Post-edit focused checks passed: `C:/Python313/python.exe -m unittest discover -s mcp/tests -p 'test_audit_errors.py'` (12 tests, 0.207 s) and `C:/Python313/python.exe -m unittest discover -s api/tests -p 'test_signed_verdicts.py'` (20 tests, 0.682 s).

Main's rollout plan is docs-only: no API restart is required. Any MCP schema refresh should be scoped to this tool description.

The one pipeline-test skip above means the optional real-parser regression did not execute in this local environment; it does not itself establish a product bug. The separate prior extraction-miss fixture remains lead-reported and unverified here, as noted above.

## Final main-reported post-freeze checks

The following are main-attributed follow-up results, not tests executed by this offline QA author. Exact wall-clock timestamps were not supplied. The full gateway unit suite passed 38/38 in 2.25 seconds after script freeze; standalone CLI TypeScript compilation passed. The helper subset was 3/3 and is included within the 38, not additive. Attestor tests passed 47/47, with TypeScript compilation passing.

After an MCP tool-description-only deployment, main reports the public inventory and corrected preview description verified at HTTP 200 with all 13 public tools. The description no longer promises automatic claim aliasing. The service was active; no API restart, index, RAM, price or network changes were made. The signed devnet write was independently verified on Solana devnet as documented in [live release evidence](2026-10-03-live-release-evidence.md). It added one unique PDA row; registry counts moved from 53 nodes/15 judgments/2 links before the paid write to 54/16/2 afterward. `judged_by_model=unspecified` is not trusted-model attribution, and the record remains one-reader `read_once`, not quorum. These reports do not change the NO-GO production decision for RAM headroom and full restore.

