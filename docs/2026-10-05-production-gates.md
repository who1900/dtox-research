# Production readiness gates - 2026-10-05

This is an offline gate definition, not a production approval or a diagnosis of the current incident. The readiness runner in [`ops/release_readonly_probe.py`](../ops/release_readonly_probe.py) performs MCP initialize, initialized notification, `tools/list`, one deliberately missing-ID `get_paper`, and two bounded positive reads: `find_papers("durable nonce", layer="web3", limit=2)` and `get_code_or_math_spec("2101.05511", target_elements="equation,algorithm", max_chars=2500)`. It makes at most six HTTP requests, requires HTTPS, sends no credentials, follows no redirects, performs no automatic retries, and calls no write tool. It asserts the exact 13-tool inventory but never calls `validate_project`, `get_verdict_message`, or `record_signed_verdict`.

The negative check passes only when the MCP result has `isError=true` **and** structured or JSON-in-content diagnostic status 404. An `isError` carrying a 500, timeout, or unstructured payload fails the gate. The two positive checks require `isError=false`, no payload `error`, nonempty data, and required identity/provenance fields: paper `id`, `title`, `source`, `url`; and spec `arxiv_id`, `title`, and nonempty section text. The spec's expected count is intentionally not fixed; an optional tool result count is not a quality threshold.

Run only when the release owner explicitly asks for a public smoke. Main separately completed the bounded public probe before and after deployment (six HTTP checks per run); see the [production status report](2026-10-05-production-status.md) for results. This document does not rerun the probe:

```powershell
C:/Python313/python.exe ops/release_readonly_probe.py
```

The single stdout JSON report includes attempted/completed calls, each request latency, total public end-to-end latency, validation `error_count`, MCP `tool_error_count` (including the expected 404), partial and truncation flags separately, the inventory assertion, and all three tool outcomes. Failures stay in the report rather than being excluded from the denominator. Response bodies, tool content, headers, session IDs, keys, and credentials are never written to a file or included in the report. `partial` and `truncated` are independent response markers, not synonyms for a failed call. The probe is capped at six HTTP requests; per-request connect/read-idle timeouts are 3/9 seconds, with no automatic retries. These phase timeouts are not a hard total deadline. Runner latency is observational for that one attempt, not an SLO or readiness score.

## Fixed-fixture quality gate

Use the frozen release fixture [`eval/paraphrase_release_20261004.json`](../eval/paraphrase_release_20261004.json) unchanged. Its preregistered raw-byte SHA-256 is `9b666344cd3e137e0fe532046b5a13e59c23c5e5c907628e7f1b1e597d0ab5ff`. Any correction after a query starts requires a new dated fixture and a new recorded hash; never tune prompts, anchors, layers, exclusions, or thresholds against observed results.

- [ ] Verify fixture hash, schema, query uniqueness, group cardinality, layer assignment, and the previously specified overlap checks before retrieval.
- [ ] Resolve every expected source read-only against indexed metadata and its primary record. Keep `indexed`, `not indexed`, `identity unresolved`, and `unknown` distinct; keep the original query denominator and show source-availability strata separately.
- [ ] Report hit@3/hit@10, first accepted-anchor rank, per-group all-three hit@10, operational invalid/partial counts, and mechanism contrasts in separate fields. An absent anchor is not proof of irrelevance or novelty; a contrast hit is not automatically a false positive.
- [ ] Independently assess whether retrieved primary-source passages support the focal mechanism. Keep no-excerpt and unresolved judgments visible; automated matching is not independent human relevance review.
- [ ] Report every attempted query, including errors, invalid answers, partial responses, and timeouts. Never compute quality only on successful responses without also reporting attempted/complete denominators.
- [ ] Compare raw results to build identity and response metadata. Keep retrieval-only timing distinct from public end-to-end latency, which includes network, MCP and tool overhead.

These are separate gates; passing the smoke runner does not pass fixture quality, evidence accuracy, recovery, authorization, or release review. Do not collapse them into a weighted number or invent a production-ready score. Record each as `PASS`, `FAIL`, or `UNKNOWN` with evidence and owner.

## Evidence/artifact chain

Preserve an auditable, immutable chain for any approved run:

`fixture + hash` → `preflight snapshot` → `run manifest` → `raw responses` → `analysis` → `independent review` → `release decision`

The manifest should identify UTC start/end, code/build identity, fixture hash, environment class (never secret values), exact attempted-call counts, timeout/budget, cache and variant settings, error/partial/truncation counts, and artifact hashes. Keep raw payloads in an access-controlled location with sensitive headers and credentials removed; the small public smoke runner intentionally emits no raw payload artifact. Analysis must be reproducible from the frozen fixture and retained raw data. Review notes must distinguish direct observation, source-availability limits, inference, and unknowns. The release decision names approver, scope, expiration/recheck trigger, rollback owner, and unresolved gates.

## Isolation, recovery, and approval boundaries

- Run comparative quality experiments in a separately bounded process/environment, with an explicit resource and wall-time budget, production feature flags unchanged, and production indexes read-only. Reuse or rotate caches only as specified by the preregistration. Abort on budget exhaustion; do not broaden a run silently.
- Before any recovery exercise, require written owner approval, fresh storage/RAM/headroom checks, a controlled window, isolated target, verified backup provenance, and an owner-approved rollback/cleanup plan. Snapshot presence, a stored checksum, SQLite quick-check, or service health is not proof of a full application restore. This checklist authorizes no restore.
- The read-only gate does not authorize configuration changes, deployment/restart, secrets or wallet access, payment, registry/chain writes, ingestion/reindexing, backup mutation, restore, or external telemetry. A public tool inventory is not authorization. Any such operation requires a separately scoped approval and its own risk/recovery plan.
- Stop and escalate if the required fixture/build identity is unavailable, artifacts cannot be safely retained, an operation would write or incur cost, or a result is ambiguous. Do not retry non-idempotent actions automatically.

## External pilot - proposal only, unconfirmed

Proposal for discussion: a two-week, read-only evaluation with three external teams. This is not an accepted commitment, scheduled pilot, customer confirmation, or production-readiness claim. Before invitation, obtain explicit participant consent and an owner-approved data-handling plan; define the test questions and success evidence without a single composite score; disclose corpus scope, known limitations, logging/retention, support owner, and stop conditions. Use a separate pilot environment or the public read path only, with no wallet, payment, signed verdict, registry write, or participant secrets. At the end, report each team's completion, failures, partial/truncated results, qualitative evidence review, and opt-in feedback separately. No team identity or telemetry is collected by this runner.
