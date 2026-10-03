# dtox research

A Web3-first retrieval service over research papers, protocol specifications and technical whitepapers, with AI agents and language models as adjacent layers. It retrieves full text where available and explicitly retains abstract-only fallbacks where it is not. It is built for agents rather than for people: evidence can be a method section, an equation in raw LaTeX, a results table, or the paragraph where the authors admit what did not work.

Live MCP endpoint, no key and no signup:

```bash
claude mcp add --transport http dtox https://read.whoim.space/mcp
codex mcp add dtox --url https://read.whoim.space/mcp
```

Other clients: `{"type": "http", "url": "https://read.whoim.space/mcp"}`. Codex supports the direct URL above. Clients that only speak stdio can bridge with `npx -y mcp-remote https://read.whoim.space/mcp`.

## What is actually different here

**Full text, cut by structure.** Available LaTeX sources are parsed with pylatexenc and split by section and by object: equations, algorithms, code listings and tables are first-class chunks, not prose. Other sources use available document text or recovered open-access PDFs. When the body cannot be fetched, a record may retain only its abstract; arXiv is not an exception. That is why `get_code_or_math_spec` can hand back an implementable formula when indexed, but cannot promise body evidence for every paper.

**Retrieval channels that fail differently.** Search is hierarchical: it ranks papers first, then reads chunks inside the best ones. Channels are merged by rank fusion.

- Dense over papers: title and abstract embedded with bge-base-en-v1.5 (int8 ONNX, 768 dim), kept in Qdrant.
- BM25 over papers (SQLite FTS5). Exact terms like `GRPO` or `durable nonce` survive here and drown in embeddings alone.
- Global chunk search: dense vectors (bge-small-en-v1.5, int8 ONNX, 384 dim) over millions of structural chunks in Qdrant, plus BM25 over the same chunks.
- The citation graph: one hop through citation edges from retrieved seeds. Traversal uses graph links, but which seeds are visited still depends on retrieval and registry matches; the overall result is not invariant to wording.

Around the channels: copies of one paper from different sources (arXiv, ACL, PMLR, OpenAlex, whitepaper) are merged into one result, boilerplate chunks (preambles, author blocks, reference lists) never represent a paper, and results carry a scope signal (`in_scope`) that says when a paper looks outside the indexed subjects. No cross-encoder reranker is in the loop: it was tried and switched off because verdicts did not change and latency got worse.

**Layers and sources.** Four layers: web3 (primary), ai-agents, llm-slm and builder-tech. Sources: arXiv (LaTeX when available, otherwise abstract fallback), IACR ePrint (abstracts, with open-access full-text recovery where available), ACL Anthology, PMLR, OpenAlex open-access works, Ethereum EIPs and ERCs, Solana SIMDs, industry whitepapers, and curated GitHub technical docs (Solana, Anchor, DeFi protocols). Source membership alone does not establish that a particular record has full text.

**A registry of adjudicated claims.** A retrieval score measures wording, never whether a paper makes a claim. It returns candidates, and the reading agent can file a verdict. The public signed write path is `record_signed_verdict` (see below); unsigned registry mutation tools (`record_claim_judgment`, `link_claim_nodes`) require `MCP_REGISTRY_WRITES_ENABLED=1`. Recorded verdicts can be reused through known claim nodes and explicit links, but arbitrary paraphrases are not guaranteed to resolve to the same claim. Near neighbours are proposals, not proof of equivalence. Signatures authenticate wallet owners, not independent reasoning: multiple distinct wallets do not automatically form an independent quorum. Scientific settlement requires agreeing decisive readings spanning at least two known trusted model identities established by the server, not client-declared model names. Missing or `unspecified` models never supply diversity. Wallet-only agreement stays `read_once` (pending); same-known-model agreement is `agreed_same_model`; opposing decisive verdicts are `contested`. Only trusted diversity can yield `confirmed_prior_art` or `ruled_out`.

**Bounded evidence and research bundles.** `get_research_bundle(query, layer=None, limit=3, max_chars=12000, strict=True)` is a public read tool proxying `POST /v1/research/bundle` with a 30-second internal request timeout. It assembles a reading packet with evidence and provenance under a character budget. Inspect the returned provenance and abstract-only fallback markers before citing a passage; candidates are not confirmed prior art. The bundle does not write registry verdicts, create claim links, submit transactions, or call a paid LLM. Existing `get_paper`, `read_paper_section` and `get_code_or_math_spec` remain available for targeted reading.

**Opt-in strict search, unchanged search defaults.** `search_research_paper(..., strict=False)` preserves the existing soft-layer and API auto-relax defaults. Non-strict results may include relaxed filters; check `relaxed`, `matches_filters` and `terms_resolution.unknown`. `strict=True` is forwarded to the API and explicitly sets `auto_relax=False`: requested layers and filters must not silently broaden. Research bundles default to `strict=True`; ordinary search still defaults to `False`.

