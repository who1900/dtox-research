#!/usr/bin/env python3
"""Watch the research stack and say something only when the answer changes.

Every problem this stack had for a week was found because a human asked. The
point of this is to turn that round. It alerts on transitions, not on states:
a check that has been failing for six hours does not need to be repeated every
fifteen minutes, and a check that recovers is news too.

Secrets come from alerts.env (chmod 600) and are never printed or logged.

  python3 monitor.py            run the checks, alert on changes
  python3 monitor.py --report   print the current picture and exit
  python3 monitor.py --test     send a test message
"""
import argparse
import contextlib
import json
import math
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ENV_FILE = "/opt/dtox-research/alerts.env"
DATA_DIR = pathlib.Path(os.getenv("DTOX_DATA_DIR", "/opt/dtox-research"))
STATE_FILE = DATA_DIR / "monitor_state.json"
STATE_DB = str(DATA_DIR / "state.db")
HEARTBEAT_FILE = DATA_DIR / "stage_heartbeats.json"
BACKUP_DIR = pathlib.Path("/opt/backups")

SERVICES = ("dtox-research", "dtox-research-api", "dtox-mcp")
CONTAINERS = ("embed-small",)
STAGES = ("harvest", "graph_harvest", "acl_harvest", "openalex", "hal_harvest",
          "github_docs", "pmlr", "quality", "fulltext", "chunk", "embed", "recheck",
          "refs", "iacr_twin", "reporter")
STAGE_STUCK_SECONDS = 45 * 60
HEARTBEAT_MAX_AGE_SECONDS = 15 * 60
LATENCY_SAMPLE_FLOOR = 20
SEARCH_P95_MAX_SECONDS = 5
STALL_MINUTES = 90
REMIND_HOURS = 6            # keep repeating while a check stays red          # the pipeline should finish a paper far more often
# 20 GB is 5% of this disk and left no room to react: by the time the alert
# fired the volume was already full, Qdrant had gone red refusing to optimise,
# and the pipeline had stopped. The base itself grows by gigabytes a week
# (fts.db 4.6 GB, state.db 1.2 GB, Qdrant 9.8 GB, latex_cache 4.6 GB) and a
# weekly backup adds a copy on top, so the warning has to come while there is
# still headroom to clear.
MIN_DISK_GB = 40
BACKUP_MAX_AGE_H = 36


def load_env():
    env = {}
    try:
        for line in pathlib.Path(ENV_FILE).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def telegram(env, text):
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                               data=data, timeout=20).read()
        return True
    except Exception:
        return False


def http_ok(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def check_services():
    out = {}
    for svc in SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=5)
            out[f"service:{svc}"] = (r.returncode == 0 and r.stdout.strip() == "active", r.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            out[f"service:{svc}"] = (False, "systemctl unavailable")
    return out


def check_containers():
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode:
            raise OSError("docker ps failed")
    except (OSError, subprocess.TimeoutExpired):
        return {f"container:{c}": (None, "unknown: local Docker unavailable") for c in CONTAINERS}
    running = set(r.stdout.split())
    return {f"container:{c}": (c in running, "running" if c in running else "missing")
            for c in CONTAINERS}


def check_api():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8010/v1/health", timeout=10) as response:
            data = json.loads(response.read(65536))
        if data.get("status") != "ok":
            raise ValueError("unhealthy")
    except (OSError, ValueError, TypeError, AttributeError):
        return {"api:health": (False, "v1/health unavailable/invalid"),
                "api:search_p95": (None, "unknown: health unavailable")}
    latency = data.get("search_latency_seconds") or {}
    samples = latency.get("samples") if isinstance(latency, dict) else None
    p95 = latency.get("p95") if isinstance(latency, dict) else None
    if type(samples) is not int or samples < LATENCY_SAMPLE_FLOOR:
        result = (None, f"unknown: samples={samples}, need >= {LATENCY_SAMPLE_FLOOR}")
    elif type(p95) not in (int, float) or not math.isfinite(p95) or p95 < 0:
        result = (None, "unknown: invalid/missing p95")
    else:
        result = (p95 <= SEARCH_P95_MAX_SECONDS, f"p95={p95:.3f}s, samples={samples}")
    return {"api:health": (True, "v1/health"), "api:search_p95": result}


def mcp_response(response, expected_id):
    raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError("MCP response too large")
    text = raw.decode("utf-8")
    if response.headers.get("Content-Type", "").startswith("text/event-stream"):
        candidates = []
        for event in text.replace("\r\n", "\n").split("\n\n"):
            payload = "\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:"))
            if payload:
                candidates.append(json.loads(payload))
        data = next((d for d in candidates if isinstance(d, dict) and d.get("id") == expected_id), {})
    else:
        data = json.loads(text)
    if (not isinstance(data, dict) or data.get("jsonrpc") != "2.0"
            or data.get("id") != expected_id or "error" in data
            or not isinstance(data.get("result"), dict)):
        raise ValueError("invalid MCP response")
    return data["result"]


