"""Explain target-paper candidate flow from an instrumented SPLADE probe."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

try:
    from eval import analyze_splade_candidate_probe as quality_oracle
    from eval import splade_candidate_probe as probe_writer
except ImportError:
    import analyze_splade_candidate_probe as quality_oracle
    import splade_candidate_probe as probe_writer


def _norm(value):
    return quality_oracle._norm(value)


def _items(value):
    if isinstance(value, dict) and isinstance(value.get("items"), list):
        return value["items"], value.get("total", len(value["items"])), bool(value.get("truncated"))
    if isinstance(value, (list, tuple)):
        return list(value), len(value), False
    return [], None, False


def _row_id(row):
    if isinstance(row, dict):
        return _norm(row.get("arxiv_id") or row.get("paper_id") or row.get("id"))
    return _norm(row)


def _rank(values, targets):
    return next((index for index, value in enumerate(values, 1) if _row_id(value) in targets), None)


def _event_list(trace):
    return trace.get("events") if isinstance(trace, dict) and isinstance(trace.get("events"), list) else []


def _trace_layers(variant, primary_layer):
    raw = variant.get("raw_payload") if isinstance(variant, dict) else None
    trace = raw.get("diagnostic_trace") if isinstance(raw, dict) else None
    events = _event_list(trace)
    grouped = defaultdict(list)
    for event in events:
        if isinstance(event, dict):
            key = event.get("layer") or "unlabeled"
            grouped[key].append(event)
    return trace, grouped


def _event_stage(events, name):
    return [event for event in events if event.get("stage") == name]


def _stage_summary(entries, targets, *, ids_only=False):
    values, total, truncated = _items(entries)
    matches = [_row_id(item) for item in values if _row_id(item) in targets]
    return {"available": entries is not None, "total": total,
            "any_target": (True if matches else None if truncated or entries is None else False),
            "target_rank": (_rank(values, targets) if matches else
                            None if truncated or entries is None else None),
            "matched_target_ids": list(dict.fromkeys(matches)),
            "truncated": truncated}


def _first(event_list):
    return event_list[0] if event_list else None


def _map_get(mapping, ids):
    if not isinstance(mapping, dict):
        return None
    for key, value in mapping.items():
        if _norm(key) in ids:
            return value
    return None


def _dedupe_evidence(event, target_ids):
    if not event:
        return {"available": False, "floor": None, "lex_exempt": None,
                "after_rank": None, "after_target": None}
    before, _before_total, before_truncated = _items(event.get("before"))
    after, _after_total, after_truncated = _items(event.get("after"))
    exempt = {_norm(value) for value in (event.get("lex_exempt") or [])}
    after_rank = _rank(after, target_ids)
    return {"available": True, "floor": event.get("floor"),
            "lex_exempt": bool(exempt & target_ids),
            "before_rank": _rank(before, target_ids),
            "after_rank": after_rank,
            "after_target": (True if after_rank is not None else
                             None if after_truncated else False),
            "truncated": before_truncated or after_truncated}


def _transition(event, target_ids):
    if not event:
        return {"available": False, "before_target": None, "after_target": None,
                "before_rank": None, "after_rank": None}
    before, _bt, before_truncated = _items(event.get("before_ids"))
    after, _at, after_truncated = _items(event.get("after_ids"))
    before_rank = _rank(before, target_ids)
    after_rank = _rank(after, target_ids)
    return {"available": True,
            "before_target": True if before_rank is not None else None if before_truncated else False,
            "after_target": True if after_rank is not None else None if after_truncated else False,
            "before_rank": before_rank, "after_rank": after_rank,
            "truncated": before_truncated or after_truncated}


def _layer_summary(events, targets, is_primary):
    stage_b_events = _event_stage(events, "stage_b")
    hier_events = _event_stage(events, "hier_run")
    dedupe_events = _event_stage(events, "dedupe_preferring_content")
    stage_rows = []
    for event in stage_b_events:
        stage_rows.append({"stage": "stage_b", "error": event.get("error"),
                           "input": _stage_summary(event.get("input_arxiv_ids"), targets),
                           "output": _stage_summary(event.get("output_hits"), targets),
                           "limit": event.get("limit")})
    for event in hier_events:
        stage_rows.append({"stage": "hier", "error": event.get("error"),
                           "ordered": _stage_summary(event.get("ordered_hits"), targets),
                           "fusion_ranks": _map_get(event.get("fusion_ranks"), targets),
                           "fusion_score": _map_get(event.get("fusion_scores"), targets),
                           "paper_score": _map_get(event.get("paper_scores"), targets),
                           "lex_exempt": bool({_norm(value) for value in event.get("lex_exempt", [])}
                                              & targets)})
    for event in dedupe_events:
        before, _total, _truncated = _items(event.get("before"))
        scores = [hit.get("score") for hit in before if _row_id(hit) in targets
                  and isinstance(hit, dict) and isinstance(hit.get("score"), (int, float))]
        summary = _dedupe_evidence(event, targets)
        summary["before_best_score"] = max(scores, default=None)
        stage_rows.append({"stage": "dedupe", **summary})
    transitions = {}
    for stage in ("inject_exact", "cap_gh_docs", "collapse_twins"):
        candidates = _event_stage(events, stage)
        transitions[stage] = [_transition(event, targets) for event in candidates]
    return {"primary_layer": is_primary, "events": stage_rows,
            "transitions": transitions,
            "stage_b_event_count": len(stage_b_events),
            "hier_event_count": len(hier_events),
            "dedupe_event_count": len(dedupe_events)}


def _final_info(variant, targets, complete):
    ids = quality_oracle._ids(variant)
    rank = quality_oracle._rank(ids, targets) if complete else None
    return {"complete": complete, "target_hit": rank is not None if complete else None,
            "target_rank": rank, "top10_target_hit": rank is not None and rank <= 10
            if complete else None,
            "result_count": len(ids) if isinstance(variant, dict) else None,
            "top10": ids[:10]}


def _primary_candidate_event(treatment, primary_layer):
    treatment = treatment if isinstance(treatment, dict) else {}
    calls = treatment.get("lexical_calls")
    expected = next((call.get("paper_ids_after") for call in calls or []
                     if isinstance(call, dict) and isinstance(call.get("paper_ids_after"), list)), None)
    raw = treatment.get("raw_payload")
    trace = raw.get("diagnostic_trace") if isinstance(raw, dict) else None
    candidates = [event for event in _event_list(trace)
                  if isinstance(event, dict) and event.get("stage") == "stage_b"
                  and event.get("layer") == primary_layer]
    if expected is None:
        return None, None
    for event in candidates:
        values, _total, truncated = _items(event.get("input_arxiv_ids"))
        if not truncated and [_norm(value) for value in values] == [_norm(value) for value in expected]:
            return event, True
    if not candidates:
        return None, None
    first_values, _total, truncated = _items(candidates[0].get("input_arxiv_ids"))
    return None, None if truncated else False


def _variant_analysis(variant, target_ids, primary_layer, query_error=False):
    variant = variant if isinstance(variant, dict) else {}
    raw = variant.get("raw_payload")
    trace, grouped = _trace_layers(variant, primary_layer)
    complete = (not query_error and not variant.get("error") and not variant.get("partial")
                and (isinstance(variant.get("results"), list)
                     or isinstance(raw, dict) and isinstance(raw.get("results"), list)))
    final = _final_info(variant, target_ids, complete)
    layers = {layer: _layer_summary(events, target_ids, layer == primary_layer)
              for layer, events in grouped.items()}
    # Always include the fixture's requested layer so absent trace data is explicit.
    if primary_layer not in layers:
        layers[primary_layer] = _layer_summary([], target_ids, True)
    combined = _combined_path(trace, target_ids, final, complete)
    return {"complete": complete, "error": variant.get("error"),
            "partial": bool(variant.get("partial")), "trace_available": isinstance(trace, dict),
            "trace_version": trace.get("version") if isinstance(trace, dict) else None,
            "primary_layer": primary_layer, "final": final, "layers": layers,
            "combined_target_path": combined,
            "trace_event_count": len(_event_list(trace))}


def _combined_path(trace, target_ids, final, complete):
    events = _event_list(trace)
    hier_events = [event for event in events if isinstance(event, dict)
                   and event.get("stage") == "hier_run"]
    dedupe_events = [event for event in events if isinstance(event, dict)
                     and event.get("stage") == "dedupe_preferring_content"]
    hier_matches = []
    hier_unknown = not hier_events
    for event in hier_events:
        values, _total, truncated = _items(event.get("ordered_hits"))
        rank = _rank(values, target_ids)
        if rank is not None:
            hier_matches.append(True)
        elif truncated or "ordered_hits" not in event:
            hier_unknown = True
    hier_any = True if hier_matches else None if hier_unknown else False

    dedupe_before_present = False
    dedupe_after_present = False
    dedupe_unknown = not dedupe_events
    passing_floor = False
    all_below_floor = True
    saw_floor_candidate = False
    score_unknown = False
    for event in dedupe_events:
        before, _before_total, before_truncated = _items(event.get("before"))
        after, _after_total, after_truncated = _items(event.get("after"))
        matching_before = [hit for hit in before if _row_id(hit) in target_ids]
        if matching_before:
            dedupe_before_present = True
            floor = event.get("floor")
            exempt = {_norm(value) for value in (event.get("lex_exempt") or [])}
            is_exempt = bool(exempt & target_ids)
            if floor is None or any(not isinstance(hit, dict)
                                    or not isinstance(hit.get("score"), (int, float))
                                    for hit in matching_before):
                score_unknown = True
            else:
                saw_floor_candidate = True
                if is_exempt or any(hit["score"] >= floor for hit in matching_before):
                    passing_floor = True
                    all_below_floor = False
                elif any(hit["score"] >= floor for hit in matching_before):
                    all_below_floor = False
        if _rank(after, target_ids) is not None:
            dedupe_after_present = True
        elif after_truncated or before_truncated or "after" not in event or "before" not in event:
            dedupe_unknown = True

    dedupe_any = True if dedupe_after_present else None if dedupe_unknown else False
    final_hit = final.get("target_hit")
    if not complete:
        cause = "unknown_incomplete_or_error"
    elif final_hit:
        cause = "target_in_final_results"
    elif dedupe_after_present:
        cause = "later_rank_or_cut"
    elif (dedupe_before_present and saw_floor_candidate and all_below_floor
          and not score_unknown and not dedupe_unknown):
        cause = "relevance_floor"
    elif dedupe_before_present and passing_floor and not dedupe_unknown:
        cause = "passed_floor_not_selected"
    elif hier_any is True and not dedupe_before_present and not dedupe_unknown:
        cause = "missing_from_dedupe_input"
    elif hier_any is False:
        cause = "target_not_observed_in_hier_trace"
    else:
        cause = "unknown_trace_evidence"
    return {"layer_scope": "union_of_all_observed_layers",
            "hier_any_target": hier_any, "dedupe_before_any_target":
            True if dedupe_before_present else None if dedupe_unknown else False,
            "dedupe_after_any_target": dedupe_any, "final_any_target": final_hit,
            "cause": cause,
            "floor_evidence_complete": not score_unknown,
            "floor_candidate_observed": saw_floor_candidate,
            "passed_floor_candidate_observed": passing_floor,
            "truncated_or_missing_stage_data": hier_unknown or dedupe_unknown}


def _added_target_rows(treatment, target_records, primary_layer, complete, final_ids,
                       primary_b_event, prefix_match):
    calls = treatment.get("lexical_calls") if isinstance(treatment, dict) else []
    calls = calls if isinstance(calls, list) else []
    added = treatment.get("added_ids") if isinstance(treatment, dict) else []
    if not isinstance(added, list):
        added = [aid for call in calls if isinstance(call, dict)
                 for aid in call.get("appended_ids", []) if isinstance(aid, str)]
    targets_by_norm = {}
    for target in target_records:
        for source in target["sources"]:
            targets_by_norm.setdefault(_norm(source), target)
    actual_added = list(dict.fromkeys(str(item) for item in added if _norm(item) in targets_by_norm))
    raw = treatment.get("raw_payload") if isinstance(treatment, dict) else None
    trace = raw.get("diagnostic_trace") if isinstance(raw, dict) else None
    events = _event_list(trace)
    primary_events = [event for event in events if event.get("layer") == primary_layer]
    b_event = primary_b_event
    hier_event = _first(_event_stage(primary_events, "hier_run"))
    dedupe_event = _first(_event_stage(primary_events, "dedupe_preferring_content"))
    ids_items, _ids_total, ids_truncated = _items(b_event.get("input_arxiv_ids") if b_event else None)
    output_hits, _hits_total, hits_truncated = _items(b_event.get("output_hits") if b_event else None)
    ordered, _ordered_total, ordered_truncated = _items(hier_event.get("ordered_hits") if hier_event else None)
    dedupe_before, _dbt, db_truncated = _items(dedupe_event.get("before") if dedupe_event else None)
    dedupe_after, _dat, da_truncated = _items(dedupe_event.get("after") if dedupe_event else None)
    floor = dedupe_event.get("floor") if dedupe_event else None
    exempt = {_norm(value) for value in (dedupe_event or {}).get("lex_exempt", [])}
    input_ids = {_row_id(value) for value in ids_items}
    final_target_ids = {_row_id(value) for value in final_ids}
    output_target_ids = {_row_id(value) for value in output_hits}
    hier_target_ids = {_row_id(value) for value in ordered}
    dedupe_target_ids = {_row_id(value) for value in dedupe_after}
    rows = []
    for source in actual_added:
        aid = _norm(source)
        target = targets_by_norm[aid]
        ids = {aid, *(_norm(value) for value in target["sources"])}
        dedupe_matching_hits = [hit for hit in dedupe_before if _row_id(hit) in ids]
        dedupe_scores = [hit.get("score") for hit in dedupe_matching_hits
                         if isinstance(hit, dict) and isinstance(hit.get("score"), (int, float))]
        dedupe_best_score = max(dedupe_scores, default=None)
        matching_hits = [hit for hit in output_hits if _row_id(hit) in ids]
        best_hit = max(matching_hits, key=lambda hit: float(hit.get("score") or 0), default=None)
        hier_rank = _rank(ordered, ids)
        dedupe_rank = _rank(dedupe_after, ids)
        candidate_rank = _rank(dedupe_before, ids)
        lex_exempt = bool(ids & exempt)
        score = best_hit.get("score") if isinstance(best_hit, dict) else None
        below_floor = (dedupe_best_score is not None and floor is not None
                       and dedupe_best_score < floor and not lex_exempt)
        input_present = bool(ids & input_ids)
        output_present = bool(ids & output_target_ids)
        if not complete:
            cause = "unknown_incomplete_or_error"
        elif not b_event or prefix_match is not True or b_event.get("error"):
            cause = "unknown_trace_missing"
        elif aid in final_target_ids or ids & final_target_ids:
            cause = "target_in_final_results"
        elif ("input_arxiv_ids" in b_event and not ids_truncated and not input_present
              and "output_hits" in b_event and not hits_truncated and not output_present):
            cause = "not_in_primary_candidates"
        elif ("input_arxiv_ids" in b_event and not ids_truncated and input_present
              and "output_hits" in b_event and not hits_truncated and not output_present):
            cause = "not_in_primary_chunk_pool"
        elif ((ids & output_target_ids or ids & hier_target_ids) and below_floor
              and dedupe_event and not (ids & dedupe_target_ids)):
            cause = "relevance_floor"
        elif dedupe_rank is not None and aid not in final_target_ids and not (ids & final_target_ids):
            cause = "later_rank_or_cut"
        else:
            cause = "not_proven"
        hier_ranks = _map_get((hier_event or {}).get("fusion_ranks"), ids)
        fusion_score = _map_get((hier_event or {}).get("fusion_scores"), ids)
        paper_score = _map_get((hier_event or {}).get("paper_scores"), ids)
        floor_evidence = {"floor": floor, "lex_exempt": lex_exempt,
                          "after_rank": dedupe_rank}
        boilerplate_worse = bool(best_hit and best_hit.get("boilerplate") is True
                                 and candidate_rank is not None and dedupe_rank is not None
                                 and dedupe_rank > candidate_rank)
        rows.append({"source_added": source, "target_canonical_id": target["canonical_id"],
                     "stage_a_input_rank": _rank(ids_items, ids),
                     "stage_b_output": {"present": (True if best_hit is not None else
                                                     None if hits_truncated else False),
                                        "best_score": score,
                                        "boilerplate": best_hit.get("boilerplate") if best_hit else None,
                                        "rank": _rank(output_hits, ids)},
                     "hier": {"present": (True if hier_rank is not None else
                                            None if ordered_truncated else False), "rank": hier_rank,
                              "fusion_ranks": hier_ranks, "fusion_score": fusion_score,
                              "paper_score": paper_score},
                     "dedupe": {**floor_evidence, "before_rank": candidate_rank,
                                "before_best_score": dedupe_best_score,
                                "after_present": (True if dedupe_rank is not None else
                                                  None if da_truncated else False)},
                     "post_dedupe": {name: _transition(event, ids)
                                     for name, event in (("inject_exact", _first(_event_stage(primary_events, "inject_exact"))),
                                                         ("cap_gh_docs", _first(_event_stage(primary_events, "cap_gh_docs"))),
                                                         ("collapse_twins", _first(_event_stage(primary_events, "collapse_twins"))))},
                     "final_rank": _rank(final_ids, ids) if complete else None,
                     "cause": cause,
                     "boilerplate_rank_worse_observed_separately": boilerplate_worse,
                     "evidence_truncated": ids_truncated or hits_truncated or ordered_truncated
                     or db_truncated or da_truncated})
    return rows


def analyze(probe, original_fixture, holdout_fixture):
    oracle, labels, _names = quality_oracle._fixture_oracle(
        {"original": original_fixture, "holdout": holdout_fixture})
    records = {}
    target_metadata = {}
    for label, fixture_path in (("original", original_fixture), ("holdout", holdout_fixture)):
        fixture_name = Path(fixture_path).name
        fixture_data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        for group in fixture_data.get("groups", []):
            targets = []
            for item in group.get("targets", []):
                canonical = _norm(item.get("canonical_id"))
                sources = list(dict.fromkeys([canonical, *(_norm(value) for value in item.get("aliases", []))]))
                targets.append({"canonical_id": canonical, "sources": [value for value in sources if value]})
            for phrase in group.get("phrases", []):
                target_metadata[(fixture_name, _norm(group.get("id")), _norm(phrase.get("id")))] = targets
    raw_records = probe.get("records", []) if isinstance(probe, dict) else []
    for record in raw_records if isinstance(raw_records, list) else []:
        if not isinstance(record, dict):
            continue
        identity = (_norm(record.get("fixture")), _norm(record.get("group_id")),
                    _norm(record.get("phrase_id")))
        if identity in oracle:
            records.setdefault(identity, record)

    per_query = []
    for identity, expected in sorted(oracle.items()):
        record = records.get(identity, {})
        targets = target_metadata.get(identity, [
            {"canonical_id": source, "sources": [source]}
            for source in sorted(expected["targets"])])
        target_ids = {_norm(value) for value in expected["targets"]}
        pair_error = bool(record.get("error"))
        baseline_variant = record.get("baseline") or {}
        treatment_variant = record.get("treatment") or {}
        baseline = _variant_analysis(baseline_variant, target_ids, expected["layer"], pair_error)
        treatment = _variant_analysis(treatment_variant, target_ids, expected["layer"], pair_error)
        final_ids = quality_oracle._ids(treatment_variant) if treatment["complete"] else []
        candidate_event, prefix_match = _primary_candidate_event(treatment_variant, expected["layer"])
        additions = _added_target_rows(treatment_variant, targets, expected["layer"],
                                       treatment["complete"], final_ids,
                                       candidate_event, prefix_match)
        per_query.append({"identity": list(identity), "query": expected["query"],
                          "layer": expected["layer"], "targets": targets,
                          "baseline": baseline, "treatment": treatment,
                          "added_target_sources": additions,
                          "trace_comparison": {"primary_candidate_stage_b_prefix_match": prefix_match,
                          "candidate_pool_scope": "matching_primary_stage_b_input_and_hit_response; says nothing about corpus/index coverage"}})
    counts = {"all": len(oracle), **{label: sum(row["identity"][0] == _norm(filename)
                                                for row in per_query)
                                     for label, filename in labels.items()}}
    combined_counts = {}
    for variant_name in ("baseline", "treatment"):
        complete_rows = [row for row in per_query if row[variant_name]["complete"]]
        causes = defaultdict(int)
        for row in complete_rows:
            causes[row[variant_name]["combined_target_path"]["cause"]] += 1
        combined_counts[variant_name] = {
            "complete_denominator": len(complete_rows),
            "hier_any_target": sum(row[variant_name]["combined_target_path"]["hier_any_target"] is True
                                    for row in complete_rows),
            "dedupe_after_any_target": sum(row[variant_name]["combined_target_path"]["dedupe_after_any_target"] is True
                                           for row in complete_rows),
            "final_any_target": sum(row[variant_name]["combined_target_path"]["final_any_target"] is True
                                     for row in complete_rows),
            "cause_counts_complete_only": dict(sorted(causes.items())),
        }
    combined_counts["paired_complete_denominator"] = sum(
        row["baseline"]["complete"] and row["treatment"]["complete"] for row in per_query)
    return {"schema_version": 1, "analysis": "splade_candidate_trace_diagnostics",
            "denominators": counts, "runner_error": probe.get("runner_error") if isinstance(probe, dict) else None,
            "pair_observation_counts": {
                "complete_baseline_and_treatment": sum(row["baseline"]["complete"] and row["treatment"]["complete"] for row in per_query),
                "partial_or_error_or_missing": sum(not (row["baseline"]["complete"] and row["treatment"]["complete"]) for row in per_query)},
            "combined_path_counts_all_layers": combined_counts,
            "cause_definitions": {"not_in_primary_candidates": "Target absent from the matching primary stage-B input shortlist and response; says nothing about corpus/index coverage.",
                                  "not_in_primary_chunk_pool": "Target was in the matching primary stage-B input shortlist but absent from its hit response; says nothing about corpus/index coverage.",
                                  "relevance_floor": "Observed below-floor, non-exempt candidate absent from dedupe output.",
                                  "later_rank_or_cut": "Target observed after dedupe but absent from complete final results.",
                                  "not_proven": "Trace does not prove one of the listed causes."},
            "truncation_note": "A missing target is conclusive only when the relevant captured list is not truncated and the stage completed.",
            "per_query": per_query}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--original", required=True, type=Path)
    parser.add_argument("--holdout", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = analyze(json.loads(args.probe.read_text(encoding="utf-8")),
                     args.original, args.holdout)
    probe_writer._write_checkpoint(args.output, report)


if __name__ == "__main__":
    main()
