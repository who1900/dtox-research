"""Bounded read-only shadow rerank probe over the live hierarchical search pool.

Usage: python eval/semantic_rank_probe.py --api-dir api --expected-main-sha256 SHA --output /tmp/semantic-rank.json
Fixture vectors are loaded from the existing small/base cached-query JSON inputs;
no query-embedding request is made. The in-process search reads configured local
Qdrant/SQLite services. The optional scorer POST is the only rerank call. Results
are exploratory retrieval signals, not primary evidence or verification.
"""

import argparse
import hashlib
import json
import math
import os
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVAL = Path(__file__).resolve().parent
SCRATCH = Path.home() / "Documents/work/Claude/.scratch_dtox"
DEFAULT_FIXTURES = [EVAL / "paraphrase_cases_20261003.json",
                    EVAL / "paraphrase_holdout_20261003.json"]
DEFAULT_SMALL = SCRATCH / "splade-20261003/queries-final.json"
DEFAULT_BASE = SCRATCH / "splade-stage-a-20261003/queries-base.json"
MAX_BUDGET_SECONDS = 180.0
MAX_QUERY_SECONDS = 12.0
DEFAULT_MAX_QUERIES = 33
MAX_QUERIES = 93
EXPECTED_MAIN_SHA256 = "5c3df936534b2b9f9246e0e20d2453c0b5553496a124d12d975b73f3fde79a1a"
SCORER_BATCH_SIZE = 12


def _environment():
    config = {
        "QDRANT_URL": "http://127.0.0.1:16335", "QDRANT_COLLECTION": "papers_fulltext",
        "COARSE_EMBED_URL": "http://127.0.0.1:16337/embed", "COARSE_COLLECTION": "papers_coarse_base",
        "EMBED_URL": "http://127.0.0.1:8006/embed", "PAPER_FTS_PATH": "/opt/dtox-research/papers_fts.db",
        "STATE_DB_PATH": "/opt/dtox-research/state.db", "API_KEYS_PATH": "/__codex_probe_no_api_keys__",
        "ATTESTOR_INTERNAL_TOKEN": "", "EXACT_MATCH": "1", "HIER_CHUNK_LEXICAL": "0",
        "HIER_GRAPH": "1", "HIER_GROUPED_SEARCH": "0", "HIER_RANK_CHANNELS": "0",
        "RERANK_ENABLED": "0", "HIER_SEARCH": "1", "PAPER_LEXICAL": "1",
        "PAPER_LEXICAL_SOURCE_BALANCE": "1", "HIER_FUSION": "rrf",
        "HIER_W_DENSE": "2", "HIER_W_LEX": "0.8", "HIER_W_GLOBAL": "1",
        "SOURCE_DISCOVERY": "0", "TITLE_BUILD_PRIORITY": "1",
    }
    return config


def _fixture_data(paths):
    rows, groups = [], {}
    for path in paths:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for group in data["groups"]:
            key = (Path(path).name, group["id"])
            targets = {str(x.get("canonical_id", x.get("id", ""))).strip().lower()
                       for x in group.get("targets", [])}
            negatives = {str(x.get("canonical_id", x.get("id", ""))).strip().lower()
                         for x in group.get("hard_negatives", [])}
            aliases = {}
            for item in group.get("targets", []) + group.get("hard_negatives", []):
                canonical = str(item.get("canonical_id", item.get("id", ""))).strip().lower()
                aliases[canonical] = canonical
                for alias in item.get("aliases", []):
                    aliases[str(alias).strip().lower()] = canonical
            groups[key] = {"targets": targets, "hard_negatives": negatives, "aliases": aliases}
            groups[key]["phrase_count"] = len(group["phrases"])
            for phrase in group["phrases"]:
                rows.append({"fixture": Path(path).name, "group_id": group["id"],
                             "phrase_id": phrase["id"], "layer": group["layer"],
                             "query": phrase["query"]})
    return rows, groups


