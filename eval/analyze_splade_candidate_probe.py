"""Aggregate paired SPLADE candidate-probe results against frozen fixtures."""
import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path


EXPECTED_PER_FIXTURE = {"original": 21, "holdout": 12}
PRODUCTION_DECISION = "HOLD_FULL_SPARSE_AND_SERVER_LATENCY_NOT_VALIDATED"


def _fixture_oracle(paths):
    oracle = {}
    names = {}
    for label, path in paths.items():
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        fixture_name = path.name
        names[label] = fixture_name
        for group in data["groups"]:
            targets = [item for item in group.get("targets", [])]
            negatives = [item for item in group.get("hard_negatives", [])]
            target_ids = set()
            negative_ids = set()
            for item in targets:
                target_ids.add(_norm(item.get("canonical_id")))
                target_ids.update(_norm(value) for value in item.get("aliases", []))
            for item in negatives:
                negative_ids.add(_norm(item.get("id") or item.get("canonical_id")))
                negative_ids.update(_norm(value) for value in item.get("aliases", []))
            for phrase in group["phrases"]:
                key = (fixture_name, group["id"], phrase["id"])
                if key in oracle:
                    raise ValueError(f"duplicate frozen fixture identity: {key}")
                oracle[key] = {"query": phrase["query"], "layer": group["layer"],
                               "targets": target_ids, "negatives": negative_ids,
                               "group_id": group["id"], "fixture": fixture_name}
    expected_names = set(names.values())
    return oracle, names, expected_names


def _norm(value):
    return str(value or "").strip().lower()


def _ids(variant):
    if not isinstance(variant, dict):
        return []
    if isinstance(variant.get("ids"), list):
        return [_norm(value) for value in variant["ids"] if _norm(value)]
    results = variant.get("results")
    if not isinstance(results, list):
        results = variant.get("raw_payload", {}).get("results", []) if isinstance(variant.get("raw_payload"), dict) else []
    return [_norm(row.get("arxiv_id") or row.get("paper_id") or row.get("id"))
            for row in results if isinstance(row, dict)
            and _norm(row.get("arxiv_id") or row.get("paper_id") or row.get("id"))]


def _rank(ids, aliases):
    return min((index for index, value in enumerate(ids, 1) if value in aliases), default=None)


def _quality(ids, oracle):
    top = ids[:10]
    target_rank = _rank(top, oracle["targets"])
    alert = any((negative_rank := _rank(top, {negative})) is not None
                and (target_rank is None or negative_rank < target_rank)
                for negative in oracle["negatives"])
    return {"target_rank_top10": target_rank, "hit@10": target_rank is not None,
            "mrr@10": 1.0 / target_rank if target_rank else 0.0,
            "known_negative_above_target_top10_warning": alert}


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct
    lo = math.floor(position)
    hi = math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def _describe(values):
    clean = [float(value) for value in values if _finite(value)]
    return {"n": len(clean), "mean": statistics.fmean(clean) if clean else None,
            "median": statistics.median(clean) if clean else None,
            "p50": _percentile(clean, .50), "p95": _percentile(clean, .95)}


