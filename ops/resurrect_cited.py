#!/usr/bin/env python3
"""Bring back papers the corpus itself keeps citing.

A paper is judged once, at harvest time, on its own abstract: off_niche when no
layer's term list matches, rejected when it failed the citation gate while it
was young. Meanwhile the finished ('done') papers keep citing it. Being cited
by several papers that already passed every gate is a stronger relevance
signal than either one-shot verdict (LVR 2208.06046 is off_niche, ConFuzzius
2005.12156 and EVMFuzz 1903.08483 are rejected, yet dozens of finished papers
cite them).

Selection: papers in status off_niche/rejected cited by >= --min-citers
distinct done papers (table citations(src, dst)). Their layers are the layers
held by MORE THAN HALF of the citing papers (if no layer clears half, the single
most common one), so a paper cited from web3 work lands in web3.

Quality gate. Default --gate skip: the paper goes to status='quality_checked'
with passed=1, the same shortcut IACR/EIP papers take. Reason: a 'rejected'
paper sent back to 'discovered' would be scored on the same S2 citation counts
and rejected again (recheck_step already retries those every quarter), so the
run would change nothing; and for off_niche papers the gate measures external
citations, which the corpus citers already supply. --gate keep sends them to
'discovered' instead (they then face the ordinary quality gate).

Volume. Without a layer filter the list is dominated by general ML classics
(Attention Is All You Need, BERT, ResNet) cited from the llm-slm layer, tens of
thousands of papers. Use --layers to restrict, and note --limit (default 500,
0 = no cap; most-cited first) bounds one --apply run.

Every paper is its own commit (sqlite timeout 30) so the live pipeline is
never blocked; the UPDATE re-checks the status, so a paper the pipeline moved
in the meantime is left alone. A lock file keeps two runs from overlapping.
Dry run (default) opens state.db read-only and changes nothing.

  resurrect_cited.py                                # dry run, top 30 + totals
  resurrect_cited.py --layers web3 --min-citers 5   # dry run for one layer
  resurrect_cited.py --layers web3 --limit 300 --apply
"""
import argparse
import collections
import contextlib
import os
import sqlite3
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: the unit tests still import this module
    fcntl = None

DATA_DIR = os.getenv("DTOX_DATA_DIR", "/opt/dtox-research")
STATUSES = ("off_niche", "rejected")
ALL_LAYERS = ("web3", "ai-agents", "llm-slm", "builder-tech")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@contextlib.contextmanager
def single_instance(path):
    """Non-blocking exclusive lock; yields False when another run holds it."""
    if fcntl is None or not hasattr(fcntl, "flock"):
        yield True
        return
    fh = open(path, "a")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()


def majority_layers(layer_counts, total):
    """Layers held by more than half of the `total` citing papers; when none
    clears half, the single most common layer (ties broken by name)."""
    if not layer_counts or total <= 0:
        return []
    winners = sorted(l for l, c in layer_counts.items() if c * 2 > total)
    if winners:
        return winners
    best = max(layer_counts.values())
    return [sorted(l for l, c in layer_counts.items() if c == best)[0]]