def _select_queries(rows, limit):
    """Round-robin fixture/group strata so bounded runs do not take a prefix."""
    buckets = {}
    order = []
    for row in rows:
        key = (row["fixture"], row["group_id"])
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)
    selected = []
    depth = 0
    while len(selected) < min(limit, len(rows)):
        advanced = False
        for key in order:
            if depth < len(buckets[key]) and len(selected) < limit:
                selected.append(buckets[key][depth])
                advanced = True
        if not advanced:
            break
        depth += 1
    return selected


def _rank_metrics(ids, target_ids, negative_ids):
    ranks = {target: next((i + 1 for i, value in enumerate(ids)
                           if value == target), None) for target in target_ids}
    neg_ranks = {item: next((i + 1 for i, value in enumerate(ids)
                             if value == item), None) for item in negative_ids}
    found = [rank for rank in ranks.values() if rank is not None]
    return {"target_ranks": ranks, "hit": bool(found),
            "mrr": 1 / min(found) if found else 0.0,
            "hard_negative_count": sum(rank is not None for rank in neg_ranks.values()),
            "hard_negative_ranks": neg_ranks}


def _canonical_normalizer(canonicalize, aliases):
    return lambda value: str(canonicalize(aliases.get(str(value).strip().lower(), value))).strip().lower()


def _phrase_group_summary(groups, records):
    result = {}
    for fixture_group, group in sorted(groups.items()):
        fixture, group_id = fixture_group
        rows = [row for row in records if row.get("fixture") == fixture and row.get("group_id") == group_id]
        hits = sum(bool(row.get("ce_complete") and row.get("cross_encoder20", {}).get("hit"))
                   for row in rows)
        result[f"{fixture}:{group_id}"] = {
            "attempted": len(rows), "expected": group["phrase_count"],
            "success": len(rows) == group["phrase_count"] == hits,
        }
    return result


def _score_post(url, timeout):
    import requests

    def score(query, pairs):
        deadline = time.monotonic() + min(3.0, timeout)
        scores, batches = [], []
        score.last_batches = batches
        for offset in range(0, len(pairs), SCORER_BATCH_SIZE):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("scorer deadline exceeded")
            batch = pairs[offset:offset + SCORER_BATCH_SIZE]
            response = requests.post(url, json={"query": query,
                                     "passages": [pair["title"] + "\n" + pair["passage"] for pair in batch]},
                                     timeout=remaining)
            response.raise_for_status()
            body = response.json()
            chunk = body.get("scores") if isinstance(body, dict) else None
            if not isinstance(chunk, list) or len(chunk) != len(batch):
                raise ValueError("scorer batch score count mismatch")
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in chunk):
                raise ValueError("scorer batch contains invalid scores")
            scores.extend(chunk)
            batches.append(len(batch))
            if time.monotonic() >= deadline:
                raise TimeoutError("scorer deadline exceeded")
        score.last_batches = batches
        return scores
    score.last_batches = []
    return score


def _load_vectors(path, expected_queries, dimension, label, section=None):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if section and isinstance(data, dict) and section in data:
        data = data[section]
    if isinstance(data, dict) and "queries" in data:
        data = data["queries"]
    if isinstance(data, list):
        data = {row["query"]: row.get("vector", row.get("query_vector")) for row in data}
    if not isinstance(data, dict):
        raise ValueError(f"{label} vectors must be a query map or record list")
    vectors = {}
    for query in expected_queries:
        vector = data.get(query)
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ValueError(f"{label} vector dimension/key mismatch for {query!r}")
        values = [float(value) for value in vector]
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"{label} vector contains a non-finite value")
        vectors[query] = values
    return vectors


