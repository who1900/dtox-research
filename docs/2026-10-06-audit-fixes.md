# External audit fixes | 2026-10-06

**Follow-up:** This report preserves the pre-repair audit and failed-smoke record. Current one-record repair results and remaining legacy-data limits are in the [prose repair report](2026-10-06-prose-repair.md).

**Final freeze:** **PASS**  
**Deployment:** deployed; all three services active; API startup health probe returned 200  
**Public smoke:** completed, with failures described below  
**Readiness:** **NOT CERTIFIED**

This report summarizes three code-fix areas and the separate state of existing indexed data. Deployment and health are confirmed, but smoke failures and the old-index blocker mean the work is only partially resolved. No claim is made that every audit finding is resolved.

## Three fix areas

1. **API search and paper sections.** Candidate filtering checks active filters; an empty LLM query returns no primary results, with up to three source candidates marked unverified. Final frozen local API suite: **806 cases, 805 passed, 1 skipped, 8.185 s**; local softguard suite: **13 passed**. Final Linux-native API suite: **13 cases, 11 passed, 2 skipped, 0.189 s**. Discussion restoration and equation/algorithm text equality were confirmed on the fixture snapshot. API production hash: `c7fc69f0c393d8b21f6dabcfb28d37061c73589c6800b2e05da68ff12d81c586`.
2. **Parser / chunk extraction.** Windows local parser suite: **58 passed**, using pinned parser 2.10 with a test-deadline override. Linux-native parser checks: **2 passed**, and the exact cached-snapshot comparison passed. The fixture snapshot moved from 77 to 90 chunks, restored Discussion, and preserved equation/algorithm payloads byte-for-byte; this does not prove repair of old indexed records. Extractor production hash: `8a18ab4dd5eaf42be670d77eae6cbb63f1fc674dbf1f9ea56bdb5c56e912802f`.
3. **MCP paper-ID compatibility.** `get_code_or_math_spec` accepts `paper_id` while retaining `arxiv_id`; `compare_methods` accepts `paper_id_a` / `paper_id_b` while retaining `arxiv_id_a` / `arxiv_id_b`. Old positional calls remain supported. Missing, non-string, or conflicting IDs return managed HTTP-status 400 tool errors before outbound HTTP. Windows local MCP suite: **36 passed**; focused Linux-native MCP suite: **8 passed**. MCP production hash: `cca456af5e8c8579cf34d80375cd86bf1751696130812a567f66ccaa286fdbf9`. Tool descriptions distinguish structured extracted elements from PDF text, which is not guaranteed to be raw LaTeX.

## Deployment and public smoke

Main reports a narrow deployment of three scoped files after the final guard. All three services were active, and the API startup health probe returned **200**. A separate post-deployment health read also confirmed API status OK. A final-script curl line failed to parse a malformed URL caused by CRLF; this was a command-format issue, not a product failure. The reported p95 samples are too small to establish an SLO.

Two public smoke runs each completed **10/10 HTTP calls**. Smoke v1 took **7.69 s**. Each run had **4 failed assertions**, not 4 HTTP error responses. Managed 400/404 tool responses were expected and passed where specified:

- **2 invalid-spec assertions** came from a BADTEST fixture targeting an EIP item as algorithm/equation when the indexed item was actually `CODE`. This is a fixture/type mismatch, not an empty-result product bug. The corrected focused CODE alias check below passes.
- Discussion retrieval against the old snapshot failed in both runs and remains tied to the legacy-data blocker below.
- Web3 raw-ID ordering assertion matched **9/10** exact IDs at the same ranks: one identifier changed from `wp:flashbots-mev` to `oa:W2938920845`. Before/after inspection found the same paper (FlashBoys 2.0), same URL (`https://arxiv.org/pdf/1904.05234`), reciprocal twins, and unchanged source identity (**10/10**). This strict raw-ID fixture assertion remains failed. It is not proof of ranking improvement, and IDs were not rewritten.
- Other smoke checks passed: tool inventory **13**, conflicting IDs returned **400**, section missing returned **404** with outline, and memory search returned **0 primary + 3 unverified** candidates.

The separate corrected CODE alias check completed **4/4 HTTP calls** successfully. Both identifier variants returned 7 code blocks with `truncated=true` and `partial=false`. Only the first 100-character code fragment was compared; its hash `14bb1c05f88c5900a68348137852067fb1b0df3836f77eb60d9f8a20b68d0c9d` matched. This does not establish equality of all seven blocks. Runner hash: `2a96fab5e60cb059d5421e1f68cf39051137718ed7ed766b3df4cc0b46e31910`; offline tests: **4 passed**. This runner was not used for the two 10-call live smokes.

The smoke results are partial, not a clean all-green gate. No index repair/write, environment change, commit, or push was performed.

## Existing indexed data: BLOCKED

