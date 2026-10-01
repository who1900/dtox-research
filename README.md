# dtox research

A Web3-first retrieval service over research papers, protocol specifications and technical whitepapers, with AI agents and language models as adjacent layers. It retrieves full text where available and explicitly retains abstract-only fallbacks where it is not. It is built for agents rather than for people: evidence can be a method section, an equation in raw LaTeX, a results table, or the paragraph where the authors admit what did not work.

Live MCP endpoint, no key and no signup:

```bash
claude mcp add --transport http dtox https://read.whoim.space/mcp
```

Other clients: `{"type": "http", "url": "https://read.whoim.space/mcp"}`. Anything that speaks stdio only (Codex CLI and friends) can bridge with `npx -y mcp-remote https://read.whoim.space/mcp`.

## What is actually different here

**Full text, cut by structure.** Available LaTeX sources are parsed with pylatexenc and split by section and by object: equations, algorithms, code listings and tables are first-class chunks, not prose. Other sources use available document text or recovered open-access PDFs. When the body cannot be fetched, a record may retain only its abstract; arXiv is not an exception. That is why `get_code_or_math_spec` can hand back an implementable formula when indexed, but cannot promise body evidence for every paper.

**Retrieval channels that fail differently.** Search is hierarchical: it ranks papers first, then reads chunks inside the best ones. Channels are merged by rank fusion.

- Dense over papers: title and abstract embedded with bge-base-en-v1.5 (int8 ONNX, 768 dim), kept in Qdrant.
- BM25 over papers (SQLite FTS5). Exact terms like `GRPO` or `durable nonce` survive here and drown in embeddings alone.
- Global chunk search: dense vectors (bge-small-en-v1.5, int8 ONNX, 384 dim) over millions of structural chunks in Qdrant, plus BM25 over the same chunks.
- The citation graph: one hop through the bibliography of the best hits. This channel does not depend on wording at all, which is the failure mode the other channels share.

Around the channels: copies of one paper from different sources (arXiv, ACL, PMLR, OpenAlex, whitepaper) are merged into one result, boilerplate chunks (preambles, author blocks, reference lists) never represent a paper, and results carry a scope signal (`in_scope`) that says when a paper looks outside the indexed subjects. No cross-encoder reranker is in the loop: it was tried and switched off because verdicts did not change and latency got worse.

**Layers and sources.** Four layers: web3 (primary), ai-agents, llm-slm and builder-tech. Sources: arXiv (LaTeX when available, otherwise abstract fallback), IACR ePrint (abstracts, with open-access full-text recovery where available), ACL Anthology, PMLR, OpenAlex open-access works, Ethereum EIPs and ERCs, Solana SIMDs, industry whitepapers, and curated GitHub technical docs (Solana, Anchor, DeFi protocols). Source membership alone does not establish that a particular record has full text.

**A registry of adjudicated claims.** A retrieval score measures wording, never whether a paper makes a claim. It returns candidates, and the reading agent can file a verdict. The public signed write path is `record_signed_verdict` (see below); unsigned registry mutation tools (`record_claim_judgment`, `link_claim_nodes`) require `MCP_REGISTRY_WRITES_ENABLED=1`. Later callers inherit recorded verdicts. Signatures authenticate wallet owners, not independent reasoning: multiple distinct wallets do not automatically form an independent quorum. Settlement requires quorum agreeing decisive readings spanning at least two known trusted model identities established by the server, not client-declared model names. Missing or `unspecified` models never supply diversity. Wallet-only agreement stays `read_once` (pending); same-known-model agreement is `agreed_same_model`; opposing decisive verdicts are `contested`. Only trusted diversity can yield `confirmed_prior_art` or `ruled_out`.

**Bounded evidence and research bundles.** `get_research_bundle(query, layer=None, limit=3, max_chars=12000, strict=True)` is a public read tool proxying `POST /v1/research/bundle` with a 30-second internal request timeout. It assembles a reading packet with evidence and provenance under a character budget. Inspect the returned provenance and abstract-only fallback markers before citing a passage; candidates are not confirmed prior art. The bundle does not write registry verdicts, create claim links, submit transactions, or call a paid LLM. Existing `get_paper`, `read_paper_section` and `get_code_or_math_spec` remain available for targeted reading.

**Opt-in strict search, unchanged search defaults.** `search_research_paper(..., strict=False)` preserves the existing soft-layer and API auto-relax defaults. Non-strict results may include relaxed filters; check `relaxed`, `matches_filters` and `terms_resolution.unknown`. `strict=True` is forwarded to the API and explicitly sets `auto_relax=False`: requested layers and filters must not silently broaden. Research bundles default to `strict=True`; ordinary search still defaults to `False`.

