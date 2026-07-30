#!/usr/bin/env python3
"""Audit the deferred and rejected shelves for false refusals.

There are more deferred papers than live ones, and nothing in the API shows
that shelf, so a bias there is invisible from every other angle. This samples
it and reports what the gate actually did, with the numbers needed to judge
whether the policy is right.

  python3 audit_deferred.py [--sample 50]
"""
import argparse
import json
import random
import sqlite3
import sys

DB = "file:/opt/dtox-research/state.db?mode=ro"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=50)
    args = ap.parse_args()

    conn = sqlite3.connect(DB, uri=True)
    conn.row_factory = sqlite3.Row

    counts = dict(conn.execute("SELECT status, COUNT(*) FROM papers GROUP BY status").fetchall())
    print("shelves:", {k: v for k, v in sorted(counts.items())})

    for status in ("deferred", "rejected"):
        rows = conn.execute(
            "SELECT arxiv_id, title, year, citation_count, venue, niche_score, layers "
            "FROM papers WHERE status=? ORDER BY RANDOM() LIMIT ?", (status, args.sample)
        ).fetchall()
        if not rows:
            continue
        print(f"\n=== {status}: sample of {len(rows)}")

        # what the gate keyed on, so a wrong policy is visible rather than implied
        no_citations = sum(1 for r in rows if not (r["citation_count"] or 0))
        has_venue = sum(1 for r in rows if (r["venue"] or "").strip())
        strong_niche = sum(1 for r in rows if (r["niche_score"] or 0) >= 5)
        recent = sum(1 for r in rows if (r["year"] or 0) >= 2025)
        print(f"  zero citations: {no_citations}/{len(rows)}   "
              f"has a venue: {has_venue}   niche_score>=5: {strong_niche}   "
              f"2025 or later: {recent}")

        # the ones worth a human eye: on-topic and well cited, yet not admitted
        suspicious = [r for r in rows
                      if (r["niche_score"] or 0) >= 5 and (r["citation_count"] or 0) >= 5]
        print(f"  candidates for a false refusal (niche>=5 and cited>=5): {len(suspicious)}")
        for r in suspicious[:8]:
            print(f"    {r['arxiv_id']:14} {str(r['year']):5} cит={str(r['citation_count']):>4} "
                  f"niche={r['niche_score']} {(r['title'] or '')[:52]}")

    print("\npolicy in force (service.py):")
    print("  quality gate passes on: a top venue, 3+ influential citations, or the")
    print("  citation threshold for the paper's age in months (<12: 3, <24: 10,")
    print("  <36: 30, <48: 60, <60: 100, else 150).")
    print("  Below that a paper younger than 24 months is deferred and re-checked")
    print("  every 14 days; older than that it is rejected for good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