def check_mcp():
    url = "http://localhost:8011/mcp"
    headers = {"Host": "localhost:8011", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    def post(body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        return urllib.request.urlopen(req, timeout=10)

    try:
        with post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                              "clientInfo": {"name": "dtox-monitor", "version": "1"}}}) as response:
            initialized = mcp_response(response, 1)
            session = response.headers.get("Mcp-Session-Id")
        version = initialized.get("protocolVersion")
        if not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
            raise ValueError("missing protocol version")
        headers["MCP-Protocol-Version"] = version
        if session:
            headers["Mcp-Session-Id"] = session
        with post({"jsonrpc": "2.0", "method": "notifications/initialized"}) as response:
            if response.status not in (200, 202, 204):
                raise ValueError("initialization rejected")
        with post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) as response:
            tools = mcp_response(response, 2).get("tools")
        if (not isinstance(tools, list) or not tools
                or not all(isinstance(tool, dict) and isinstance(tool.get("name"), str)
                           and tool["name"] for tool in tools)):
            raise ValueError("invalid tool list")
        return {"mcp:port": (True, f"initialize/tools/list ok, {len(tools)} tools; no tools called")}
    except (OSError, ValueError, TypeError, AttributeError, StopIteration):
        return {"mcp:port": (False, "MCP initialize/tools/list unavailable/invalid")}


def check_embed():
    url = os.getenv("EMBED_HEALTH_URL", "http://127.0.0.1:8005/healthz")
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read(65536))
        return {"embed:health": (data.get("status") == "ok", "embed /healthz")}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"embed:health": (False, "embed health unavailable/invalid")}


