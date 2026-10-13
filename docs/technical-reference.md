# Developer guide

[Product overview](../README.md) · [Research workflow](agent-research-workflow.md)

## Services

| Component | Role |
| --- | --- |
| `pipeline/` | Source acquisition, extraction, structured chunks and index publication |
| `api/` | FastAPI research endpoints |
| `mcp/` | Public research MCP with 13 tools |
| `embed/` | CPU ONNX embedding services |
| `attestor/` | Wallet-signed readings recorded through Solana SAS |
| `x402-gateway/` | Optional paid MCP integration |
| `ops/` | Maintenance and recovery utilities |
| `eval/` | Retrieval evaluation tools and fixtures |

Retrieval combines paper-level vectors, passage vectors, SQLite FTS5 BM25 and citation links. Paper embeddings use 768 dimensions; passage embeddings use 384. The API can optionally apply a cross-encoder reranker through `RERANK_ENABLED`.

Sources include research papers, Ethereum EIPs/ERCs, Solana SIMDs, whitepapers and curated technical documents. Available LaTeX is parsed into source sections and structured elements; other sources use their document extraction route. Document responses carry acquisition, extraction and coverage metadata. Abstract-only records remain discoverable.

## Public MCP tools

| Tool | Purpose |
| --- | --- |
| `find_papers` | Discover papers and specifications |
| `search_research_paper` | Search indexed passages |
| `get_paper` | Read a document card and outline |
| `read_paper_section` | Read a selected source section |
| `count_papers` | Count records matching filters |
| `similar_papers` | Explore related documents |
| `get_code_or_math_spec` | Retrieve equations, algorithms, code and tables |
| `compare_methods` | Compare evidence from two documents |
| `research_trends` | Explore topic and publication trends |
| `validate_project` | Assemble evidence against project claims |
| `get_research_bundle` | Build a bounded packet of source evidence |
| `get_verdict_message` | Prepare a message for a wallet-signed reading |
| `record_signed_verdict` | Submit a wallet-signed reading |

Clients obtain the detailed schemas through MCP discovery. Use `paper_id` for document identifiers, including `eip:`, `simd:`, `iacr:` and research-provider IDs. The previous `arxiv_id` argument remains a compatibility alias where supported.

`get_research_bundle` defaults to `limit=3`, `max_chars=12000`, `strict=True`. Ordinary search defaults to `strict=False`; select strict mode to retain the requested filters. Responses include pagination, truncation and source metadata for the requested scope.

The signing workflow prepares a message, obtains the caller's wallet signature, and records the attributed reading through SAS on Solana devnet. The free read tools work without a wallet. See [attestor](../attestor/README.md) for message and verification details.

## Self-hosting

Use Python 3.12, Docker and Qdrant. Select a Qdrant image compatible with your deployment. Model weights are downloaded from their model repositories during the embedding image build.

From the repository root:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
```

Apply this environment in each service terminal; shell exports do not propagate to other terminals. A .env file is not automatically loaded by these commands.

```bash
. .venv/bin/activate
export DTOX_DATA_DIR="$PWD/data"
export STATE_DB_PATH="$DTOX_DATA_DIR/state.db"
export RESEARCH_KEYS_PATH="$PWD/local-access-config.json"
export QDRANT_URL="http://127.0.0.1:6333"
export EMBED_URL="http://127.0.0.1:8005/embed"
export EMBED_BATCH_URL="http://127.0.0.1:8005/embed_batch"
```

Start storage and the passage embedding service:

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

Configure the paper-level embedding service and both Qdrant collections with their corresponding dimensions. Provision the local API access configuration at `RESEARCH_KEYS_PATH` and supply the matching `RESEARCH_API_KEY` to the MCP process through your environment or secret manager. Keep internal API, embedding and storage endpoints on loopback or a private network.

Run ingestion, API and MCP in separate terminals using the shared environment:

```bash
python3 pipeline/service.py
```

```bash
uvicorn api.main:app --port 8010
```

```bash
python3 mcp/server.py
```

Keep `MCP_REGISTRY_WRITES_ENABLED=0` for an anonymous public endpoint. Public signed verdicts use the caller's wallet signature; unsigned registry operations require distinct authenticated identities.

## x402 integration

The gateway exposes a separate MCP endpoint. Its paid evidence bundle uses the same research bundle operation as the free research service. Network and price configuration are described in the [gateway guide](../x402-gateway/README.md).

Paid reads settle after a successful handler. Signed verdict writes settle upfront, before the handler runs. Their configured price is a **per-batch-attempt fee**, including a batch that is rejected after settlement. There is **no automatic refund** of completed settlement. The response carries a payment receipt when available and per-item outcomes for partial batches. Signed writes are not automatically retried; reconcile the receipt and attestation state before retrying an uncertain outcome.

Payment authorizes a request; reviewer attribution and registry status are separate fields. The public payment integration uses Solana devnet. Configure EVM assets and facilitator support for the selected network; Ethereum Sepolia has no default asset mapping in the installed SDK.

## Checks

Install offline test dependencies:

```bash
pip install -r requirements-test.txt
```

Run the suites relevant to your change:

```bash
python -B -m unittest discover -s api/tests -p 'test_*.py'
python -B -m unittest discover -s mcp/tests -p 'test_*.py'
python -B -m unittest discover -s pipeline/tests -p 'test_*.py'
python -B -m unittest discover -s ops/tests -p 'test_*.py'
```

In `attestor/` or `x402-gateway/`:

```bash
npm ci
npm test
npm run build
```

GitHub **Checks** validates application changes. **Embedding image** builds and smoke-tests the embedding container when its files change. Documentation-only edits do not launch application suites. These workflows use isolated test environments rather than production databases.

## License

[GNU AGPL-3.0](../LICENSE).