**Calibration measured, not guessed.** Score bands are the percentiles of on-topic hits inside each layer, because the layers are not comparable: measured, an on-topic hit lands near 0.90 in `llm-slm` and near 0.81 in `web3`. One global threshold made every crypto claim look like open ground.

## Honesty as a feature

The failure this service works hardest to avoid is a confident answer where it should own up.

- Read the scope signal for subjects outside the four layers: nearest neighbours are not proof of coverage or novelty.
- A quiet result ships with `corpus_coverage`, because silence over 800 indexed papers means something and silence over three does not.
- A request for one specific paper that is not indexed says so instead of quietly returning neighbours.
- There are no patents in here, and the service says that rather than letting a founder read absence as novelty.
- Full-text availability is per record: arXiv can fall back to abstracts, while IACR can have recovered open-access text. A legacy source-family `fulltext` label alone is not proof of body availability; inspect evidence provenance.
- Where an answer supplies `corpus.as_of`, retain it: the index grows and yesterday's retrieval is not reproducible without a corpus snapshot. Not every read endpoint supplies this metadata.

## Layout

```
pipeline/    harvest, quality gate, LaTeX extraction, chunking, embedding,
             citation graph, deferred re-checks. One systemd service, 14
             stages in threads, SQLite WAL for durability. coarse_index.py
             maintains papers_coarse, a small in-RAM title+abstract index
             used to shortlist candidate articles before the on-disk
             per-chunk search.
api/         FastAPI: search, spec, compare, trends, validate, adjudicate,
             claim linking. api/tests holds the regression harness.
mcp/         MCP server (streamable-http) wrapping the API as 13 public tools:
             search_research_paper, find_papers, get_paper,
             read_paper_section, count_papers, similar_papers,
             get_code_or_math_spec, compare_methods, research_trends,
             validate_project, get_research_bundle, get_verdict_message,
             record_signed_verdict.
attestor/    wallet-signed verdicts written to Solana as SAS attestations.
x402-gateway/ isolated USDC payment surface (x402 v2). Production runs
             Solana-only, live on devnet; Base support exists in code.
embed/       the embedding service (bge-small and bge-base), CPU only, ONNX
             int8. rerank_service.py is an experiment, not used in production.
ops/         backups with verified restores, monitoring with alerts on
             transitions, an audit of the deferred and rejected shelves.
```

## Running it

You need Qdrant, Docker, and Python 3.12. Model weights are downloaded from
their official Hugging Face repositories during the image build; they are not
stored in git.

```bash
cp .env.example .env          # fill in what you need; nothing is required to start
cp keys.example.json keys.json
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
docker build -t embed-small embed/
docker run -d --name embed-small --cpus=6 --memory=9g \
  -e ORT_THREADS=5 -p 127.0.0.1:8005:8080 embed-small
python3 pipeline/service.py   # ingestion, resumable, safe to kill
uvicorn api.main:app --port 8010
python3 mcp/server.py         # needs RESEARCH_API_KEY
```

Generate a random local API key, replace the placeholder in `keys.json`, and
set the same value as `RESEARCH_API_KEY`. Runtime paths and internal service
URLs can be overridden with the variables documented in `.env.example`.

The public MCP disables unsigned registry mutation. Keep `MCP_REGISTRY_WRITES_ENABLED=0` unless
the endpoint is protected by per-user authentication; otherwise anonymous
callers share the server identity and can poison the claim registry. The one
public write is `record_signed_verdict`, which needs a wallet signature.

The pipeline is designed to be killed. Every stage commits per paper, so a crash or a reboot resumes from the last committed status rather than starting over.

## Verifiable verdicts on Solana

Unsigned writes are disabled by default because anonymous callers share one server identity. The signed-verdict path uses `attestor` (see `attestor/README.md`): an agent signs with its own ed25519 wallet, and the attestor verifies the signature before writing a Solana devnet SAS attestation. `get_verdict_message` returns the canonical message; `record_signed_verdict` submits the signature. This makes authorship verifiable, not the correctness or independence of the judgment. One operator can create many wallets, so even multiple signed wallets agreeing remain `read_once` (pending) without trusted model diversity; they never automatically confirm prior art or rule it out. Self-declared model names and missing model attribution cannot settle a claim. A paid x402 write surface may add a per-attempt cost, but does not establish independent reasoning. Reviewer reputation is not built yet. Research bundles and ordinary read tools do not invoke this write flow.