def journal_stage_activity():
    checks = {f"pipeline:stage:{stage}": (None, "unknown: no persistent stage heartbeat") for stage in STAGES}
    try:
        result = subprocess.run(["journalctl", "--unit=dtox-research.service", "--since=-90min",
                                 "--lines=2000", "--output=json", "--no-pager"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            return checks
        for line in result.stdout.splitlines():
            try:
                entry = json.loads(line)
                message = entry.get("MESSAGE", "")
                match = re.search(r"\bstage ([a-z_]+): (heartbeat|\d+)\b", message)
                stage = match.group(1) if match else "reporter" if "counts=" in message else None
                if stage not in STAGES:
                    continue
                age = time.time() - int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000
                if age < 0:
                    continue
                if match and match.group(2) == "heartbeat":
                    checks[f"pipeline:stage:{stage}"] = (age <= STAGE_STUCK_SECONDS,
                                                         f"journal heartbeat {age / 60:.0f} min old")
                elif checks[f"pipeline:stage:{stage}"][0] is None:
                    checks[f"pipeline:stage:{stage}"] = (None, f"heartbeat unknown; journal activity {age / 60:.0f} min old")
            except (ValueError, TypeError, KeyError):
                continue
    except (OSError, subprocess.TimeoutExpired):
        pass
    return checks


def service_process_identity():
    result = subprocess.run(
        ["systemctl", "show", "dtox-research.service", "--property=MainPID", "--property=ControlGroup"],
        capture_output=True, text=True, timeout=5)
    if result.returncode:
        raise OSError("systemd identity unavailable")
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if set(properties) != {"MainPID", "ControlGroup"}:
        raise OSError("systemd identity incomplete")
    main_pid = int(properties["MainPID"])
    group = properties["ControlGroup"]
    if main_pid <= 0 or not group.startswith("/") or group == "/":
        raise OSError("live service identity unavailable")
    return main_pid, group


def proc_process_identity(pid):
    def read(name):
        with (pathlib.Path("/proc") / str(pid) / name).open(encoding="ascii") as stream:
            value = stream.read(4097)
        if len(value) > 4096:
            raise ValueError("oversized process identity")
        return value

    head, stat = read("stat").rsplit(")", 1)
    fields = stat.split()
    if int(head.split(" (", 1)[0]) != pid:
        raise ValueError("process PID mismatch")
    if fields[0] in ("Z", "X", "x"):
        raise FileNotFoundError("process is no longer live")
    ppid, ticks = int(fields[1]), int(fields[19])
    if ppid < 0 or ticks <= 0:
        raise ValueError("invalid process identity")
    groups = []
    for line in read("cgroup").splitlines():
        hierarchy, controllers, path = line.split(":", 2)
        if not hierarchy.isdigit() or not path.startswith("/"):
            raise ValueError("invalid process cgroup")
        if hierarchy == "0" and controllers == "" or "name=systemd" in controllers.split(","):
            groups.append(path)
    return ppid, ticks, tuple(groups)


def validate_snapshot_process(snapshot):
    try:
        main_pid, group = service_process_identity()
        target = proc_process_identity(snapshot["pid"])
        parent = target if snapshot["pid"] == main_pid else proc_process_identity(main_pid)
        if target[2] != (group,) or parent[2] != (group,):
            return False, "snapshot process is outside exact service ControlGroup"
        if snapshot["pid"] != main_pid and target[0] != main_pid:
            return False, "snapshot PID is neither service MainPID nor its direct child"
        started = snapshot.get("process_start_ticks")
        if started is None:
            return None, "unknown: legacy snapshot lacks process_start_ticks; awaiting updated producer"
        if type(started) is not int or started <= 0:
            return False, "invalid snapshot process_start_ticks"
        if started != target[1]:
            return None, "unknown: old snapshot PID/starttime; waiting for live service snapshot"
        if service_process_identity() != (main_pid, group):
            return None, "unknown: service restarted during identity check"
        if (proc_process_identity(snapshot["pid"]) != target
                or snapshot["pid"] != main_pid and proc_process_identity(main_pid) != parent):
            return None, "unknown: process identity changed during check"
        return True, "snapshot process verified in service ControlGroup"
    except FileNotFoundError:
        return None, "unknown: old snapshot PID expired; waiting for live service snapshot"
    except (OSError, subprocess.TimeoutExpired):
        return None, "unknown: cannot verify live service/proc identity"
    except (ValueError, TypeError, IndexError):
        return False, "invalid live service/proc identity"


def check_stage_heartbeats():
    try:
        with HEARTBEAT_FILE.open(encoding="utf-8") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized snapshot")
        snapshot = json.loads(raw)
    except FileNotFoundError:
        return journal_stage_activity()
    except (OSError, ValueError):
        return {"pipeline:heartbeat_file": (False, "heartbeat snapshot unreadable/invalid")}
    now = time.time()
    try:
        written = snapshot["written_at"]
        if (snapshot["version"] != 1 or type(snapshot["pid"]) is not int
                or type(written) not in (int, float) or not math.isfinite(written)
                or written > now + 60 or not isinstance(snapshot["stages"], dict)):
            raise ValueError("invalid schema")
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"pipeline:heartbeat_file": (False, "heartbeat schema invalid")}
    verified, detail = validate_snapshot_process(snapshot)
    if verified is not True:
        return {"pipeline:heartbeat_file": (verified, detail)}
    age = now - written
    checks = {"pipeline:heartbeat_file": (age <= HEARTBEAT_MAX_AGE_SECONDS, f"snapshot {age / 60:.0f} min old")}
    for stage in STAGES:
        record = snapshot["stages"].get(stage)
        key = f"pipeline:stage:{stage}"
        if not isinstance(record, dict):
            checks[key] = (None, "unknown: stage absent from snapshot")
            continue
        phase = record.get("phase")
        timestamp = record.get("started_at") if phase == "busy" else record.get("heartbeat_at")
        if (phase not in ("busy", "idle", "waiting", "backoff", "starting")
                or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                or timestamp > now + 60):
            checks[key] = (False, "invalid stage heartbeat")
            continue
        elapsed = now - timestamp
        if elapsed > STAGE_STUCK_SECONDS:
            checks[key] = (False, f"{'stuck call' if phase == 'busy' else 'stale heartbeat'} {elapsed / 60:.0f} min")
        elif age > HEARTBEAT_MAX_AGE_SECONDS:
            checks[key] = (None, f"unknown: stale snapshot; last phase={phase}")
        else:
            checks[key] = (phase != "backoff", f"{phase}, {elapsed / 60:.0f} min; idle is not a stall")
    return checks


def check_pipeline():
    """Completion-update activity is not evidence of first completion."""
    try:
        with contextlib.closing(sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=20)) as conn:
            done, latest = conn.execute("SELECT COUNT(*), MAX(updated_at) FROM papers WHERE status='done'").fetchone()
            queue = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE status IN "
                "('discovered', 'quality_checked', 'fulltext_fetched', 'chunked')").fetchone()[0]
    except sqlite3.Error as e:
        return {"pipeline:progress": (False, f"state db unreadable: {e}")}, None
    return {"pipeline:new_completed": (None, "unknown: no first-completion journalinserttime"),
            "pipeline:reprocessing": (None, "unknown: updated_at cannot separate first completion/reprocessing")}, (done, queue, latest)


