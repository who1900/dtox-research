"""Read-only, in-process shadow probe for SPLADE Stage-A append candidates."""
import argparse
import hashlib
import json
import math
import os
import signal
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path.home() / "Documents/work/Claude/.scratch_dtox/splade-stage-a-20261003"
DEFAULT_COMPARISON = SCRATCH / "comparison.json"
DEFAULT_CORPUS = SCRATCH / "corpus.jsonl"
DEFAULT_SMALL_QUERIES = Path.home() / "Documents/work/Claude/.scratch_dtox/splade-20261003/queries-final.json"
DEFAULT_ORIGINAL = ROOT / "eval/paraphrase_cases_20261003.json"
DEFAULT_HOLDOUT = ROOT / "eval/paraphrase_holdout_20261003.json"
MAX_SPLADE_RANK = 40
APPEND_LIMIT = 10


def _fixture_queries(paths):
    found = []
    for path in paths:
        fixture = json.loads(Path(path).read_text(encoding="utf-8"))
        for group in fixture["groups"]:
            for phrase in group["phrases"]:
                found.append({"fixture": Path(path).name, "group_id": group["id"],
                              "phrase_id": phrase["id"], "layer": group["layer"],
                              "query": phrase["query"]})
    if len(found) != 33 or len({row["query"] for row in found}) != 33:
        raise ValueError(f"expected exactly 33 unique fixture queries; got {len(found)}")
    return found


def _load_query_vectors(path, expected, dimension, label):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data = data.get("queries", data) if isinstance(data, dict) else data
    if isinstance(data, list):
        data = {row["query"]: row.get("vector", row.get("query_vector")) for row in data}
    if not isinstance(data, dict):
        raise ValueError(f"{label} query vectors must be a JSON map or record list")
    if set(data) != set(expected):
        raise ValueError(f"{label} query keys mismatch: missing={len(set(expected)-set(data))}, extra={len(set(data)-set(expected))}")
    vectors = {}
    for query in expected:
        vector = data[query]
        if not isinstance(vector, list) or len(vector) != dimension:
            raise ValueError(f"{label} vector must have dimension {dimension}: {query!r}")
        values = [float(item) for item in vector]
        if not all(math.isfinite(item) for item in values):
            raise ValueError(f"{label} vector contains non-finite values: {query!r}")
        vectors[query] = values
    return vectors


def _corpus_aliases(path):
    canonical_to_source = {}
    canonical_layers = {}
    aliases = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            canonical = str(row.get("paper_id") or row.get("canonical_id") or row.get("arxiv_id") or "").strip().lower()
            source = str(row.get("arxiv_id") or canonical).strip()
            if not canonical or not source:
                continue
            aliases.setdefault(canonical, set()).update((canonical, source.lower()))
            for key in ("aliases", "canonical_aliases", "source_aliases"):
                value = row.get(key) or []
                aliases[canonical].update(str(item).strip().lower() for item in (value if isinstance(value, list) else [value]) if str(item).strip())
            canonical_to_source.setdefault(canonical, source)
            canonical_layers.setdefault(canonical, set()).update(_layers(row.get("layers")))
    alias_to_canonical = {alias: canonical for canonical, values in aliases.items() for alias in values}
    return canonical_to_source, alias_to_canonical, canonical_layers


def _layers(value):
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {item.strip().strip("\"'") for item in value.strip("[]").replace("|", ",").split(",") if item.strip().strip("\"'")}
    return set()


def append_splade_candidates(original, papers, query, layer, year_from, year_to,
                             limit=15, lexical_future=None, *, ranked_ids=(),
                             source_by_canonical=None, alias_to_canonical=None,
                             canonical_layers=None, append_limit=APPEND_LIMIT):
    """Faithfully call production lexical logic, then append up to ten eligible SPLADE-only ids."""
    papers, lexical_ids = original(papers, query, layer, year_from, year_to,
                                   limit=limit, lexical_future=lexical_future)
    papers = list(papers)
    original_prefix = [dict(row) for row in papers]
    original_raw_ids = {str(row["arxiv_id"]).lower() for row in papers}
    lexical_snapshot = list(lexical_ids)
    source_by_canonical = source_by_canonical or {}
    alias_to_canonical = alias_to_canonical or {}
    canonical_layers = canonical_layers or {}
    original_ids = original_raw_ids | {alias_to_canonical.get(aid, aid) for aid in original_raw_ids}
    floor = min((row["paper_score"] for row in papers), default=0.0)
    appended = []
    seen = set(original_ids)
    for splade_id in list(ranked_ids)[:MAX_SPLADE_RANK] if append_limit else ():
        canonical = alias_to_canonical.get(str(splade_id).lower(), str(splade_id).lower())
        source = source_by_canonical.get(canonical)
        if not source or canonical in seen:
            continue
        if layer and layer not in canonical_layers.get(canonical, set()):
            continue
        appended.append({"arxiv_id": source, "paper_score": floor})
        seen.add(canonical)
        seen.add(source.lower())
        if len(appended) == append_limit:
            break
    papers.extend(appended)
    assert papers[:len(original_prefix)] == original_prefix
    assert lexical_ids == lexical_snapshot
    return papers, lexical_ids, [row["arxiv_id"] for row in appended]


