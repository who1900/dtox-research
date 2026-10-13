#!/usr/bin/env python3
"""Bounded paraphrase retrieval benchmark. Uses only the Python standard library."""
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path


MAX_QUERIES = 24
MAX_WORKERS = 2
RUN_BUDGET_SECONDS = 180
MCP_ENDPOINT = "https://read.whoim.space/mcp"
_mcp_state = threading.local()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def queries_from_fixture(fixture):
    queries = []
    for group in fixture["groups"]:
        for phrase in group["phrases"]:
            queries.append({"group_id": group["id"], "layer": group["layer"],
                            "phrase_id": phrase["id"], "query": phrase["query"]})
    return queries


def _paper_ids(row):
    ids = []
    for value in [row.get("arxiv_id"), row.get("canonical_id")]:
        if value:
            ids.append(str(value).strip().lower())
    twins = row.get("twins") or []
    if isinstance(twins, list):
        ids.extend(str(value).strip().lower() for value in twins if value)
    return ids


def score(fixture, records):
    by_key, duplicate_keys = {}, set()
    for row in records:
        key = (row.get("group_id"), row.get("phrase_id"))
        if key in by_key:
            duplicate_keys.add(key)
        by_key[key] = row
    phrase_scores, group_scores = [], []
    invalid_count = 0
    negative_flags = []
    for group in fixture["groups"]:
        targets = {}
        for target in group["targets"]:
            canonical = str(target["canonical_id"]).strip().lower()
            targets[canonical] = {canonical, *(str(a).strip().lower() for a in target.get("aliases", []))}
        target_by_id = {alias: canonical for canonical, aliases in targets.items() for alias in aliases}
        negatives = {str(n["id"]).strip().lower(): n for n in group.get("hard_negatives", [])}
        gp = []
        for phrase in group["phrases"]:
            key = (group["id"], phrase["id"])
            record = by_key.get(key)
            valid = bool(key not in duplicate_keys and record and not record.get("error") and not record.get("partial")
                         and isinstance(record.get("results"), list)
                         and all(isinstance(x, dict) for x in record["results"]))
            ranks, neg_ranks, seen = {}, {}, set()
            paper_rank = 0
            if valid:
                for row in record["results"]:
                    ids = _paper_ids(row)
                    identity = next((target_by_id[x] for x in ids if x in target_by_id), ids[0] if ids else None)
                    if not identity or identity in seen:
                        continue
                    seen.add(identity)
                    paper_rank += 1
                    rank = paper_rank
                    for canonical, aliases in targets.items():
                        if set(ids) & aliases and canonical not in ranks:
                            ranks[canonical] = rank
                    for neg_id in negatives:
                        if neg_id in ids and neg_id not in neg_ranks:
                            neg_ranks[neg_id] = rank
            best = min(ranks.values()) if ranks else None
            hit3 = bool(best is not None and best <= 3) if valid else None
            hit10 = bool(best is not None and best <= 10) if valid else None
            flags = [nid for nid, rank in neg_ranks.items()
                     if (best is not None and rank < best and rank <= 10) or (best is None and rank <= 10)]
            negative_flags.extend({"group_id": group["id"], "phrase_id": phrase["id"],
                                   "negative_id": n, "rank": neg_ranks[n]} for n in flags)
            metric = {"group_id": group["id"], "phrase_id": phrase["id"], "valid": valid,
                      "hit@3": hit3, "hit@10": hit10, "first_target_rank": best,
                      "MRR@10": (1 / best if best and best <= 10 else 0.0) if valid else None,
                      "hard_negative_ranks": neg_ranks, "hard_negative_flags": flags,
                      "known_targets": sorted(ranks)}
            phrase_scores.append(metric)
            gp.append(metric)
            if not valid:
                invalid_count += 1
        ranks = [p["first_target_rank"] for p in gp]
        all_hit = bool(gp) and all(p["valid"] and p["hit@10"] for p in gp)
        known_sets = [set(p["known_targets"]) for p in gp if p["valid"]]
        intersection = set.intersection(*known_sets) if known_sets else set()
        union = set.union(*known_sets) if known_sets else set()
        group_scores.append({"group_id": group["id"], "all_phrasings_hit@10": all_hit,
                             "worst_rank": max(ranks) if all_hit else None,
                             "rank_spread": max(ranks) - min(ranks) if all_hit else None,
                             "target_agreement_valid_phrasings_only": (len(intersection) / len(union)) if union else None,
                             "valid_phrasings": sum(p["valid"] for p in gp),
                             "phrase_count": len(gp)})
    passed = bool(group_scores) and all(g["all_phrasings_hit@10"] for g in group_scores)
    return {"phrases": phrase_scores, "groups": group_scores,
            "operational_invalid_count": invalid_count, "negative_flag_count": len(negative_flags),
            "negative_flags": negative_flags, "all_groups_pass": passed}