def check_backups():
    newest = sorted(BACKUP_DIR.glob("judgments-*.db"), reverse=True)
    if not newest:
        return {"backup:judgments": (False, "no backup of the registry")}
    age_h = (time.time() - newest[0].stat().st_mtime) / 3600
    return {"backup:judgments": (age_h < BACKUP_MAX_AGE_H, f"{age_h:.0f}h old")}


def run_checks(state):
    checks = {}
    checks.update(check_services())
    checks.update(check_containers())

    checks.update(check_api())
    checks.update(check_mcp())
    checks.update(check_embed())
    checks.update(check_stage_heartbeats())

    try:
        with urllib.request.urlopen(
                os.getenv("QDRANT_URL", "http://127.0.0.1:6333") + "/collections/papers_fulltext", timeout=20) as r:
            status = json.loads(r.read())["result"]["status"]
        # yellow means it is optimising, which is a healthy busy state and
        # happens after every snapshot and every large upsert. Only red or an
        # unreachable collection is worth waking anyone for.
        checks["qdrant:collection"] = (status in ("green", "yellow"), status)
    except Exception:
        checks["qdrant:collection"] = (False, "configured Qdrant collection unavailable")

    pipe_checks, progress = check_pipeline()
    checks.update(pipe_checks)
    if progress:
        done, queue, latest = progress
        last_done = state.get("done")
        baseline_known = last_done is not None and "completion_update" in state
        previous_update = state.get("completion_update")
        last_seen = state.get("done_at", time.time())
        moved = baseline_known and (done > last_done or (latest and previous_update and latest > previous_update))
        stalled_min = (time.time() - last_seen) / 60 if last_done is not None else 0
        if moved or not baseline_known:
            state["done_at"] = time.time()
        state["done"], state["completion_update"] = done, latest
        checks["pipeline:progress"] = (
            True if queue == 0 else None if not baseline_known else bool(moved or stalled_min < STALL_MINUTES),
            f"{done} done, {queue} queued; completion-update activity (not new papers)" if moved
            else f"{done} done, {queue} queued; {'idle queue' if queue == 0 else 'baseline unknown' if not baseline_known else f'no completion-update activity for {stalled_min:.0f} min'}")

    checks.update(check_backups())

    free_gb = shutil.disk_usage("/").free // 1024 ** 3
    checks["disk:free"] = (free_gb >= MIN_DISK_GB, f"{free_gb} GB free")
    return checks


