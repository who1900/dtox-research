import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[2] / "eval" / "analyze_splade_candidate_probe.py"
SPEC = importlib.util.spec_from_file_location("analyze_splade_candidate_probe", MODULE_PATH)
analyzer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(analyzer)


def _write_fixture(path, groups):
    path.write_text(json.dumps({"groups": groups}), encoding="utf-8")


def _fixtures(tmp_path):
    paths = {}
    for label, count in (("original", 7), ("holdout", 4)):
        groups = [{"id": f"{label}-g{i}", "layer": "web3",
                   "targets": [{"canonical_id": "target", "aliases": ["target-alias"]}],
                   "hard_negatives": [{"id": "negative", "aliases": ["negative-alias"]}],
                   "phrases": [{"id": f"q{j}", "query": f"query {i} {j}"} for j in range(3)]}
                  for i in range(count)]
        path = tmp_path / f"{label}.json"
        _write_fixture(path, groups)
        paths[label] = path
    return paths


def _record(fixture, group, phrase, base=("target",), treatment=("target",),
            baseline_ms=10, treatment_ms=12, partial=False):
    def variant(ids, latency):
        return {"results": [{"arxiv_id": value} for value in ids],
                "latency_ms": latency, "partial": partial}
    return {"fixture": fixture, "group_id": group, "phrase_id": phrase,
            "query": f"query {group.rsplit('g', 1)[-1]} {phrase[1:]}", "layer": "web3",
            "baseline": variant(base, baseline_ms),
            "treatment": {**variant(treatment, treatment_ms), "added_ids": ["added-paper"],
                          "lexical_calls": [{"original_prefix_preserved": True,
                                             "lexical_unchanged": True,
                                             "appended_ids": ["added-paper"],
                                             "paper_ids_after": ["baseline-paper", "added-paper"]}]}}


def _full_probe(paths, transform=None):
    records = []
    for label, path in paths.items():
        count = 7 if label == "original" else 4
        for i in range(count):
            for j in range(3):
                record = _record(path.name, f"{label}-g{i}", f"q{j}")
                records.append(transform(record, label, i, j) if transform else record)
    return {"records": records, "skipped": [], "runner_error": None}


def test_missing_pair_keeps_unconditional_denominator_and_fails_gate(tmp_path):
    paths = _fixtures(tmp_path)
    probe = _full_probe(paths)
    probe["records"].pop()
    probe["skipped"] = [{"index": 32, "query": "query 3 2", "reason": "budget_exhausted"}]
    report = analyzer.aggregate(probe, paths["original"], paths["holdout"])
    assert report["overall"]["unconditional_denominator"] == 33
    assert report["overall"]["strict_quality_denominator"] == 32
    assert report["strict_quality_counts"]["missing_pairs"] == 1
    assert report["strict_quality_counts"]["skipped_pairs"] == 1
    assert report["preregistered_strict_release_gate"]["screen"] == "FAIL"


def test_alias_matching_alert_warning_and_timer_excludes_incomplete_pair(tmp_path):
    paths = _fixtures(tmp_path)
    def mutate(record, label, i, j):
        if label == "original" and i == 0 and j == 0:
            record["baseline"]["results"] = [{"arxiv_id": "negative-alias"},
                                              {"arxiv_id": "target-alias"}]
            record["treatment"]["results"] = [{"arxiv_id": "target-alias"}]
        if label == "holdout" and i == 0 and j == 0:
            record["treatment"]["partial"] = True
        return record
    report = analyzer.aggregate(_full_probe(paths, mutate), paths["original"], paths["holdout"])
    assert report["overall"]["baseline"]["known_negative_above_target_top10_warnings"] == 1
    assert report["overall"]["baseline"]["hit@10"] == 32
    assert report["overall"]["baseline"]["unconditional_variant_observation"]["hit@10"] == 33
    assert report["overall"]["treatment"]["unconditional_variant_observation"]["hit@10"] == 32
    assert report["overall"]["treatment"]["unconditional_variant_observation"]["denominator"] == 33
    assert report["latency_ms"]["excluded_incomplete_pairs"] == 1
    assert report["latency_ms"]["paired_delta_treatment_minus_baseline"]["n"] == 32
    assert report["candidate_invariants"]["status"] is True
    assert report["candidate_counts_vs_final_quality"]["actual_appended_candidates_total"] == 33
    assert "not precision" in report["warning"]


def test_all_group_failures_are_reported_and_trigger_gate(tmp_path):
    paths = _fixtures(tmp_path)
    def mutate(record, label, i, j):
        if label == "original" and i == 1:
            record["baseline"]["results"] = [{"arxiv_id": "other"}]
            record["treatment"]["results"] = [{"arxiv_id": "other"}]
        return record
    report = analyzer.aggregate(_full_probe(paths, mutate), paths["original"], paths["holdout"])
    group = report["per_group"]["original.json/original-g1"]
    assert group["baseline"]["all_3_hit@10"] is False
    assert group["treatment"]["all_3_hit@10"] is False
    assert group["all_3_hit@10_stable_or_better"] is True
    assert report["preregistered_strict_release_gate"]["production_decision"] == analyzer.PRODUCTION_DECISION


