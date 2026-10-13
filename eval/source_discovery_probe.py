"""Bounded, read-only A/B runner for hierarchical source discovery."""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_FIXTURES = (EVAL_DIR / "paraphrase_cases_20261003.json",
                    EVAL_DIR / "paraphrase_holdout_20261003.json")
SCRATCH = Path.home() / "Documents/work/Claude/.scratch_dtox"
DEFAULT_SMALL = SCRATCH / "splade-20261003/queries-final.json"
DEFAULT_BASE = SCRATCH / "splade-stage-a-20261003/queries-base.json"
MAX_BUDGET_SECONDS = 180.0
MAX_QUERY_SECONDS = 12.0


def _normal_id(value):
    return str(value or "").strip().lower()


def _fixture_oracles(fixtures, fixture_queries):
    """Map each query to target and hard-negative IDs with declared aliases."""
    by_key = {}
    for path in fixtures:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for group in data["groups"]:
            oracle = {"targets": set(), "hard_negatives": set()}
            alias_map = {}
            for kind in oracle:
                for target in group.get(kind, []):
                    canonical = _normal_id(target.get("canonical_id", target.get("id")))
                    oracle[kind].add(canonical)
                    alias_map[canonical] = canonical
                    for alias in target.get("aliases", []):
                        alias = _normal_id(alias)
                        oracle[kind].add(alias)
                        alias_map[alias] = canonical
                oracle[kind].discard("")
            for phrase in group["phrases"]:
                by_key[(Path(path).name, group["id"], phrase["id"])] = (oracle, alias_map)
    oracle_by_query = {}
    for item in fixture_queries:
        key = (item["fixture"], item["group_id"], item["phrase_id"])
        oracle, alias_map = by_key.get(key, ({"targets": set(), "hard_negatives": set()}, {}))
        oracle_by_query[item["query"]] = {
            "targets": sorted(oracle["targets"]),
            "hard_negatives": sorted(oracle["hard_negatives"]),
            "canonical_alias_map": alias_map,
        }
    return oracle_by_query


def _candidate_metadata(candidate):
    """Retain bounded, auditable discovery provenance; omit document bodies."""
    return {key: candidate.get(key) for key in (
        "arxiv_id", "candidate_channel", "retrieval_role", "candidate_reason", "relevance_floor",
        "passes_relevance_floor", "verification_required", "score", "url", "fusion_ranks")}


def _rank_in(ids, oracle, alias_map):
    ranks = {}
    for paper_id in oracle:
        canonical = alias_map.get(paper_id, paper_id)
        ranks[paper_id] = next((index + 1 for index, actual in enumerate(ids)
                                if alias_map.get(actual, actual) == canonical), None)
    return ranks


def _canonical(paper_id, alias_map, canonicalize):
    normalized = _normal_id(paper_id)
    return _normal_id(canonicalize(paper_id)) if canonicalize else alias_map.get(normalized, normalized)


