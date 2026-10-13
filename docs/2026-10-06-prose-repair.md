# SnapKV Discussion prose repair | 2026-10-06

**Repair:** applied once, journal stage `done`  
**Scope:** one existing paper record and its missing Discussion prose only  
**Readiness:** **NOT CERTIFIED**  
**Runtime health after repair:** all three services active; `/v1/health` returned 200, status `ok`; no restart

This closes the missing-prose finding for the identified SnapKV snapshot only. It does not repair the old paper-wide FTS mapping or certify the corpus, full quality gate, or SLO.

## Frozen inputs and checks

- Engine SHA-256: `9aa0d64e9997a1c5097a9580a55c68ccdf8ccadc7a629b2fb315f843a306a0b9`
- Planner SHA-256: `c92d7a0ff3797db387efcdef0e2ce338ae9b0c59656dd3c884b524f869390261`
- Cached source SHA-256: `909db89f85a3c19011d68b64c942bb3ce58c268e62106dae99dde57c16ed8876`
- Before-cache SHA-256: `c6810e6b74839a4505c1cff3252c4f3e2e144b961707659d7b054ffe7972cdd4`
- Main independently ran the full local ops suite: **60 passed**, 4.313 s. Focused native tests: **26 passed**, 5.677 s. The earlier focused local 26-test run is part of the full local suite and is not counted as an additional 26.
- An interim native run had **25 passed and 1 fixture error** (`NameError`); the fixture was corrected before freeze.

## Strict source boundary and plan

The planner locates one unescaped exact `\section{Discussions}` in the comment-stripped cached source, outside protected literal spans. The bounded body ends at the first supported structural boundary, here `\bibliographystyle`. Ambiguous or missing headers, unsupported boundaries, and missing end boundaries fail closed. There is no legacy fallback.

The bounded body was 1201 characters with SHA-256 `2611e0a48ca14c8138e9d79e77c34e806dd9668ce29de42d7f6c2d9878ce729c`. Parsing only `\section{Discussions}\n` plus that body produced **one** authentic prose chunk, 1186 characters, at next index **77**, text SHA-256 `c4559eb475e9f81a1da477db5ce5384a80dbb0a6c4b5d9e4dfd2179d8182a188`. It contains the limitation text and excludes the Medusa appendix and bibliography. Full-source structured hashes remained equal. Both parser modes were `pylatexenc_recovered`.

The former four-chunk plan is retained as historical evidence of the incorrect scope; it was not applied.

## Apply result

- Apply completed in **1.173 s**. This includes the internal `BEGIN IMMEDIATE` state guard and is not a separate lock-duration measurement.
- Qdrant increased from 72 to 73 points. The prior 72 point payloads and vectors were exactly equal as dictionaries.
- Cache increased from 77 to 78 chunk objects. The prior 77 objects are exactly equal; the JSON file was reserialized and is not claimed byte-identical. Source was unchanged. After-cache SHA-256: `939d6a8fe82a1d335bbe3b9beae792bff609bc1b77f018d916860a29c3d7b63f`.
- FTS added row `11309047`, UUID `37938d7a-4aeb-5fbd-849c-65aa84a72fb7`. Receipt repair ID: `339a4a92110eba8c99fe482ac44411369e473793d5d8e23b7c8813254d02f816`; digest: `82f045b75965c6452a6f6ffd730ecf46c6b8d6cfee266a68431731007352f744`.
- The FTS receipt covers the transaction and a partial side-catalog new row only. Whole-paper legacy mapping remains unknown and unmapped; one new side-catalog row is confirmed, with no full-coverage claim. One sampled old FTS row, `497572`, retained hash `a780667ad7afcdcdc854cda2852d6ccb6d91e6d9978e294e6db1a85a1f24df8c`. The entire FTS table was not hashed.
- Resume completed with the same FTS receipt and no additional row. Journal stage is `done`.
- Post-apply health: all three services active, `/v1/health` returned 200 with status `ok`; no restart occurred. Paper state remained `done`, `extractor_version=3`, `pending_sync=0`.
- All four production file hashes were unchanged: API `c7fc69f0c393d8b21f6dabcfb28d37061c73589c6800b2e05da68ff12d81c586`, MCP `cca456af5e8c8579cf34d80375cd86bf1751696130812a567f66ccaa286fdbf9`, extractor `8a18ab4dd5eaf42be670d77eae6cbb63f1fc674dbf1f9ea56bdb5c56e912802f`, service hash prefix `60b685...` as previously reported.
- Main's bounded FTS MATCH for `"generative aspect" AND "prompt inference"` returned rowid `11309047`, the expected UUID `37938d7a-4aeb-5fbd-849c-65aa84a72fb7`, and the expected SnapID in **0.46 s**. This verifies the new row only and does not establish full-paper FTS coverage.

## Read verification and remaining limits

Two public MCP reads succeeded with `isError=false`. Section output changed from the old 273-character figure-only result to a 1461-character figure-plus-prose result; both limitation phrases were present. All three available equation/algorithm sections were returned, 687 characters total, untruncated, and their complete JSON objects were unchanged. Source: [arXiv 2404.14469](https://arxiv.org/abs/2404.14469).

The existing misclassified figure was not fixed, and `parse_completeness` remains unknown. Whole-paper legacy FTS mapping remains unknown and unmapped; only the one new side-catalog row is known. A durable journal exists for this repair, but it does not establish whole-paper coverage. The parser fix addresses the tested conversion failure only; it is not a claim to prevent all future loss. No mass reindex, status reset, environment or secret change, payment, access or chain change, commit, or push occurred.

Private journal and old-cache/old-vector backups were retained with mode 600 on the private host. There is no external backup and no production restore was performed. A five-second guarded FTS preflight took 2.0 seconds and passed after an earlier preflight timeout that stopped before mutation. An initial apply caller error (`state_db_path` missing) occurred before effects; the caller was corrected and the apply then completed. No additional FTS, cache, or Qdrant writes beyond the described single additions were made.

The original public-smoke failures remain in the [audit report](2026-10-06-audit-fixes.md). This targeted repair does not clear the strict Web3 raw-ID assertion or establish ranking improvement. Readiness remains **NOT CERTIFIED**.
