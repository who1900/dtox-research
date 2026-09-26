#!/usr/bin/env python3
"""Run the citation-context benchmark against dtox's /v1/search endpoint and
report retrieval metrics.

    python eval/run_bench.py --bench eval/bench/citation_bench.jsonl \
        --api http://127.0.0.1:8010 --keys /opt/dtox-research-api/keys.json \
        --split dev --limit 10 --tag baseline

    # compare two runs
    python eval/run_bench.py --compare eval/runs/baseline.json eval/runs/hybrid_off.json

See eval/README.md for interpretation notes and limitations.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval import bench_lib as bl  # noqa: E402

DEFAULT_TIMEOUT_S = 120


def load_bench(path, split):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if split != "all" and row["split"] != split:
                continue
            rows.append(row)
    return rows


def load_api_key(keys_path):
    with open(keys_path, "r", encoding="utf-8") as f:
        keys = json.load(f)
    if not keys:
        raise SystemExit(f"no keys found in {keys_path}")
    return next(iter(keys.keys()))


def run_one_query(api_base, api_key, row, limit, params, timeout):
    body = {"query": row["query"], "limit": limit}
    if params:
        body.update(params)
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    # A faster backend pushes the run past the key's 60/min limit, and a 429
    # scored as a miss made one run look like a quality collapse. Wait it out
    # and time only the attempt that was actually served.
    for _attempt in range(6):
        start = time.monotonic()
        try:
            resp = requests.post(f"{api_base}/v1/search", json=body, headers=headers,
                                 timeout=timeout)
        except requests.RequestException as e:
            return {
                "qid": row["qid"], "target": row["target"], "layer": row["layer"],
                "bucket": row["bucket"], "named": row["named"], "split": row["split"],
                "rank": None, "error": True, "error_detail": str(e),
                "latency_ms": (time.monotonic() - start) * 1000, "partial": False,
            }
        if resp.status_code != 429:
            break
        time.sleep(10)
    latency_ms = (time.monotonic() - start) * 1000
    if resp.status_code != 200:
        return {
            "qid": row["qid"], "target": row["target"], "layer": row["layer"],
            "bucket": row["bucket"], "named": row["named"], "split": row["split"],
            "rank": None, "error": True, "error_detail": f"HTTP {resp.status_code}",
            "latency_ms": latency_ms, "partial": False,
        }
    data = resp.json()
    results = data.get("results", [])
    result_ids = [r.get("arxiv_id") for r in results]
    rank = bl.rank_of_target(result_ids, row["target"], source_id=row.get("source_id"))
    return {
        "qid": row["qid"], "target": row["target"], "layer": row["layer"],
        "bucket": row["bucket"], "named": row["named"], "split": row["split"],
        "rank": rank, "error": False, "partial": bool(data.get("partial")),
        "latency_ms": latency_ms, "query": row["query"],
    }


def run_bench(bench_rows, api_base, api_key, limit, params, concurrency, timeout=DEFAULT_TIMEOUT_S):
    results = [None] * len(bench_rows)
    done = 0

    def task(i, row):
        return i, run_one_query(api_base, api_key, row, limit, params, timeout)

    if concurrency <= 1:
        for i, row in enumerate(bench_rows):
            _, r = task(i, row)
            results[i] = r
            done += 1
            if done % 25 == 0:
                print(f"  ... {done}/{len(bench_rows)}", file=sys.stderr)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futures = [ex.submit(task, i, row) for i, row in enumerate(bench_rows)]
            for fut in as_completed(futures):
                i, r = fut.result()
                results[i] = r
                done += 1
                if done % 25 == 0:
                    print(f"  ... {done}/{len(bench_rows)}", file=sys.stderr)
    return results


def print_metrics_table(title, metrics):
    print(f"\n=== {title} ===")
    keys = ["n", "hit@1", "hit@3", "hit@10", "mrr@10", "ndcg@10",
            "error_rate", "partial_rate", "p50_latency_ms", "p95_latency_ms"]
    for k in keys:
        v = metrics.get(k)
        if isinstance(v, float):
            print(f"  {k:<16} {v:.4f}")
        else:
            print(f"  {k:<16} {v}")


def compute_all_metrics(rows):
    overall = bl.aggregate_metrics(rows)
    by_layer = {k: bl.aggregate_metrics(v) for k, v in bl.group_by(rows, lambda r: r["layer"]).items()}
    by_bucket = {k: bl.aggregate_metrics(v) for k, v in bl.group_by(rows, lambda r: r["bucket"]).items()}
    by_named = {("named" if k else "descriptive"): bl.aggregate_metrics(v)
                for k, v in bl.group_by(rows, lambda r: r["named"]).items()}
    return {"overall": overall, "by_layer": by_layer, "by_bucket": by_bucket, "by_named": by_named}


def do_run(args):
    bench_rows = load_bench(args.bench, args.split)
    if not bench_rows:
        raise SystemExit(f"no bench rows for split={args.split} in {args.bench}")
    api_key = load_api_key(args.keys)
    params = json.loads(args.params) if args.params else {}

    print(f"running {len(bench_rows)} queries against {args.api} "
          f"(split={args.split}, limit={args.limit}, concurrency={args.concurrency})",
          file=sys.stderr)
    rows = run_bench(bench_rows, args.api, api_key, args.limit, params,
                     args.concurrency, args.timeout)

    metrics = compute_all_metrics(rows)
    print_metrics_table("overall", metrics["overall"])
    for layer, m in sorted(metrics["by_layer"].items()):
        print_metrics_table(f"layer={layer}", m)
    for bucket, m in sorted(metrics["by_bucket"].items()):
        print_metrics_table(f"bucket={bucket}", m)
    for named, m in sorted(metrics["by_named"].items()):
        print_metrics_table(f"named={named}", m)

    out = {
        "tag": args.tag, "config": {
            "api": args.api, "split": args.split, "limit": args.limit,
            "params": params, "bench": args.bench,
        },
        "metrics": metrics, "rows": rows,
    }
    out_dir = Path("eval/runs")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.tag}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out_path}", file=sys.stderr)


def do_compare(args):
    a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
    b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
    print(f"\n=== compare: {a.get('tag')} -> {b.get('tag')} ===")
    delta = bl.compare_metrics(a["metrics"]["overall"], b["metrics"]["overall"])
    for k, v in delta.items():
        print(f"  {k:<16} {v:+.4f}")

    for section in ("by_layer", "by_bucket", "by_named"):
        a_groups = a["metrics"].get(section, {})
        b_groups = b["metrics"].get(section, {})
        for key in sorted(set(a_groups) | set(b_groups)):
            if key not in a_groups or key not in b_groups:
                continue
            d = bl.compare_metrics(a_groups[key], b_groups[key])
            print(f"\n  -- {section}={key} --")
            for k, v in d.items():
                print(f"    {k:<16} {v:+.4f}")

    a_by_qid = {r["qid"]: r for r in a.get("rows", [])}
    b_by_qid = {r["qid"]: r for r in b.get("rows", [])}
    lost, gained = [], []
    for qid in set(a_by_qid) & set(b_by_qid):
        ra, rb = a_by_qid[qid], b_by_qid[qid]
        a_top3 = ra["rank"] is not None and ra["rank"] <= 3
        b_top3 = rb["rank"] is not None and rb["rank"] <= 3
        if a_top3 and not b_top3:
            lost.append(qid)
        elif b_top3 and not a_top3:
            gained.append(qid)
    print(f"\n  queries where target left top-3: {len(lost)} {lost[:20]}")
    print(f"  queries where target entered top-3: {len(gained)} {gained[:20]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bench", default="eval/bench/citation_bench.jsonl")
    ap.add_argument("--api", default="http://127.0.0.1:8010")
    ap.add_argument("--keys", default="/opt/dtox-research-api/keys.json")
    ap.add_argument("--split", default="dev", choices=["dev", "test", "all"])
    ap.add_argument("--params", default=None, help="JSON dict merged into the /v1/search body")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--compare", nargs=2, default=None, metavar=("RUN_A", "RUN_B"))
    args = ap.parse_args()

    if args.compare:
        do_compare(args)
    else:
        do_run(args)


if __name__ == "__main__":
    main()
