"""Bounded, read-only three-variant evaluation against the configured API.

Example (run on the API host): python eval/large_search_eval.py --api-dir /opt/dtox-research-api --fixtures eval/old.json eval/new.json --output /tmp/large-search.json --expected-main-hash SHA256
Embeddings are generated once per unique query and saved beside output as
<output>.vectors.json. The search itself uses the normal read-only API paths.
"""
import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
MAX_BUDGET_SECONDS = 1200
MAX_QUERY_SECONDS = 12
VARIANTS = ("primary10", "primary20", "discovery10")
SECRET_PATTERN = re.compile(r"(?i)(bearer\s+|(?:api[_-]?key|token|password|secret)=)[^\s&,;]+")


def _norm(value):
    return str(value or "").strip().lower()


def _load_fixtures(paths):
    records = []
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for group in data["groups"]:
            aliases = {}
            targets, negatives = set(), set()
            for field, destination in (("targets", targets), ("hard_negatives", negatives)):
                for item in group.get(field, []):
                    canonical = _norm(item.get("canonical_id", item.get("id")))
                    if canonical:
                        destination.add(canonical)
                        aliases[canonical] = canonical
                    for alias in item.get("aliases", []):
                        alias = _norm(alias)
                        if alias:
                            destination.add(alias)
                            aliases[alias] = canonical
            for phrase in group["phrases"]:
                records.append({"fixture": Path(path).name, "group_id": group["id"],
                                "phrase_id": phrase["id"], "layer": group.get("layer"),
                                "query": phrase["query"], "targets": sorted(targets),
                                "hard_negatives": sorted(negatives), "aliases": aliases})
    return records


def _stable_order(records):
    """Shuffle deterministically within groups and round-robin the strata."""
    strata = {}
    for row in records:
        strata.setdefault((row["fixture"], str(row["layer"]), str(row["group_id"])), []).append(row)
    rng = random.Random(20261004)
    keys = sorted(strata)
    for key in keys:
        rng.shuffle(strata[key])
    rng.shuffle(keys)
    ordered = []
    layers = sorted({key[1] for key in keys})
    by_layer = {layer: [key for key in keys if key[1] == layer] for layer in layers}
    for layer_keys in by_layer.values():
        rng.shuffle(layer_keys)
    while any(strata.values()):
        for layer in layers:
            for key in by_layer[layer]:
                if strata[key]:
                    ordered.append(strata[key].pop())
    return ordered


def _metric(ids, targets, aliases=None, limit=None):
    """Return known-target hit and reciprocal rank; this is not precision."""
    aliases = aliases or {}
    visible = list(ids[:limit] if limit is not None else ids)
    canonical_targets = {aliases.get(_norm(x), _norm(x)) for x in targets}
    for rank, paper_id in enumerate(visible, 1):
        if aliases.get(_norm(paper_id), _norm(paper_id)) in canonical_targets:
            return {"hit": True, "mrr": 1.0 / rank, "rank": rank}
    return {"hit": False, "mrr": 0.0, "rank": None}


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo), 3)


