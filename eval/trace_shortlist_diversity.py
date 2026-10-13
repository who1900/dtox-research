"""Read-only observer; run in an isolated candidate process, never a live server.

Usage with the existing runtime configuration:
    python -m eval.trace_shortlist_diversity --fixture eval/paraphrase_release_20261004.json
The JSON report is written to stdout. No flags, models, fixtures or index writes.
"""
import argparse
import copy
import hashlib
import json
import math
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from eval.paraphrase_eval import queries_from_fixture, score


_LOCK = threading.Lock()


class _NoSearchCache:
    def get(self, *_args):
        return None

    def set(self, *_args):
        pass


def _hits(hits):
    return [{"arxiv_id": (hit.get("payload") or {}).get("arxiv_id"),
             "score": hit.get("score"), "stage_b_origin": hit.get("stage_b_origin", "pooled")}
            for hit in hits]


def trace_search(app, body, **search_kwargs):
    """Observe the actual flow without extra per-anchor retrieval or flag changes."""
    passes, local = [], threading.local()
    original = {name: getattr(app, name) for name in (
        "_coarse_paper_search", "_lexical_paper_shortlist", "_hier_stage_b_hits", "_hier_run")}

    def coarse(*args, **kwargs):
        papers = original["_coarse_paper_search"](*args, **kwargs)
        if getattr(local, "current", None) is not None:
            local.current["stage_a_dense"] = copy.deepcopy(papers)
            local.current["stage_a_shortlist"] = copy.deepcopy(papers)
        return papers

    def lexical(*args, **kwargs):
        papers, ids = original["_lexical_paper_shortlist"](*args, **kwargs)
        if getattr(local, "current", None) is not None:
            local.current["stage_a_shortlist"] = copy.deepcopy(papers)
            local.current["stage_a_lexical_ids"] = list(ids)
        return papers, ids

    def stage_b(*args, **kwargs):
        hits = original["_hier_stage_b_hits"](*args, **kwargs)
        if getattr(local, "current", None) is not None:
            local.current["stage_b_calls"].append({
                "requested_ids": list(args[2]), "chunk_budget": args[3],
                "hits": _hits(hits), "partial": list(getattr(hits, "partial", [])),
                "diversity": copy.deepcopy(getattr(hits, "trace", None))})
        return hits

    def hierarchy(*args, **kwargs):
        request = args[3]
        current = {"layer": request.layer, "strict": request.strict,
                   "stage_a_dense": [], "stage_a_shortlist": [], "stage_b_calls": []}
        previous = getattr(local, "current", None)
        local.current = current
        try:
            hits, leftovers, partial, extra = original["_hier_run"](*args, **kwargs)
            current.update({"hier_hits": _hits(hits), "partial": list(partial),
                            "fallback": extra is None,
                            "lex_exempt": sorted((extra or {}).get("lex_exempt", [])),
                            "fusion_ranks": copy.deepcopy((extra or {}).get("fusion_ranks", {}))})
            return hits, leftovers, partial, extra
        finally:
            local.current = previous
            passes.append(current)

    config = {name: getattr(app, name) for name in (
        "HIER_SEARCH", "HIER_FUSION", "HIER_PAPERS", "HIER_CHUNKS", "HIER_LEXICAL_PAPERS",
        "HIER_GROUPED_SEARCH", "HIER_RANK_CHANNELS", "HIER_CHUNK_LEXICAL", "PAPER_LEXICAL",
        "HIER_W_DENSE", "HIER_W_LEX", "HIER_W_GLOBAL", "HIER_GRAPH", "SOURCE_DISCOVERY")}
    with _LOCK, ExitStack() as stack:
        stack.enter_context(patch.object(app, "search_cache", _NoSearchCache()))
        for name, wrapper in (("_coarse_paper_search", coarse), ("_lexical_paper_shortlist", lexical),
                              ("_hier_stage_b_hits", stage_b), ("_hier_run", hierarchy)):
            stack.enter_context(patch.object(app, name, wrapper))
        try:
            payload = app._run_search(body, **search_kwargs)
            error = None
        except Exception as exc:
            payload = {"results": None, "partial": True}
            error = type(exc).__name__
    results = payload.get("results") or []
    primary = {app.canonical_id(row.get("arxiv_id")) for row in results}
    candidates = {app.canonical_id(row.get("arxiv_id"))
                  for row in payload.get("source_candidates", [])}
    for item in passes:
        default = (app.MIN_RELEVANCE_BY_LAYER.get(item["layer"], app.MIN_RELEVANCE_SCORE)
                   if item["layer"] else min([app.MIN_RELEVANCE_SCORE,
                                             *app.MIN_RELEVANCE_BY_LAYER.values()]))
        floor = default if body.min_score is None else (
            body.min_score if body.strict else min(body.min_score, default + app.MIN_SCORE_MAX_RAISE))
        exemptions = set() if item["strict"] else set(item.get("lex_exempt", []))
        rows = {}
        for entry in item["stage_a_shortlist"]:
            aid = entry["arxiv_id"]
            rows.setdefault(app.canonical_id(aid), {"arxiv_id": aid, "stage_a": True,
                                                   "paper_score": entry["paper_score"]})
        for hit in item.get("hier_hits", []):
            aid = hit["arxiv_id"]
            row = rows.setdefault(app.canonical_id(aid), {"arxiv_id": aid, "stage_a": False})
            value = hit["score"]
            finite = (isinstance(value, (int, float)) and not isinstance(value, bool)
                      and math.isfinite(value))
            if finite and (row.get("chunk_score") is None or value > row["chunk_score"]):
                row.update({"chunk_score": value, "passes_chunk_floor": value >= floor,
                            "lexical_exemption": aid in exemptions,
                            "passage_qualified": value >= floor or aid in exemptions})
        for key, row in rows.items():
            row.update({"primary_returned": key in primary, "candidate_returned": key in candidates,
                        "relevance_floor": floor, "verification_required": True})
            row["loss_stage"] = ("returned" if key in primary else
                                 "fallback_unobserved" if item.get("fallback") else
                                 "chunk_not_observed" if "chunk_score" not in row else
                                 "passage_floor" if not row["passage_qualified"] else
                                 "post_qualification_not_returned")
        item["sources"] = list(rows.values())
    return {"payload": payload, "error": error,
            "trace": {"version": "shortlist-diversity-v1", "config": config, "passes": passes,
                      "scope": "internal runtime candidate, not public MCP proof",
                      "note": "Discovery rank/cosine is not relevance labelling or scientific proof."}}


