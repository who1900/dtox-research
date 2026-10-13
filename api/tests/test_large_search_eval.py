import unittest

from eval.large_search_eval import _metric, _percentile, _stable_order, _summarize


class LargeSearchMetricTests(unittest.TestCase):
    def test_alias_target_hit_and_reciprocal_rank(self):
        metric = _metric(["noise", "alias-id"], ["canonical-id"],
                         {"alias-id": "canonical-id"}, 10)
        self.assertEqual(metric, {"hit": True, "mrr": 0.5, "rank": 2})

    def test_targets_are_known_sources_not_precision(self):
        self.assertEqual(_metric(["unjudged-paper"], ["known-target"]),
                         {"hit": False, "mrr": 0.0, "rank": None})

    def test_limit_and_percentile(self):
        self.assertFalse(_metric(["x", "target"], ["target"], limit=1)["hit"])
        self.assertEqual(_percentile([0, 10], .5), 5.0)
        self.assertIsNone(_percentile([], .95))

    def test_stratified_order_is_deterministic_and_preserves_records(self):
        rows = [{"fixture": "old", "layer": "a", "group_id": "g", "phrase_id": str(i)} for i in range(4)]
        rows += [{"fixture": "new", "layer": "b", "group_id": "h", "phrase_id": str(i)} for i in range(3)]
        first, second = _stable_order(rows), _stable_order(rows)
        self.assertEqual([x["phrase_id"] for x in first], [x["phrase_id"] for x in second])
        self.assertEqual(sorted(x["phrase_id"] for x in first), sorted(x["phrase_id"] for x in rows))
        self.assertEqual(first[0]["layer"], "a")
        self.assertEqual(first[1]["layer"], "b")

    def test_incomplete_triplet_is_failure_penalized(self):
        record = {"fixture": "old.json", "group_id": "g", "layer": "a", "variants": {
            "primary10": {"complete": True, "metrics": {"hit@10": True, "mrr": 1}, "latency_ms": 1},
            "primary20": {"complete": False, "metrics": {"hit@10": False}, "latency_ms": 2},
            "discovery10": {"complete": True, "metrics": {"hit@10": True, "mrr": 1}, "latency_ms": 3},
        }}
        summary = _summarize([record])
        self.assertEqual(summary["complete_triplets"], 0)
        self.assertEqual(summary["all_three_hit_inclusive_failures_as_miss"], 0)
        self.assertEqual(summary["variants"]["primary20"]["failed_or_partial"], 1)


if __name__ == "__main__":
    unittest.main()