def _finite(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _candidate_invariants(record):
    """Collect runner-reported invariants only; absent evidence stays unknown."""
    found = {}
    for source in (record, record.get("treatment") or {}):
        for key in ("original_prefix_preserved", "lexical_unchanged", "candidate_prefix_preserved"):
            if isinstance(source.get(key), bool):
                found[key] = source[key]
    calls = (record.get("treatment") or {}).get("lexical_calls", [])
    for call in calls if isinstance(calls, list) else []:
        for key in ("original_prefix_preserved", "lexical_unchanged", "candidate_prefix_preserved"):
            if isinstance(call.get(key), bool):
                found[key] = found.get(key, True) and call[key]
    return found


def _candidate_counts(record):
    treatment = record.get("treatment") or {}
    added = treatment.get("added_ids")
    calls = treatment.get("lexical_calls", [])
    if not isinstance(added, list):
        added = next((call.get("appended_ids") for call in calls
                      if isinstance(call, dict) and isinstance(call.get("appended_ids"), list)), None)
    after = next((call.get("paper_ids_after") for call in calls
                  if isinstance(call, dict) and isinstance(call.get("paper_ids_after"), list)), None)
    return {"appended_candidate_count": len(added) if isinstance(added, list) else None,
            "treatment_stage_a_candidate_count": len(after) if after is not None else None,
            "baseline_stage_a_candidate_count": len(after) - len(added)
            if after is not None and isinstance(added, list) else None,
            "baseline_final_top10_count": len(_ids(record.get("baseline"))[:10]),
            "treatment_final_top10_count": len(_ids(treatment)[:10])}


def aggregate(probe, original_fixture, holdout_fixture):
    oracle, labels, _ = _fixture_oracle({"original": original_fixture, "holdout": holdout_fixture})
    expected = set(oracle)
    raw_records = probe.get("records", [])
    records = {}
    duplicates = []
    extras = []
    malformed = []
    fixture_mismatches = []
    for record in raw_records if isinstance(raw_records, list) else []:
        if not isinstance(record, dict):
            malformed.append("non_object_record")
            continue
        key = (_norm(record.get("fixture")), _norm(record.get("group_id")), _norm(record.get("phrase_id")))
        if key not in expected:
            extras.append(list(key))
        elif key in records:
            duplicates.append(list(key))
        else:
            records[key] = record
            expected_row = oracle[key]
            for field in ("query", "layer"):
                if record.get(field) != expected_row[field]:
                    fixture_mismatches.append({"identity": list(key), "field": field})

    pairs = []
    missing = []
    skipped = probe.get("skipped", [])
    skip_by_identity = {}
    for item in skipped if isinstance(skipped, list) else []:
        if isinstance(item, dict):
            key = (_norm(item.get("fixture")), _norm(item.get("group_id")), _norm(item.get("phrase_id")))
            if all(key):
                skip_by_identity[key] = item.get("reason", "skipped")
            elif item.get("query") is not None:
                matches = [identity for identity, value in oracle.items()
                           if value["query"] == item["query"]]
                if len(matches) == 1:
                    skip_by_identity[matches[0]] = item.get("reason", "skipped")
    for key in sorted(expected):
        record = records.get(key)
        if not record:
            missing.append({"identity": list(key), "reason": skip_by_identity.get(key, "missing_record")})
            pairs.append({"identity": list(key), "fixture": key[0], "group_id": key[1],
                          "phrase_id": key[2], "valid": False, "error": None,
                          "partial": False, "missing": True, "baseline_ids": [],
                          "treatment_ids": [], "baseline_latency_ms": None,
                          "treatment_latency_ms": None, "candidate_invariants": {},
                          "baseline_unconditional": {"usable": False, "quality": None},
                          "treatment_unconditional": {"usable": False, "quality": None},
                          "candidate_counts": {"appended_candidate_count": None,
                                               "treatment_stage_a_candidate_count": None,
                                               "baseline_stage_a_candidate_count": None,
                                               "baseline_final_top10_count": 0,
                                               "treatment_final_top10_count": 0}})
            continue
        baseline = record.get("baseline")
        treatment = record.get("treatment")
        pair_error = record.get("error") or (baseline or {}).get("error") or (treatment or {}).get("error")
        if not pair_error and any(not isinstance(variant, dict)
                                  or not isinstance(variant.get("results"), list)
                                  and not (isinstance(variant.get("raw_payload"), dict)
                                           and isinstance(variant["raw_payload"].get("results"), list))
                                  for variant in (baseline, treatment)):
            pair_error = "missing_completed_results"
        partial = any(not isinstance(v, dict) or bool(v.get("partial")) for v in (baseline, treatment))
        valid = not pair_error and not partial
        row = {"identity": list(key), "fixture": key[0], "group_id": key[1], "phrase_id": key[2],
               "valid": valid, "error": pair_error, "partial": partial,
               "baseline_ids": _ids(baseline), "treatment_ids": _ids(treatment),
               "baseline_latency_ms": (baseline or {}).get("latency_ms"),
               "treatment_latency_ms": (treatment or {}).get("latency_ms"),
               "candidate_invariants": _candidate_invariants(record),
               "candidate_counts": _candidate_counts(record)}
        for variant_name, variant in (("baseline", baseline), ("treatment", treatment)):
            usable = (isinstance(variant, dict) and not variant.get("error") and not variant.get("partial")
                      and (isinstance(variant.get("results"), list)
                           or isinstance(variant.get("raw_payload"), dict)
                           and isinstance(variant["raw_payload"].get("results"), list)))
            row[f"{variant_name}_unconditional"] = {
                "usable": usable,
                "error": bool(record.get("error") or not isinstance(variant, dict) or variant.get("error")),
                "partial": bool(isinstance(variant, dict) and variant.get("partial")),
                "quality": _quality(row[f"{variant_name}_ids"], oracle[key]) if usable else None}
        if valid:
            row["baseline_quality"] = _quality(row["baseline_ids"], oracle[key])
            row["treatment_quality"] = _quality(row["treatment_ids"], oracle[key])
        pairs.append(row)

    def summarize(rows):
        valid = [row for row in rows if row["valid"]]
        out = {"unconditional_denominator": len(rows), "strict_quality_denominator": len(valid),
               "strict_quality_failures": len(rows) - len(valid), "completed_valid_pairs": len(valid)}
        for variant in ("baseline", "treatment"):
            metrics = [row[f"{variant}_quality"] for row in valid]
            observed = [row[f"{variant}_unconditional"]["quality"] for row in rows
                        if row.get(f"{variant}_unconditional", {}).get("usable")]
            observed_sum = sum(item[f"{variant}_unconditional"]["quality"]["mrr@10"] for item in rows
                               if item.get(f"{variant}_unconditional", {}).get("usable"))
            unconditional_states = [row.get(f"{variant}_unconditional", {}) for row in rows]
            out[variant] = {"hit@10": sum(m["hit@10"] for m in metrics),
                            "mrr@10_sum": sum(m["mrr@10"] for m in metrics),
                            "mrr@10": statistics.fmean(m["mrr@10"] for m in metrics) if metrics else None,
                            "known_negative_above_target_top10_warnings": sum(
                                m["known_negative_above_target_top10_warning"] for m in metrics),
                            "unconditional_variant_observation": {
                                "denominator": len(rows), "usable_results": len(observed),
                                "hit@10": sum(m["hit@10"] for m in observed),
                                "mrr@10_sum": observed_sum,
                                "mrr@10": observed_sum / len(rows) if rows else None,
                                "warnings": sum(m["known_negative_above_target_top10_warning"] for m in observed),
                                "error_pairs": sum(bool(item.get("error")) for item in unconditional_states),
                                "partial_pairs": sum(bool(item.get("partial")) for item in unconditional_states)}}
        return out

    fixture_metrics = {}
    for label, filename in labels.items():
        fixture_metrics[label] = summarize([row for row in pairs if row["fixture"] == filename])
        fixture_metrics[label]["expected_denominator"] = EXPECTED_PER_FIXTURE[label]

    valid_pairs = [row for row in pairs if row["valid"]]
    group_rows = defaultdict(list)
    per_query = []
    wins = losses = ties = 0
    regressions = []
    displaced = []
    for row in valid_pairs:
        before, after = row["baseline_quality"], row["treatment_quality"]
        delta = after["mrr@10"] - before["mrr@10"]
        wins += delta > 0
        losses += delta < 0
        ties += delta == 0
        lost_hit = before["hit@10"] and not after["hit@10"]
        if lost_hit:
            regressions.append(row["identity"])
        removed = [paper for paper in row["baseline_ids"][:10] if paper not in set(row["treatment_ids"][:10])]
        if removed:
            displaced.append({"identity": row["identity"], "displaced_baseline_top10_ids": removed})
        row["paired_mrr_delta_treatment_minus_baseline"] = delta
        row["baseline_top10_displaced_ids"] = removed
        per_query.append(row)
        group_rows[(row["fixture"], row["group_id"])].append(row)

    per_group = {}
    group_decreases = []
    for key, rows in sorted(group_rows.items()):
        item = {"expected_queries": 3, "valid_queries": len(rows)}
        for variant in ("baseline", "treatment"):
            vals = [row[f"{variant}_quality"] for row in rows]
            item[variant] = {"hit@10": sum(v["hit@10"] for v in vals),
                             "mrr@10": statistics.fmean(v["mrr@10"] for v in vals) if vals else None,
                             "all_3_hit@10": len(vals) == 3 and all(v["hit@10"] for v in vals)}
        item["all_3_hit@10_stable_or_better"] = (
            item["treatment"]["all_3_hit@10"] >= item["baseline"]["all_3_hit@10"]
            and item["treatment"]["hit@10"] >= item["baseline"]["hit@10"]
            and _not_decreased(item["treatment"]["mrr@10"], item["baseline"]["mrr@10"]))
        if not item["all_3_hit@10_stable_or_better"]:
            group_decreases.append(list(key))
        per_group["/".join(key)] = item

    lat_pairs = [row for row in valid_pairs if _finite(row["baseline_latency_ms"]) and _finite(row["treatment_latency_ms"])]
    deltas = [float(row["treatment_latency_ms"]) - float(row["baseline_latency_ms"]) for row in lat_pairs]
    invariants = [value for row in valid_pairs for value in row["candidate_invariants"].values()]
    candidate_counts = [row["candidate_counts"] for row in pairs]
    overall = summarize(pairs)
    hold = fixture_metrics["holdout"]
    all_valid = (len(valid_pairs) == 33 and len(pairs) == 33 and not missing and not duplicates
                 and not extras and not malformed and not fixture_mismatches and not probe.get("runner_error"))
    no_error_partial_growth = (sum(bool(row["error"]) for row in pairs) == 0
                               and sum(bool(row["partial"]) for row in pairs) == 0)
    no_query_hit_loss = not regressions
    no_group_decrease = not group_decreases
    no_alert_growth = (overall["treatment"]["known_negative_above_target_top10_warnings"] <=
                       overall["baseline"]["known_negative_above_target_top10_warnings"]
                       and hold["treatment"]["known_negative_above_target_top10_warnings"] <=
                       hold["baseline"]["known_negative_above_target_top10_warnings"])
    quality_nonregression = (overall["treatment"]["hit@10"] >= overall["baseline"]["hit@10"]
                             and _not_decreased(overall["treatment"]["mrr@10"], overall["baseline"]["mrr@10"])
                             and hold["treatment"]["hit@10"] >= hold["baseline"]["hit@10"]
                             and _not_decreased(hold["treatment"]["mrr@10"], hold["baseline"]["mrr@10"]))
    positive_gain = (overall["treatment"]["hit@10"] > overall["baseline"]["hit@10"]
                     or (overall["treatment"]["mrr@10"] is not None
                         and overall["baseline"]["mrr@10"] is not None
                         and overall["treatment"]["mrr@10"] > overall["baseline"]["mrr@10"]))
    checks = {"all_33_pairs_valid": all_valid,
              "no_error_or_partial_growth": no_error_partial_growth,
              "no_overall_or_holdout_hit10_mrr10_decrease": quality_nonregression,
              "no_per_query_baseline_hit_lost": no_query_hit_loss,
              "no_group_all3_hit10_stability_decrease": no_group_decrease,
              "no_known_negative_alert_growth_overall_and_holdout": no_alert_growth,
              "positive_overall_final_hit10_or_mrr10_gain": positive_gain,
              "candidate_prefix_invariants_independently_observed": all(invariants) if invariants else None}
    release_pass = all(value is True for value in checks.values())
    return {"schema_version": 1, "evaluation": "paired_final_search_known_source_probe",
            "denominators": {"unconditional": 33, "strict_quality_expected": 33,
                             "original_expected": 21, "holdout_expected": 12},
            "strict_quality_counts": {"valid_pairs": len(valid_pairs), "failed_pairs": len(pairs)-len(valid_pairs),
                                      "missing_pairs": len(missing), "duplicate_identities": len(duplicates),
                                      "skipped_pairs": sum("reason" in item for item in missing),
                                      "error_pairs": sum(bool(row["error"]) for row in pairs),
                                      "partial_pairs": sum(bool(row["partial"]) for row in pairs),
                                      "extra_identities": len(extras), "malformed_records": len(malformed),
                                      "fixture_mismatches": len(fixture_mismatches)},
            "missing": missing, "duplicates": duplicates, "extras": extras, "malformed": malformed,
            "fixture_mismatches": fixture_mismatches,
            "runner_error": probe.get("runner_error"), "overall": overall,
            "per_fixture": fixture_metrics, "per_group": per_group,
            "paired_comparison": {"wins": wins, "losses": losses, "ties": ties,
                                  "regressed_target_queries": regressions,
                                  "all_baseline_ids_displaced": displaced},
            "candidate_counts_vs_final_quality": {
                "actual_appended_candidates_total": sum(v["appended_candidate_count"] or 0 for v in candidate_counts),
                "actual_appended_candidates_reported_queries": sum(v["appended_candidate_count"] is not None for v in candidate_counts),
                "baseline_stage_a_candidates_total_reported": sum(v["baseline_stage_a_candidate_count"] or 0 for v in candidate_counts),
                "treatment_stage_a_candidates_total_reported": sum(v["treatment_stage_a_candidate_count"] or 0 for v in candidate_counts),
                "final_top10_ids_total": {"baseline": sum(v["baseline_final_top10_count"] for v in candidate_counts),
                                          "treatment": sum(v["treatment_final_top10_count"] for v in candidate_counts)},
                "final_target_hits@10": {"baseline": overall["baseline"]["hit@10"],
                                         "treatment": overall["treatment"]["hit@10"]},
                "per_query": [{"identity": row["identity"], **row["candidate_counts"]} for row in pairs]},
            "latency_ms": {"baseline_complete_pairs": _describe([r["baseline_latency_ms"] for r in lat_pairs]),
                           "treatment_complete_pairs": _describe([r["treatment_latency_ms"] for r in lat_pairs]),
                           "paired_delta_treatment_minus_baseline": _describe(deltas),
                           "excluded_incomplete_pairs": 33-len(lat_pairs),
                           "scope": "complete pairs only; cached embeddings; SPLADE query encoding excluded; not a speed proof"},
            "candidate_invariants": {"runner_reported_values": [r["candidate_invariants"] for r in pairs
                                                                   if r["candidate_invariants"]],
                                     "status": (checks["candidate_prefix_invariants_independently_observed"]
                                                if invariants else "not_independently_observed")},
            "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "preregistered_strict_release_gate": {"checks": checks, "screen": "PASS" if release_pass else "FAIL",
                                                  "production_decision": PRODUCTION_DECISION},
            "warning": "Known-negative-above-target counts are inspection warnings, not precision estimates.",
            "pairs": per_query}


def _not_decreased(after, before):
    return after is not None and before is not None and after >= before


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--originalfixture", required=True, type=Path)
    parser.add_argument("--holdoutfixture", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = aggregate(json.loads(args.probe.read_text(encoding="utf-8")),
                       args.originalfixture, args.holdoutfixture)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
