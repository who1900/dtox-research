# Offline gateway contract gate

Run from `x402-gateway`, with installed local dependencies and Python/Pydantic:

```sh
node node_modules/tsx/dist/cli.mjs --test test/*.test.ts
node test/verify-build.mjs
node test/verify-build.mjs --check-dist
node test/verify-build.mjs --before /absolute/path/to/runtime-contract-before.json
node test/verify-build.mjs --check-scoped --before /absolute/path/to/runtime-contract-before.json
```

The Python exporter reads real backend model AST and executes only selected
Pydantic classes, not application imports, startup, auth, retrieval or requests.
Missing Python/Pydantic fails the tests; no fallback fixtures or skipped gate.
The 490-case bundle boundary grid uses real Pydantic validation. Exact numeric
schema equality covers the continuous integer ranges, not only the sample grid.
Other tool checks cover actual MCP parameter signatures, backend body models,
existing aliases/defaults, unchanged 30000-character spec/compare budgets and
additive spec cursor. In-memory upstream/payment fixtures are not live E2E.

Only bundle `limit=6..15` and `max_chars=20001..30000` are newly rejected. The real
backend already rejected those ranges before retrieval. Successful request
ranges remain unchanged, as do 3/12000 defaults, strict behavior, prices and
upfront signed-write / authorization-read payment policy. Nothing is clamped
or silently truncated by this fix. Backend capacity is not changed.

The real runtime snapshot also showed stricter signed-field patterns in the
local candidate than in production. Those candidate-only wire restrictions
are aligned to the deployed schema, not rolled into this release. Backend and
attestor cryptographic/security validation is unchanged. `--before` compares
all nine compiled schemas to that real snapshot, permitting only the two
bundle upper-bound corrections and optional cursor, and compares full service
info including captured prices, payment policy, mode and networks. The input
snapshot stays private and read-only; no production contact is made.

The build check compiles twice into isolated temporary directories under test,
requires byte-identical emitted files, checks source/compiled discovery and all
nine tool contracts, and reports runtime/compiler, config/package/lock, source
and emitted SHA256 metadata. Temporary output is removed; existing dist is
read-only. Source tests passing does not imply an existing dist is current.

`--check-dist` exits nonzero for any missing, stale or extra compiled JS. This
is the full-rebuild release-artifact gate, not a requirement to replace protected
runtime files for a scoped release.

`--check-scoped --before ...` requires the actual snapshot and verifies a
temporary overlay of existing compiled JS with only candidate tools.js and
tool-schemas.js replaced. Real SDK registrations and MCP tools/list are checked
against all nine actual schemas, including additionalProperties and nested
strictness. Existing payments/server/config/upstream/errors JS and other files
must stay byte-identical; before/after existing-dist hashes must also match.
The report includes the four-file candidate manifest and preserved JS SHA256.
No existing file is replaced by this verification. Full dist drift (including
an unused extra api.js) does not alone block a passing four-file scoped gate.
The actual live SHA preservation guard still belongs to main before rollout.
Main alone owns subsequent build/release. Never restore historical payment TS
to match an old build. No deploy, SSH, env access, external network, real wallet
or monetary transaction is performed by these checks.