def _summarize(records):
    result = {"attempted": len(records), "variants": {}}
    for name in VARIANTS:
        rows = [r["variants"].get(name) for r in records]
        complete_rows = [x for x in rows if x and x.get("complete")]
        result["variants"][name] = {
            "attempted": len(rows), "complete": sum(bool(x and x.get("complete")) for x in rows),
            "failed_or_partial": sum(not bool(x and x.get("complete")) for x in rows),
            "hit_at_10_all_attempted": sum(bool(x and x.get("complete") and x.get("metrics", {}).get("hit@10")) for x in rows),
            "hit_at_20_all_attempted": sum(bool(x and x.get("complete") and x.get("metrics", {}).get("hit@20")) for x in rows),
            "reach_hit_at_20_all_attempted": sum(bool(x and x.get("complete") and x.get("metrics", {}).get("reach_hit@20")) for x in rows),
            "known_hard_negative_primary_present": sum(bool(x and x.get("complete") and x.get("metrics", {}).get("known_hard_negative_primary")) for x in rows),
            "known_hard_negative_candidate_present_unverified": sum(bool(x and x.get("complete") and x.get("metrics", {}).get("known_hard_negative_candidate_unverified")) for x in rows),
            "mrr_known_target": round(statistics.mean(x["metrics"]["mrr"] for x in complete_rows), 6) if complete_rows else None,
            "latency_p50_ms": _percentile([x["latency_ms"] for x in rows if x and x.get("latency_ms") is not None], .5),
            "latency_p95_ms": _percentile([x["latency_ms"] for x in rows if x and x.get("latency_ms") is not None], .95),
            "json_bytes": sum(x.get("json_bytes", 0) for x in rows if x),
            "json_chars": sum(x.get("json_chars", 0) for x in rows if x),
        }
    complete = [r for r in records if all(r["variants"].get(v, {}).get("complete") for v in VARIANTS)]
    result["complete_triplets"] = len(complete)
    result["all_three_hit"] = sum(all(r["variants"][v]["metrics"]["hit@10"] for v in VARIANTS) for r in complete)
    result["all_three_hit_inclusive_failures_as_miss"] = sum(all(
        r["variants"].get(v, {}).get("complete") and
        r["variants"].get(v, {}).get("metrics", {}).get("hit@10", False) for v in VARIANTS) for r in records)
    result["primary20_vs_discovery_reach_hit20"] = {
        "attempted": len(records), "paired_complete": sum(
            bool(r["variants"].get("primary20", {}).get("complete") and
                 r["variants"].get("discovery10", {}).get("complete")) for r in records),
        "baseline_hits_inclusive": sum(bool(r["variants"].get("primary20", {}).get("complete") and r["variants"].get("primary20", {}).get("metrics", {}).get("hit@20")) for r in records),
        "discovery_reach_hits_inclusive": sum(bool(r["variants"].get("discovery10", {}).get("complete") and r["variants"].get("discovery10", {}).get("metrics", {}).get("reach_hit@20")) for r in records),
    }
    result["groups"] = {}
    group_keys = sorted({(r["fixture"], r["group_id"]) for r in records})
    for fixture, group_id in group_keys:
        group_rows = [r for r in records if (r["fixture"], r["group_id"]) == (fixture, group_id)]
        key = f"{fixture}:{group_id}"
        result["groups"][key] = {"fixture": fixture, "group_id": group_id,
            "phrases_attempted": len(group_rows), "variants": {name: {
                "hit_at_10": sum(bool(r["variants"].get(name, {}).get("complete") and
                    r["variants"].get(name, {}).get("metrics", {}).get("hit@10")) for r in group_rows),
                "reach_hit_at_20": sum(bool(r["variants"].get(name, {}).get("complete") and
                    r["variants"].get(name, {}).get("metrics", {}).get("reach_hit@20")) for r in group_rows),
                "phrases_attempted": len(group_rows)} for name in VARIANTS},
            "all_phrases_hit_at_10_each_variant": bool(group_rows) and all(
                all(r["variants"].get(name, {}).get("complete") and
                    r["variants"][name].get("metrics", {}).get("hit@10") for r in group_rows)
                for name in VARIANTS)}
    layers = sorted({str(r.get("layer")) for r in records})
    result["layers"] = {layer: {"attempted": sum(str(r.get("layer")) == layer for r in records),
        "all_three_hit_inclusive": sum(str(r.get("layer")) == layer and all(
            r["variants"].get(v, {}).get("metrics", {}).get("hit@10", False) for v in VARIANTS)
            for r in records)} for layer in layers}
    result["macro_group_all_phrases_all_variants_hit_rate"] = (round(statistics.mean(
        row["all_phrases_hit_at_10_each_variant"] for row in result["groups"].values()), 6)
        if result["groups"] else None)
    result["cohorts_by_fixture"] = {}
    for fixture in sorted({r["fixture"] for r in records}):
        cohort = [r for r in records if r["fixture"] == fixture]
        result["cohorts_by_fixture"][fixture] = {"attempted": len(cohort),
            "complete_triplets": sum(all(r["variants"].get(v, {}).get("complete") for v in VARIANTS) for r in cohort),
            "all_three_hit_inclusive_failures_as_miss": sum(all(
                r["variants"].get(v, {}).get("complete") and
                r["variants"].get(v, {}).get("metrics", {}).get("hit@10") for v in VARIANTS)
                for r in cohort)}
    paired = {"primary10_vs_discovery10": [], "primary20_vs_discovery10": []}
    for r in records:
        a, b, c = (r["variants"].get(v) for v in VARIANTS)
        if a and c and a.get("complete") and c.get("complete"):
            paired["primary10_vs_discovery10"].append(c["latency_ms"] - a["latency_ms"])
        if b and c and b.get("complete") and c.get("complete"):
            paired["primary20_vs_discovery10"].append(c["latency_ms"] - b["latency_ms"])
    result["paired_latency_delta_ms"] = {key: {"n": len(vals), "median": _percentile(vals, .5),
        "bootstrap_95_ci": _bootstrap_ci(vals, seed=7)} for key, vals in paired.items()}
    return result