**Calibration measured, not guessed.** Score bands are the percentiles of on-topic hits inside each layer, because the layers are not comparable: measured, an on-topic hit lands near 0.90 in `llm-slm` and near 0.81 in `web3`. One global threshold made every crypto claim look like open ground.

## Honesty as a feature

The failure this service works hardest to avoid is a confident answer where it should own up.

- Read the scope signal for subjects outside the four layers: nearest neighbours are not proof of coverage or novelty.
- `corpus_coverage` can describe a bounded retrieved sample, not the entire corpus. Empty results reflect index coverage and retrieval limits, never establish novelty; inspect evidence and scope.
- A request for one specific paper that is not indexed says so instead of quietly returning neighbours.
- There are no patents in here, and the service says that rather than letting a founder read absence as novelty.
- Full-text availability is per record: arXiv can fall back to abstracts, while IACR can have recovered open-access text. A legacy source-family `fulltext` label alone is not proof of body availability; inspect evidence provenance.
- Where an answer supplies `corpus.as_of`, retain it as an observation time, not a corpus snapshot identifier or a reproducibility guarantee. Not every read endpoint supplies this metadata.

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
x402-gateway/ isolated USDC payment surface (x402 v2). Observed public mode:
             Solana devnet only. EVM network configuration is not live E2E proof.
embed/       the embedding service (bge-small and bge-base), CPU only, ONNX
             int8. rerank_service.py is an experiment, not used in production.
ops/         backup and restore utilities (restore verification not established), alerts on
             transitions, an audit of the deferred and rejected shelves.
```

## Running it

You need Qdrant, Docker, and Python 3.12. Model weights are downloaded from
their official Hugging Face repositories during the image build; they are not
stored in git.

Setup terminal, from the repository root (POSIX shell examples):

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
```

Run this shared environment block in each service terminal, from the repository
root; shell exports do not propagate to other terminals:

```bash
. .venv/bin/activate
# These are non-secret local example paths and URLs, not production locations.
# A .env file is not automatically loaded by these commands.
export DTOX_DATA_DIR="$PWD/data"
export STATE_DB_PATH="$DTOX_DATA_DIR/state.db"
export RESEARCH_KEYS_PATH="$PWD/local-access-config.json"
export QDRANT_URL="http://127.0.0.1:6333"
export EMBED_URL="http://127.0.0.1:8005/embed"
export EMBED_BATCH_URL="http://127.0.0.1:8005/embed_batch"
```

Setup terminal: start local storage and embedding services. Replace
`<reviewed-version>` with an operator-reviewed compatible Qdrant image version
before running; do not apply this example to an existing production collection.
The named volume persists Qdrant storage across container replacement. Both
published ports are bound to localhost.

```bash
docker volume create dtox-qdrant-data
docker run -d --name dtox-qdrant \
  -p 127.0.0.1:6333:6333 \
  -v dtox-qdrant-data:/qdrant/storage \
  'qdrant/qdrant:<reviewed-version>'
docker build -t embed-small embed/
docker run -d --name embed-small --cpus=6 --memory=9g \
  -e ORT_THREADS=5 -p 127.0.0.1:8005:8080 embed-small
```

Terminal 1: apply the shared environment block, then run ingestion:

```bash
python3 pipeline/service.py   # ingestion, resumable, safe to kill
```

Terminal 2: apply the shared environment block, then run the API independently:

```bash
uvicorn api.main:app --port 8010
```

Terminal 3: apply the shared environment block and securely supply the matching
`RESEARCH_API_KEY`, then run MCP independently:

```bash
python3 mcp/server.py         # needs RESEARCH_API_KEY
```

Provision the local access configuration at the example `RESEARCH_KEYS_PATH`
through your own secure setup, and supply its matching `RESEARCH_API_KEY` to
the MCP process using your secret-management method. Never commit credentials.
Copying a `.env` file alone does not load it into Python or uvicorn; explicitly
export configuration into each process environment. Run ingestion, API and MCP
in separate processes with the same configured paths. Wait for local Qdrant and
embedding readiness before starting ingestion. Model availability and required
collections must also be provisioned. These container commands were not run
as part of this audit. A clean self-host smoke test has not
been performed; this is a setup outline, not a turnkey verification claim.

The public MCP disables unsigned registry mutation. Keep `MCP_REGISTRY_WRITES_ENABLED=0` unless
the endpoint is protected by per-user authentication; otherwise anonymous
callers share the server identity and can poison the claim registry. The one
public write is `record_signed_verdict`, which needs a wallet signature.

The pipeline is designed to be killed. Every stage commits per paper, so a crash or a reboot resumes from the last committed status rather than starting over.

## Verifiable verdicts on Solana