def _http_json(endpoint, payload, timeout, headers=None):
    deadline = getattr(_mcp_state, "deadline", None)
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("run budget exceeded")
    request_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    request_headers.update(headers or {})
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                 headers=request_headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    if "text/event-stream" in response.headers.get("Content-Type", "") or body.lstrip().startswith("data:"):
        messages = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        if not messages:
            raise ValueError("empty MCP event stream")
        body = messages[-1]
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data


def _mcp_call(endpoint, item, timeout, strict=False):
    if not getattr(_mcp_state, "initialized", False) or getattr(_mcp_state, "endpoint", None) != endpoint:
        init = _http_json(endpoint, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2024-11-05",
                                                 "capabilities": {}, "clientInfo": {"name": "paraphrase-eval", "version": "1.0"}}}, timeout)
        if "error" in init:
            raise ValueError(f"MCP initialize error: {init['error']}")
        notification = urllib.request.Request(
            endpoint, data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}, method="POST")
        notify_timeout = min(timeout, _mcp_state.deadline - time.monotonic()) if getattr(_mcp_state, "deadline", None) else timeout
        if notify_timeout <= 0:
            raise TimeoutError("run budget exceeded")
        with urllib.request.urlopen(notification, timeout=notify_timeout):
            pass
        _mcp_state.initialized = True
        _mcp_state.endpoint = endpoint
    response = _http_json(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "search_research_paper",
                                                "arguments": {"query": item["query"], "layer": item["layer"],
                                                               "limit": 10, "strict": strict}}}, timeout)
    if "error" in response:
        raise ValueError(f"MCP JSON-RPC error: {response['error']}")
    result = response.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise ValueError("MCP tool call failed")
    content = result.get("structuredContent")
    if content is None:
        text_blocks = [b.get("text", "") for b in result.get("content", []) if b.get("type") == "text"]
        if len(text_blocks) != 1:
            raise ValueError("MCP response has no structured result")
        content = json.loads(text_blocks[0])
    if not isinstance(content, dict) or not isinstance(content.get("results"), list):
        raise ValueError("MCP payload has no results array")
    return content


def _request(endpoint, api_key, item, timeout, transport="api", strict=False, deadline=None):
    if deadline is not None:
        if time.monotonic() >= deadline:
            return {**item, "raw_payload": None, "results": None, "partial": False,
                    "error": "run budget exceeded", "latency_ms": 0}
        timeout = max(0.001, min(timeout, deadline - time.monotonic()))
    _mcp_state.deadline = deadline
    payload = {"query": item["query"], "layer": item["layer"], "limit": 10,
               "hybrid": True, "strict": strict, "compact": True}
    if strict:
        payload["auto_relax"] = False
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    started = time.monotonic()
    try:
        if transport == "mcp":
            data = _mcp_call(endpoint, item, timeout, strict)
        else:
            data = _http_json(endpoint, payload, timeout, headers)
        return {**item, "raw_payload": data, "results": data.get("results"), "partial": bool(data.get("partial")),
                "relaxed": data.get("relaxed"), "filters": data.get("filters"),
                "error": None, "latency_ms": round((time.monotonic() - started) * 1000, 2)}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError) as exc:
        return {**item, "raw_payload": None, "results": None, "partial": False, "error": str(exc),
                "latency_ms": round((time.monotonic() - started) * 1000, 2)}