def run(args):
    if not 0 < args.budget_seconds <= MAX_BUDGET_SECONDS:
        raise ValueError("budget-seconds must be in (0, 180]")
    if not 0 < args.per_query_seconds <= MAX_QUERY_SECONDS:
        raise ValueError("per-query-seconds must be in (0, 12]")
    if not 1 <= args.max_queries <= MAX_QUERIES:
        raise ValueError("max-queries must be in [1, 93]")
    if str(EVAL) not in sys.path:
        sys.path.insert(0, str(EVAL))
    import splade_candidate_probe as helpers

    started = time.monotonic()
    all_rows, groups = _fixture_data(args.fixtures)
    rows = _select_queries(all_rows, args.max_queries)
    selected_keys = {(row["fixture"], row["group_id"], row["phrase_id"]) for row in rows}
    queries = [row["query"] for row in rows]
    vector_path = getattr(args, "vectors", None)
    small_path = vector_path or args.small_queries
    base_path = vector_path or args.base_queries
    small = _load_vectors(small_path, queries, 384, "small", "small" if vector_path else None)
    base = _load_vectors(base_path, queries, 768, "base", "base" if vector_path else None)
    config = _environment()
    env_original = {key: os.environ.get(key) for key in config}
    os.environ.update(config)
    output = {"schema_version": 1, "runner_operational": True, "records": [], "skipped": [],
              "runner_error": None, "config": {**config,
              "fixtures": [str(Path(p).resolve()) for p in args.fixtures],
              "small_queries_path": str(args.small_queries.resolve()),
              "base_queries_path": str(args.base_queries.resolve()),
              "vectors_path": str(vector_path.resolve()) if vector_path else None,
              "expected_main_sha256": args.expected_main_sha256.lower(),
              "budget_seconds": args.budget_seconds, "per_query_seconds": args.per_query_seconds,
              "max_queries": args.max_queries, "search_limit": 10, "candidate_limit": 20,
              "expected_query_count": len(all_rows), "selected_query_count": len(rows),
              "passages_per_paper": 2, "max_pairs": 40, "scorer_timeout_seconds": min(3, args.per_query_seconds),
              "scorer_batch_size": SCORER_BATCH_SIZE, "primary_evidence": False,
              "query_selection": "stable round-robin fixture/group strata",
              "fixture_sha256": {str(Path(p).resolve()): hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                 for p in args.fixtures},
              "vector_sha256": {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in {small_path, base_path}},
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
    app = None
    try:
        main_path = args.api_dir / "main.py"
        if not main_path.is_file():
            raise FileNotFoundError(f"main.py not found under {args.api_dir}")
        actual = hashlib.sha256(main_path.read_bytes()).hexdigest()
        output["config"]["main_sha256"] = actual
        if actual.lower() != args.expected_main_sha256.lower():
            raise ValueError(f"main.py SHA mismatch: expected {args.expected_main_sha256}, got {actual}")
        api_dir = str(args.api_dir.resolve())
        if api_dir not in sys.path:
            sys.path.insert(0, api_dir)
        sys.path.insert(0, str(EVAL))
        import main as app
        try:
            from semantic_rank_experiment import build_candidates, rerank_candidates
        except ImportError:
            from api.semantic_rank_experiment import build_candidates, rerank_candidates
        cache_names = ("search_cache", "embedding_cache", "coarse_embedding_cache", "qdrant_result_cache")
        old_caches = {name: getattr(app, name) for name in cache_names if hasattr(app, name)}
        helpers._disable_caches(app)
        old_embed, old_coarse = app.embed_query, app.embed_query_coarse
        app.embed_query = lambda query, *a, **kw: small[query]
        app.embed_query_coarse = lambda query, *a, **kw: base[query]
        old_hier = app._hier_run
        old_switches = {name: getattr(app, name) for name in
                        ("SOURCE_DISCOVERY", "TITLE_BUILD_PRIORITY", "HIER_SEARCH", "HIER_CHUNK_LEXICAL",
                         "HIER_GRAPH", "HIER_GROUPED_SEARCH", "HIER_RANK_CHANNELS", "RERANK_ENABLED",
                         "PAPER_LEXICAL", "PAPER_LEXICAL_SOURCE_BALANCE", "HIER_FUSION",
                         "HIER_W_DENSE", "HIER_W_LEX", "HIER_W_GLOBAL")}
        runtime = {"SOURCE_DISCOVERY": False, "TITLE_BUILD_PRIORITY": True,
                   "HIER_SEARCH": True, "HIER_CHUNK_LEXICAL": False, "HIER_GRAPH": True,
                   "HIER_GROUPED_SEARCH": False, "HIER_RANK_CHANNELS": False,
                   "RERANK_ENABLED": False, "PAPER_LEXICAL": True,
                   "PAPER_LEXICAL_SOURCE_BALANCE": True, "HIER_FUSION": "rrf",
                   "HIER_W_DENSE": 2.0, "HIER_W_LEX": 0.8, "HIER_W_GLOBAL": 1.0}
        for name, value in runtime.items():
            setattr(app, name, value)
        output["config"]["main_globals"] = runtime
        output["config"]["api_main_sha256"] = actual
        experiment_path = Path(args.api_dir) / "semantic_rank_experiment.py"
        if not experiment_path.is_file():
            experiment_path = ROOT / "api" / "semantic_rank_experiment.py"
        output["config"]["semantic_experiment_sha256"] = hashlib.sha256(experiment_path.read_bytes()).hexdigest()
        output["config"]["auto_relax"] = False

        deadline = started + args.budget_seconds
        capture_lock = threading.Lock()
        scorer_disabled = False
        for item in rows:
            if time.monotonic() >= deadline:
                output["stop_reason"] = "wall_budget_exhausted"
                break
            record = {key: item[key] for key in ("fixture", "group_id", "phrase_id", "layer", "query")}
            record["complete"] = False
            captured = []
            def capture(*a, _query=item["query"], _captured=captured, **kw):
                capture_started = time.monotonic()
                result = old_hier(*a, **kw)
                hits, _left, _partial, extra = result
                search_body = a[3] if len(a) > 3 else kw.get("body")
                call_query = kw.get("query", getattr(search_body, "query", None))
                if call_query == _query:
                    with capture_lock:
                        _captured.append({"layer": getattr(search_body, "layer", None), "hits": hits,
                                          "fusion": (extra or {}).get("fusion_ranks", {}),
                                          "partial": _partial, "elapsed_ms": (time.monotonic() - capture_started) * 1000})
                return result
            app._hier_run = capture
            timeout = min(args.per_query_seconds, max(0, deadline - time.monotonic()))
            if timeout <= 0:
                record["error"] = "wall_budget_exhausted_before_search"
                output["records"].append(record)
                output["stop_reason"] = "wall_budget_exhausted"
                break
            body = app.SearchBody(query=item["query"], layer=item["layer"], limit=10,
                                  compact=False, hybrid=True, diagnose=False, auto_relax=False)
            t0 = time.monotonic()
            try:
                payload = helpers._run_with_timeout(
                    lambda: app._run_search(body, dense_timeout=timeout,
                                            embed_wait_timeout=timeout, embed_http_timeout=timeout), timeout)
                baseline_ids = [app.canonical_id(r.get("arxiv_id")).strip().lower()
                                for r in payload.get("results", [])[:10] if r.get("arxiv_id")]
                record["baseline_primary10"] = baseline_ids
                record["partial"] = bool(payload.get("partial"))
                if not captured:
                    raise RuntimeError("hierarchical pool was not captured")
                ordered = sorted(captured, key=lambda entry: (
                    entry["layer"] != item["layer"], entry["layer"] or ""))
                hits, fusion, partial = [], {}, []
                for entry in ordered:
                    hits.extend(entry["hits"] or [])
                    for paper_id, ranks in (entry["fusion"] or {}).items():
                        fusion.setdefault(paper_id, ranks)
                    partial.extend(entry["partial"] or [])
                record["timing_ms"] = {"search_total": round((time.monotonic() - t0) * 1000, 2),
                                        "query_encoding": "cached_excluded",
                                        "hier_captures": [round(entry["elapsed_ms"], 2) for entry in captured]}
                canonical = lambda value: app.canonical_id(value)
                build_started = time.monotonic()
                candidates = build_candidates(hits, canonical, app._hit_is_boilerplate,
                                              fusion_ranks=fusion, limit=20, passages_per_paper=2)
                record["timing_ms"]["candidate_build"] = round((time.monotonic() - build_started) * 1000, 2)
                record["pool_hit_count"] = len(hits)
                record["candidate_count"] = len(candidates)
                source_floors = {}
                configured_floors = {}
                for entry in ordered:
                    layer_name = entry["layer"] or item["layer"]
                    layer_floor = app.MIN_RELEVANCE_BY_LAYER.get(layer_name, app.MIN_RELEVANCE_SCORE)
                    for hit in entry["hits"] or []:
                        paper_id = (hit.get("payload") or {}).get("arxiv_id")
                        if paper_id:
                            configured_floors.setdefault(canonical(paper_id), layer_floor)
                for hit in hits:
                    payload_hit = hit.get("payload") or {}
                    paper_id = payload_hit.get("arxiv_id")
                    floor_value = hit.get("relevance_floor", payload_hit.get("relevance_floor"))
                    if paper_id and isinstance(floor_value, (int, float)) and math.isfinite(floor_value):
                        source_floors[canonical(paper_id)] = float(floor_value)
                record["source_rank_provenance"] = [{"arxiv_id": c["arxiv_id"],
                    "canonical_id": c["canonical_id"], "paper_rank": c["paper_rank"],
                    "channel_ranks": c["channel_ranks"], "score": c["score"],
                    "relevance_floor": source_floors.get(c["canonical_id"]),
                    "configured_floor": configured_floors.get(c["canonical_id"]),
                    "passes_relevance_floor": (c["score"] >= source_floors[c["canonical_id"]]
                        if c["score"] is not None and c["canonical_id"] in source_floors else None),
                    "verification_required": True} for c in candidates]
                group = groups[(item["fixture"], item["group_id"])]
                oracle = group["aliases"]
                normalize = _canonical_normalizer(canonical, oracle)
                targets = {normalize(x) for x in group["targets"]}
                negatives = {normalize(x) for x in group["hard_negatives"]}
                def ids_for(rows_):
                    return [normalize(c["arxiv_id"]) for c in rows_]
                baseline_ids = [normalize(r.get("arxiv_id")) for r in payload.get("results", [])[:10]
                                if r.get("arxiv_id")]
                record["baseline_primary10"] = baseline_ids
                record["baseline_metrics"] = _rank_metrics(baseline_ids, targets, negatives)
                source_ids = ids_for(candidates)
                scorer_remaining = min(3.0, timeout - (time.monotonic() - t0), deadline - time.monotonic())
                scorer = _score_post(args.rerank_url, max(0.0, scorer_remaining))
                rerank_started = time.monotonic()
                if scorer_disabled:
                    report = {"status": "fallback", "reason": "scorer_disabled_after_failure",
                              "pairs_scored": 0, "candidates": candidates, "error_type": None}
                else:
                    try:
                        report = helpers._run_with_timeout(
                            lambda: rerank_candidates(item["query"], candidates, scorer),
                            max(0.001, scorer_remaining))
                    except helpers._QueryTimeout:
                        scorer_disabled = True
                        report = {"status": "fallback", "reason": "scorer_timeout",
                                  "error_type": "TimeoutError", "pairs_scored": 0,
                                  "candidates": candidates}
                rerank_elapsed = (time.monotonic() - rerank_started) * 1000
                if report.get("status") == "fallback" and report.get("error_type") in (
                        "TimeoutError", "ReadTimeout", "ConnectTimeout", "ConnectionError", "ValueError"):
                    scorer_disabled = True
                reranked = report["candidates"]
                record["semantic_rank_status"] = report["status"]
                record["rerank_effective_order"] = "semantic" if report["status"] == "reranked" else "baseline_fallback"
                record["scorer_error"] = report.get("error_type") or (
                    report.get("reason") if report.get("status") == "fallback" else None)
                record["pairs_scored"] = report.get("pairs_scored", 0)
                record["scorer_batches"] = list(getattr(scorer, "last_batches", []))
                record["source_discovery10"] = _rank_metrics(source_ids[:10], targets, negatives)
                record["source_discovery20"] = _rank_metrics(source_ids[:20], targets, negatives)
                record["cross_encoder10"] = _rank_metrics(ids_for(reranked)[:10], targets, negatives)
                record["cross_encoder20"] = _rank_metrics(ids_for(reranked)[:20], targets, negatives)
                record["cross_encoder_candidates"] = [{"arxiv_id": c["arxiv_id"],
                    "semantic_logit": c.get("semantic_logit"), "semantic_rank": c.get("semantic_rank"),
                    "verification_required": True, "retrieval_role": "unverified_source_candidate"}
                    for c in reranked]
                record["timing_ms"]["rerank"] = round(rerank_elapsed, 2)
                record["pool_partial"] = bool(partial)
                record["baseline_complete"] = not record["partial"]
                record["source_complete"] = not record["pool_partial"] and not record["partial"]
                record["ce_complete"] = report["status"] == "reranked" and not record["pool_partial"] and not record["partial"]
                record["complete"] = record["baseline_complete"] and record["source_complete"] and record["ce_complete"]
                if record["partial"] or record["pool_partial"]:
                    record["error"] = "partial_search_pool"
            except BaseException as exc:
                record["error"] = type(exc).__name__
                record["complete"] = False
                if isinstance(exc, helpers._QueryTimeout):
                    output["runner_operational"] = False
                    output["stop_reason"] = "per_query_timeout"
            output["records"].append(record)
            if time.monotonic() >= deadline:
                output["stop_reason"] = "wall_budget_exhausted"
            if not record["complete"] and output.get("stop_reason"):
                break
        done = len(output["records"])
        output["skipped"] = [{"index": i, "query": row["query"], "complete": False,
                              "reason": output.get("stop_reason", "not_run")}
                             for i, row in enumerate(rows[done:], done)]
        for i, row in enumerate(all_rows):
            key = (row["fixture"], row["group_id"], row["phrase_id"])
            if key not in selected_keys:
                output["skipped"].append({"index": i, "fixture": row["fixture"],
                    "group_id": row["group_id"], "phrase_id": row["phrase_id"],
                    "reason": "not_selected_by_max_queries"})
        output["config"]["scorer_disabled_after_failure"] = scorer_disabled
        output["summary"] = {"attempted_queries": len(output["records"]),
                             "expected_queries": len(all_rows), "selected_queries": len(rows),
                             "skipped_queries": len(output["skipped"]),
                             "complete_queries": sum(r.get("complete", False) for r in output["records"]),
                             "baseline_complete_queries": sum(r.get("baseline_complete", False) for r in output["records"]),
                             "source_complete_queries": sum(r.get("source_complete", False) for r in output["records"]),
                             "ce_complete_queries": sum(r.get("ce_complete", False) for r in output["records"]),
                             "paired_ce_complete": sum(r.get("baseline_complete") and r.get("source_complete")
                                                        and r.get("ce_complete") for r in output["records"]),
                             "phrase_group_success": _phrase_group_summary(groups, output["records"])}
    except BaseException as exc:
        output["runner_error"] = type(exc).__name__
        output["runner_operational"] = False
    finally:
        if app is not None:
            if "old_hier" in locals():
                app._hier_run = old_hier
            if "old_embed" in locals():
                app.embed_query, app.embed_query_coarse = old_embed, old_coarse
            for name, value in locals().get("old_caches", {}).items():
                setattr(app, name, value)
            for name, value in locals().get("old_switches", {}).items():
                setattr(app, name, value)
        for key, value in env_original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        output["elapsed_seconds"] = round(time.monotonic() - started, 3)
        Path(args.output).write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str) + "\n",
                                     encoding="utf-8")
    return output


def main_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-dir", required=True, type=Path)
    parser.add_argument("--fixtures", nargs="+", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--small-queries", type=Path, default=DEFAULT_SMALL)
    parser.add_argument("--base-queries", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--vectors", type=Path,
                        help="combined cached vectors JSON: {small:{query:vec}, base:{query:vec}}")
    parser.add_argument("--expected-main-sha256", default=EXPECTED_MAIN_SHA256)
    parser.add_argument("--rerank-url", default="http://127.0.0.1:8006/rerank")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-queries", type=int, default=DEFAULT_MAX_QUERIES)
    parser.add_argument("--budget-seconds", type=float, default=MAX_BUDGET_SECONDS)
    parser.add_argument("--per-query-seconds", type=float, default=MAX_QUERY_SECONDS)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(EVAL))
    output = run(args)
    return 0 if output["runner_operational"] else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
