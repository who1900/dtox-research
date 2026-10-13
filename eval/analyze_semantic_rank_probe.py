"""Aggregate raw semantic-rank probe JSON without joining fixture labels to hits.

Examples:
  python eval/analyze_semantic_rank_probe.py --inputs pilot.json --output pilot-report.json --mode pilot
  python eval/analyze_semantic_rank_probe.py --inputs source-a.json source-b.json --output source-report.json --mode source-only

Modes are explicit labels. ``source-only`` never reports CE quality, even if
an input happens to contain fallback-order metrics. ``pilot`` is deliberately
single-input so a small pilot cannot be pooled with a larger selected run.
"""

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path


MODES = ("source-only", "mixed", "pilot")


def _truth(row, field):
    return isinstance(row, dict) and row.get(field) is True


def _ce_actual(row):
    return (_truth(row, "ce_complete") and
            row.get("semantic_rank_status") == "reranked")


def _mrr_at_10(metrics):
    if not isinstance(metrics, dict):
        return None
    ranks = metrics.get("target_ranks")
    if not isinstance(ranks, dict):
        return None
    valid = [rank for rank in ranks.values()
             if isinstance(rank, int) and not isinstance(rank, bool) and 1 <= rank <= 10]
    return 1.0 / min(valid) if valid else 0.0


def _metric_summary(rows, field, eligibility):
    values = []
    for row in rows:
        if eligibility(row):
            metric = row.get(field)
            value = _mrr_at_10(metric)
            if value is not None:
                values.append(value)
    return {"n": len(values), "mrr_at_10": statistics.mean(values) if values else None}


def _hit_summary(rows, field, eligibility, cutoff, expected=None):
    values = []
    for row in rows:
        metric = row.get(field)
        if eligibility(row) and isinstance(metric, dict) and isinstance(metric.get("target_ranks"), dict):
            ranks = metric["target_ranks"].values()
            values.append(any(isinstance(rank, int) and not isinstance(rank, bool)
                              and 1 <= rank <= cutoff for rank in ranks))
    count = sum(values)
    denominator = len(values)
    return {f"known_target_hit_at_{cutoff}_count": count,
            "fraction_of_complete": count / denominator if denominator else None,
            "complete_denominator": denominator,
            "fraction_of_expected": count / expected if expected else None,
            "expected_denominator": expected,
            "cutoff": cutoff}


def _mrr_wins(rows, left_field, right_field, eligibility):
    outcomes = Counter()
    pairs = []
    for row in rows:
        if not eligibility(row):
            continue
        left = _mrr_at_10(row.get(left_field))
        right = _mrr_at_10(row.get(right_field))
        if left is None or right is None:
            continue
        pairs.append((left, right))
        outcomes["win"] += right > left
        outcomes["loss"] += right < left
        outcomes["tie"] += right == left
    return {"n": len(pairs), "wins": outcomes["win"], "losses": outcomes["loss"],
            "ties": outcomes["tie"], "meaning": "right_mrr_at_10_vs_left_mrr_at_10"}


def _hit_wins(rows, left_field, right_field, eligibility):
    outcomes = Counter()
    pairs = 0
    for row in rows:
        if not eligibility(row):
            continue
        left = _target_hit_at(row.get(left_field), 10)
        right = _target_hit_at(row.get(right_field), 10)
        if left is None or right is None:
            continue
        pairs += 1
        outcomes["win"] += right and not left
        outcomes["loss"] += left and not right
        outcomes["tie"] += left == right
    return {"n": pairs, "wins": outcomes["win"], "losses": outcomes["loss"],
            "ties": outcomes["tie"], "meaning": "right_binary_target_hit_at_10_vs_left"}


def _target_hit_at(metric, cutoff):
    if not isinstance(metric, dict) or not isinstance(metric.get("target_ranks"), dict):
        return None
    return any(isinstance(rank, int) and not isinstance(rank, bool) and 1 <= rank <= cutoff
               for rank in metric["target_ranks"].values())