def run(fixture, endpoint, workers=1, timeout=30, api_key=None, budget_seconds=RUN_BUDGET_SECONDS, transport="api", strict=False):
    if workers < 1 or workers > MAX_WORKERS:
        raise ValueError("workers must be between 1 and 2")
    queries = queries_from_fixture(fixture)
    if len(queries) > MAX_QUERIES:
        raise ValueError(f"fixture has {len(queries)} queries; maximum is {MAX_QUERIES}")
    if budget_seconds <= 0 or budget_seconds > RUN_BUDGET_SECONDS:
        raise ValueError("budget-seconds must be between 1 and 180")
    started = time.monotonic()
    deadline = started + budget_seconds
    results = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = {pool.submit(_request, endpoint, api_key, q, timeout, transport, strict, deadline): q for q in queries}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=budget_seconds):
            if time.monotonic() - started > budget_seconds:
                break
            results.append(future.result())
            print(f"completed {len(results)}/{len(queries)}", file=sys.stderr, flush=True)
    except concurrent.futures.TimeoutError:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    complete = {(r["group_id"], r["phrase_id"]) for r in results}
    results.extend({**q, "raw_payload": None, "results": None, "partial": False, "error": "run budget exceeded"}
                   for q in queries if (q["group_id"], q["phrase_id"]) not in complete)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--results", type=Path, help="score previously saved raw query results")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--transport", choices=("api", "mcp"), default="api")
    parser.add_argument("--strict", action="store_true", help="disable automatic relaxation")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--api-key-env")
    parser.add_argument("--max-queries", type=int, default=MAX_QUERIES)
    parser.add_argument("--budget-seconds", type=int, default=RUN_BUDGET_SECONDS)
    args = parser.parse_args(argv)
    if (args.cases is None) == (args.results is None):
        parser.error("provide exactly one of --cases or --results")
    fixture_path = args.cases if args.cases else None
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if args.results:
        saved = load_json(args.results)
        fixture = saved["fixture"]
        records = saved["raw_results"]
        original_config = saved.get("config", {})
        original_started = saved.get("started_utc")
        endpoint = original_config.get("endpoint")
        transport = original_config.get("transport", "api")
        strict = original_config.get("strict", False)
    else:
        fixture = load_json(args.cases)
        count = len(queries_from_fixture(fixture))
        if args.max_queries < 1 or count > args.max_queries or args.max_queries > MAX_QUERIES:
            parser.error(f"query count {count} exceeds cap (max allowed {MAX_QUERIES})")
        key = os.environ.get(args.api_key_env) if args.api_key_env else None
        if args.api_key_env and key is None:
            parser.error(f"environment variable {args.api_key_env} is not set")
        transport = args.transport
        endpoint = args.endpoint or (MCP_ENDPOINT if transport == "mcp" else "http://localhost:8010/v1/search")
        strict = args.strict
        original_started = now
        original_config = {"transport": transport, "endpoint": endpoint,
                           "workers": args.workers, "timeout": args.timeout,
                           "budget_seconds": args.budget_seconds, "strict": strict}
        records = run(fixture, endpoint, args.workers, args.timeout, key, args.budget_seconds, transport, strict)
    metrics = score(fixture, records)
    fixture_hash = hashlib.sha256(json.dumps(fixture, sort_keys=True).encode()).hexdigest()
    output = {"started_utc": original_started,
              "ended_utc": saved.get("ended_utc") if args.results else dt.datetime.now(dt.timezone.utc).isoformat(),
              "fixture_sha256": saved.get("fixture_sha256", fixture_hash) if args.results else fixture_hash,
              "config": original_config,
              "fixture": fixture, "raw_results": records, "metrics": metrics}
    if args.results:
        output["rescored_utc"] = now
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    return 0 if metrics["all_groups_pass"] and not metrics["negative_flag_count"] and not metrics["operational_invalid_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
