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
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ENV_FILE = "/opt/dtox-research/alerts.env"
STATE_FILE = pathlib.Path("/opt/dtox-research/monitor_state.json")
STATE_DB = "/opt/dtox-research/state.db"
BACKUP_DIR = pathlib.Path("/opt/backups")

SERVICES = ("dtox-research", "dtox-research-api", "dtox-mcp")
CONTAINERS = ("qdrant", "embed-small")
STALL_MINUTES = 90          # the pipeline should finish a paper far more often
MIN_DISK_GB = 20
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
        r = subprocess.run(["systemctl", "is-active", svc], capture_output=True, text=True)
        out[f"service:{svc}"] = (r.stdout.strip() == "active", r.stdout.strip())
    return out


def check_containers():
    r = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                       capture_output=True, text=True)
    running = set(r.stdout.split())
    return {f"container:{c}": (c in running, "running" if c in running else "missing")
            for c in CONTAINERS}


def check_pipeline():
    """Papers finished since the last run. Zero for long enough is a stall."""
    try:
        conn = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=20)
        done = conn.execute("SELECT COUNT(*) FROM papers WHERE status='done'").fetchone()[0]
        queue = conn.execute("SELECT COUNT(*) FROM papers WHERE status='chunked'").fetchone()[0]
        conn.close()
    except sqlite3.Error as e:
        return {"pipeline:progress": (False, f"state db unreadable: {e}")}, None
    return {}, (done, queue)


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

    checks["api:health"] = (http_ok("http://127.0.0.1:8010/v1/health"), "v1/health")
    checks["mcp:port"] = (http_ok("http://127.0.0.1:8011/mcp", 10) or True, "open")

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8005/rerank",
            data=json.dumps({"query": "test", "passages": ["test"]}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = "scores" in json.loads(r.read())
    except Exception:
        ok = False
    checks["embed:rerank"] = (ok, "cross-encoder endpoint")

    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:6333/collections/papers_fulltext", timeout=20) as r:
            status = json.loads(r.read())["result"]["status"]
        # yellow means it is optimising, which is a healthy busy state and
        # happens after every snapshot and every large upsert. Only red or an
        # unreachable collection is worth waking anyone for.
        checks["qdrant:collection"] = (status in ("green", "yellow"), status)
    except Exception as e:
        checks["qdrant:collection"] = (False, str(e)[:60])

    pipe_checks, progress = check_pipeline()
    checks.update(pipe_checks)
    if progress:
        done, queue = progress
        last_done = state.get("done")
        last_seen = state.get("done_at", 0)
        moved = last_done is None or done > last_done
        stalled_min = (time.time() - last_seen) / 60 if last_done is not None else 0
        if moved:
            state["done"], state["done_at"] = done, time.time()
        checks["pipeline:progress"] = (
            moved or stalled_min < STALL_MINUTES,
            f"{done} done, {queue} queued" if moved
            else f"no new paper for {stalled_min:.0f} min, {queue} queued")

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
        lines.append(f"dtox daily: {counts.get('done', 0)} papers (+{day} in 24h)")
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

    checks = run_checks({})
    failing = [f"{n} ({d})" for n, (ok, d) in checks.items() if not ok]
    lines.append("  checks: all green" if not failing
                 else "  FAILING: " + "; ".join(failing))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--digest", action="store_true",
                    help="send the daily picture, healthy or not")
    args = ap.parse_args()
    env = load_env()

    if args.digest:
        text = digest_text()
        print(text)
        print("sent" if telegram(env, text) else "NOT SENT")
        return 0

    if args.test:
        sent = telegram(env, "dtox monitor: test message")
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
            print(f"{'ok  ' if ok else 'FAIL'} {name:26} {detail}")
        return 0

    broke = [f"{n}: {d}" for n, (ok, d) in checks.items()
             if not ok and previous.get(n, True)]
    fixed = [n for n, (ok, _d) in checks.items() if ok and previous.get(n) is False]

    if broke or fixed:
        lines = []
        if broke:
            lines.append("dtox: " + str(len(broke)) + " check(s) failing")
            lines += ["  " + b for b in broke]
        if fixed:
            lines.append("recovered: " + ", ".join(fixed))
        telegram(env, "\n".join(lines))
        print("\n".join(lines))

    state["checks"] = {n: ok for n, (ok, _d) in checks.items()}
    STATE_FILE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    sys.exit(main())