def _timings(rows, timing_key, eligibility):
    values = []
    for row in rows:
        if not eligibility(row):
            continue
        value = (row.get("timing_ms") or {}).get(timing_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values.append(float(value))
    values.sort()
    if not values:
        return {"n": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None}
    return {"n": len(values), "mean_ms": statistics.mean(values),
            "p50_ms": values[(len(values) - 1) // 2],
            "p95_ms": values[min(len(values) - 1, math.ceil(len(values) * .95) - 1)]}


def _candidate_summary(rows):
    candidates = [candidate for row in rows
                  for candidate in (row.get("source_rank_provenance") or [])
                  if isinstance(candidate, dict)]
    candidate_counts = [row.get("candidate_count") for row in rows
                        if isinstance(row.get("candidate_count"), int)
                        and not isinstance(row.get("candidate_count"), bool)]
    count_distribution = Counter(candidate_counts)
    return {
        "count": len(candidates),
        "candidate_count_by_query": dict(sorted(count_distribution.items())),
        "queries_below_20_candidates": sum(count < 20 for count in candidate_counts),
        "unverified_source_leads": sum(candidate.get("verification_required") is True
                                         for candidate in candidates),
        "unknown_source_relevance": sum(candidate.get("passes_relevance_floor") is None
                                         for candidate in candidates),
        "below_relevance_floor": sum(candidate.get("passes_relevance_floor") is False
                                      for candidate in candidates),
        "identity_proof_established": False,
        "score_is_not_identity_or_evidence": True,
    }


def _scope_summary(rows, mode, expected=None):
    attempted = len(rows)
    baseline_complete = [row for row in rows if _truth(row, "baseline_complete")]
    source_complete = [row for row in rows if _truth(row, "source_complete")]
    paired_source = [row for row in rows if _truth(row, "baseline_complete") and
                     _truth(row, "source_complete")]
    ce_complete = [row for row in rows if _ce_actual(row)]
    fallback_types = Counter()
    for row in rows:
        if row.get("semantic_rank_status") == "fallback":
            fallback_types[row.get("scorer_error") or "unspecified_fallback"] += 1

    summary = {
        "attempted": attempted,
        "baseline_complete": len(baseline_complete),
        "baseline_incomplete_attempts": attempted - len(baseline_complete),
        "source_complete": len(source_complete),
        "source_incomplete_attempts": attempted - len(source_complete),
        "paired_source_complete": len(paired_source),
        "paired_source_incomplete_attempts": attempted - len(paired_source),
        "ce_actual_complete": len(ce_complete),
        "ce_actual_incomplete_attempts": attempted - len(ce_complete),
        "fallback_count": sum(fallback_types.values()),
        "fallback_types": dict(sorted(fallback_types.items())),
        "baseline_primary10": _metric_summary(rows, "baseline_metrics",
                                               lambda r: _truth(r, "baseline_complete")),
        "source10_equal_budget": _metric_summary(rows, "source_discovery10",
                                                  lambda r: _truth(r, "source_complete")),
        "source20_larger_budget_not_apples_to_apples": _metric_summary(
            rows, "source_discovery20", lambda r: _truth(r, "source_complete")),
        "baseline_vs_source10_mrr_at_10_complete_pairs": _mrr_wins(
            rows, "baseline_metrics", "source_discovery10",
            lambda r: _truth(r, "baseline_complete") and _truth(r, "source_complete")),
        "baseline_vs_source10_binary_hit_at_10_complete_pairs": _hit_wins(
            rows, "baseline_metrics", "source_discovery10",
            lambda r: _truth(r, "baseline_complete") and _truth(r, "source_complete")),
        "timing_ms": {
            "candidate_build_source_complete": _timings(
                rows, "candidate_build", lambda r: _truth(r, "source_complete")),
            "rerank_actual_ce_complete_only": _timings(rows, "rerank", _ce_actual),
            "rerank_fallback_attempts": _timings(
                rows, "rerank", lambda r: r.get("semantic_rank_status") == "fallback"),
        },
        "ce_metrics_included": mode != "source-only",
    }
    if mode != "source-only":
        summary["ce_actual_mrr_at_10"] = _metric_summary(
            rows, "cross_encoder10", _ce_actual)
        summary["source10_vs_ce_actual_mrr_at_10_complete_pairs"] = _mrr_wins(
            rows, "source_discovery10", "cross_encoder10", _ce_actual)
    else:
        summary["ce_actual_mrr_at_10"] = None
        summary["source10_vs_ce_actual_mrr_at_10_complete_pairs"] = None
    summary["source10_vs_source20_mrr_at_10_larger_budget_not_apples_to_apples"] = _mrr_wins(
        rows, "source_discovery10", "source_discovery20",
        lambda r: _truth(r, "source_complete"))
    summary["source10_vs_source20_binary_hit_at_10_larger_budget_not_apples_to_apples"] = _hit_wins(
        rows, "source_discovery10", "source_discovery20",
        lambda r: _truth(r, "source_complete"))
    summary["known_target_hits"] = {
        "baseline_primary10_at_10": _hit_summary(rows, "baseline_metrics",
            lambda r: _truth(r, "baseline_complete"), 10, expected),
        "source10_at_10": _hit_summary(rows, "source_discovery10",
            lambda r: _truth(r, "source_complete"), 10, expected),
        "source20_at_10": _hit_summary(rows, "source_discovery20",
            lambda r: _truth(r, "source_complete"), 10, expected),
        "source20_at_20": _hit_summary(rows, "source_discovery20",
            lambda r: _truth(r, "source_complete"), 20, expected),
    }
    if mode != "source-only":
        summary["known_target_hits"]["ce_actual_at_10"] = _hit_summary(
            rows, "cross_encoder10", _ce_actual, 10, expected)
    summary["candidate_verification"] = _candidate_summary(rows)
    summary["completion_rates"] = {
        "baseline_complete_over_expected": len(baseline_complete) / expected if expected else None,
        "source_complete_over_expected": len(source_complete) / expected if expected else None,
        "paired_source_complete_over_expected": len(paired_source) / expected if expected else None,
        "ce_actual_complete_over_expected": len(ce_complete) / expected if expected else None,
        "baseline_complete_over_attempted": len(baseline_complete) / attempted if attempted else None,
        "source_complete_over_attempted": len(source_complete) / attempted if attempted else None,
        "paired_source_complete_over_attempted": len(paired_source) / attempted if attempted else None,
        "ce_actual_complete_over_attempted": len(ce_complete) / attempted if attempted else None,
        "expected_denominator": expected, "attempted_denominator": attempted,
    }
    return summary


def _group_summary(rows, mode, raw_summary=None):
    groups = defaultdict(list)
    for row in rows:
        groups[(row.get("fixture"), row.get("group_id"))].append(row)
    output = {}
    for (fixture, group_id), group_rows in sorted(groups.items(), key=lambda pair: str(pair[0])):
        unique_phrases = {row.get("phrase_id") for row in group_rows}
        exact_three = len(group_rows) == 3 and len(unique_phrases) == 3 and None not in unique_phrases
        source_complete = exact_three and all(
            _truth(row, "baseline_complete") and _truth(row, "source_complete")
            for row in group_rows)
        source_hit_all = source_complete and all(
            (row.get("source_discovery10") or {}).get("hit") is True for row in group_rows)
        ce_complete = exact_three and mode != "source-only" and all(
            _ce_actual(row) for row in group_rows)
        ce_hit_all = ce_complete and all(
            (row.get("cross_encoder10") or {}).get("hit") is True for row in group_rows)
        output[f"{fixture}:{group_id}"] = {
            "fixture": fixture, "group_id": group_id, "attempted_rows": len(group_rows),
            "unique_phrases": len(unique_phrases), "expected_phrases": 3,
            "exact_three_complete_source_group": source_complete,
            "source10_hit_all_three": bool(source_hit_all),
            "exact_three_actual_ce_complete_group": ce_complete,
            "ce10_hit_all_three": bool(ce_hit_all),
        }
    phrase_groups = (raw_summary or {}).get("phrase_group_success") or {}
    for key, info in phrase_groups.items():
        if key in output or not isinstance(info, dict):
            continue
        attempted = info.get("attempted", 0)
        expected = info.get("expected", 3)
        output[key] = {"fixture_group_key": key, "attempted_rows": attempted,
                       "unique_phrases": attempted, "expected_phrases": expected,
                       "exact_three_complete_source_group": False,
                       "source10_hit_all_three": False,
                       "exact_three_actual_ce_complete_group": False,
                       "ce10_hit_all_three": False,
                       "group_reported_by_runner": True,
                       "runner_group_success": info.get("success") is True}
    return output


def _load_input(path):
    raw = Path(path).read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise ValueError(f"{path}: expected a JSON object containing records[]")
    return data, {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                  "bytes": len(raw)}


def _one_input(data, fingerprint, mode):
    rows = [row for row in data["records"] if isinstance(row, dict)]
    config = data.get("config") or {}
    expected = config.get("expected_query_count", config.get("selected_query_count"))
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        expected = len(rows) + len(data.get("skipped") or [])
    keys = [(row.get("fixture"), row.get("group_id"), row.get("phrase_id")) for row in rows]
    duplicate_keys = [key for key, count in Counter(keys).items() if count > 1]
    scopes = defaultdict(list)
    for row in rows:
        scopes[(row.get("fixture"), row.get("layer"))].append(row)
    group_summary = data.get("summary") or {}
    phrase_group_success = group_summary.get("phrase_group_success") or {}
    groups = _group_summary(rows, mode, group_summary)
    skipped = data.get("skipped") or []
    unscoped_skips = sum(not isinstance(row, dict) or not row.get("fixture") or
                         not row.get("group_id") for row in skipped)
    for skip in skipped:
        if isinstance(skip, dict) and skip.get("fixture") and skip.get("group_id"):
            key = f"{skip['fixture']}:{skip['group_id']}"
            groups.setdefault(key, {"fixture": skip["fixture"], "group_id": skip["group_id"],
                "attempted_rows": 0, "unique_phrases": 0, "expected_phrases": 3,
                "exact_three_complete_source_group": False, "source10_hit_all_three": False,
                "exact_three_actual_ce_complete_group": False, "ce10_hit_all_three": False,
                "known_skipped_group": True})
    return {
        "input": fingerprint,
        "mode": mode,
        "expected": expected,
        "selected": config.get("selected_query_count"),
        "attempted": len(rows),
        "skipped": len(data.get("skipped") or []),
        "invalid_record_rows": len(data["records"]) - len(rows),
        "duplicate_query_keys": len(duplicate_keys),
        "runner_operational": data.get("runner_operational"),
        "runner_error_present": bool(data.get("runner_error")),
        "summary": _scope_summary(rows, mode, expected),
        "groups": groups,
        "group_coverage": {"groups_reported": len(phrase_group_success),
                           "groups_with_attempted_records": len({(r.get("fixture"), r.get("group_id")) for r in rows}),
                           "skipped_rows_without_group_identity": unscoped_skips,
                           "complete_group_roster_known": unscoped_skips == 0},
        "by_fixture_layer": {
            f"{fixture}:{layer}": {"fixture": fixture, "layer": layer,
                                    **_scope_summary(scope, mode)}
            for (fixture, layer), scope in sorted(scopes.items(), key=lambda pair: str(pair[0]))
        },
    }, rows


def analyze(inputs, mode):
    """Read and independently summarize raw probe files; never join oracle rows."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    if mode == "pilot" and len(inputs) != 1:
        raise ValueError("pilot mode accepts exactly one input to prevent cohort pooling")
    reports, all_rows, fingerprints = [], [], []
    for path in inputs:
        data, fingerprint = _load_input(path)
        report, rows = _one_input(data, fingerprint, mode)
        reports.append(report)
        all_rows.extend(rows)
        fingerprints.append(fingerprint)
    # Repeated fixture/query keys are retained as separate attempts. All group
    # denominators stay input-scoped above; this combined view performs no joins.
    duplicate_keys = Counter((row.get("fixture"), row.get("group_id"), row.get("phrase_id"))
                             for row in all_rows)
    summary = _scope_summary(all_rows, mode, sum(item["expected"] for item in reports))
    return {
        "schema_version": 1,
        "mode": mode,
        "interpretation": ("Source retrieval only; CE metrics intentionally excluded."
                           if mode == "source-only" else
                           "CE metrics include only successful actual reranks with ce_complete=true."),
        "inputs": fingerprints,
        "input_reports": reports,
        "denominators": {"expected": sum(item["expected"] for item in reports),
                         "attempted": len(all_rows),
                         "skipped": sum(item["skipped"] for item in reports)},
        "combined_attempts": len(all_rows),
        "duplicate_fixture_group_phrase_keys_across_inputs": sum(
            count - 1 for count in duplicate_keys.values() if count > 1),
        "combined_summary": summary,
        "groups": reports[0]["groups"] if len(inputs) == 1 else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=MODES)
    args = parser.parse_args(argv)
    report = analyze(args.inputs, args.mode)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                      allow_nan=False) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    main()
