#!/usr/bin/env python3
"""Re-check off_niche papers against the current niche filter.

off_niche is decided once, at harvest time, by the term lists of that day.
The lists have grown since, so papers that pass today can sit rejected
forever. This re-runs service.upsert_discovered on each of them: same code
path as a fresh harvest, so a paper that now matches goes back to
'discovered' with its layers, and one that does not is left untouched.

Run from the pipeline directory (it imports service and niche_filter):

  resurrect_off_niche.py                 # dry run: counts per layer
  resurrect_off_niche.py --apply         # write, committing every 500
  resurrect_off_niche.py --since 2020    # only papers from that year on
"""
import argparse
import collections

import niche_filter as nf
import service


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--since", type=int, default=0)
    args = ap.parse_args()
    conn = service.get_conn()
    rows = conn.execute(
        "SELECT arxiv_id, title, year, abstract FROM papers "
        "WHERE status='off_niche' AND COALESCE(year, 0) >= ?", (args.since,)).fetchall()
    by_layer, by_year, n = collections.Counter(), collections.Counter(), 0
    for i, r in enumerate(rows):
        text = f"{r['title']} {r['abstract']}" if r["abstract"] else r["title"]
        layers = sorted(l for l, d in nf.niche_score(text).items() if d["score"] > 0)
        if layers:
            n += 1
            by_layer[",".join(layers)] += 1
            by_year[r["year"]] += 1
            if args.apply:
                service.upsert_discovered(conn, r["arxiv_id"], r["title"], r["year"], layers[0],
                                          abstract=r["abstract"], commit=False)
                if n % 500 == 0:
                    conn.commit()
        if (i + 1) % 50000 == 0:
            print(f"{i + 1}/{len(rows)} scanned, {n} pass", flush=True)
    if args.apply:
        conn.commit()
    verb = "resurrected" if args.apply else "would resurrect"
    print(f"{verb} {n} of {len(rows)}", flush=True)
    for k, v in by_layer.most_common(12):
        print(f"  {v:6d} {k}", flush=True)
    print("  by year:", sorted(by_year.items(), key=lambda kv: kv[0] or 0)[-8:], flush=True)


if __name__ == "__main__":
    main()