The old-snapshot FTS mapping preflight reported `mapped=false` and **0 rows**. A planner proposed **4 additive chunks (77-80)**; its dry-run passed **7 tests**, but application remains disabled. There is no atomic journal. The deployed code fix prevents future loss but does not repair the old record. Durable journaling and index mapping are required before any existing-data repair. No FTS/cache/Qdrant write or reindex was performed.

## MCP identifier compatibility

`get_code_or_math_spec` now accepts `paper_id` as the preferred neutral argument and retains `arxiv_id` for existing clients. `compare_methods` accepts `paper_id_a` / `paper_id_b` while retaining `arxiv_id_a` / `arxiv_id_b`. The new and legacy schema properties are optional in the runtime MCP catalog so the implementation can validate the logical requirement itself.

- Legacy positional forms remain in their original positional slots.
- Either identifier spelling works by keyword; mixed spellings across the two comparison inputs are accepted.
- Missing IDs and conflicting spellings become MCP tool errors before any outbound API request. Equal values (after surrounding whitespace trim) are not conflicts.
- IDs are not rewritten to an assumed arXiv prefix. Existing path quoting remains in use; EIP-style IDs and IACR IDs containing `/` are covered by offline request assertions.
- Existing API response fields are retained. `paper_id` is added as an alias only when absent; existing `arxiv_id` values are not relabelled or overwritten.
- Tool documentation now describes indexed structured elements accurately: some extracted equations retain raw LaTeX, but PDF-derived text is not guaranteed to be exact LaTeX.

## Candidate-preparation utility

[`ops/prepare_audit_fix_patch.py`](../ops/prepare_audit_fix_patch.py) builds a new candidate from either a Git baseline or a supplied baseline file whose SHA-256 is explicitly verified, plus a current source file. The caller supplies the baseline identity/hash, current path, output path, existing function names to replace, and any new helper names. Optional marker insertion is constrained to one unique top-level line preceding the selected functions; otherwise helpers are inserted before the earliest selected function.

The builder matches only unique top-level `FunctionDef` / `AsyncFunctionDef` nodes, includes decorators in copied source, verifies the resulting selected ASTs exactly match the requested current ASTs, and refuses duplicate definitions or helpers already present in the baseline. It copies no imports, constants, or other globals. It preserves baseline text and line endings outside selected spans, normalizing only copied function snippets to the baseline newline style. Builder tests reported **9 passed**; isolated candidates were prepared for Main review.

The MCP candidate splice targets were:

| Operation | Top-level name |
|---|---|
| Insert helper | `_resolve_paper_id` |
| Replace existing function | `get_code_or_math_spec` |
| Replace existing function | `compare_methods` |
| Additional imports or global constants | None |

`Optional`, MCP `ToolError`, URL quoting, and the error helper were dependencies already present in the baseline MCP module.

## Verification evidence

| Area | Result | Qualification |
|---|---:|---|
| Area | Result | Qualification |
|---|---:|---|
| API local suite | 805 passed, 1 skipped / 806 cases, 8.185 s | final frozen suite |
| Local softguard suite | 13 passed | local |
| API native suite | 11 passed, 2 skipped / 13 cases, 0.189 s | Linux |
| Parser suite | 58 passed | Windows, pinned 2.10, deadline override |
| Parser native checks | 2 passed | Linux, exact cached-snapshot comparison passed |
| MCP suites | 36 passed local; focused 8 passed | Windows local and Linux native |
| Old-timeout tests | 9 passed, 2 skipped / 11 cases | Linux candidate |
| Candidate builder | 9 passed | offline |
| Corrected CODE alias runner | 4 passed | offline; live check separately 4/4 HTTP |
| Each public smoke run | 10/10 HTTP completed; 4 failed assertions | two runs; v1 7.69 s |
| Old-snapshot FTS mapping preflight | 0 rows; unmapped | existing-data repair blocked |
| Additive chunk planner dry-run | 7 tests passed; 4 chunks planned | apply disabled |

The reported snapshot comparison moved from 77 to 90 chunks, restored Discussion, and kept equation/algorithm payloads byte-equal. This is fixture/snapshot evidence, not proof that old indexed records were repaired.

## Remaining blocker and release state

Final freeze is **PASS** and the narrow deployment is confirmed. Readiness is **NOT CERTIFIED**. The old-snapshot FTS mapping preflight reported `mapped=false` and **0 rows**, and there is no atomic journal. The planner proposed **4 additive chunks (77-80)** and passed **7 dry-run tests**, but apply remains disabled until durable journaling and index mapping exist. The deployed code fix prevents future loss but does not repair the old record. Old-snapshot Discussion and strict Web3 raw-ID assertions remain explicit failures. No FTS/cache/Qdrant write, reindex, environment change, payment, access change, chain change, commit, or push was performed. No readiness score is assigned.