def anchor_paths(group, trace, canonical_id):
    paths = []
    for anchor in group["targets"]:
        ids = {canonical_id(aid) for aid in [anchor["canonical_id"], *anchor.get("aliases", [])]}
        observations = [row for item in trace["passes"] for row in item.get("sources", [])
                        if canonical_id(row["arxiv_id"]) in ids]
        paths.append({"canonical_id": anchor["canonical_id"], "observations": observations,
                      "stage_a_observed": any(row["stage_a"] for row in observations),
                      "index_coverage": "not_measured",
                      "absence_note": "No observation is not evidence that the source is unindexed."})
    return paths


def run_fixture(app, fixture, budget_seconds=180):
    prereg = fixture["preregistration"]
    queries = queries_from_fixture(fixture)
    if (prereg.get("status") != "frozen_before_live" or len(queries) != prereg["query_count"]
            or len(queries) > 24 or not 0 < budget_seconds <= 180):
        raise ValueError("require a frozen, bounded fixture (at most 24 queries / 180 seconds)")
    groups = {group["id"]: group for group in fixture["groups"]}
    records, started = [], time.monotonic()
    for item in queries:
        remaining = budget_seconds - (time.monotonic() - started)
        if remaining <= 0:
            records.append({**item, "results": None, "partial": True, "error": "run budget exceeded"})
            continue
        body = app.SearchBody(query=item["query"], layer=item["layer"],
                              limit=prereg["retrieval_limit"], strict=prereg["strict"],
                              hybrid=True, dedupe=True, compact=False, diagnose=False, auto_relax=False)
        outcome = trace_search(app, body, dense_timeout=min(app.SEARCH_QDRANT_TIMEOUT, remaining),
                               embed_wait_timeout=min(app.SEARCH_EMBED_WAIT_TIMEOUT, remaining),
                               embed_http_timeout=min(app.SEARCH_EMBED_HTTP_TIMEOUT, remaining))
        payload = outcome["payload"]
        records.append({**item, "results": payload.get("results"), "partial": payload.get("partial", False),
                        "error": outcome["error"], "raw_payload": payload,
                        "diagnostic_trace": outcome["trace"],
                        "anchor_paths": anchor_paths(groups[item["group_id"]], outcome["trace"],
                                                     app.canonical_id)})
    return {"records": records, "summary": score(fixture, records),
            "elapsed_seconds": time.monotonic() - started,
            "scope": "internal runtime candidate; unchanged frozen known-source gates, not precision/recall"}


def compare_complete_hits(fixture, before, after):
    if before.get("fixture") != fixture:
        raise ValueError("baseline embedded fixture must equal the unchanged frozen fixture")
    baseline = score(fixture, before["raw_results"])
    candidate = score(fixture, after["records"])
    old = {(row["group_id"], row["phrase_id"]): row for row in baseline["phrases"]}
    new = {(row["group_id"], row["phrase_id"]): row for row in candidate["phrases"]}
    retained, lost, unconfirmed = [], [], []
    for key, row in old.items():
        if row["valid"] and row["hit@10"]:
            item = {"group_id": key[0], "phrase_id": key[1]}
            if not new[key]["valid"]:
                unconfirmed.append(item)
            elif new[key]["hit@10"]:
                retained.append(item)
            else:
                lost.append(item)
    paired = [key for key in old if old[key]["valid"] and new[key]["valid"]]
    return {"baseline_complete_count": sum(row["valid"] for row in old.values()),
            "candidate_complete_count": sum(row["valid"] for row in new.values()),
            "complete_pair_count": len(paired),
            "baseline_known_hits_on_complete_pairs": sum(old[key]["hit@10"] for key in paired),
            "candidate_known_hits_on_complete_pairs": sum(new[key]["hit@10"] for key in paired),
            "retained_complete_hits": retained, "lost_complete_hits": lost,
            "unconfirmed_prior_hits_due_to_incomplete_candidate": unconfirmed,
            "baseline_fixture_identity": "embedded_fixture_equal; byte-hash conventions may differ",
            "baseline_recorded_fixture_sha256": before.get("fixture_sha256"),
            "note": "Non-causal comparison: load/contention and public-vs-internal transport differ."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--budget-seconds", type=float, default=180)
    parser.add_argument("--before", type=Path, help="read-only public-before JSON with embedded fixture")
    args = parser.parse_args(argv)
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    before = json.loads(args.before.read_bytes()) if args.before else None
    if before is not None and before.get("fixture") != fixture:
        raise ValueError("baseline embedded fixture differs; do not edit fixtures or anchors")
    from api import main as app
    output = run_fixture(app, fixture, args.budget_seconds)
    output["fixture_sha256"] = hashlib.sha256(fixture_bytes).hexdigest()
    output["candidate_main_sha256"] = hashlib.sha256(Path(app.__file__).read_bytes()).hexdigest()
    if before is not None:
        output["baseline_comparison"] = compare_complete_hits(fixture, before, output)
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))
    return 0 if output["summary"]["all_groups_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
