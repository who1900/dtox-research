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

## Qdrant host OOM guard (deployed and verified 2026-10-02)

`oracle/dtox-qdrant-oom-guard.timer` runs the dedicated oneshot at boot and
each minute (persistent calendar timer). Verified deployment installed the tracked
script at `/opt/dtox-research/ops/qdrant_oom_guard.py`, with both
units installed on Oracle and the timer enabled/running. The real oneshot
under its systemd sandbox/capabilities selected qdrant PID `321228` and
verified `oom_score_adj=-900`; wrapper main PID `321193` remained unchanged.
No Qdrant restart was performed. These PIDs are verification-time observations,
not configuration.
Script and parent directories must be root-owned and not writable by
untrusted users.

The guard uses only the local Docker socket, inspects the exact `qdrant`
container, validates running/not-paused/not-restarting state and its main PID.
The target is that main PID if its comm is `qdrant`, otherwise **exactly one
direct qdrant child** from `/proc/MainPID/task/MainPID/children`. Both parent
and child must match the exact Docker container ID in cgroup; the child must
have PPID=MainPID and identical cgroup membership. Both process start times,
PPIDs and cgroups are checked again with pinned proc descriptors, alongside
a fresh container inspect and a second direct-child enumeration, before
setting **only the target PID** to `oom_score_adj=-900`. No process search,
secrets, restart, recreation or Docker configuration changes. Ambiguous
children, another container/host cgroup, changed PID, missing processes or
insufficient permissions fail with a nonzero status and clear journal message.

The Oracle host need not already have `/opt/dtox-research`: create the
root-owned `/opt/dtox-research/ops` directory for this dedicated script.
The service is capped at 64 MiB, 20% of one CPU, 16 tasks and 20 seconds.
Root plus CAP_SYS_RESOURCE is required to lower the score; access to the
Docker socket is privileged even though the script only invokes inspect.
These service limits do **not** change Qdrant's container memory/CPU limits.
The score reduces host OOM risk, not cgroup OOM at Docker's existing 10 GiB
limit, and cannot prevent every kill. After a container replacement there is
up to one timer interval before protection is restored. Only direct children
are eligible; no recursive descendant discovery. The first main-only guard
safely rejected the `bash` wrapper and its timer was left disabled until the
direct-child support passed review and the real systemd verification above.

Read-only verification after a separately authorized installation:
`systemctl status dtox-qdrant-oom-guard.timer dtox-qdrant-oom-guard.service`
and `journalctl -u dtox-qdrant-oom-guard.service --no-pager -n 20`.

## Local operational checks (no deployment/network by default)

`python -m unittest api.tests.test_ops_integrity api.tests.test_pipeline_heartbeat eval.tests.test_web3_evidence`
uses temporary SQLite fixtures and subprocess/HTTP/proc mocks only.
`python eval/check_web3_evidence.py` validates the small golden fixture
offline. Live evidence checks require both `--network` and an explicit
`--api http://HOST:PORT`; REST authentication can use an existing internal
key via `--api-key-env DTOX_API_KEY` (explicit environment variable; never
printed). No key files or signed/write endpoints are used. Offline validation
does not read authentication. The nine-case fixture includes six user-reported
baseline ranks and exact query strings measured by the lead through live MCP
on 2026-10-02, including the Chainlink miss; this runner did not rerun them.
The other three cases are unmeasured. Acceptance ceilings are 3 for 7702/4337
and Chainlink, 4 for nonce, 5 for VDF and 10 for blobs. Top-3 is reported
separately. Chainlink's verified zero-element spec is a known extraction gap,
not an invented minimum-structure requirement. Context truncation is distinct from partial retrieval;
mixed structural/prose sections do not fail merely because prose is present.

Monitor reports new completions and reprocessing as unknown without a
first-completion journal (`journalinserttime` is absent from the current
schema). `updated_at` is only completion-update activity, never new papers.
Pending queue is the sum of discovered, quality_checked, fulltext_fetched
and chunked, excluding deferred/rejected; zero chunked alone is not idle.
Pipeline's reporter atomically replaces `DTOX_DATA_DIR/stage_heartbeats.json`
(default deployed data directory `/opt/dtox-research`) initially after stage
startup and every 300 seconds. The snapshot contains version, PID, process
start ticks from `/proc/self/stat`, write time
and per-stage start/end/error timestamps and busy/idle/waiting/backoff state,
not paper text or credentials. Every iteration updates heartbeat even when
zero papers were processed; a busy call lasting over 45 minutes is unhealthy,
an idle stage with current heartbeat is healthy. Watchdog inspects each stage,
not the newest heartbeat across all threads. Reporter writes are atomic and
do not change queue scheduling, quality logic or embedding concurrency.

Monitor reads only systemd MainPID/ControlGroup and process stat/cgroup to
verify the snapshot PID is either MainPID or its direct child (wrapper case),
with both processes in the **exact** service ControlGroup. The stored process
start ticks must match, and service/process identities are checked twice.
Wrong cgroup or PPID is rejected; expired/reused old PIDs and a service restart
during validation are unknown, not a stuck new process. Legacy snapshots
without start ticks remain unknown until the updated producer writes one;
deploy both monitor and pipeline source for the PID-reuse binding.
The required snapshot age remains <= 15 minutes.
If the snapshot is absent, bounded read-only service-journal inspection may
show activity; ordinary messages do not prove an idle stage's heartbeat.
Search p95 requires at least 20
samples; smaller samples are unknown. `--report` does not load alert secrets,
send notifications or persist state.

Contabo deployment paths remain `/opt/dtox-research/monitor.py` and
`/opt/dtox-research/backup.py` (not `/ops/`); Oracle uses the dedicated new
`/opt/dtox-research/ops/qdrant_oom_guard.py`. Contabo checks only local
`embed-small`, actual embed `/healthz` (`EMBED_HEALTH_URL` override), and the
Qdrant collection at `QDRANT_URL`, not its obsolete local Qdrant container.
Disabled rerank is not probed. MCP health POSTs initialize/initialized/tools-list
to localhost:8011 with an allowed Host, parses JSON/SSE, and invokes no tools.