def _candidate_integrity(candidates, primary, alias_map, canonicalize=None):
    primary_ids = {_canonical(row.get("arxiv_id"), alias_map, canonicalize)
                   for row in primary if isinstance(row, dict)}
    errors = []
    if len(candidates) > 10:
        errors.append("candidate_count_exceeds_10")
    seen = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            errors.append(f"candidate_{index}_not_object")
            continue
        paper_id = _normal_id(candidate.get("arxiv_id"))
        canonical = _canonical(paper_id, alias_map, canonicalize)
        if canonical in primary_ids:
            errors.append(f"candidate_{index}_overlaps_primary_canonical_id")
        if canonical in seen:
            errors.append(f"candidate_{index}_duplicate_canonical_id")
        seen.add(canonical)
        if candidate.get("verification_required") is not True:
            errors.append(f"candidate_{index}_verification_not_required")
        if candidate.get("retrieval_role") != "source_candidate":
            errors.append(f"candidate_{index}_invalid_role")
        if candidate.get("candidate_channel") not in ("fused", "citation_graph"):
            errors.append(f"candidate_{index}_invalid_channel")
        elif candidate.get("candidate_channel") == "citation_graph":
            graph_rank = (candidate.get("fusion_ranks") or {}).get("graph")
            if not isinstance(graph_rank, (int, float)) or not math.isfinite(graph_rank) or graph_rank < 1:
                errors.append(f"candidate_{index}_missing_citation_graph_rank")
        reason = candidate.get("candidate_reason")
        if reason not in ("outside_primary_result_budget", "fragment_below_relevance_floor"):
            errors.append(f"candidate_{index}_invalid_reason")
        floor, score = candidate.get("relevance_floor"), candidate.get("score")
        numeric = lambda value: isinstance(value, (int, float)) and math.isfinite(value)
        if not numeric(floor) or not numeric(score):
            errors.append(f"candidate_{index}_invalid_floor_or_score")
        else:
            passes = score >= floor
            if candidate.get("passes_relevance_floor") is not passes:
                errors.append(f"candidate_{index}_floor_flag_mismatch")
            expected_reason = "outside_primary_result_budget" if passes else "fragment_below_relevance_floor"
            if reason != expected_reason:
                errors.append(f"candidate_{index}_reason_floor_mismatch")
        url = candidate.get("url") or candidate.get("source_url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            errors.append(f"candidate_{index}_missing_actual_source_url")
    return {"valid": not errors, "candidate_count": len(candidates), "errors": errors}


def _variant_summary(payload, elapsed_ms, oracle, canonicalize=None,
                     include_primary_snapshot=False):
    if not isinstance(payload, dict):
        raise ValueError("search payload must be an object")
    primary = payload.get("results", [])
    candidates = payload.get("source_candidates", [])
    if not isinstance(primary, list) or not isinstance(candidates, list):
        raise ValueError("results and source_candidates must be arrays")
    primary_ids = [_normal_id(row.get("arxiv_id")) for row in primary if isinstance(row, dict)]
    candidate_ids = [_normal_id(row.get("arxiv_id")) for row in candidates if isinstance(row, dict)]
    output_chars = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
    alias_map = oracle["canonical_alias_map"]
    canonical_alias_map = alias_map
    if canonicalize:
        canonical_alias_map = {paper_id: _normal_id(canonicalize(paper_id))
                               for paper_id in set(alias_map) | set(primary_ids) | set(candidate_ids)}
    target_ranks_primary = _rank_in(primary_ids[:10], oracle["targets"], canonical_alias_map)
    target_ranks_reach = _rank_in((primary_ids + candidate_ids)[:20], oracle["targets"], canonical_alias_map)
    negative_ranks_primary = _rank_in(primary_ids[:10], oracle["hard_negatives"], canonical_alias_map)
    negative_ranks_candidate = _rank_in(candidate_ids[:10], oracle["hard_negatives"], canonical_alias_map)
    canonical_primary = json.dumps(primary, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"), default=str)
    summary = {
        "primary_ids": primary_ids[:10],
        "_primary_results_canonical": canonical_primary,
        "primary_count": len(primary),
        "primary_target_ranks": target_ranks_primary,
        "primary_hit_at_10": any(rank is not None for rank in target_ranks_primary.values()),
        "source_candidate_count": len(candidates),
        "source_candidates": [_candidate_metadata(row) for row in candidates[:10]
                              if isinstance(row, dict)],
        "discovery_reach_ids_up_to_20": (primary_ids + candidate_ids)[:20],
        "discovery_reach_target_ranks_up_to_20": target_ranks_reach,
        "discovery_reach_hit": any(rank is not None for rank in target_ranks_reach.values()),
        "hard_negative_primary_ranks": negative_ranks_primary,
        "hard_negative_candidate_ranks": negative_ranks_candidate,
        "hard_negative_candidate_interpretation": "candidate presence is unverified; not evidence of falsity",
        "candidate_integrity": _candidate_integrity(candidates, primary, alias_map, canonicalize),
        "candidate_channel_counts": {
            channel: sum(row.get("candidate_channel") == channel for row in candidates
                         if isinstance(row, dict))
            for channel in ("fused", "citation_graph")},
        "candidate_channels_alternate": all(
            left.get("candidate_channel") != right.get("candidate_channel")
            for left, right in zip(candidates, candidates[1:])
            if isinstance(left, dict) and isinstance(right, dict)),
        "output_chars": output_chars,
        "latency_ms": round(elapsed_ms, 2),
        "partial": bool(payload.get("partial")),
        "complete": not bool(payload.get("partial")),
    }
    if include_primary_snapshot:
        summary["primary_results_snapshot"] = primary[:10]
    return summary


def _pair_metrics(baseline, treatment):
    """Do not assign equality or gains unless both variants completed."""
    if not baseline or not treatment or not baseline.get("complete") or not treatment.get("complete"):
        return {"complete": False, "primary_ids_exact_equal": None,
                "primary_count_equal": None, "primary_hit_at_10_delta": None,
                "discovery_reach_hit_delta": None}
    return {
        "complete": True,
        "primary_ids_exact_equal": baseline["primary_ids"] == treatment["primary_ids"],
        "primary_results_exact_equal": baseline["_primary_results_canonical"] == treatment["_primary_results_canonical"],
        "primary_count_equal": baseline["primary_count"] == treatment["primary_count"],
        "primary_hit_at_10_delta": int(treatment["primary_hit_at_10"]) - int(baseline["primary_hit_at_10"]),
        "discovery_reach_hit_delta": int(treatment["discovery_reach_hit"]) - int(baseline["discovery_reach_hit"]),
    }


def _summarize(records):
    complete = [row for row in records if row.get("metrics", {}).get("complete")]
    return {
        "expected_queries": 33,
        "recorded_queries": len(records),
        "complete_pairs": len(complete),
        "incomplete_pairs": len(records) - len(complete),
        "primary_exact_equal_pairs": sum(row["metrics"]["primary_ids_exact_equal"] for row in complete),
        "primary_result_objects_exact_equal_pairs": sum(row["metrics"]["primary_results_exact_equal"] for row in complete),
        "primary_count_equal_pairs": sum(row["metrics"]["primary_count_equal"] for row in complete),
        "primary_hit_at_10_baseline": sum(row["baseline"]["primary_hit_at_10"] for row in complete),
        "primary_hit_at_10_treatment": sum(row["treatment"]["primary_hit_at_10"] for row in complete),
        "discovery_reach_hits_baseline": sum(row["baseline"]["discovery_reach_hit"] for row in complete),
        "discovery_reach_hits_treatment": sum(row["treatment"]["discovery_reach_hit"] for row in complete),
        "hard_negative_primary_inclusions_baseline": sum(
            sum(rank is not None for rank in row["baseline"]["hard_negative_primary_ranks"].values())
            for row in complete),
        "hard_negative_primary_inclusions_treatment": sum(
            sum(rank is not None for rank in row["treatment"]["hard_negative_primary_ranks"].values())
            for row in complete),
        "hard_negative_candidate_observations_baseline_unverified": sum(
            sum(rank is not None for rank in row["baseline"]["hard_negative_candidate_ranks"].values())
            for row in complete),
        "hard_negative_candidate_observations_treatment_unverified": sum(
            sum(rank is not None for rank in row["treatment"]["hard_negative_candidate_ranks"].values())
            for row in complete),
        "candidate_integrity_valid_pairs": sum(row["treatment"]["candidate_integrity"]["valid"]
                                                for row in complete),
        "candidate_channel_alternation_pairs": sum(row["treatment"]["candidate_channels_alternate"]
                                                    for row in complete),
        **_size_statistics(complete),
    }


def _percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 2)


def _size_statistics(complete):
    deltas, baselines, candidate_counts = [], [], []
    for row in complete:
        base_chars = row["baseline"]["output_chars"]
        treatment_chars = row["treatment"]["output_chars"]
        deltas.append(treatment_chars - base_chars)
        baselines.append(base_chars)
        candidate_counts.append(row["treatment"]["source_candidate_count"])
    return {
        "additional_json_chars_p50": _percentile(deltas, 0.50),
        "additional_json_chars_p95": _percentile(deltas, 0.95),
        "additional_json_chars_ratio_to_baseline": round(sum(deltas) / sum(baselines), 6) if sum(baselines) else None,
        "average_treatment_candidate_count": round(sum(candidate_counts) / len(candidate_counts), 3) if candidate_counts else None,
        "average_treatment_fused_candidate_count": round(sum(
            row["treatment"]["candidate_channel_counts"]["fused"] for row in complete) / len(complete), 3)
            if complete else None,
        "average_treatment_citation_graph_candidate_count": round(sum(
            row["treatment"]["candidate_channel_counts"]["citation_graph"] for row in complete) / len(complete), 3)
            if complete else None,
    }


def _environment(api_dir):
    config = {
        "QDRANT_URL": "http://127.0.0.1:16335",
        "QDRANT_COLLECTION": "papers_fulltext",
        "COARSE_EMBED_URL": "http://127.0.0.1:16337/embed",
        "COARSE_COLLECTION": "papers_coarse_base",
        "EMBED_URL": "http://127.0.0.1:8006/embed",
        "PAPER_FTS_PATH": "/opt/dtox-research/papers_fts.db",
        "STATE_DB_PATH": "/opt/dtox-research/state.db",
        "API_KEYS_PATH": "/__codex_probe_no_api_keys__",
        "ATTESTOR_INTERNAL_TOKEN": "",
        "EXACT_MATCH": "1",
        "HIER_CHUNK_LEXICAL": "0",
        "HIER_GRAPH": "1",
        "HIER_GROUPED_SEARCH": "0",
        "HIER_RANK_CHANNELS": "0",
        "RERANK_ENABLED": "0",
        "HIER_SEARCH": "1",
        "PAPER_LEXICAL": "1",
        "PAPER_LEXICAL_SOURCE_BALANCE": "1",
        "HIER_FUSION": "rrf",
        "HIER_W_DENSE": "2",
        "HIER_W_LEX": "0.8",
        "HIER_W_GLOBAL": "1",
        "SOURCE_DISCOVERY": "0",
    }
    os.environ.update(config)
    return config


def _write_output(path, output):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str) + "\n",
                    encoding="utf-8")