def test_query_hit_regression_displacement_and_duplicate_aliases(tmp_path):
    paths = _fixtures(tmp_path)
    def mutate(record, label, i, j):
        if label == "original" and i == 0 and j == 0:
            record["baseline"]["results"] = [{"arxiv_id": "base-a"}, {"arxiv_id": "target-alias"}]
            record["treatment"]["results"] = [{"arxiv_id": "base-a"}, {"arxiv_id": "other"}]
        return record
    report = analyzer.aggregate(_full_probe(paths, mutate), paths["original"], paths["holdout"])
    assert report["paired_comparison"]["regressed_target_queries"] == [
        ["original.json", "original-g0", "q0"]]
    displaced = report["paired_comparison"]["all_baseline_ids_displaced"]
    assert displaced[0]["displaced_baseline_top10_ids"] == ["target-alias"]
    assert report["preregistered_strict_release_gate"]["checks"]["no_per_query_baseline_hit_lost"] is False


def test_nonregression_with_no_positive_gain_is_not_a_pass(tmp_path):
    paths = _fixtures(tmp_path)
    report = analyzer.aggregate(_full_probe(paths), paths["original"], paths["holdout"])
    checks = report["preregistered_strict_release_gate"]["checks"]
    assert checks["no_overall_or_holdout_hit10_mrr10_decrease"] is True
    assert checks["positive_overall_final_hit10_or_mrr10_gain"] is False
    assert report["preregistered_strict_release_gate"]["screen"] == "FAIL"


def test_aliases_are_one_target_oracle_and_ranked_correctly(tmp_path):
    paths = _fixtures(tmp_path)
    def mutate(record, label, i, j):
        if label == "original" and i == 0 and j == 0:
            record["treatment"]["results"] = [{"arxiv_id": "other"},
                                                {"arxiv_id": "target-alias"}]
        return record
    report = analyzer.aggregate(_full_probe(paths, mutate), paths["original"], paths["holdout"])
    row = next(row for row in report["pairs"] if row["identity"] == ["original.json", "original-g0", "q0"])
    assert row["treatment_quality"]["target_rank_top10"] == 2
    assert row["treatment_quality"]["mrr@10"] == .5


def test_duplicate_extra_and_fixture_query_mismatch_fail_join(tmp_path):
    paths = _fixtures(tmp_path)
    probe = _full_probe(paths)
    probe["records"].append(dict(probe["records"][0]))
    probe["records"].append({"fixture": "unexpected.json", "group_id": "x", "phrase_id": "x"})
    probe["records"][1]["query"] = "wrong frozen query"
    report = analyzer.aggregate(probe, paths["original"], paths["holdout"])
    assert report["strict_quality_counts"]["duplicate_identities"] == 1
    assert report["strict_quality_counts"]["extra_identities"] == 1
    assert report["fixture_mismatches"] == [{"identity": ["original.json", "original-g0", "q1"], "field": "query"}]
    assert report["preregistered_strict_release_gate"]["checks"]["all_33_pairs_valid"] is False


def test_partial_counts_fail_strict_pair_but_preserve_unconditional_baseline(tmp_path):
    paths = _fixtures(tmp_path)
    def mutate(record, label, i, j):
        if label == "holdout" and i == 0 and j == 0:
            record["treatment"]["partial"] = True
        return record
    report = analyzer.aggregate(_full_probe(paths, mutate), paths["original"], paths["holdout"])
    counts = report["strict_quality_counts"]
    assert counts["partial_pairs"] == 1
    assert counts["failed_pairs"] == 1
    assert report["per_fixture"]["holdout"]["baseline"]["unconditional_variant_observation"]["hit@10"] == 12
    assert report["per_fixture"]["holdout"]["treatment"]["unconditional_variant_observation"]["usable_results"] == 11
    assert report["overall"]["baseline"]["unconditional_variant_observation"]["mrr@10"] == 1.0
    assert report["overall"]["treatment"]["unconditional_variant_observation"]["mrr@10"] == 32 / 33
    assert report["overall"]["baseline"]["unconditional_variant_observation"]["error_pairs"] == 0
    assert report["overall"]["treatment"]["unconditional_variant_observation"]["partial_pairs"] == 1


def test_unreported_prefix_invariant_is_unknown_and_holds_gate(tmp_path):
    paths = _fixtures(tmp_path)
    probe = _full_probe(paths)
    for record in probe["records"]:
        record["treatment"]["lexical_calls"] = [{"appended_ids": [], "paper_ids_after": ["target"]}]
    report = analyzer.aggregate(probe, paths["original"], paths["holdout"])
    assert report["candidate_invariants"]["status"] == "not_independently_observed"
    assert report["preregistered_strict_release_gate"]["checks"]["candidate_prefix_invariants_independently_observed"] is None
    assert report["preregistered_strict_release_gate"]["screen"] == "FAIL"


def test_group_all3_hit10_regression_is_detected(tmp_path):
    paths = _fixtures(tmp_path)
    def mutate(record, label, i, j):
        if label == "original" and i == 0:
            record["treatment"]["results"] = [{"arxiv_id": "other"}]
        return record
    report = analyzer.aggregate(_full_probe(paths, mutate), paths["original"], paths["holdout"])
    group = report["per_group"]["original.json/original-g0"]
    assert group["baseline"]["all_3_hit@10"] is True
    assert group["treatment"]["all_3_hit@10"] is False
    assert group["all_3_hit@10_stable_or_better"] is False
    assert report["preregistered_strict_release_gate"]["checks"]["no_group_all3_hit10_stability_decrease"] is False