def digest_text():
    """The daily heartbeat.

    Alerting only on transitions is right for failures and wrong for trust: a
    silent monitor and a dead one look identical. Once a day it says what it
    sees, so silence in between means something."""
    lines = []
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=30)
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM papers GROUP BY status").fetchall())
        day = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE status='done' AND updated_at > ?",
            [(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400)))]
        ).fetchone()[0]
        layers = {l: conn.execute(
            "SELECT COUNT(*) FROM papers WHERE status='done' AND layers LIKE ?",
            (f"%{l}%",)).fetchone()[0] for l in ("llm-slm", "ai-agents", "web3")}
        edges = conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0]
        conn.close()
        lines.append(f"dtox daily: {counts.get('done', 0)} papers; {day} done rows updated in 24h")
        lines.append("  new_completed=unknown; reprocessing=unknown (no first-completion journalinserttime)")
        lines.append(f"  layers: llm {layers['llm-slm']}, agents {layers['ai-agents']}, "
                     f"web3 {layers['web3']}")
        lines.append(f"  queue {counts.get('chunked', 0)}, deferred "
                     f"{counts.get('deferred', 0)}, graph {edges} edges")
    except sqlite3.Error as e:
        lines.append(f"dtox daily: state db unreadable: {e}")

    try:
        conn = sqlite3.connect("file:/opt/dtox-research-api/judgments.db?mode=ro",
                               uri=True, timeout=20)
        nodes = conn.execute("SELECT COUNT(*) FROM claim_nodes").fetchone()[0]
        judged = conn.execute("SELECT COUNT(*) FROM claim_judgments").fetchone()[0]
        conn.close()
        lines.append(f"  registry: {nodes} claims, {judged} verdicts")
    except sqlite3.Error:
        pass

    # run_checks({}) looked harmless and was the whole bug: with no saved state
    # the progress check has nothing to compare against, counts that as movement
    # and reports green. The digest was announcing a healthy pipeline while it
    # had been stopped for a day.
    try:
        saved = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    checks = run_checks(saved)
    failing = [f"{n} ({d})" for n, (ok, d) in checks.items() if ok is False]
    unknown = [n for n, (ok, _d) in checks.items() if ok is None]
    lines.append("  FAILING: " + "; ".join(failing) if failing else "  checks: no known failures")
    if unknown:
        lines.append("  UNKNOWN: " + ", ".join(unknown))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--report", action="store_true")
    mode.add_argument("--test", action="store_true")
    mode.add_argument("--digest", action="store_true",
                    help="send the daily picture, healthy or not")
    args = ap.parse_args()

    if args.digest:
        text = digest_text()
        print(text)
        print("sent" if telegram(load_env(), text) else "NOT SENT")
        return 0

    if args.test:
        sent = telegram(load_env(), "dtox monitor: test message")
        print("sent" if sent else "not sent: TELEGRAM_CHAT_ID missing or delivery failed")
        return 0 if sent else 1

    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}

    checks = run_checks(state)
    previous = state.get("checks", {})

    if args.report:
        for name, (ok, detail) in sorted(checks.items()):
            print(f"{'UNKNOWN' if ok is None else 'ok' if ok else 'FAIL':7} {name:26} {detail}")
        return 0

    env = load_env()

    # Alerting only on the transition made a lasting failure indistinguishable
    # from health: the pipeline died, one message went out, and then silence for
    # 29 hours while the daily digest reported all green. Anything still broken
    # is repeated every REMIND_HOURS.
    reminded_at = state.get("reminded_at", {})
    now = time.time()
    broke, still = [], []
    for n, (ok, d) in checks.items():
        if ok is None:
            continue
        if ok:
            reminded_at.pop(n, None)
            continue
        if previous.get(n, True):
            broke.append(f"{n}: {d}")
            reminded_at[n] = now
        elif now - reminded_at.get(n, 0) >= REMIND_HOURS * 3600:
            still.append(f"{n}: {d} (unresolved for "
                         f"{(now - reminded_at.get(n, now)) / 3600 + REMIND_HOURS:.0f}h+)")
            reminded_at[n] = now
    state["reminded_at"] = reminded_at
    fixed = [n for n, (ok, _d) in checks.items() if ok is True and previous.get(n) is False]

    if broke or still or fixed:
        lines = []
        if broke:
            lines.append("dtox: " + str(len(broke)) + " check(s) failing")
            lines += ["  " + b for b in broke]
        if still:
            lines.append("STILL FAILING:")
            lines += ["  " + b for b in still]
        if fixed:
            lines.append("recovered: " + ", ".join(fixed))
        telegram(env, "\n".join(lines))
        print("\n".join(lines))

    state["checks"] = {n: ok for n, (ok, _d) in checks.items()}
    STATE_FILE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