Unsigned writes are disabled by default because anonymous callers share one server identity. The signed-verdict path uses `attestor` (see `attestor/README.md`): an agent signs with its own ed25519 wallet, and the attestor verifies the signature before writing a Solana devnet SAS attestation. `get_verdict_message` returns the canonical message; `record_signed_verdict` submits the signature. This makes authorship verifiable, not the correctness or independence of the judgment. One operator can create many wallets, so even multiple signed wallets agreeing remain `read_once` (pending) without trusted model diversity; they never automatically confirm prior art or rule it out. Self-declared model names and missing model attribution cannot settle a claim. A paid x402 write surface may add a per-attempt cost, but does not establish independent reasoning. Reviewer reputation is not built yet. Research bundles and ordinary read tools do not invoke this write flow.

## Tests

Historical lead-reported results (2026-10-02, after the ops PID-check fix): API full suite **498 tests: 497 passed, 1 skipped**; MCP **11 passed**; eval **40 passed**. These are observations from that run, not current-branch totals. No CI run or success is claimed here.

Local unit suites:

```bash
python -m unittest discover -s api/tests -p 'test_*.py'
python -m unittest discover -s mcp/tests -p 'test_*.py'
python -m unittest discover -s eval/tests -p 'test_*.py'
python -m unittest discover -s pipeline/tests -p 'test_*.py'
python -m unittest discover -s ops/tests -p 'test_*.py'
```

MCP tests include AST proxy contracts and offline SDK protocol-error regressions.
Use the pinned dependencies for deployment validation; testing another SDK
major version does not establish compatibility with the deployed version.
Gateway tests use the real installed x402 SDK with a mock facilitator and
in-memory MCP transport. They make no real payments or network requests.

Separate live-API regression harness:

```bash
python3 api/tests/harness.py            # run against a live API
python3 api/tests/harness.py --baseline # freeze the current behaviour
```

The live-API harness mixes judgements a human verified by reading the papers with snapshots that freeze behaviour. It has caught real defects: an introduction-only match that buried a genuine precedent, a per-paper chunk choice that preferred the introduction, and an internal search path that spent the caller's rate limit.

## Paid x402 policy (code contract, not a deployment claim)

The paid `get_evidence_bundle` proxies the same `get_research_bundle` tool as
the free endpoint, with `limit=3`, `max_chars=12000`, `strict=True` by default.
It is a payment transport, not a claim of exclusive evidence or proven added
scientific value. Unsupported bundle filters are rejected, not silently dropped.

Only paid `record_signed_verdict` uses SDK `extra.paymentFlow='upfront'`:
settlement succeeds before the write handler is invoked. The configured price
is a per-batch-attempt fee, not a fee per accepted item. If settlement fails,
the write handler is not called. Once settlement succeeds, handler errors,
all-rejected batches and timeouts retain the payment receipt in
`_meta['x402/payment-response']` when the response reaches the client. There is
no automatic refund or rollback of a completed payment. A lost response can
leave payment or write outcome uncertain; reconcile the receipt and attestation
state before a user-authorized retry. Signed writes are not automatically
retried. Partial batches preserve all per-item statuses; all statuses >=400
produce an MCP error. Payment is not scientific quorum.

Paid reads retain `authorization`: settlement follows a successful handler;
a failed read handler does not settle. Transient upstream reads may retry once.
Disabled/shadow modes do not charge. This change has only offline mocked SDK
validation, not a newly deployed or paid live E2E validation.

EVM configuration includes Base Sepolia, Ethereum Sepolia and Arbitrum Sepolia
identifiers, alongside Solana devnet. The observed public payment surface was
Solana devnet only. Offline lifecycle tests cover Base Sepolia and Solana devnet,
not real settlement on either. With the installed SDK 2.24.0, Ethereum Sepolia
has no default asset mapping in `ExactEvmScheme`; enabling default EVM networks
can fail during requirement construction. Asset and facilitator support need
separate verification before enabling those networks. No multichain or mainnet
live-readiness claim is made.

## Historical deployed smoke observations - 2026-10-02

The following deployment and public smoke results were reported by the lead
agent for that observation window and were not rerun for this change. At that
time the public MCP exposed **13 tools**, advertised search `strict=False` and
bundle `strict=True`, and did not expose unsigned registry writes. These
observations do not establish deployment of the current checkout.

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

Explicit unknowns remain: the p95 sample count was **1 (<20)**, so p95 is not established; no journal was available to distinguish first completions from reprocessing; the queue observation was **14**, with an unknown initial progress baseline. No overnight soak, growth-rate estimate or long-term-stability claim is made. Mainnet is **unchanged / not assessed**; verified smoke is not a mainnet-readiness claim. This is a historical observation, not a corpus snapshot or continuous monitoring.

The [offline evidence QA report](docs/2026-10-02-evidence-review.html) records the guards, confirmed smoke results and remaining verification boundaries without external telemetry.

## What it does not do

Four layers only (web3, ai-agents, llm-slm, builder-tech). No patents. No guarantee that a given paper is present, inside the subjects or otherwise. The registry is young. Score bands and the scope gate are anchored to absolute numbers and drift as the corpus grows, which is the next thing to fix.

## Licence

AGPL-3.0. Use it, fork it, run it. If you run a modified version as a service, publish the modifications.