def find_candidates(conn, min_citers=3, statuses=STATUSES, layers=None):
    """List of dicts (id, title, year, status, citers, layers, layer_counts),
    most cited first. `layers`: keep only papers whose majority layers include
    one of them."""
    marks = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT c.dst AS dst, s.layers AS layers, COUNT(*) AS n "
        f"FROM citations c "
        f"JOIN papers s ON s.arxiv_id = c.src AND s.status = 'done' "
        f"JOIN papers d ON d.arxiv_id = c.dst AND d.status IN ({marks}) "
        f"GROUP BY c.dst, s.layers", tuple(statuses)).fetchall()
    total = collections.Counter()
    per_layer = collections.defaultdict(collections.Counter)
    for r in rows:
        total[r[0]] += r[2]
        for layer in (r[1] or "").split(","):
            if layer:
                per_layer[r[0]][layer] += r[2]
    wanted = set(layers) if layers else None
    out = []
    for dst, n in total.items():
        if n < min_citers:
            continue
        chosen = majority_layers(per_layer[dst], n)
        if not chosen or (wanted and not wanted & set(chosen)):
            continue
        out.append({"id": dst, "citers": n, "layers": chosen, "layer_counts": dict(per_layer[dst])})
    out.sort(key=lambda c: (-c["citers"], c["id"]))
    if out:
        meta = {}
        ids = [c["id"] for c in out]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" for _ in chunk)
            for r in conn.execute(f"SELECT arxiv_id, title, year, status FROM papers WHERE arxiv_id IN ({q})", chunk):
                meta[r[0]] = r
        for c in out:
            r = meta.get(c["id"])
            c["title"], c["year"], c["status"] = (r[1], r[2], r[3]) if r else (None, None, None)
    return out


def resurrect(conn, candidates, gate="skip", limit=0, log=print):
    """Move candidates back into the pipeline, one commit per paper. Returns
    the number actually changed. gate: 'skip' -> quality_checked/passed=1,
    'keep' -> discovered."""
    status, passed = ("quality_checked", 1) if gate == "skip" else ("discovered", None)
    changed = 0
    for c in candidates[:limit] if limit else candidates:
        cur = conn.execute(
            "UPDATE papers SET status=?, layers=?, passed=COALESCE(?, passed), updated_at=? "
            "WHERE arxiv_id=? AND status IN ('off_niche','rejected')",
            (status, ",".join(c["layers"]), passed, now_iso(), c["id"]))
        conn.commit()  # per paper: an open transaction stalls the live pipeline
        changed += cur.rowcount
        if changed and changed % 100 == 0:
            log(f"{changed} resurrected")
    return changed


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-citers", type=int, default=3)
    ap.add_argument("--layers", default="", help="comma list; keep papers whose majority layers include one")
    ap.add_argument("--gate", choices=("skip", "keep"), default="skip")
    ap.add_argument("--limit", type=int, default=500, help="max papers changed per --apply (0 = no cap)")
    ap.add_argument("--top", type=int, default=30, help="rows to print")
    ap.add_argument("--db", default=os.path.join(DATA_DIR, "state.db"))
    ap.add_argument("--lock", default=os.path.join(DATA_DIR, "resurrect_cited.lock"))
    return ap.parse_args(argv)


def report(cands, top, verb):
    by_layer = collections.Counter(",".join(c["layers"]) for c in cands)
    by_status = collections.Counter(c["status"] for c in cands)
    print(f"{verb}: {len(cands)} papers; by status {dict(by_status)}", flush=True)
    for k, v in by_layer.most_common(8):
        print(f"  {v:6d} {k}", flush=True)
    for c in cands[:top]:
        print(f"{c['id']} | {(c['title'] or '')[:70]} | {c['status']} | {c['citers']} | {c['layer_counts']}",
              flush=True)


def main(argv=None):
    args = parse_args(argv)
    layers = [x.strip() for x in args.layers.split(",") if x.strip()]
    bad = [x for x in layers if x not in ALL_LAYERS]
    if bad:
        print(f"unknown layers: {bad}", file=sys.stderr)
        return 2
    with single_instance(args.lock) as got:
        if not got:
            print("another run holds the lock, exiting", flush=True)
            return 0
        if args.apply:
            conn = sqlite3.connect(args.db, timeout=30)
        else:  # dry run never opens state.db for writing
            conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
        try:
            cands = find_candidates(conn, args.min_citers, layers=layers or None)
            report(cands, args.top, "would resurrect" if not args.apply else "candidates")
            if args.apply:
                n = resurrect(conn, cands, args.gate, args.limit)
                print(f"resurrected {n} (gate={args.gate}, limit={args.limit or 'none'})", flush=True)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