## Tests

Latest lead-verified aggregate results (2026-10-02, after the ops PID-check fix): API full suite **498 tests: 497 passed, 1 skipped**; MCP **11 passed**; eval **40 passed**. These supersede the earlier totals and focused-test snapshot. No CI run or success is claimed here.

Local unit suites:

```bash
python -m unittest discover -s api/tests -p 'test_*.py'
python -m unittest discover -s mcp/tests -p 'test_*.py'
python -m unittest discover -s eval/tests -p 'test_*.py'
```

The MCP tests inspect tool signatures through AST and mock HTTP requests, so they do not depend on compatibility between installed FastMCP versions.

Separate live-API regression harness:

```bash
python3 api/tests/harness.py            # run against a live API
python3 api/tests/harness.py --baseline # freeze the current behaviour
```

The live-API harness mixes judgements a human verified by reading the papers with snapshots that freeze behaviour. It has caught real defects: an introduction-only match that buried a genuine precedent, a per-paper chunk choice that preferred the introduction, and an internal search path that spent the caller's rate limit.

## Deployed validation snapshot - 2026-10-02

The following deployment and public smoke results were verified by the lead agent during deployment. API and MCP are deployed. The public MCP exposes **13 tools**, advertises search `strict=False` and bundle `strict=True`, and does not expose unsigned registry writes. The existing signed-verdict path is separate from the read tools.

- Public research bundle for **EIP-4337**, `limit=1`, `max_chars=6000`: **10 code fragments**, **3866 returned characters**, **1.17 seconds**, `partial=false`.
- Public **VDF** search with `limit=4`: the two IACR results are `fulltext=false` / abstract-only; the other PDF-backed results are `fulltext=true`. Acquisition metadata, not the document-ID prefix, determines these labels.
- Additional public fast-validation smoke: **11.56 seconds**, `error=null`, pure-read operation; external ERC-4337 candidate context with **two unread candidates** and **no automatic confirmation**. This is distinct from the 1.17-second research-bundle observation.

A count-only SQL check found **zero legacy `trusted-model:`-prefixed records**, without reading or exposing names. This supports conservative handling of legacy attribution, not a claim of independently confirmed verdicts.

Stage-6 before/after search checks showed **unchanged ranks**:

| Case | Before | After |
| --- | --- | --- |
| Canonical EIP-4337 | 1 | 1 |
| EIP-7702 | 1 | 1 |
| Nonce | 3 | 3 |
| VDF | 4 | 4 |
| EIP-4844 | 9 | 9 |
| Oracle / Chainlink v2 | Missing from top 10 | Missing from top 10 |

These checks demonstrate unchanged retrieval ordering for these cases, **not a ranked-quality gain**. The individual 1.17-second bundle and 11.56-second fast-validation observations are not latency distributions. No soak or load test was run; **p95 is not established**.

The Qdrant guard is deployed and working with `oom_score_adj=-900`, maintained by systemd every minute, without restarting Qdrant. The heartbeat-wrapper PID-check fix is complete. **Final ops status: verified smoke**, verified by the lead agent during deployment from live `--report`: API health, embed health, Qdrant and the 13-tool MCP check were all OK. The validated snapshot was about one minute old; all **14 stages plus the reporter** had current `starting` / `idle` / `busy` states, with no failures. The report recorded disk **41 GB** and backup age **19 hours**.

Explicit unknowns remain: the p95 sample count was **1 (<20)**, so p95 is not established; no journal was available to distinguish first completions from reprocessing; the queue observation was **14**, with an unknown initial progress baseline. No overnight soak, growth-rate estimate or long-term-stability claim is made. Mainnet is **unchanged / not assessed**; verified smoke is not a mainnet-readiness claim. This is a frozen lead-verified snapshot, not continuous monitoring.

The [offline evidence QA report](docs/2026-10-02-evidence-review.html) records the guards, confirmed smoke results and remaining verification boundaries without external telemetry.

## What it does not do

Four layers only (web3, ai-agents, llm-slm, builder-tech). No patents. No guarantee that a given paper is present, inside the subjects or otherwise. The registry is young. Score bands and the scope gate are anchored to absolute numbers and drift as the corpus grows, which is the next thing to fix.

## Licence

AGPL-3.0. Use it, fork it, run it. If you run a modified version as a service, publish the modifications.