def _bootstrap_ci(values, seed=7, iterations=1000):
    if not values:
        return None
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(iterations)]
    return [_percentile(means, .025), _percentile(means, .975)]


def _search_summary(payload, elapsed, row, canonicalize):
    primary = payload.get("results") or []
    candidates = payload.get("source_candidates") or []
    ids = [_norm(x.get("arxiv_id")) for x in primary if isinstance(x, dict)]
    candidate_ids = [_norm(x.get("arxiv_id")) for x in candidates if isinstance(x, dict)]
    aliases = row["aliases"].copy()
    def canonical(pid):
        normalized = _norm(pid)
        aliased = aliases.get(normalized, normalized)
        if canonicalize:
            try:
                return _norm(canonicalize(aliased))
            except Exception:
                pass
        return _norm(aliased)
    canonical_targets = {canonical(target) for target in row["targets"]}
    canonical_negatives = {canonical(target) for target in row["hard_negatives"]}
    canonical_ids = [canonical(pid) for pid in ids]
    canonical_candidate_ids = [canonical(pid) for pid in candidate_ids]
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    primary_negative = any(canonical(pid) in canonical_negatives for pid in ids[:10])
    candidate_negative = any(canonical(pid) in canonical_negatives for pid in candidate_ids[:10])
    def hit(values, limit):
        return any(pid in canonical_targets for pid in values[:limit])
    def mrr(values, limit):
        return next((1 / rank for rank, pid in enumerate(values[:limit], 1) if pid in canonical_targets), 0.0)
    return {"complete": not bool(payload.get("partial")), "partial": bool(payload.get("partial")),
            "latency_ms": round(elapsed * 1000, 2), "metrics": {
                "hit@10": hit(canonical_ids, 10), "hit@20": hit(canonical_ids, 20),
                "reach_hit@20": hit(canonical_ids + canonical_candidate_ids, 20),
                "mrr": mrr(canonical_ids, 20),
                "known_hard_negative_primary": primary_negative,
                "known_hard_negative_candidate_unverified": candidate_negative},
            "primary": primary[:20], "candidates": candidates[:20], "primary_ids": ids[:20],
            "canonical_primary_ids": canonical_ids[:20], "candidate_ids": candidate_ids[:20],
            "canonical_candidate_ids": canonical_candidate_ids[:20], "warnings": _warnings(payload),
            "json_chars": len(compact), "json_bytes": len(compact.encode("utf-8"))}


def _warnings(payload):
    keys = ("retrieval_warning", "warnings", "partial", "error", "failed_sources", "source_errors")
    out = {}
    for key in keys:
        value = payload.get(key)
        if value:
            if isinstance(value, list):
                out[key] = [_redact(str(x))[:240] for x in value[:20]]
            elif isinstance(value, dict):
                out[key] = {_redact(str(k))[:80]: _redact(str(v))[:240] for k, v in list(value.items())[:20]}
            else:
                out[key] = _redact(str(value))[:500]
    return out


def _redact(value):
    return SECRET_PATTERN.sub(r"\1[REDACTED]", value)


def _write(path, output):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str) + "\n", encoding="utf-8")