def _disable_caches(app):
    class NullCache:
        def get(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    for name in ("search_cache", "embedding_cache", "coarse_embedding_cache",
                 "qdrant_result_cache"):
        if hasattr(app, name):
            setattr(app, name, NullCache())


class _QueryTimeout(BaseException):
    pass


def _run_with_timeout(call, timeout):
    if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGALRM)
        def alarm_handler(_signum, _frame):
            raise _QueryTimeout("search exceeded per-query timeout")
        signal.signal(signal.SIGALRM, alarm_handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return call()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    done = threading.Event()
    result = {}
    def run():
        try:
            result["value"] = call()
        except BaseException as error:
            result["error"] = error
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()
    if not done.wait(timeout):
        raise _QueryTimeout("search exceeded per-query timeout; stopping runner")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _set_env(api_dir):
    config = {
        "QDRANT_URL": "http://127.0.0.1:16335",
        "COARSE_EMBED_URL": "http://127.0.0.1:16337/embed",
        "EMBED_URL": "http://127.0.0.1:8006/embed",
        "QDRANT_COLLECTION": "papers_fulltext",
        "COARSE_COLLECTION": "papers_coarse_base",
        "PAPER_FTS_PATH": "/opt/dtox-research/papers_fts.db",
        "STATE_DB_PATH": "/opt/dtox-research/state.db",
        "API_KEYS_PATH": "/__codex_probe_no_api_keys__",
        "ATTESTOR_INTERNAL_TOKEN": "",
        "EXACT_MATCH": "1",
        "PAPER_LEXICAL_SOURCE_BALANCE": "1",
        "RERANK_ENABLED": "0",
        "HIER_GROUPED_SEARCH": "0",
        "HIER_RANK_CHANNELS": "0",
        "HIER_SEARCH": "1",
        "HIER_GRAPH": "1",
        "PAPER_LEXICAL": "1",
        "HIER_FUSION": "rrf",
        "HIER_W_DENSE": "2",
        "HIER_W_LEX": "0.8",
        "HIER_W_GLOBAL": "1",
    }
    os.environ.update(config)
    return config


def _run_pair(app, query, layer, ranked_splade, source_map, alias_map, layers, append_limit):
    original = app._lexical_paper_shortlist
    call_log = []
    def wrapped(papers, q, q_layer, year_from, year_to, limit=15, lexical_future=None):
        if q != query or q_layer != layer:
            return original(papers, q, q_layer, year_from, year_to,
                            limit=limit, lexical_future=lexical_future)
        updated, lexical_ids, added = append_splade_candidates(
            original, papers, q, q_layer, year_from, year_to, limit, lexical_future,
            ranked_ids=ranked_splade[q], source_by_canonical=source_map,
            alias_to_canonical=alias_map, canonical_layers=layers,
            append_limit=append_limit)
        call_log.append({"query": q, "layer": q_layer, "appended_ids": added,
                         "lexical_ids": list(lexical_ids),
                         "paper_ids_after": [row["arxiv_id"] for row in updated]})
        return updated, lexical_ids
    return wrapped, call_log


def _install_query_vectors(app, small, base):
    """Install bge-small (384) for chunks and bge-base (768) for coarse papers."""
    app.embed_query = lambda query, *a, **kw: small[query]
    app.embed_query_coarse = lambda query, *a, **kw: base[query]


def main_cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-dir", required=True, type=Path)
    parser.add_argument("--small-queries", type=Path, default=DEFAULT_SMALL_QUERIES)
    parser.add_argument("--base-queries", type=Path, default=SCRATCH / "queries-base.json")
    parser.add_argument("--comparison", type=Path, default=DEFAULT_COMPARISON)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--original", type=Path, default=DEFAULT_ORIGINAL)
    parser.add_argument("--holdout", type=Path, default=DEFAULT_HOLDOUT)
    parser.add_argument("--budget-seconds", type=float, default=180)
    parser.add_argument("--per-query-seconds", type=float, default=12)
    parser.add_argument("--append-limit", type=int, default=APPEND_LIMIT)
    parser.add_argument("--output", type=Path, required=True,
                        help="local checkpoint JSON; rewritten after every completed pair")
    args = parser.parse_args(argv)
    started = time.monotonic()
    budget = min(max(args.budget_seconds, 0), 180)
    if not 0 <= args.append_limit <= APPEND_LIMIT:
        parser.error("--append-limit must be between 0 and 10")
    if args.per_query_seconds <= 0:
        parser.error("--per-query-seconds must be positive")
    fixtures = _fixture_queries((args.original, args.holdout))
    queries = [item["query"] for item in fixtures]
    small = _load_query_vectors(args.small_queries, queries, 384, "small")
    base = _load_query_vectors(args.base_queries, queries, 768, "base")
    comparison = json.loads(args.comparison.read_text(encoding="utf-8"))
    ranked = {}
    for item in comparison["phrases"]:
        key = (item["fixture"], item["group_id"], item["phrase_id"])
        ranked[key] = item["ranked_ids"]["splade"][:MAX_SPLADE_RANK]
    for item in fixtures:
        item["splade_ranked_ids"] = ranked[(item["fixture"], item["group_id"], item["phrase_id"])]
    source_map, alias_map, corpus_layers = _corpus_aliases(args.corpus)
    output = {"schema_version": 1, "denominator": 33, "records": [], "skipped": [],
              "runner_error": None, "runner_operational": True,
              "config": {"api_dir": str(args.api_dir.resolve()),
              "budget_seconds": budget, "append_limit": args.append_limit,
              "per_query_seconds": args.per_query_seconds,
              "QDRANT_URL": "http://127.0.0.1:16335",
              "QDRANT_COLLECTION": "papers_fulltext",
              "COARSE_EMBED_URL": "http://127.0.0.1:16337/embed",
              "EMBED_URL": "http://127.0.0.1:8006/embed",
              "COARSE_COLLECTION": "papers_coarse_base",
              "PAPER_FTS_PATH": "/opt/dtox-research/papers_fts.db",
              "STATE_DB_PATH": "/opt/dtox-research/state.db",
              "API_KEYS_PATH": "<nonexistent-dummy-path>",
              "small_queries_path": str(args.small_queries),
              "base_queries_path": str(args.base_queries),
              "comparison_path": str(args.comparison),
              "corpus_path": str(args.corpus),
              "original_fixture_path": str(args.original),
              "holdout_fixture_path": str(args.holdout),
              "EXACT_MATCH": "1", "PAPER_LEXICAL_SOURCE_BALANCE": "1",
              "RERANK_ENABLED": "0", "HIER_GRAPH": "1",
              "HIER_GROUPED_SEARCH": "0", "HIER_RANK_CHANNELS": "0",
              "HIER_SEARCH": "1", "PAPER_LEXICAL": "1",
              "HIER_FUSION": "rrf", "HIER_W_DENSE": "2",
              "HIER_W_LEX": "0.8", "HIER_W_GLOBAL": "1",
              "splade_examined_rank_limit": MAX_SPLADE_RANK,
              "comparison_sha256": hashlib.sha256(args.comparison.read_bytes()).hexdigest(),
              "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest()}}
    _set_env(args.api_dir)
    try:
        if not (args.api_dir / "main.py").is_file():
            raise FileNotFoundError(f"main.py not found under --api-dir: {args.api_dir}")
        actual_hash = hashlib.sha256((args.api_dir / "main.py").read_bytes()).hexdigest()
        expected_hash = "bd38ed430a1ced3167ff4eef7c075e4bd5d662c87c4dfd62d3589c74a8387d19"
        output["config"]["main_sha256"] = actual_hash
        if actual_hash != expected_hash:
            raise ValueError(f"selected main.py hash mismatch: expected {expected_hash}, got {actual_hash}")
        api_path = str(args.api_dir.resolve())
        if api_path not in sys.path:
            sys.path.insert(0, api_path)
        import main as app
        _disable_caches(app)
        original_lexical = app._lexical_paper_shortlist
        for index, item in enumerate(fixtures):
            if time.monotonic() - started >= budget:
                output["skipped"].extend({"index": i, "query": rest["query"], "reason": "budget_exhausted"}
                                        for i, rest in enumerate(fixtures[index:], index))
                break
            record = {**{k: item[k] for k in ("fixture", "group_id", "phrase_id", "query", "layer")},
                      "order": [], "baseline": None, "treatment": None, "error": None}
            for variant in (("baseline", "treatment") if index % 2 == 0 else ("treatment", "baseline")):
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    record["error"] = "budget_exhausted_between_pair"
                    output["skipped"].append({"index": index, "query": item["query"],
                                              "reason": "budget_exhausted_between_pair"})
                    break
                record["order"].append(variant)
                _install_query_vectors(app, small, base)
                app._lexical_paper_shortlist = original_lexical
                call_log = []
                if variant == "treatment":
                    app._lexical_paper_shortlist, call_log = _run_pair(
                        app, item["query"], item["layer"],
                        {item["query"]: item["splade_ranked_ids"]}, source_map,
                        alias_map, corpus_layers, args.append_limit)
                body = app.SearchBody(query=item["query"], layer=item["layer"], limit=10,
                                      compact=False, hybrid=True)
                t0 = time.monotonic()
                try:
                    timeout = min(args.per_query_seconds, remaining)
                    try:
                        payload = _run_with_timeout(
                            lambda: app._run_search(body, dense_timeout=timeout,
                                                    embed_wait_timeout=timeout,
                                                    embed_http_timeout=timeout), timeout)
                    finally:
                        app._lexical_paper_shortlist = original_lexical
                    record[variant] = {"raw_payload": payload, "results": payload.get("results", []),
                                       "latency_ms": round(1000*(time.monotonic()-t0), 2),
                                       "added_ids": list(dict.fromkeys(
                                           aid for event in call_log for aid in event["appended_ids"])),
                                       "lexical_calls": call_log,
                                       "partial": bool(payload.get("partial"))}
                except BaseException as exc:
                    record[variant] = {"raw_payload": None, "results": [], "latency_ms": round(1000*(time.monotonic()-t0), 2),
                                       "added_ids": list(dict.fromkeys(
                                           aid for event in call_log for aid in event["appended_ids"])),
                                       "lexical_calls": call_log,
                                       "error": f"{type(exc).__name__}: {exc}", "partial": True}
                    app._lexical_paper_shortlist = original_lexical
                    if isinstance(exc, _QueryTimeout):
                        record["runner_stop"] = True
                        output["runner_operational"] = False
                        output["skipped"].extend({"index": i, "query": rest["query"],
                                                 "reason": "query_timeout_runner_stopped"}
                                                for i, rest in enumerate(fixtures[index+1:], index+1))
                        break
            # explicit inclusion marker against baseline top ten
            baseline_ids = {r.get("arxiv_id") for r in (record.get("baseline") or {}).get("results", [])}
            selected_ids = list((record.get("treatment") or {}).get("added_ids", []))
            for variant in ("baseline", "treatment"):
                current = record.get(variant) or {}
                ids = [r.get("arxiv_id") for r in current.get("results", [])]
                current["ids"] = ids
                current["added_ids"] = selected_ids if variant == "baseline" else current.get("added_ids", [])
                current["added_ids_in_results"] = [aid for aid in selected_ids if aid in ids]
                current["kept"] = {aid: aid in ids for aid in selected_ids}
                current["baseline_ids"] = sorted(baseline_ids) if variant == "treatment" else None
            output["records"].append(record)
            _write_checkpoint(args.output, output)
            print(f"checkpoint pairs={len(output['records'])}/33 skipped={len(output['skipped'])}",
                  file=sys.stderr, flush=True)
            if record.get("runner_stop"):
                break
    except BaseException as exc:
        output["runner_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        output["elapsed_seconds"] = round(time.monotonic()-started, 3)
        completed_indices = {i for i, _record in enumerate(output["records"])}
        already_skipped = {item["index"] for item in output["skipped"]}
        output["skipped"].extend({"index": i, "query": item["query"], "reason": "runner_terminated"}
                                for i, item in enumerate(fixtures)
                                if i not in completed_indices and i not in already_skipped)
        output["completed_pairs"] = len(output["records"])
        _write_checkpoint(args.output, output)
        print(f"result={args.output} pairs={output['completed_pairs']}/33 skipped={len(output['skipped'])} "
              f"error={output['runner_error']}", file=sys.stderr, flush=True)


def _write_checkpoint(path, output):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, separators=(",", ":"), default=str) + "\n",
                    encoding="utf-8")


if __name__ == "__main__":
    main_cli()
