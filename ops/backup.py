#!/usr/bin/env python3
"""Backups for the research stack, with the copy verified before it counts.

Not all four stores are equal and the schedule says so:

  judgments.db  IRREPLACEABLE. Every claim node, verdict and link a reader ever
                filed. 224 KB that nothing can rebuild. Backed up every run and
                kept for a long time.
  state.db      Expensive: the harvest cursors, the quality decisions and the
                citation graph. Rebuildable in principle, days of API calls in
                practice.
  fts.db        Cheap: rebuilt from Qdrant in about eight minutes. Weekly.
  qdrant        Expensive but bulky: snapshot through its own API, weekly.

A backup nobody has opened is a guess, so each SQLite copy is opened, checked
and counted before the old ones are pruned.

  python3 backup.py            daily set
  python3 backup.py --weekly   daily set plus fts and a qdrant snapshot
  python3 backup.py --verify   check the newest set and exit
"""
import argparse
import datetime
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import urllib.request

DEST = pathlib.Path("/opt/backups")
QDRANT = "http://localhost:6333"
COLLECTION = "papers_fulltext"

STORES = {
    "judgments": {"path": "/opt/dtox-research-api/judgments.db", "keep": 30,
                  "tables": ["claim_nodes", "claim_judgments", "claim_links"]},
    "state": {"path": "/opt/dtox-research/state.db", "keep": 7,
              "tables": ["papers", "citations", "harvest_cursor"]},
    "fts": {"path": "/opt/dtox-research/fts.db", "keep": 2, "weekly": True,
            "tables": ["chunks"]},
}


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def copy_sqlite(src, dst):
    """Online backup through SQLite's own API: safe against a live writer,
    unlike cp, which can catch a database mid-transaction."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    target = sqlite3.connect(str(dst))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def verify(dst, tables):
    conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True, timeout=60)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            return False, {"integrity": ok}
        counts = {}
        for t in tables:
            try:
                counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                counts[t] = None
        return True, counts
    finally:
        conn.close()


def prune(prefix, keep):
    files = sorted(DEST.glob(f"{prefix}-*.db"), reverse=True)
    for old in files[keep:]:
        old.unlink()
    return len(files[keep:])


def qdrant_snapshot():
    req = urllib.request.Request(f"{QDRANT}/collections/{COLLECTION}/snapshots",
                                 method="POST")
    with urllib.request.urlopen(req, timeout=1800) as r:
        result = json.loads(r.read())["result"]
    # keep the two newest and drop the rest: they are 4 GB each
    with urllib.request.urlopen(f"{QDRANT}/collections/{COLLECTION}/snapshots",
                                timeout=120) as r:
        snaps = sorted(json.loads(r.read())["result"],
                       key=lambda s: s["creation_time"], reverse=True)
    for old in snaps[2:]:
        req = urllib.request.Request(
            f"{QDRANT}/collections/{COLLECTION}/snapshots/{old['name']}",
            method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=300)
        except Exception:
            pass
    return result["name"], result.get("size", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    DEST.mkdir(parents=True, exist_ok=True)

    if args.verify:
        bad = 0
        for name, cfg in STORES.items():
            newest = sorted(DEST.glob(f"{name}-*.db"), reverse=True)
            if not newest:
                # a weekly store with no copy yet is not a failure, it is a
                # schedule that has not come round; a daily one missing is
                weekly = cfg.get("weekly")
                print(f"{name:10} {'not due yet' if weekly else 'NO BACKUP'}")
                bad += 0 if weekly else 1
                continue
            ok, info = verify(newest[0], cfg["tables"])
            print(f"{name:10} {'ok ' if ok else 'BAD'} {newest[0].name} {info}")
            bad += 0 if ok else 1
        return 1 if bad else 0

    failures = 0
    for name, cfg in STORES.items():
        if cfg.get("weekly") and not args.weekly:
            continue
        src = pathlib.Path(cfg["path"])
        if not src.exists():
            print(f"{name}: source missing at {src}")
            failures += 1
            continue
        dst = DEST / f"{name}-{stamp()}.db"
        copy_sqlite(src, dst)
        ok, info = verify(dst, cfg["tables"])
        if not ok:
            print(f"{name}: VERIFY FAILED {info}, keeping the file for inspection")
            failures += 1
            continue
        removed = prune(name, cfg["keep"])
        print(f"{name:10} {dst.stat().st_size // 1024:>8} KB  {info}  pruned {removed}")

    if args.weekly:
        try:
            snap, size = qdrant_snapshot()
            print(f"qdrant     {size // 1024 // 1024:>8} MB  snapshot {snap}")
        except Exception as e:
            print(f"qdrant: snapshot failed: {e}")
            failures += 1

    free = shutil.disk_usage("/").free // 1024 // 1024 // 1024
    print(f"disk free: {free} GB")
    if free < 20:
        print("WARNING: less than 20 GB free")
        failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
