import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from eval.analyze_semantic_rank_probe import analyze


def record(phrase, *, fixture="fixture-a", group="shared", layer="web3",
           baseline=True, source=True, ce=False, baseline_rank=2, source_rank=1,
           source20_rank=None, ce_rank=1, status=None, error=None, rerank_ms=50):
    def metrics(rank):
        return {"target_ranks": {"canonical-target": rank}, "hit": rank is not None}
    return {
        "fixture": fixture, "group_id": group, "phrase_id": phrase, "layer": layer,
        "baseline_complete": baseline, "source_complete": source,
        "ce_complete": ce, "semantic_rank_status": status or ("reranked" if ce else "fallback"),
        "scorer_error": error, "baseline_metrics": metrics(baseline_rank),
        "source_discovery10": metrics(source_rank),
        "source_discovery20": metrics(source_rank if source20_rank is None else source20_rank),
        "cross_encoder10": metrics(ce_rank),
        "timing_ms": {"candidate_build": 4, "rerank": rerank_ms},
    }


def write_input(root, name, records, *, expected=3, selected=None, skipped=None, summary=None):
    path = root / name
    data = {"runner_operational": True, "records": records, "skipped": skipped or [],
            "summary": summary or {},
            "config": {"expected_query_count": expected,
                       "selected_query_count": len(records) if selected is None else selected}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class AnalyzeSemanticRankProbeTests(unittest.TestCase):
    def test_denominators_all_attempts_and_exact_three_complete_group(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [record("p1"), record("p2", source=False), record("p3", baseline=False)]
            path = write_input(root, "run.json", rows, expected=93, selected=3,
                               skipped=[{"complete": False}] * 60)
            report = analyze([path], "source-only")
            input_report = report["input_reports"][0]
            self.assertEqual(input_report["expected"], 93)
            self.assertEqual(input_report["attempted"], 3)
            self.assertEqual(input_report["skipped"], 60)
            summary = input_report["summary"]
            self.assertEqual(summary["baseline_complete"], 2)
            self.assertEqual(summary["source_complete"], 2)
            self.assertEqual(summary["paired_source_complete"], 1)
            self.assertEqual(summary["ce_actual_complete"], 0)
            group = input_report["groups"]["fixture-a:shared"]
            self.assertFalse(group["exact_three_complete_source_group"])
            self.assertEqual(report["denominators"]["expected"], 93)

    def test_source10_equal_budget_and_source20_labeled_separately(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_input(root, "source.json", [
                record("p1", baseline_rank=2, source_rank=1, source20_rank=11),
                record("p2", baseline_rank=2, source_rank=2, source20_rank=1),
                record("p3", baseline_rank=None, source_rank=None, source20_rank=None)])
            report = analyze([path], "source-only")["input_reports"][0]["summary"]
            self.assertEqual(report["baseline_vs_source10_mrr_at_10_complete_pairs"], {
                "n": 3, "wins": 1, "losses": 0, "ties": 2,
                "meaning": "right_mrr_at_10_vs_left_mrr_at_10"})
            self.assertEqual(report["source10_equal_budget"]["n"], 3)
            self.assertIn("source20_larger_budget_not_apples_to_apples", report)
            self.assertEqual(report["source10_vs_source20_mrr_at_10_larger_budget_not_apples_to_apples"], {
                "n": 3, "wins": 1, "losses": 1, "ties": 1,
                "meaning": "right_mrr_at_10_vs_left_mrr_at_10"})
            self.assertEqual(report["source10_vs_source20_binary_hit_at_10_larger_budget_not_apples_to_apples"], {
                "n": 3, "wins": 0, "losses": 1, "ties": 2,
                "meaning": "right_binary_target_hit_at_10_vs_left"})
            self.assertEqual(report["source20_larger_budget_not_apples_to_apples"]["mrr_at_10"],
                             1 / 3)
            self.assertEqual(report["known_target_hits"]["source20_at_20"]["known_target_hit_at_20_count"], 2)
            self.assertEqual(report["known_target_hits"]["source20_at_20"]["expected_denominator"], 3)
            self.assertEqual(report["known_target_hits"]["source20_at_10"]["known_target_hit_at_10_count"], 1)
            self.assertEqual(report["known_target_hits"]["source20_at_10"]["fraction_of_expected"], 1 / 3)
            self.assertEqual(report["baseline_vs_source10_binary_hit_at_10_complete_pairs"]["n"], 3)
            self.assertIsNone(report["ce_actual_mrr_at_10"])

    def test_fallback_never_contributes_to_ce_metrics_or_ce_timing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [record("p1", ce=False, status="fallback", error="ConnectionError",
                           ce_rank=1, rerank_ms=300),
                    record("p2", ce=True, status="reranked", ce_rank=1, rerank_ms=50),
                    record("p3", ce=False, status="fallback", error="scorer_disabled_after_failure")]
            path = write_input(root, "mixed.json", rows)
            summary = analyze([path], "mixed")["input_reports"][0]["summary"]
            self.assertEqual(summary["ce_actual_complete"], 1)
            self.assertEqual(summary["ce_actual_mrr_at_10"]["n"], 1)
            self.assertEqual(summary["timing_ms"]["rerank_actual_ce_complete_only"]["n"], 1)
            self.assertEqual(summary["timing_ms"]["rerank_fallback_attempts"]["n"], 2)
            self.assertEqual(summary["fallback_types"], {
                "ConnectionError": 1, "scorer_disabled_after_failure": 1})

    def test_groups_are_fixture_scoped_and_require_three_unique_complete_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [record(f"p{i}", fixture="one", group="same") for i in range(3)]
            rows += [record("p1", fixture="two", group="same"),
                     record("p2", fixture="two", group="same")]
            path = write_input(root, "groups.json", rows, expected=5)
            groups = analyze([path], "source-only")["input_reports"][0]["groups"]
            self.assertEqual(set(groups), {"one:same", "two:same"})
            self.assertTrue(groups["one:same"]["exact_three_complete_source_group"])
            self.assertFalse(groups["two:same"]["exact_three_complete_source_group"])

    def test_entirely_skipped_group_is_retained_from_runner_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = "holdout.json:skipped-group"
            path = write_input(root, "skips.json", [], expected=3,
                               skipped=[{"fixture": "holdout.json", "group_id": "skipped-group"}] * 3,
                               summary={"phrase_group_success": {key: {
                                   "attempted": 0, "expected": 3, "success": False}}})
            group = analyze([path], "source-only")["input_reports"][0]["groups"][key]
            self.assertEqual(group["expected_phrases"], 3)
            self.assertEqual(group["attempted_rows"], 0)
            self.assertFalse(group["exact_three_complete_source_group"])

    def test_complete_rates_include_expected_denominator_and_unscoped_group_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_input(root, "expected.json", [record("p1")], expected=4,
                               skipped=[{"complete": False}] * 3)
            result = analyze([path], "source-only")["input_reports"][0]
            rates = result["summary"]["completion_rates"]
            self.assertEqual(rates["source_complete_over_expected"], .25)
            self.assertEqual(rates["expected_denominator"], 4)
            self.assertEqual(rates["attempted_denominator"], 1)
            self.assertFalse(result["group_coverage"]["complete_group_roster_known"])

    def test_alias_normalized_metric_ids_are_consumed_without_oracle_join(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [record(f"p{i}", baseline_rank=1, source_rank=1) for i in range(3)]
            path = write_input(root, "aliases.json", rows)
            summary = analyze([path], "source-only")["input_reports"][0]["summary"]
            self.assertEqual(summary["baseline_primary10"]["mrr_at_10"], 1.0)
            self.assertEqual(summary["source10_equal_budget"]["mrr_at_10"], 1.0)

    def test_candidate_relevance_and_identity_are_explicitly_unverified(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = record("p1")
            row["source_rank_provenance"] = [
                {"verification_required": True, "passes_relevance_floor": None},
                {"verification_required": True, "passes_relevance_floor": False}]
            path = write_input(root, "unknown.json", [row])
            status = analyze([path], "source-only")["input_reports"][0]["summary"]["candidate_verification"]
            self.assertEqual(status["unknown_source_relevance"], 1)
            self.assertEqual(status["below_relevance_floor"], 1)
            self.assertFalse(status["identity_proof_established"])
            self.assertTrue(status["score_is_not_identity_or_evidence"])

    def test_duplicate_keys_are_reported_without_group_join_expansion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = write_input(root, "a.json", [record("p1"), record("p1"), record("p2"), record("p3")])
            b = write_input(root, "b.json", [record("p1"), record("p2"), record("p3")])
            report = analyze([a, b], "source-only")
            self.assertEqual(report["input_reports"][0]["duplicate_query_keys"], 1)
            self.assertEqual(report["combined_attempts"], 7)
            self.assertEqual(report["duplicate_fixture_group_phrase_keys_across_inputs"], 4)
            self.assertIsNone(report["groups"])

    def test_pilot_cannot_be_pooled_and_mode_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a = write_input(root, "pilot-a.json", [record("p1")])
            b = write_input(root, "pilot-b.json", [record("p2")])
            with self.assertRaisesRegex(ValueError, "exactly one input"):
                analyze([a, b], "pilot")
            report = analyze([a], "source-only")
            self.assertEqual(report["mode"], "source-only")
            self.assertIn("CE metrics intentionally excluded", report["interpretation"])

    def test_raw_input_sha_and_byte_length_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = write_input(root, "raw.json", [record("p1")])
            raw = path.read_bytes()
            fingerprint = analyze([path], "source-only")["inputs"][0]
            self.assertEqual(fingerprint["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(fingerprint["bytes"], len(raw))


if __name__ == "__main__":
    unittest.main()
