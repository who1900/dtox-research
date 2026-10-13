"""Diagnostic wrapper for observing production candidate flow in the SPLADE probe."""
import hashlib
import inspect
import json
import os
import sys
import threading
import uuid

try:
    from eval import splade_candidate_probe as probe
except ImportError:
    import splade_candidate_probe as probe


TRACE_VERSION = 1
MAX_CAPTURE = 500
_TRACE_LOCK = threading.Lock()
_EVENTS = []
_ACTIVE_QUERIES = {}
_LOCAL = threading.local()

_CONFIG_NAMES = (
    "HIER_CHUNK_LEXICAL", "HIER_SEARCH", "HIER_CHUNKS",
    "MIN_RELEVANCE_SCORE", "MIN_RELEVANCE_BY_LAYER", "HIER_FUSION",
    "HIER_W_DENSE", "HIER_W_LEX", "HIER_W_GLOBAL", "HIER_GRAPH",
    "HIER_GROUPED_SEARCH", "HIER_RANK_CHANNELS", "BOILERPLATE_FILTER",
    "SOFT_LAYERS",
)


def _safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(v) for v in value]
    return str(value)


def _bounded(items, compact):
    items = list(items or [])
    captured = [compact(item) for item in items[:MAX_CAPTURE]]
    return {"items": captured, "total": len(items), "truncated": len(items) > MAX_CAPTURE}


def _hit(hit, app):
    payload = hit.get("payload") or {}
    try:
        boilerplate = bool(app._hit_is_boilerplate(hit))
    except Exception:
        boilerplate = None
    return {
        "chunk_id": hit.get("id"), "arxiv_id": payload.get("arxiv_id"),
        "score": hit.get("score"), "section_type": payload.get("section_type"),
        "title": payload.get("title"), "boilerplate": boilerplate,
    }


def _result(result):
    return {key: result.get(key) for key in ("arxiv_id", "score", "section_type", "title")}


def _ids(results):
    return [row.get("arxiv_id") for row in (results or [])]


def _log(event):
    trace_id = getattr(_LOCAL, "trace_id", None)
    if trace_id is None:
        return
    row = {
        "trace_id": trace_id, "query": getattr(_LOCAL, "query", None),
        "layer": getattr(_LOCAL, "layer", None), "thread": threading.current_thread().name,
        **event,
    }
    with _TRACE_LOCK:
        _EVENTS.append(row)