def main_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-dir", required=True, type=Path)
    parser.add_argument("--fixtures", nargs=2, type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--small-queries", type=Path, default=DEFAULT_SMALL)
    parser.add_argument("--base-queries", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-main-hash", required=True)
    parser.add_argument("--include-primary-snapshots", action="store_true",
                        help="retain at most ten full compact primary result objects per variant")
    parser.add_argument("--budget-seconds", type=float, default=MAX_BUDGET_SECONDS)
    parser.add_argument("--per-query-seconds", type=float, default=MAX_QUERY_SECONDS)
    args = parser.parse_args(argv)
    if not 0 < args.budget_seconds <= MAX_BUDGET_SECONDS:
        parser.error("--budget-seconds must be in (0, 180]")
    if not 0 < args.per_query_seconds <= MAX_QUERY_SECONDS:
        parser.error("--per-query-seconds must be in (0, 12]")

    sys.path.insert(0, str(EVAL_DIR))
    import splade_candidate_probe as helpers

    started = time.monotonic()
    fixtures = helpers._fixture_queries(args.fixtures)
    queries = [row["query"] for row in fixtures]
    small = helpers._load_query_vectors(args.small_queries, queries, 384, "small")
    base = helpers._load_query_vectors(args.base_queries, queries, 768, "base")
    original_env_switch = os.environ.get("SOURCE_DISCOVERY")
    config = _environment(args.api_dir)
    output = {"schema_version": 1, "runner_operational": True, "records": [],
              "skipped": [], "runner_error": None,
              "config": {**config, "api_dir": str(args.api_dir.resolve()),
                         "fixtures": [str(Path(path).resolve()) for path in args.fixtures],
                         "small_queries_path": str(args.small_queries.resolve()),
                         "base_queries_path": str(args.base_queries.resolve()),
                         "expected_main_sha256": args.expected_main_hash.lower(),
                         "budget_seconds": args.budget_seconds,
                         "per_query_seconds": args.per_query_seconds,
                         "search_limit": 10, "diagnose": False,
                         "include_primary_snapshots": args.include_primary_snapshots,
                         "output_contract": "primary results and discovery reach reported separately"}}
    original_switch = None
    try:
        main_path = args.api_dir / "main.py"
        if not main_path.is_file():
            raise FileNotFoundError(f"main.py not found under --api-dir: {args.api_dir}")
        actual_hash = hashlib.sha256(main_path.read_bytes()).hexdigest()
        output["config"]["main_sha256"] = actual_hash
        if actual_hash.lower() != args.expected_main_hash.lower():
            raise ValueError(f"main.py hash mismatch: expected {args.expected_main_hash}, got {actual_hash}")
        api_path = str(args.api_dir.resolve())
        if api_path not in sys.path:
            sys.path.insert(0, api_path)
        import main as app
        helpers._disable_caches(app)
        helpers._install_query_vectors(app, small, base)
        oracles = _fixture_oracles(args.fixtures, fixtures)
        deadline = started + args.budget_seconds
        stop = False
        for index, item in enumerate(fixtures):
            record = {key: item[key] for key in ("fixture", "group_id", "phrase_id", "layer", "query")}
            record["targets_normalized"] = oracles[item["query"]]["targets"]
            record["hard_negatives_normalized"] = oracles[item["query"]]["hard_negatives"]
            record["order"] = ["baseline", "treatment"] if index % 2 == 0 else ["treatment", "baseline"]
            record["baseline"] = None
            record["treatment"] = None
            record["errors"] = []
            for variant in record["order"]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record["errors"].append("budget_exhausted_before_pair_complete")
                    output["stop_reason"] = "wall_budget_exhausted"
                    stop = True
                    break
                app.SOURCE_DISCOVERY = variant == "treatment"
                os.environ["SOURCE_DISCOVERY"] = "1" if variant == "treatment" else "0"
                body = app.SearchBody(query=item["query"], layer=item["layer"],
                                      limit=10, compact=True, hybrid=True, diagnose=False)
                query_started = time.monotonic()
                try:
                    timeout = min(args.per_query_seconds, remaining)
                    payload = helpers._run_with_timeout(
                        lambda: app._run_search(body, dense_timeout=timeout,
                                                embed_wait_timeout=timeout,
                                                embed_http_timeout=timeout), timeout)
                    record[variant] = _variant_summary(
                        payload, (time.monotonic() - query_started) * 1000,
                        oracles[item["query"]], getattr(app, "canonical_id", None),
                        args.include_primary_snapshots)
                    if record[variant]["partial"]:
                        record["errors"].append(f"{variant}_partial_response")
                        continue
                except BaseException as exc:
                    record[variant] = {"complete": False, "partial": True,
                                       "latency_ms": round((time.monotonic() - query_started) * 1000, 2),
                                       "error": f"{type(exc).__name__}: {exc}"}
                    record["errors"].append(f"{variant}_error")
                    if isinstance(exc, helpers._QueryTimeout):
                        output["runner_operational"] = False
                        output["stop_reason"] = "hard_query_timeout_runner_stopped"
                        stop = True
                    break
            record["metrics"] = _pair_metrics(record["baseline"], record["treatment"])
            for variant in ("baseline", "treatment"):
                if isinstance(record[variant], dict):
                    record[variant].pop("_primary_results_canonical", None)
            output["records"].append(record)
            output["summary"] = _summarize(output["records"])
            _write_output(args.output, output)
            if stop:
                break
        if stop:
            output["skipped"] = [{"index": i, "query": row["query"], "complete": False,
                                  "reason": output.get("stop_reason", "budget_exhausted")}
                                 for i, row in enumerate(fixtures[len(output["records"]):], len(output["records"]))]
    except BaseException as exc:
        output["runner_error"] = f"{type(exc).__name__}: {exc}"
        output["runner_operational"] = False
    finally:
        if "app" in locals() and original_switch is not None:
            app.SOURCE_DISCOVERY = original_switch
        if original_env_switch is None:
            os.environ.pop("SOURCE_DISCOVERY", None)
        else:
            os.environ["SOURCE_DISCOVERY"] = original_env_switch
        output["elapsed_seconds"] = round(time.monotonic() - started, 3)
        output["summary"] = _summarize(output["records"])
        _write_output(args.output, output)
    return output


if __name__ == "__main__":
    main_cli()
