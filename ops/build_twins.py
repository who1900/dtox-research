#!/usr/bin/env python3
"""Group copies of the same paper that came in through different sources.

A paper published at ACL is usually on arXiv too, and PMLR, OpenAlex and IACR
overlap with arXiv the same way. Each copy is its own row and its own set of
chunks, so a search could spend three of its ten slots on one paper, and a
benchmark target on arXiv counted as a miss when the ACL copy came up. 17,908
extra copies were found in a corpus of 165k.

Rules (strongest first): exact title, arXiv id in source_url, identical
abstract opening, fuzzy title (token Jaccard, part/version markers must match).
Groups never hold two arXiv ids, span more than a year, or exceed MAX_GROUP.
Exact title grouping is by normalised title (longer than 25 characters, so "Introduction"
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
import html
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
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(title or "").lower()).strip()


STOP = frozenset("a an the of in on for and or to with via from by at as is are its their our using towards toward".split())
ROMAN = frozenset("i ii iii iv v vi vii viii ix x".split())
FUZZY_JACCARD = 0.8
WP_OVERLAP = 0.9
MIN_WP_TOKENS = 5
MIN_FUZZY_TOKENS = 4
ABSTRACT_CHARS = 200
MIN_ABSTRACT_CHARS = 150
MAX_GROUP = 12
ID_TITLE_JACCARD = 0.5
ID_ABSTRACT_CHARS = 100
# Many teams answer one shared task under near-identical titles.
SHARED_TASK = frozenset("semeval wmt trec clef iwslt sigmorphon bionlp".split())
MAX_BUCKET = 60
ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})|10\.48550/arxiv\.(\d{4}\.\d{4,5})", re.I)


def arxiv_from_url(url):
    m = ARXIV_RE.search(url or "")
    return (m.group(1) or m.group(2)) if m else None


def tokens(title):
    return frozenset(t for t in norm(title).split() if t not in STOP)


def _marks(toks):
    """Tokens that tell numbered parts and versions apart."""
    return frozenset(t for t in toks if t in ROMAN or any(c.isdigit() for c in t) or len(t) == 1)


def fuzzy_similar(a, b, threshold=FUZZY_JACCARD):
    if (a | b) & SHARED_TASK:
        return False
    if len(a) < MIN_FUZZY_TOKENS or len(b) < MIN_FUZZY_TOKENS:
        return False
    if _marks(a) != _marks(b):
        return False
    return len(a & b) / len(a | b) >= threshold


def id_corroborated(title_a, title_b, abs_a, abs_b):
    """An arXiv id in a source url is also what data sets and citing works
    carry, so the titles or the abstract openings must agree too."""
    ta, tb = tokens(title_a), tokens(title_b)
    if ta and tb and len(ta & tb) / len(ta | tb) >= ID_TITLE_JACCARD:
        return True
    xa, xb = norm(abs_a)[:ID_ABSTRACT_CHARS], norm(abs_b)[:ID_ABSTRACT_CHARS]
    return len(xa) >= ID_ABSTRACT_CHARS and xa == xb


def wp_similar(a, b):
    """Whitepaper titles are often a truncation of the paper's: the shorter
    token set must sit almost entirely inside the longer one."""
    if min(len(a), len(b)) < MIN_WP_TOKENS or _marks(a) != _marks(b):
        return False
    return len(a & b) / min(len(a), len(b)) >= WP_OVERLAP


class _Groups:
    """Union-find that refuses a merge which would put two arXiv records, a
    year spread over one, or more than MAX_GROUP members in one group."""

    def __init__(self, rows):
        self.parent = {}
        self.size = {}
        self.arxiv = {}
        self.years = {}
        for arxiv_id, _t, year, *_rest in rows:
            self.parent[arxiv_id] = arxiv_id
            self.size[arxiv_id] = 1
            self.arxiv[arxiv_id] = source(arxiv_id) == "arxiv"
            self.years[arxiv_id] = (year, year) if year else None

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.arxiv[ra] and self.arxiv[rb]:
            return False
        if self.size[ra] + self.size[rb] > MAX_GROUP:
            return False
        ya, yb = self.years[ra], self.years[rb]
        merged = ya or yb
        if ya and yb:
            merged = (min(ya[0], yb[0]), max(ya[1], yb[1]))
        if merged and merged[1] - merged[0] > 1:
            return False
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        self.arxiv[ra] = self.arxiv[ra] or self.arxiv[rb]
        self.years[ra] = merged
        return True


def _norm_abstract(text):
    return norm(text)[:ABSTRACT_CHARS]


def build(rows, stats=None):
    """rows: (arxiv_id, title, year[, abstract, source_url]). Returns {member:
    canonical} for every member of a group of two or more, the canonical
    mapping to itself. Rules, strongest first: exact title, arXiv id in the
    source url, identical abstract opening, fuzzy title. stats, if given, is
    filled with the number of merges each rule made."""
    rows = [tuple(r) + (None,) * (5 - len(r)) for r in rows]
    groups = _Groups(rows)
    stats = stats if stats is not None else {}
    log = stats.get("log")
    stats.update(title=0, id=0, abstract=0, fuzzy=0)
    year_of = {r[0]: r[2] for r in rows}
    row_of = {r[0]: r for r in rows}

    by_title = collections.defaultdict(list)
    for arxiv_id, title, _y, _ab, _u in rows:
        key = norm(title)
        if len(key) > MIN_TITLE_CHARS:
            by_title[key].append(arxiv_id)
    for members in by_title.values():
        members.sort(key=lambda m: (rank(m), m))
        for m in members[1:]:
            if groups.union(members[0], m):
                stats["title"] += 1
                if log is not None:
                    log.append(("title", members[0], m))

    for arxiv_id, _t, _y, _ab, url in rows:
        target = arxiv_from_url(url)
        if target and target != arxiv_id and target in groups.parent and source(arxiv_id) != "arxiv":
            other = row_of[target]
            if not id_corroborated(other[1], _t, other[3], _ab):
                continue
            if groups.union(target, arxiv_id):
                stats["id"] += 1
                if log is not None:
                    log.append(("id", target, arxiv_id))

    by_abs = collections.defaultdict(list)
    for arxiv_id, _t, _y, ab, _u in rows:
        key = _norm_abstract(ab)
        if len(key) >= MIN_ABSTRACT_CHARS:
            by_abs[key].append(arxiv_id)
    for members in by_abs.values():
        members.sort(key=lambda m: (rank(m), m))
        for m in members[1:]:
            if groups.union(members[0], m):
                stats["abstract"] += 1
                if log is not None:
                    log.append(("abstract", members[0], m))

    toks = {}
    df = collections.Counter()
    for arxiv_id, title, _y, _ab, _u in rows:
        if len(norm(title)) > MIN_TITLE_CHARS:
            t = tokens(title)
            if len(t) >= MIN_FUZZY_TOKENS:
                toks[arxiv_id] = t
                df.update(t)
    buckets = collections.defaultdict(list)
    for arxiv_id, t in toks.items():
        for w in sorted(t, key=lambda w: (df[w], w))[:3]:
            buckets[w].append(arxiv_id)
    seen = set()
    for members in buckets.values():
        if len(members) > MAX_BUCKET:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if (a, b) in seen or (b, a) in seen:
                    continue
                seen.add((a, b))
                if source(a) == "arxiv" and source(b) == "arxiv":
                    continue
                ya, yb = year_of[a], year_of[b]
                if ya and yb and abs(ya - yb) > 1:
                    continue
                wp = "wp" in (source(a), source(b))
                if (wp_similar(toks[a], toks[b]) if wp else fuzzy_similar(toks[a], toks[b])):
                    if groups.union(a, b):
                        stats["fuzzy"] += 1
                        if log is not None:
                            log.append(("fuzzy", a, b))

    clusters = collections.defaultdict(list)
    for member in groups.parent:
        clusters[groups.find(member)].append(member)
    canonical = {}
    for members in clusters.values():
        if len(members) > 1:
            head = min(members, key=lambda m: (rank(m), m))
            for member in members:
                canonical[member] = head
    return canonical


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/opt/dtox-research/state.db")
    ap.add_argument("--out", default="/opt/dtox-research/twins.json")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    rows = conn.execute("SELECT arxiv_id, title, year, abstract, source_url FROM papers WHERE status='done'").fetchall()
    stats = {}
    canonical = build(rows, stats)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(args.out) or ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump({"canonical": canonical}, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, args.out)
    groups = len(set(canonical.values()))
    print(f"{len(rows)} papers, {groups} groups, {len(canonical) - groups} extra copies; merges {stats}")


if __name__ == "__main__":
    main()
