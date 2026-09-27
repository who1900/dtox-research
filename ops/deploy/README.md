# Deployed topology (snapshot 2026-09-27)

Copies of the systemd units, drop-ins and cron lines running in production, so the setup can be rebuilt from the repo. No secrets live here: key and token files are referenced by path and stay on the hosts.

## Contabo (ingestion + API), `contabo/`

- `dtox-research` pipeline; drop-ins point it at the Oracle Qdrant (`QDRANT_URL=http://127.0.0.1:16335`), keep arXiv harvesting on OAI-PMH (`ARXIV_EXPORT_API_ENABLED=0`) and keep the paper-level BM25 index in sync (`PAPER_FTS_ENABLED=1`).
- `dtox-research-api`; `search-v2.conf` switches on hierarchical retrieval with paper-level BM25 and rank fusion (dense 1.0, lexical 0.8, global 1.0) and skips chunk-level BM25 in that mode. Rollback: remove the file and restart.
- `dtox-qdrant-tunnel`: SSH forwards to the Oracle host (16335 -> Qdrant 6333, 16337 -> rerank 8090). The tunnel key is restricted on Oracle to these forwards with `command="/usr/sbin/nologin"`.
- `dtox-attestor`, `dtox-x402` (live on Solana devnet).
- `crontab.txt`: backups, monitor and public stats, each given `QDRANT_URL` explicitly.

Ports already taken on this host by other projects include 6335/6336 (slopng Qdrant); check `ss -tlnp` and `docker ps` before choosing one.

## Oracle (Qdrant host), `oracle/`

- Qdrant 1.18.2 in Docker, `127.0.0.1:6333`, `--memory 10g`, storage and snapshots on host volumes under `/opt/qdrant`.
- `dtox-rerank` (disabled: cross-encoders did not help on the benchmark, see eval/results).
- `embed/embed_service.py` (bge-base) and `ops/coarse_reembed.py` for the paper index re-embed.