def _instrument(app):
    config = {name: _safe(getattr(app, name)) for name in _CONFIG_NAMES
              if hasattr(app, name)}
    wrappers = {}

    original_run = app._run_search

    def run_search(body, *args, **kwargs):
        is_top = kwargs.get("_top", args[4] if len(args) > 4 else True)
        query = (getattr(body, "query", "") or "").strip()
        layer = getattr(body, "layer", None)
        previous = (getattr(_LOCAL, "trace_id", None), getattr(_LOCAL, "query", None),
                    getattr(_LOCAL, "layer", None))
        owner = bool(is_top and previous[0] is None)
        trace_id = previous[0]
        if owner:
            trace_id = uuid.uuid4().hex
            with _TRACE_LOCK:
                _ACTIVE_QUERIES[query] = trace_id
        elif trace_id is None:
            with _TRACE_LOCK:
                trace_id = _ACTIVE_QUERIES.get(query)
        _LOCAL.trace_id, _LOCAL.query, _LOCAL.layer = trace_id, query, layer
        if trace_id is not None:
            _log({"stage": "run_search", "top": bool(is_top), "phase": "start"})
        try:
            payload = original_run(body, *args, **kwargs)
        except BaseException as exc:
            if trace_id is not None:
                _log({"stage": "run_search", "top": bool(is_top), "error": type(exc).__name__})
            raise
        finally:
            if owner:
                with _TRACE_LOCK:
                    events = [dict(row) for row in _EVENTS if row["trace_id"] == trace_id]
                    _EVENTS[:] = [row for row in _EVENTS if row["trace_id"] != trace_id]
                    _ACTIVE_QUERIES.pop(query, None)
            else:
                events = None
            _LOCAL.trace_id, _LOCAL.query, _LOCAL.layer = previous
        if owner and isinstance(payload, dict):
            payload = dict(payload)
            payload["diagnostic_trace"] = {"version": TRACE_VERSION, "config": config,
                                           "events": events or []}
        return payload

    app._run_search = run_search
    wrappers["_run_search"] = original_run

    original_hier = app._hier_run
    signature = inspect.signature(original_hier)

    def hier_run(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs)
        query = bound.arguments.get("query", "")
        body = bound.arguments.get("body")
        previous = (getattr(_LOCAL, "query", None), getattr(_LOCAL, "layer", None))
        _LOCAL.query = query or previous[0]
        _LOCAL.layer = getattr(body, "layer", None) if body is not None else previous[1]
        try:
            value = original_hier(*args, **kwargs)
        except BaseException as exc:
            _log({"stage": "hier_run", "error": type(exc).__name__})
            raise
        finally:
            _LOCAL.query, _LOCAL.layer = previous
        hits, _leftover, _partial, extra = value
        extra = extra or {}
        _log({"stage": "hier_run", "ordered_hits": _bounded(hits, lambda h: _hit(h, app)),
              "paper_scores": _safe(extra.get("paper_scores")),
              "fusion_ranks": _safe(extra.get("fusion_ranks")),
              "fusion_scores": _safe(extra.get("fusion_scores")),
              "lex_exempt": _safe(extra.get("lex_exempt"))})
        return value

    app._hier_run = hier_run
    wrappers["_hier_run"] = original_hier

    original_b = app._hier_stage_b_hits

    def stage_b(vector, must, arxiv_ids, dense_limit, timeout):
        try:
            hits = original_b(vector, must, arxiv_ids, dense_limit, timeout)
        except BaseException as exc:
            _log({"stage": "stage_b", "input_arxiv_ids": _bounded(arxiv_ids, _safe),
                  "must": _safe(must), "limit": dense_limit, "error": type(exc).__name__})
            raise
        _log({"stage": "stage_b", "input_arxiv_ids": _bounded(arxiv_ids, _safe),
              "must": _safe(must), "limit": dense_limit,
              "output_hits": _bounded(hits, lambda h: _hit(h, app))})
        return hits

    app._hier_stage_b_hits = stage_b
    wrappers["_hier_stage_b_hits"] = original_b

    original_dedupe = app._dedupe_preferring_content

    def dedupe(candidate_hits, floor, lex_exempt_ids, limit):
        result = original_dedupe(candidate_hits, floor, lex_exempt_ids, limit)
        _log({"stage": "dedupe_preferring_content",
              "before": _bounded(candidate_hits, lambda h: _hit(h, app)),
              "floor": floor, "lex_exempt": _safe(lex_exempt_ids), "limit": limit,
              "after": _bounded(result, _result)})
        return result

    app._dedupe_preferring_content = dedupe
    wrappers["_dedupe_preferring_content"] = original_dedupe

    original_collapse = app._collapse_twins

    def collapse(results):
        before = _ids(results)
        value = original_collapse(results)
        _log({"stage": "collapse_twins", "before_ids": _bounded(before, _safe),
              "after_ids": _bounded(_ids(value), _safe)})
        return value

    app._collapse_twins = collapse
    wrappers["_collapse_twins"] = original_collapse

    original_cap = app._cap_gh_docs

    def cap(results, query, limit):
        before = _ids(results)
        value = original_cap(results, query, limit)
        _log({"stage": "cap_gh_docs", "before_ids": _bounded(before, _safe),
              "after_ids": _bounded(_ids(value[0]), _safe)})
        return value

    app._cap_gh_docs = cap
    wrappers["_cap_gh_docs"] = original_cap

    original_exact = app._inject_exact

    def inject(results, query, constraints, limit, timeout):
        before = _ids(results)
        value = original_exact(results, query, constraints, limit, timeout)
        _log({"stage": "inject_exact", "before_ids": _bounded(before, _safe),
              "after_ids": _bounded(_ids(value), _safe)})
        return value

    app._inject_exact = inject
    wrappers["_inject_exact"] = original_exact

    original_merge = app._merge_soft_layers

    def merge(primary, partner_payload, limit):
        before = {"primary": _ids(primary.get("results", [])),
                  "partner": _ids(partner_payload.get("results", []))}
        value = original_merge(primary, partner_payload, limit)
        _log({"stage": "merge_soft_layers", "before_ids": before,
              "after_ids": _bounded(_ids(value), _safe)})
        return value

    app._merge_soft_layers = merge
    wrappers["_merge_soft_layers"] = original_merge
    return config, wrappers


def _set_env(api_dir, original_set_env=None):
    config = (original_set_env or probe._set_env)(api_dir)
    os.environ["HIER_CHUNK_LEXICAL"] = "0"
    return config


def main_cli(argv=None):
    original_set_env = probe._set_env

    original_disable = probe._disable_caches
    state = {}

    def disable_and_instrument(app):
        original_disable(app)
        state["config"], state["wrappers"] = _instrument(app)

    original_checkpoint = probe._write_checkpoint

    def write_checkpoint(path, output):
        output.setdefault("config", {})["HIER_CHUNK_LEXICAL"] = "0"
        output["diagnostic_trace_version"] = TRACE_VERSION
        original_checkpoint(path, output)

    probe._set_env = lambda api_dir: _set_env(api_dir, original_set_env)
    probe._disable_caches = disable_and_instrument
    probe._write_checkpoint = write_checkpoint
    try:
        probe.main_cli(argv)
    finally:
        probe._set_env = original_set_env
        probe._disable_caches = original_disable
        probe._write_checkpoint = original_checkpoint

    checkpoint_parser = probe.argparse.ArgumentParser(add_help=False)
    checkpoint_parser.add_argument("--output", type=probe.Path)
    parsed, _unknown = checkpoint_parser.parse_known_args(
        list(argv) if argv is not None else sys.argv[1:])
    if parsed.output:
        path = parsed.output
        if path.is_file():
            output = json.loads(path.read_text(encoding="utf-8"))
            output.setdefault("config", {})["HIER_CHUNK_LEXICAL"] = "0"
            output["diagnostic_trace_version"] = TRACE_VERSION
            probe._write_checkpoint(path, output)


if __name__ == "__main__":
    main_cli()