def main_cli(argv=None):
    from source_discovery_probe import _environment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-dir", required=True, type=Path)
    parser.add_argument("--fixtures", nargs="+", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-main-hash", required=True)
    parser.add_argument("--budget-seconds", type=float, default=900)
    parser.add_argument("--per-query-seconds", type=float, default=12)
    args = parser.parse_args(argv)
    if not 0 < args.budget_seconds <= MAX_BUDGET_SECONDS or not 0 < args.per_query_seconds <= MAX_QUERY_SECONDS:
        parser.error("budget must be in (0,1200], per-query timeout in (0,12]")
    sys.path.insert(0, str(EVAL_DIR))
    import splade_candidate_probe as helpers
    started = time.monotonic()
    output = {"schema_version": 1, "records": [], "runner_error": None,
              "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "config": {"fixtures": [str(p) for p in args.fixtures], "budget_seconds": args.budget_seconds,
                         "per_query_seconds": args.per_query_seconds, "expected_main_sha256": args.expected_main_hash.lower()}}
    original_env = os.environ.get("SOURCE_DISCOVERY")
    app = None
    original_switch = None
    try:
        output["config"]["fixture_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.fixtures}
        main_path = args.api_dir / "main.py"
        if not main_path.is_file():
            raise FileNotFoundError(f"main.py missing under --api-dir: {args.api_dir}")
        digest = hashlib.sha256(main_path.read_bytes()).hexdigest()
        output["config"]["main_sha256"] = digest
        if digest.lower() != args.expected_main_hash.lower():
            raise ValueError("main.py SHA-256 mismatch")
        queries = _stable_order(_load_fixtures(args.fixtures))
        if not queries:
            raise ValueError("fixtures contain no phrases")
        fixture_keys = [(r["fixture"], r["group_id"], r["phrase_id"]) for r in queries]
        if len(fixture_keys) != len(set(fixture_keys)):
            raise ValueError("duplicate fixture/group/phrase identity")
        config = _environment(args.api_dir)
        output["config"]["environment"] = config
        api_path = str(args.api_dir.resolve())
        if api_path not in sys.path:
            sys.path.insert(0, api_path)
        import main as app
        original_switch = app.SOURCE_DISCOVERY
        helpers._disable_caches(app)
        original_embed = app.embed_query
        original_coarse = app.embed_query_coarse
        # Generate once per unique query, serially, then persist vectors for audit/replay.
        vectors = {"small": {}, "base": {}}
        embedding_failures = {}
        deadline = started + args.budget_seconds
        embedding_timed_out = False
        for query in dict.fromkeys(r["query"] for r in queries):
            try:
                embed_timeout = min(MAX_QUERY_SECONDS, deadline - time.monotonic())
                if embed_timeout <= 0:
                    raise TimeoutError("wall budget exhausted during embedding generation")
                vectors["small"][query] = helpers._run_with_timeout(
                    lambda: original_embed(query, wait_timeout=embed_timeout, request_timeout=embed_timeout), embed_timeout)
                embed_timeout = min(MAX_QUERY_SECONDS, deadline - time.monotonic())
                if embed_timeout <= 0:
                    raise TimeoutError("wall budget exhausted during embedding generation")
                vectors["base"][query] = helpers._run_with_timeout(
                    lambda: original_coarse(query, timeout=embed_timeout), embed_timeout)
            except helpers._QueryTimeout as exc:
                embedding_failures[query] = {"type": type(exc).__name__, "message": _redact(str(exc))[:400]}
                vectors["small"].pop(query, None)
                vectors["base"].pop(query, None)
                embedding_timed_out = True
                break
            except Exception as exc:
                embedding_failures[query] = {"type": type(exc).__name__, "message": _redact(str(exc))[:400]}
                vectors["small"].pop(query, None)
                vectors["base"].pop(query, None)
        if embedding_timed_out:
            for query in dict.fromkeys(r["query"] for r in queries):
                if query not in vectors["small"] or query not in vectors["base"]:
                    embedding_failures.setdefault(query, {"type": "EmbeddingRunnerStopped", "message": "embedding timeout stopped further generation"})
        vector_path = args.output.with_suffix(args.output.suffix + ".vectors.json")
        _write(vector_path, vectors)
        output["embedding_generation"] = {"unique_queries": len(set(r["query"] for r in queries)),
            "successful_small": len(vectors["small"]), "successful_base": len(vectors["base"]),
            "failed_queries": len(embedding_failures), "failures": embedding_failures,
            "vectors_path": str(vector_path)}
        def frozen_small(text, *a, **kw): return vectors["small"][text.strip()]
        def frozen_base(text, *a, **kw): return vectors["base"][text.strip()]
        app.embed_query, app.embed_query_coarse = frozen_small, frozen_base
        app._large_eval_coverage = {}
        covered_ids = sorted({x for r in queries for x in r["targets"] + list(r["aliases"])} )
        app._large_eval_coverage = app._paper_rows(covered_ids)
        output["coverage"] = {pid: pid in app._large_eval_coverage for pid in covered_ids}
        output["coverage_counts"] = {"known_indexed_alias_or_target": sum(output["coverage"].values()),
                                     "unknown_or_not_indexed": len(output["coverage"]) - sum(output["coverage"].values()),
                                     "targets_affected_by_unknown_coverage": sum(not any(
                                         output["coverage"].get(pid, False) for pid in row["targets"] + [
                                             alias for alias, canonical in row["aliases"].items()
                                             if canonical in row["targets"]])
                                         for row in queries)}
        for index, row in enumerate(queries):
            record = {k: row[k] for k in ("fixture", "group_id", "phrase_id", "layer", "query", "targets", "hard_negatives")}
            target_lookup = set(row["targets"])
            target_lookup.update(alias for alias, canonical in row["aliases"].items() if canonical in row["targets"])
            record["target_indexed"] = any(x in app._large_eval_coverage for x in target_lookup)
            record["variants"] = {}
            record["search_body"] = {"layer": row["layer"], "hybrid": True, "compact": True,
                                      "diagnose": False, "limits": {"primary10": 10,
                                      "primary20": 20, "discovery10": 10}}
            if row["query"] in embedding_failures:
                record["errors"] = ["embedding_generation_failed"]
                for variant in VARIANTS:
                    record["variants"][variant] = {"complete": False, "partial": True,
                        "latency_ms": None, "error": "embedding_generation_failed",
                        "warnings": embedding_failures[row["query"]]}
                output["records"].append(record)
                output["summary"] = _summarize(output["records"])
                _write(args.output, output)
                continue
            order = (VARIANTS[index % 3:] + VARIANTS[:index % 3])
            record["order"] = list(order)
            for variant in order:
                remaining = min(deadline - time.monotonic(), args.per_query_seconds)
                if remaining <= 0:
                    record["errors"] = ["wall_budget_exhausted"]
                    break
                app.SOURCE_DISCOVERY = variant == "discovery10"
                os.environ["SOURCE_DISCOVERY"] = "1" if app.SOURCE_DISCOVERY else "0"
                limit = 20 if variant == "primary20" else 10
                body = app.SearchBody(query=row["query"], layer=row["layer"], limit=limit,
                                      compact=True, hybrid=True, diagnose=False)
                if variant == "discovery10":
                    body = body.model_copy(update={"limit": 10})
                began = time.monotonic()
                try:
                    payload = helpers._run_with_timeout(lambda: app._run_search(body,
                        dense_timeout=remaining, embed_wait_timeout=remaining,
                        embed_http_timeout=remaining), remaining)
                    record["variants"][variant] = _search_summary(payload, time.monotonic()-began,
                        row, getattr(app, "canonical_id", None))
                except BaseException as exc:
                    record["variants"][variant] = {"complete": False, "partial": True,
                        "latency_ms": round((time.monotonic()-began)*1000, 2),
                        "error": type(exc).__name__, "warnings": {"error": _redact(str(exc))[:400]}}
                    if isinstance(exc, helpers._QueryTimeout):
                        output["stop_reason"] = "query_timeout"
                        break
            p10, d10 = (record["variants"].get(v) for v in ("primary10", "discovery10"))
            p20 = record["variants"].get("primary20")
            record["paired_metrics"] = {
                "primary10_discovery10_exact_primary_ids": (
                    p10.get("canonical_primary_ids") == d10.get("canonical_primary_ids")
                    if p10 and d10 and p10.get("complete") and d10.get("complete") else None),
                "discovery_reach_hit20_minus_primary20_hit20": (
                    int(d10["metrics"]["reach_hit@20"]) - int(p20["metrics"]["hit@20"])
                    if p20 and d10 and p20.get("complete") and d10.get("complete") else None),
            }
            output["records"].append(record)
            output["summary"] = _summarize(output["records"])
            _write(args.output, output)
            if record.get("errors") or output.get("stop_reason"):
                break
        output["unattempted"] = max(0, len(queries) - len(output["records"]))
    except BaseException as exc:
        output["runner_error"] = {"type": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        if app is not None:
            if "original_embed" in locals():
                app.embed_query, app.embed_query_coarse = original_embed, original_coarse
            if hasattr(app, "SOURCE_DISCOVERY"):
                app.SOURCE_DISCOVERY = original_switch if original_switch is not None else False
        if original_env is None:
            os.environ.pop("SOURCE_DISCOVERY", None)
        else:
            os.environ["SOURCE_DISCOVERY"] = original_env
        output["elapsed_seconds"] = round(time.monotonic() - started, 3)
        output["summary"] = _summarize(output["records"])
        _write(args.output, output)
    return output


if __name__ == "__main__":
    result = main_cli()
    if result.get("runner_error"):
        raise SystemExit(1)
