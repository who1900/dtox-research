"""Corrected, read-only analysis for saved large-search evaluation runs."""

import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

try:
    from .source_discovery_probe import _candidate_integrity
except ImportError:
    from source_discovery_probe import _candidate_integrity


VARIANTS = ("primary10", "primary20", "discovery10")
EXPECTED_QUERIES = 93
EXPECTED_GROUPS = 31
BOOTSTRAP_SEED = 20261004
BOOTSTRAP_ITERATIONS = 1000
OLD_FIXTURES = {"paraphrase_cases_20261003.json", "paraphrase_holdout_20261003.json"}
NEW_FIXTURES = {"paraphrase_fresh_20261004.json"}


def _norm(value):
    return str(value or "").strip().lower()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fixture_rows(paths):
    rows = []
    for path in paths:
        fixture_name = Path(path).name
        data = _read_json(path)
        for group in data.get("groups", []):
            target_ids = set()
            negative_ids = set()
            target_aliases = {}
            all_aliases = {}
            for field, accepted, alias_target in (
                ("targets", target_ids, target_aliases),
                ("hard_negatives", negative_ids, None),
            ):
                for item in group.get(field, []):
                    canonical = _norm(item.get("canonical_id", item.get("id")))
                    if canonical:
                        accepted.add(canonical)
                        all_aliases[canonical] = canonical
                        if alias_target is not None:
                            alias_target[canonical] = canonical
                    for raw_alias in item.get("aliases", []):
                        alias = _norm(raw_alias)
                        if alias:
                            accepted.add(alias)
                            all_aliases[alias] = canonical
                            if alias_target is not None:
                                alias_target[alias] = canonical
            for phrase in group.get("phrases", []):
                rows.append({
                    "fixture": fixture_name,
                    "fixture_path": str(Path(path)),
                    "group_id": str(group["id"]),
                    "phrase_id": str(phrase["id"]),
                    "layer": group.get("layer"),
                    "query": phrase.get("query"),
                    "targets": sorted(target_ids),
                    "target_aliases": target_aliases,
                    "accepted_target_ids": sorted(target_ids),
                    "hard_negatives": sorted(negative_ids),
                    "aliases_for_integrity": all_aliases,
                })
                rows[-1]["accepted_target_ids"] = sorted(
                    target_ids | {a for a, c in target_aliases.items() if c in target_ids})
    keys = [(r["fixture"], r["group_id"], r["phrase_id"]) for r in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("fixture query keys are not unique; rename duplicate fixture basenames or IDs")
    return rows


def _record_key(row):
    fixture = Path(str(row.get("fixture", ""))).name
    return fixture, str(row.get("group_id", "")), str(row.get("phrase_id", ""))


def _load_raw(paths):
    observations = defaultdict(list)
    observations_by_input = {}
    run_meta = []
    for path in paths:
        data = _read_json(path)
        input_name = str(Path(path))
        local = defaultdict(list)
        run_meta.append({
            "input": input_name,
            "runner_error": bool(data.get("runner_error")),
            "runner_error_type": ((data.get("runner_error") or {}).get("type")
                                  if isinstance(data.get("runner_error"), dict) else
                                  "string_error" if data.get("runner_error") else None),
            "stop_reason": data.get("stop_reason"),
            "runner_unattempted": data.get("unattempted"),
            "record_count": len(data.get("records", [])),
            "config": data.get("config", {}),
            "fixture_names": sorted({Path(str(name)).name for name in data.get("config", {}).get("fixtures", [])}),
        })
        for row in data.get("records", []):
            key = _record_key(row)
            observations[key].append((input_name, row))
            local[key].append((input_name, row))
        observations_by_input[input_name] = local
    return observations, run_meta, observations_by_input


def _raw_for_key(observations, key):
    found = observations.get(key, [])
    if not found:
        return None, False, []
    if len(found) == 1:
        return found[0][1], False, [found[0][0]]
    # Duplicate inputs are safe to merge only when their record payloads agree.
    serialized = [json.dumps(row, sort_keys=True, ensure_ascii=False, default=str) for _, row in found]
    if len(set(serialized)) == 1:
        return found[0][1], False, [name for name, _ in found]
    merged = dict(found[0][1])
    merged_variants = {}
    conflicts = False
    for name in VARIANTS:
        variants = [row.get("variants", {}).get(name) for _, row in found
                    if row.get("variants", {}).get(name) is not None]
        if not variants:
            continue
        encoded = [json.dumps(item, sort_keys=True, ensure_ascii=False, default=str) for item in variants]
        if len(set(encoded)) > 1:
            conflicts = True
        else:
            merged_variants[name] = variants[0]
    merged["variants"] = merged_variants
    return merged, conflicts, [name for name, _ in found]


def _objects(rows):
    return [row for row in (rows or []) if isinstance(row, dict)]


def _ids(result, objects_key, ids_key):
    objects = _objects(result.get(objects_key))
    if objects:
        return [_norm(row.get("arxiv_id")) for row in objects if row.get("arxiv_id")]
    return [_norm(value) for value in result.get(ids_key, []) if value]


def _independent_hit(result, fixture, variant, metric_name):
    primary = _ids(result, "primary", "primary_ids")
    candidates = _ids(result, "candidates", "candidate_ids")
    accepted = set(fixture["accepted_target_ids"])
    if variant == "primary10":
        actual = primary[:10]
    elif variant == "primary20":
        actual = primary[:20]
    else:
        actual = (primary[:10] + candidates[:10])[:20]
    if metric_name == "hit@10":
        actual = primary[:10]
    elif metric_name == "hit@20":
        actual = primary[:20]
    elif metric_name == "reach_hit@20":
        actual = ((primary[:20] + candidates[:20])[:20] if variant == "primary20"
                  else (primary[:10] + candidates[:10])[:20])
    return any(pid in accepted for pid in actual)


def _percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def _latency_stats(values):
    return {
        "n": len(values),
        "median_ms": round(_percentile(values, .5), 3) if values else None,
        "p95_ms": round(_percentile(values, .95), 3) if values else None,
    }


def _cluster_bootstrap(deltas, iterations=BOOTSTRAP_ITERATIONS, seed=BOOTSTRAP_SEED):
    """Percentile CIs for median and p95, resampling fixture/group clusters."""
    clusters = {key: list(values) for key, values in deltas.items() if values}
    if not clusters:
        return {"clusters": 0, "observations": 0, "iterations": iterations,
                "seed": seed, "median_ci_ms": None, "p95_ci_ms": None}
    rng = random.Random(seed)
    keys = sorted(clusters)
    boot_medians, boot_p95s = [], []
    for _ in range(iterations):
        sample = []
        for key in rng.choices(keys, k=len(keys)):
            sample.extend(clusters[key])
        boot_medians.append(_percentile(sample, .5))
        boot_p95s.append(_percentile(sample, .95))
    return {
        "clusters": len(clusters),
        "observations": sum(map(len, clusters.values())),
        "iterations": iterations,
        "seed": seed,
        "median_ci_ms": [round(_percentile(boot_medians, .025), 3),
                         round(_percentile(boot_medians, .975), 3)],
        "p95_ci_ms": [round(_percentile(boot_p95s, .025), 3),
                      round(_percentile(boot_p95s, .975), 3)],
    }


def _field_map(value, prefix=""):
    out = {}
    if isinstance(value, dict):
        for key, child in value.items():
            out.update(_field_map(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            out.update(_field_map(child, f"{prefix}[{index}]"))
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        out[prefix] = float(value)
    return out


def _raw_metrics(result):
    metrics = result.get("metrics") if isinstance(result, dict) else None
    return metrics if isinstance(metrics, dict) else {}


def _fixture_cohorts(fixtures):
    by_fixture = defaultdict(list)
    for row in fixtures:
        by_fixture[row["fixture"]].append(row)
    cohorts = {"old": [], "new": [], "other": []}
    for name, rows in by_fixture.items():
        stem = Path(name).stem.lower()
        if name in OLD_FIXTURES:
            label = "old"
        elif name in NEW_FIXTURES:
            label = "new"
        elif len(rows) == 33 or any(word in stem for word in ("old", "dev", "baseline")):
            label = "old"
        elif len(rows) == 60 or any(word in stem for word in ("new", "fresh")):
            label = "new"
        else:
            label = "other"
        cohorts[label].extend(rows)
    return cohorts, by_fixture


def _summarize_scope(planned, raw_rows, conflicts):
    variants_summary = {}
    complete_by_key = {}
    latency = {name: {"all_attempted": [], "complete": []} for name in VARIANTS}
    raw_hits = {name: Counter() for name in VARIANTS}
    negative_presence = {name: Counter() for name in VARIANTS}
    negative_occurrences = {name: Counter() for name in VARIANTS}
    warnings = {name: Counter() for name in VARIANTS}
    spot_checks = Counter()
    failure_kinds = {name: Counter() for name in VARIANTS}
    statuses = {name: Counter() for name in VARIANTS}
    source_failure_counts = {name: 0 for name in VARIANTS}
    spot_check_examples = []

    for fixture in planned:
        key = (fixture["fixture"], fixture["group_id"], fixture["phrase_id"])
        record = raw_rows.get(key)
        conflict = key in conflicts
        for name in VARIANTS:
            result = (record or {}).get("variants", {}).get(name)
            complete = bool(result and result.get("complete") and not conflict)
            complete_by_key[(key, name)] = complete
            if record is None:
                statuses[name]["unattempted"] += 1
            elif result is None:
                statuses[name]["missing_variant"] += 1
            elif complete:
                statuses[name]["complete"] += 1
            elif result.get("partial"):
                statuses[name]["partial_or_failed"] += 1
            else:
                statuses[name]["failed_or_conflicted"] += 1
            if conflict:
                failure_kinds[name]["duplicate_raw_conflict"] += 1
            if not result:
                continue
            elapsed = result.get("latency_ms")
            if isinstance(elapsed, (int, float)) and math.isfinite(elapsed):
                latency[name]["all_attempted"].append(float(elapsed))
                if complete:
                    latency[name]["complete"].append(float(elapsed))
            if not complete:
                kind = result.get("error") or ("partial" if result.get("partial") else "incomplete")
                failure_kinds[name][str(kind)] += 1
            for warning_key, value in (result.get("warnings") or {}).items() if isinstance(result.get("warnings"), dict) else []:
                warnings[name][str(warning_key)] += 1
            result_warnings = result.get("warnings")
            if isinstance(result_warnings, dict) and result_warnings.get("failed_sources"):
                source_failure_counts[name] += 1
            metrics = _raw_metrics(result)
            for metric_name in ("hit@10", "hit@20", "reach_hit@20",
                                "known_hard_negative_primary",
                                "known_hard_negative_candidate_unverified"):
                if complete and bool(metrics.get(metric_name)):
                    raw_hits[name][metric_name] += 1
            if complete:
                negatives = set(fixture["hard_negatives"])
                primary_ids = _ids(result, "primary", "primary_ids")
                candidate_ids = _ids(result, "candidates", "candidate_ids")
                negative_sets = {
                    "primary_top10": set(primary_ids[:10]) & negatives,
                    "primary_top20": set(primary_ids[:20]) & negatives,
                    "candidate_top10_unverified": set(candidate_ids[:10]) & negatives,
                    "primary10_plus_candidate10_unverified":
                        (set(primary_ids[:10]) | set(candidate_ids[:10])) & negatives,
                    "primary20_plus_candidate10_unverified":
                        (set(primary_ids[:20]) | set(candidate_ids[:10])) & negatives,
                }
                for label, ids_found in negative_sets.items():
                    negative_presence[name][label] += bool(ids_found)
                    negative_occurrences[name][label] += len(ids_found)
                for metric_name in ("hit@10", "hit@20", "reach_hit@20"):
                    reported = metrics.get(metric_name)
                    if isinstance(reported, bool):
                        independent = _independent_hit(result, fixture, name, metric_name)
                        spot_checks["checked"] += 1
                        if reported != independent:
                            spot_checks["mismatches"] += 1
                            if len(spot_check_examples) < 40:
                                spot_check_examples.append({
                                    "fixture": fixture["fixture"],
                                    "group_id": fixture["group_id"],
                                    "phrase_id": fixture["phrase_id"],
                                    "variant": name,
                                    "metric": metric_name,
                                    "raw_metric": reported,
                                    "fixture_id_check": independent,
                                    "fixture_accepted_target_ids": fixture["accepted_target_ids"],
                                    "primary_ids": _ids(result, "primary", "primary_ids")[:20],
                                    "candidate_ids": _ids(result, "candidates", "candidate_ids")[:20],
                                })

    for name in VARIANTS:
        mrr_all = []
        mrr_complete = []
        json_bytes = json_chars = 0
        for fixture in planned:
            key = (fixture["fixture"], fixture["group_id"], fixture["phrase_id"])
            result = (raw_rows.get(key) or {}).get("variants", {}).get(name)
            if not result:
                mrr_all.append(0.0)
                continue
            value = _raw_metrics(result).get("mrr", 0.0)
            mrr = float(value) if isinstance(value, (int, float)) and math.isfinite(value) else 0.0
            complete = complete_by_key[(key, name)]
            mrr_all.append(mrr if complete else 0.0)
            if complete:
                mrr_complete.append(mrr)
            json_bytes += int(result.get("json_bytes") or 0)
            json_chars += int(result.get("json_chars") or 0)
        variants_summary[name] = {
            "denominator_planned_queries": len(planned),
            "complete": statuses[name]["complete"],
            "failed_partial_missing_or_conflicted": len(planned) - statuses[name]["complete"],
            "status_counts": dict(statuses[name]),
            "failure_kinds": dict(failure_kinds[name]),
            "raw_metric_hits_failure_inclusive": dict(raw_hits[name]),
            "known_hard_negative_queries_needing_read": dict(negative_presence[name]),
            "known_hard_negative_unique_ids_needing_read": dict(negative_occurrences[name]),
            "raw_mrr_mean_failure_inclusive": round(statistics.mean(mrr_all), 6) if mrr_all else None,
            "raw_mrr_mean_complete_only": round(statistics.mean(mrr_complete), 6) if mrr_complete else None,
            "latency_ms": {sample: _latency_stats(vals) for sample, vals in latency[name].items()},
            "json_chars_sum": json_chars,
            "json_utf8_bytes_sum": json_bytes,
            "warning_key_counts": dict(warnings[name]),
            "rows_with_failed_sources_warning": source_failure_counts[name],
        }

    complete_pairs = {"primary10_vs_discovery10": [], "primary20_vs_discovery10": []}
    complete_triplets = []
    pair_counts = {key: Counter() for key in complete_pairs}
    slot_stats = Counter()
    id_same_stats = Counter()
    object_same_stats = Counter()
    numeric_changes = defaultdict(list)
    paired_latency = {key: defaultdict(list) for key in complete_pairs}
    integrity_counts = Counter()
    integrity_examples = []
    misses = []

    for fixture in planned:
        key = (fixture["fixture"], fixture["group_id"], fixture["phrase_id"])
        record = raw_rows.get(key)
        variants = (record or {}).get("variants", {})
        complete = {name: complete_by_key[(key, name)] for name in VARIANTS}
        if all(complete.values()):
            complete_triplets.append(key)
        p10, p20, d10 = (variants.get(name) for name in VARIANTS)
        if complete["primary20"] and complete["discovery10"]:
            complete_pairs["primary20_vs_discovery10"].append((fixture, p20, d10))
            left = bool(_raw_metrics(p20).get("hit@20"))
            right = bool(_raw_metrics(d10).get("reach_hit@20"))
            pair_counts["primary20_vs_discovery10"]["wins" if right and not left else
                "losses" if left and not right else "ties"] += 1
            left_ids = _ids(p20, "primary", "primary_ids")[:20]
            right_ids = (_ids(d10, "primary", "primary_ids")[:10] +
                         _ids(d10, "candidates", "candidate_ids")[:10])[:20]
            slot_stats["pairs"] += 1
            slot_stats["exact_sequence_equal"] += left_ids == right_ids
            slot_stats["same_id_slots"] += sum(a == b for a, b in zip(left_ids, right_ids))
            slot_stats["compared_slots"] += max(len(left_ids), len(right_ids))
            slot_stats["common_prefix_slots"] += next((i for i, (a, b) in enumerate(zip(left_ids, right_ids)) if a != b), min(len(left_ids), len(right_ids)))
            lt, rt = p20.get("latency_ms"), d10.get("latency_ms")
            if all(isinstance(x, (int, float)) and math.isfinite(x) for x in (lt, rt)):
                paired_latency["primary20_vs_discovery10"][(fixture["fixture"], fixture["group_id"])].append(float(rt) - float(lt))
        if complete["primary10"] and complete["discovery10"]:
            complete_pairs["primary10_vs_discovery10"].append((fixture, p10, d10))
            p_ids = _ids(p10, "primary", "primary_ids")[:10]
            d_ids = _ids(d10, "primary", "primary_ids")[:10]
            same_ids = p_ids == d_ids
            id_same_stats["pairs"] += 1
            id_same_stats["same"] += same_ids
            p_obj, d_obj = p10.get("primary"), d10.get("primary")
            if isinstance(p_obj, list) and isinstance(d_obj, list):
                same_obj = json.dumps(p_obj, ensure_ascii=False, sort_keys=True, default=str) == json.dumps(d_obj, ensure_ascii=False, sort_keys=True, default=str)
                object_same_stats["pairs"] += 1
                object_same_stats["same"] += same_obj
                if same_ids and len(p_obj) == len(d_obj):
                    for left, right in zip(p_obj, d_obj):
                        for field, value in _field_map(right).items():
                            old = _field_map(left).get(field)
                            if old is not None:
                                numeric_changes[field].append(value - old)
            lt, rt = p10.get("latency_ms"), d10.get("latency_ms")
            if all(isinstance(x, (int, float)) and math.isfinite(x) for x in (lt, rt)):
                paired_latency["primary10_vs_discovery10"][(fixture["fixture"], fixture["group_id"])].append(float(rt) - float(lt))

        if record:
            candidates = d10.get("candidates", []) if isinstance(d10, dict) else []
            primary = d10.get("primary", []) if isinstance(d10, dict) else []
            integrity = _candidate_integrity(candidates, primary,
                fixture["aliases_for_integrity"] | fixture["target_aliases"])
            integrity_counts["rows"] += 1
            integrity_counts["valid"] += bool(integrity.get("valid"))
            integrity_counts["invalid"] += not bool(integrity.get("valid"))
            for error in integrity.get("errors", []):
                integrity_counts[f"error:{error}"] += 1
                if len(integrity_examples) < 20:
                    integrity_examples.append({"fixture": fixture["fixture"], "group_id": fixture["group_id"],
                                               "phrase_id": fixture["phrase_id"], "error": error})
        hit_flags = {
            "primary10": bool(_raw_metrics(p10).get("hit@10")) if complete["primary10"] else False,
            "primary20": bool(_raw_metrics(p20).get("hit@20")) if complete["primary20"] else False,
            "discovery_reach20": bool(_raw_metrics(d10).get("reach_hit@20")) if complete["discovery10"] else False,
        }
        if not any(hit_flags.values()):
            misses.append({"fixture": fixture["fixture"], "group_id": fixture["group_id"],
                "phrase_id": fixture["phrase_id"], "layer": fixture["layer"],
                "target_indexed_raw": (record or {}).get("target_indexed"),
                "hits_failure_inclusive": hit_flags,
                "complete": complete})

    pair_comparison = {}
    for name, pairs in complete_pairs.items():
        pair_comparison[name] = {"common_complete_pairs": len(pairs), **dict(pair_counts[name])}
        deltas = paired_latency[name]
        flat = [value for values in deltas.values() for value in values]
        pair_comparison[name]["latency_delta_discovery_minus_primary_ms"] = {
            **_latency_stats(flat), "cluster_bootstrap_95ci": _cluster_bootstrap(deltas)}
    pair_comparison["primary10_vs_discovery10"]["same_primary_ids"] = {
        "n": id_same_stats["pairs"], "equal": id_same_stats["same"],
        "different": id_same_stats["pairs"] - id_same_stats["same"]}
    pair_comparison["primary10_vs_discovery10"]["same_primary_result_objects"] = {
        "n": object_same_stats["pairs"], "equal": object_same_stats["same"],
        "different": object_same_stats["pairs"] - object_same_stats["same"]}
    pair_comparison["primary10_vs_discovery10"]["numeric_field_changes_treatment_minus_baseline"] = {
        field: {"n": len(values), "changed": sum(v != 0 for v in values),
                "median_delta": round(_percentile(values, .5), 6),
                "p95_abs_delta": round(_percentile(sorted(abs(v) for v in values), .95), 6)}
        for field, values in sorted(numeric_changes.items())}
    pair_comparison["primary20_vs_discovery10"]["equal_20_slot_ids"] = {
        "n": slot_stats["pairs"], "exact_sequence_equal": slot_stats["exact_sequence_equal"],
        "same_id_slots": slot_stats["same_id_slots"], "compared_slots": slot_stats["compared_slots"],
        "common_prefix_slots_sum": slot_stats["common_prefix_slots"]}

    misses.sort(key=lambda row: (row.get("target_indexed_raw") is True,
                                 sum(row["hits_failure_inclusive"].values()) == 0), reverse=True)
    return {
        "planned_queries": len(planned),
        "raw_records_present": sum((r["fixture"], r["group_id"], r["phrase_id"]) in raw_rows for r in planned),
        "unattempted_planned_queries": sum((r["fixture"], r["group_id"], r["phrase_id"]) not in raw_rows for r in planned),
        "duplicate_raw_conflict_keys": len(conflicts),
        "complete_triplets": len(complete_triplets),
        "common_complete_pairs": {name: len(pairs) for name, pairs in complete_pairs.items()},
        "variants": variants_summary,
        "paired_comparisons": pair_comparison,
        "metric_spotcheck": {"checked_raw_boolean_metrics": spot_checks["checked"],
                             "mismatches_vs_fixture_target_ids": spot_checks["mismatches"],
                             "examples": spot_check_examples},
        "candidate_integrity_discovery10": {**dict(integrity_counts), "examples": integrity_examples},
        "all_three_miss_queries": misses[:40],
        "all_three_miss_count": len(misses),
    }


def _group_stability(planned, raw_rows, conflicts):
    groups = defaultdict(list)
    for row in planned:
        groups[(row["fixture"], row["group_id"])].append(row)
    output = {}
    for (fixture, group_id), rows in sorted(groups.items()):
        phrase_n = len(rows)
        counts = {"primary10": 0, "primary20": 0, "discovery_reach20": 0}
        all_complete = {name: True for name in counts}
        for row in rows:
            key = (fixture, group_id, row["phrase_id"])
            record = raw_rows.get(key)
            variants = (record or {}).get("variants", {})
            selections = (("primary10", "primary10", "hit@10"),
                          ("primary20", "primary20", "hit@20"),
                          ("discovery_reach20", "discovery10", "reach_hit@20"))
            for metric_key, variant, metric in selections:
                result = variants.get(variant)
                complete = bool(result and result.get("complete") and key not in conflicts)
                if not complete:
                    all_complete[metric_key] = False
                if complete and bool(_raw_metrics(result).get(metric)):
                    counts[metric_key] += 1
        planned_three = phrase_n == 3
        output[f"{fixture}:{group_id}"] = {
            "planned_phrases": phrase_n,
            "requires_exactly_three": planned_three,
            "recorded_phrases": sum((fixture, group_id, row["phrase_id"]) in raw_rows for row in rows),
            "complete_hit_phrases": counts,
            "complete_all_planned_phrases": all_complete,
            "stable_hit_all_three_phrases": {
                metric: bool(planned_three and all_complete[metric] and count == 3)
                for metric, count in counts.items()},
            "interpretation": ("diagnostic_only_not_quality_regression" if
                               any(row.get("layer") == "builder-tech" for row in rows) else None),
        }
    return output


def _layer_summary(planned, raw_rows, conflicts):
    output = {}
    for layer in sorted({str(row.get("layer")) for row in planned}):
        rows = [row for row in planned if str(row.get("layer")) == layer]
        output[layer] = {"planned_queries": len(rows), "variants": {}}
        if layer == "builder-tech":
            output[layer]["interpretation"] = "diagnostic_only_not_a_genuine_quality_regression_cohort"
        for variant, metric in (("primary10", "hit@10"), ("primary20", "hit@20"),
                                ("discovery10", "reach_hit@20")):
            complete = hit = 0
            for row in rows:
                key = (row["fixture"], row["group_id"], row["phrase_id"])
                result = (raw_rows.get(key) or {}).get("variants", {}).get(variant)
                if result and result.get("complete") and key not in conflicts:
                    complete += 1
                    hit += bool(_raw_metrics(result).get(metric))
            output[layer]["variants"][variant] = {"complete": complete,
                "failed_or_unattempted_counted_as_miss": len(rows) - complete,
                "hits_failure_inclusive": hit}
    return output


def _coverage(fixtures, raw_runs):
    target_ids = sorted({target for row in fixtures for target in row["accepted_target_ids"]})
    reports = []
    for run in raw_runs:
        data = run["data"]
        raw_coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
        per_fixture = {}
        for name in sorted({row["fixture"] for row in fixtures}):
            target_ids_for_fixture = sorted({target for row in fixtures if row["fixture"] == name
                                              for target in row["accepted_target_ids"]})
            present = sum(bool(raw_coverage.get(pid)) for pid in target_ids_for_fixture)
            fixture_rows = [row for row in fixtures if row["fixture"] == name]
            raw_records = {_record_key(row): row for row in data.get("records", [])}
            recomputed_rows = sum(any(bool(raw_coverage.get(pid)) for pid in row["accepted_target_ids"])
                                  for row in fixture_rows)
            raw_indexed_rows = sum(bool((raw_records.get((row["fixture"], row["group_id"], row["phrase_id"])) or {}).get("target_indexed"))
                                   for row in fixture_rows)
            per_fixture[name] = {"target_ids_only": len(target_ids_for_fixture),
                                 "indexed_target_ids": present,
                                 "not_indexed_or_missing_from_raw_coverage": len(target_ids_for_fixture) - present,
                                 "planned_query_rows": len(fixture_rows),
                                 "target_indexed_rows_recomputed_from_target_ids_only": recomputed_rows,
                                 "target_indexed_rows_as_recorded_by_runner": raw_indexed_rows}
        reports.append({"input": run["input"],
                        "target_ids_only": len(target_ids),
                        "indexed_target_ids": sum(bool(raw_coverage.get(pid)) for pid in target_ids),
                        "not_indexed_or_missing_from_raw_coverage": sum(not bool(raw_coverage.get(pid)) for pid in target_ids),
                        "by_fixture": per_fixture})
    return reports


def analyze(inputs, fixture_paths):
    planned = _fixture_rows(fixture_paths)
    observations, run_meta, observations_by_input = _load_raw(inputs)
    raw_rows, conflicts = {}, set()
    for key in {(r["fixture"], r["group_id"], r["phrase_id"]) for r in planned}:
        record, conflict, _ = _raw_for_key(observations, key)
        if record is not None:
            raw_rows[key] = record
        if conflict:
            conflicts.add(key)
    unexpected = sorted(set(observations) - {(r["fixture"], r["group_id"], r["phrase_id"]) for r in planned})
    _, by_fixture = _fixture_cohorts(planned)
    overall = _summarize_scope(planned, raw_rows, conflicts)
    cohorts = {"old": None, "new": None, "other": None}
    cohort_sources = {name: [] for name in cohorts}
    for meta in run_meta:
        stem = Path(meta["input"]).stem.lower()
        fixture_names = set(meta["fixture_names"])
        if stem in ("dev", "old", "baseline") or "old" in stem:
            label = "old"
        elif stem in ("fresh", "new", "treatment") or "fresh" in stem or "new" in stem:
            label = "new"
        else:
            selected = [row for row in planned if row["fixture"] in fixture_names]
            n = len(selected)
            label = "old" if n == 33 else "new" if n == 60 else "other"
        cohort_sources[label].append(meta["input"])
    for label, source_paths in cohort_sources.items():
        if not source_paths:
            continue
        names = {name for meta in run_meta if meta["input"] in source_paths for name in meta["fixture_names"]}
        rows = [row for row in planned if row["fixture"] in names]
        local_observations = defaultdict(list)
        for source in source_paths:
            for key, values in observations_by_input[source].items():
                local_observations[key].extend(values)
        local_rows, local_conflicts = {}, set()
        for key in {(row["fixture"], row["group_id"], row["phrase_id"]) for row in rows}:
            item, conflict, _ = _raw_for_key(local_observations, key)
            if item is not None:
                local_rows[key] = item
            if conflict:
                local_conflicts.add(key)
        cohorts[label] = _summarize_scope(rows, local_rows, local_conflicts)
    fixture_summaries = {name: _summarize_scope(rows, raw_rows, conflicts)
                         for name, rows in by_fixture.items()}
    group_rows = _group_stability(planned, raw_rows, conflicts)
    expected_groups = len({(r["fixture"], r["group_id"]) for r in planned})
    group_phrase_counts = Counter()
    for key in {(r["fixture"], r["group_id"]) for r in planned}:
        group_phrase_counts[sum((r["fixture"], r["group_id"]) == key for r in planned)] += 1
    return {
        "schema_version": 1,
        "method": "independent post-run summary; fixture target IDs/aliases; raw variant metrics retained separately",
        "planned_denominator": {"queries": len(planned), "groups": expected_groups,
                                "expected_queries": EXPECTED_QUERIES,
                                "expected_groups": EXPECTED_GROUPS,
                                "matches_expected": len(planned) == EXPECTED_QUERIES and expected_groups == EXPECTED_GROUPS,
                                "group_phrase_counts": dict(group_phrase_counts)},
        "inputs": run_meta,
        "raw_record_keys_not_in_fixtures": [{"fixture": f, "group_id": g, "phrase_id": p}
                                             for f, g, p in unexpected],
        "overall": overall,
        "cohorts_old_33_new_60": cohorts,
        "by_fixture": fixture_summaries,
        "by_layer": _layer_summary(planned, raw_rows, conflicts),
        "groups": group_rows,
        "diagnostic_builder_tech": {
            "interpretation": "mechanism/tool-routing diagnostics; not a genuine quality-regression cohort",
            "planned_queries": sum(row.get("layer") == "builder-tech" for row in planned),
            "groups": [key for key, value in group_rows.items()
                       if value.get("interpretation") == "diagnostic_only_not_quality_regression"],
        },
        "interpretation_flags": [{
            "group": key,
            "note": "The fixture target is not exhaustive; a Zerocash result may be correct despite the Zcash target being absent.",
        } for key in group_rows if "zcash-succinct-proof-verification" in key.lower()],
        "target_coverage_only": _coverage(planned, [
            {"input": str(Path(path)), "data": _read_json(path)} for path in inputs]),
        "notes": [
            "Failures, partial results and unattempted fixture queries contribute zero hits to failure-inclusive totals.",
            "Target spot-check accepts only fixture canonical IDs and declared target aliases; raw metrics are not rewritten.",
            "Hard-negative presence is a read/inspect signal, not a precision or falsity judgment.",
            "Latency confidence intervals resample (fixture, group) clusters, not individual phrases.",
            "Output-size measurements are JSON characters/UTF-8 bytes, never token estimates.",
            f"Builder-tech diagnostic cohort: {sum(row.get('layer') == 'builder-tech' for row in planned)} queries; do not interpret as a genuine quality regression.",
            "Zcash target absence is not exhaustive evidence: a Zerocash result may be correct and requires mechanism review.",
        ],
    }


def _public_report(report):
    public = {key: report[key] for key in (
        "planned_denominator", "overall", "cohorts_old_33_new_60", "by_fixture",
        "by_layer", "groups", "target_coverage_only", "notes")}
    public["target_coverage_only"] = [
        {key: value for key, value in item.items() if key != "input"}
        | {"input_name": Path(item["input"]).name}
        for item in public["target_coverage_only"]]
    return public


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True, type=Path)
    parser.add_argument("--fixtures", nargs="+", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--public-output", type=Path,
                        help="optional sanitized aggregate JSON without raw input paths/config")
    args = parser.parse_args(argv)
    report = analyze(args.inputs, args.fixtures)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
                           encoding="utf-8")
    if args.public_output:
        args.public_output.parent.mkdir(parents=True, exist_ok=True)
        args.public_output.write_text(json.dumps(_public_report(report), ensure_ascii=False,
                                                  indent=2, default=str) + "\n",
                                      encoding="utf-8")
    return report


if __name__ == "__main__":
    main()
