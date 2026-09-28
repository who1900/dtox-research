#!/usr/bin/env python3
"""Group copies of the same paper that came in through different sources.

A paper published at ACL is usually on arXiv too, and PMLR, OpenAlex and IACR
overlap with arXiv the same way. Each copy is its own row and its own set of
chunks, so a search could spend three of its ten slots on one paper, and a
benchmark target on arXiv counted as a miss when the ACL copy came up. 17,908
extra copies were found in a corpus of 165k.

Grouping is by normalised title (longer than 25 characters, so "Introduction"
and "Proceedings" never merge) with publication years at most one apart. Two
arXiv ids with the same title are left apart: those are genuinely different
records far more often than duplicates. The arXiv copy is the canonical one,
since its LaTeX full text is the richest; then ACL, PMLR, IACR, HAL, OpenAlex.

Writes a JSON file the API reads; the state database is only read, so this
never takes the pipeline's write lock.

  build_twins.py --db /opt/dtox-research/state.db --out /opt/dtox-research/twins.json
"""
import argparse
import collections
import json
import os
import re
import sqlite3
import tempfile

MIN_TITLE_CHARS = 25
PREFERENCE = ("arxiv", "acl", "pmlr", "iacr", "hal", "oa")


def source(arxiv_id):
    return arxiv_id.split(":", 1)[0] if ":" in arxiv_id else "arxiv"


def rank(arxiv_id):
    src = source(arxiv_id)
    return PREFERENCE.index(src) if src in PREFERENCE else len(PREFERENCE)


def norm(title):
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def build(rows):
    """rows: (arxiv_id, title, year). Returns {member: canonical} for every
    member of a group of two or more, the canonical mapping to itself."""
    by_title = collections.defaultdict(list)
    for arxiv_id, title, year in rows:
        key = norm(title)
        if len(key) > MIN_TITLE_CHARS:
            by_title[key].append((arxiv_id, year))
    canonical = {}
    for members in by_title.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (rank(m[0]), m[0]))
        head, head_year = members[0]
        group = [head]
        arxiv_seen = source(head) == "arxiv"
        for arxiv_id, year in members[1:]:
            if head_year and year and abs(head_year - year) > 1:
                continue
            if source(arxiv_id) == "arxiv" and arxiv_seen:
                continue
            group.append(arxiv_id)
        if len(group) > 1:
            for arxiv_id in group:
                canonical[arxiv_id] = head
    return canonical


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/dtox-research/state.db")
    ap.add_argument("--out", default="/opt/dtox-research/twins.json")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute("SELECT arxiv_id, title, year FROM papers WHERE status='done'").fetchall()
    canonical = build(rows)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(args.out) or ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"canonical": canonical}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, args.out)
    groups = len(set(canonical.values()))
    print(f"{len(rows)} papers, {groups} groups, {len(canonical) - groups} extra copies")


if __name__ == "__main__":
    main()
