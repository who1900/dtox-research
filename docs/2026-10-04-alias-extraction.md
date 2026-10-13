# Alias extraction

STATUS: DEPLOYED. Narrow source fix; manual MCP smoke verified; not global production ready.

- Global trusted twins; bounded alias fallback only in spec and bundle.
- Fill only missing categories; requested source has precedence; maximum 2 alias reads.
- Provenance: actual source ID, source URL and fragment hash.
- No model, ranking, or registry changes. Flags remain off.
- Before: get_code_or_math_spec("oa:W2945678139", target_elements="equation,algorithm", max_chars=2500) returned=0, available=0, chars=0. Canonical "1809.09044" returned=11, available=21, chars=2286.
- Before: web3 bundle "data availability sampling for light clients", limit=2, max_chars=6000 returned 0 equations, 28 text characters, and made 2 paper retrieval calls.
- API full suite: 761 tests in 8.867 s; 760 passed, 1 skipped.
- MCP: 26 passed in 0.314 s.
- Total: 787 tests; 786 passed, 1 skipped.
- AST comparison with prior release: only `paper_spec` and `research_bundle` modified in `main.py`; 2 new pure helpers.
- Deployment confirmed; backup: `/opt/dtox-research-api/release-backups/dtox-alias-20261004.CPEqklfB`. Health OK; registry counts unchanged at 54/16/2.
- Post-deploy canonical MCP result: 11 returned, 21 available, 2286 chars, matching pre-deploy hashes. OA retry returned 11/21/2286 with the same canonical hash and text; actual source `1809.09044`, requested OA passport keeps `extraction_type=pdf_text` and `exact_latex=false`; extracted canonical sections have `exact_latex=true`.
- The initial OA call returned `isError=true`; its error was not retained. Retry succeeded in 10.567 s, versus 5.499 s pre-deploy. Canonical post-deploy took 6.379 s versus 4.296 s pre-deploy. These observations do not support claiming no latency regression.
- Bundle post-deploy: 6.165 s, 7 equations, 2 limitations, 3000 chars, 4 retrieval calls; before: 28 text chars and 2 paper retrieval calls. Selected second alias changed to OA twin `W4289494000` for the same paper.
- Algorithms are still missing. Alias reads remain bounded to a maximum of 2; this is a source-fix deployment with manual MCP smoke checks, not evidence of global production readiness.

