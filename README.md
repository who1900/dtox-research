# dtox research

A Web3-first retrieval service over the **full text** of research papers, protocol specifications and technical whitepapers, with AI agents and language models as the two adjacent layers. It is built for agents rather than for people: the unit it returns is a method section, an equation in raw LaTeX, a results table, or the paragraph where the authors admit what did not work.

Live MCP endpoint, no key and no signup:

```bash
claude mcp add --transport http dtox https://read.whoim.space/mcp
```

Other clients: `{"type": "http", "url": "https://read.whoim.space/mcp"}`. Anything that speaks stdio only (Codex CLI and friends) can bridge with `npx -y mcp-remote https://read.whoim.space/mcp`.

## What is actually different here

**Full text, cut by structure.** Papers are fetched as LaTeX sources, parsed with pylatexenc, and split by section and by object: equations, algorithms, code listings and tables are first-class chunks, not prose. That is why `get_code_or_math_spec` can hand back an implementable formula instead of a paraphrase.

**Three retrieval channels that fail differently.**

- Dense vectors (bge-small-en-v1.5, int8 ONNX, 384 dim) over millions of structural chunks in Qdrant.
- Lexical BM25 over the same chunks in SQLite FTS5, fused by reciprocal rank. Exact terms like `GRPO` or `durable nonce` survive here and drown in embeddings alone.
- The citation graph: one hop through the bibliography of the best hits. This channel does not depend on wording at all, which is the failure mode the other two share.

**A registry of adjudicated claims.** A retrieval score measures wording, never whether a paper makes a claim. So the service does not pretend: it returns candidates, and the reading agent files a verdict with `record_claim_judgment`. Later callers inherit that verdict. A claim is only settled when independent readers agree, and identity comes from the authenticated key rather than from a self-declared string.

**Calibration measured, not guessed.** Score bands are the percentiles of on-topic hits inside each layer, because the layers are not comparable: measured, an on-topic hit lands near 0.90 in `llm-slm` and near 0.81 in `web3`. One global threshold made every crypto claim look like open ground.

## Honesty as a feature

The failure this service works hardest to avoid is a confident answer where it should own up.

- An idea outside the three subjects is refused, not answered with "no match".
- A quiet result ships with `corpus_coverage`, because silence over 800 indexed papers means something and silence over three does not.
- A request for one specific paper that is not indexed says so instead of quietly returning neighbours.
- There are no patents in here, and the service says that rather than letting a founder read absence as novelty.
- IACR records are abstract-only and carry `fulltext: false`.
- Every answer carries `corpus.as_of`, since the index grows and yesterday's verdict is not reproducible without it.

## Layout

```
pipeline/    harvest, quality gate, LaTeX extraction, chunking, embedding,
             citation graph, deferred re-checks. One systemd service, eight
             stages in threads, SQLite WAL for durability.
api/         FastAPI: search, spec, compare, trends, validate, adjudicate,
             claim linking. api/tests holds the regression harness.
mcp/         MCP server (streamable-http) wrapping the API as seven tools.
x402-gateway/ isolated Base + Solana USDC payment surface for read-only tools.
embed/       the embedding and reranking service, CPU only, ONNX int8.
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

The public MCP should be read-only. Keep `MCP_REGISTRY_WRITES_ENABLED=0` unless
the endpoint is protected by per-user authentication; otherwise anonymous
callers share the server identity and can poison the claim registry.

The pipeline is designed to be killed. Every stage commits per paper, so a crash or a reboot resumes from the last committed status rather than starting over.

## Tests

```bash
python3 api/tests/harness.py            # run against a live API
python3 api/tests/harness.py --baseline # freeze the current behaviour
```

Seventeen cases, split between judgements a human verified by reading the papers and snapshots that only freeze today's behaviour. It has caught real defects: an introduction-only match that buried a genuine precedent, a per-paper chunk choice that preferred the introduction, and an internal search path that spent the caller's rate limit.

## What it does not do

Three subjects only. No patents. No guarantee that a given paper is present, inside the subjects or otherwise. The registry is young. Score bands and the scope gate are anchored to absolute numbers and drift as the corpus grows, which is the next thing to fix.

## Licence

AGPL-3.0. Use it, fork it, run it. If you run a modified version as a service, publish the modifications.
