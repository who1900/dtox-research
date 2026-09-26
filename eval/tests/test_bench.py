import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from eval import bench_lib as bl


class MarkerFilterTests(unittest.TestCase):
    def test_single_bracket_passes(self):
        text = ("We build on the transformer architecture introduced for "
                "sequence modeling in [31] and extend it with sparse attention.")
        got = bl.is_usable_context(text)
        self.assertIsNotNone(got)
        query, kind = got
        self.assertEqual(kind, "single")
        self.assertNotIn("[31]", query)

    def test_single_author_year_paren_passes(self):
        text = ("Retrieval augmented generation was shown to reduce hallucination "
                "rates substantially in open domain question answering (Hu et al., 2021).")
        got = bl.is_usable_context(text)
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "author_year")

    def test_single_author_year_inline_passes(self):
        text = ("The idea of chain of thought prompting for multi step arithmetic "
                "reasoning tasks was popularized by Wei et al. (2022) in this setting.")
        got = bl.is_usable_context(text)
        self.assertIsNotNone(got)

    def test_multiple_numbers_in_one_bracket_rejected(self):
        text = ("Several prior systems explored this direction extensively "
                "already and reported similar findings across benchmarks [3, 5].")
        self.assertIsNone(bl.is_usable_context(text))

    def test_range_bracket_rejected(self):
        text = ("Several prior systems explored this direction extensively "
                "already and reported similar findings across benchmarks [3-7].")
        self.assertIsNone(bl.is_usable_context(text))

    def test_multiple_bracket_groups_rejected(self):
        text = ("Several prior systems explored this direction extensively "
                "already such as A [1] and B [2] with similar findings shown.")
        self.assertIsNone(bl.is_usable_context(text))

    def test_bracket_plus_author_year_rejected(self):
        text = ("Several prior systems explored this direction extensively "
                "already, both [1] and Smith et al. (2020), with similar results.")
        self.assertIsNone(bl.is_usable_context(text))

    def test_too_short_rejected(self):
        text = "As shown in [1], it works."
        self.assertIsNone(bl.is_usable_context(text))

    def test_too_long_rejected(self):
        text = ("word " * 90) + "as introduced in [1]."
        self.assertIsNone(bl.is_usable_context(text))

    def test_listy_filler_rejected(self):
        text = ("For the encoder backbone we use the architecture proposed "
                "in prior work for this exact same purpose here today [1].")
        self.assertIsNone(bl.is_usable_context(text))

    def test_comparison_filler_rejected(self):
        text = ("We compare with the baseline approach described previously "
                "in this closely related line of prior research work [1].")
        self.assertIsNone(bl.is_usable_context(text))


class NamedQueryTests(unittest.TestCase):
    def test_named_when_title_token_present(self):
        title = "BERT: Pre-training of Deep Bidirectional Transformers"
        query = "This work extends BERT to the multilingual setting for classification."
        self.assertTrue(bl.is_named_query(query, title))

    def test_not_named_when_only_description(self):
        title = "BERT: Pre-training of Deep Bidirectional Transformers"
        query = ("The idea of masking random tokens and predicting them from "
                 "surrounding context has been widely adopted since then.")
        self.assertFalse(bl.is_named_query(query, title))

    def test_acronym_match(self):
        title = "GPT-4 Technical Report"
        query = "We evaluate against GPT-4 on a battery of reasoning benchmarks."
        self.assertTrue(bl.is_named_query(query, title))


class SplitTests(unittest.TestCase):
    def test_split_is_deterministic(self):
        a = bl.split_for_target("2103.12345")
        b = bl.split_for_target("2103.12345")
        self.assertEqual(a, b)

    def test_split_uses_both_buckets(self):
        seen = {bl.split_for_target(f"210{i}.0000{i}") for i in range(20)}
        self.assertEqual(seen, {"dev", "test"})


class PopularityBucketTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(bl.popularity_bucket(1), "1-4")
        self.assertEqual(bl.popularity_bucket(4), "1-4")
        self.assertEqual(bl.popularity_bucket(5), "5-49")
        self.assertEqual(bl.popularity_bucket(49), "5-49")
        self.assertEqual(bl.popularity_bucket(50), "50+")
        self.assertEqual(bl.popularity_bucket(1000), "50+")


class RankAndMetricsTests(unittest.TestCase):
    def test_rank_excludes_source_id(self):
        results = ["2001.00001", "2002.00002", "2003.00003"]
        rank = bl.rank_of_target(results, "2002.00002", source_id=None)
        self.assertEqual(rank, 2)

    def test_source_id_is_excluded_before_ranking(self):
        # source paper appears first (it cites itself's neighbourhood) but must
        # not count, and must not shift indices of the papers behind it wrongly
        results = ["2000.99999", "2001.00001", "2002.00002"]
        rank = bl.rank_of_target(results, "2002.00002", source_id="2000.99999")
        self.assertEqual(rank, 2)

    def test_rank_none_when_absent(self):
        results = ["2001.00001", "2002.00002"]
        self.assertIsNone(bl.rank_of_target(results, "9999.99999"))

    def test_hit_and_reciprocal_rank(self):
        self.assertTrue(bl.hit_at_k(1, 1))
        self.assertFalse(bl.hit_at_k(2, 1))
        self.assertAlmostEqual(bl.reciprocal_rank(4, 10), 0.25)
        self.assertEqual(bl.reciprocal_rank(None, 10), 0.0)
        self.assertEqual(bl.reciprocal_rank(11, 10), 0.0)

    def test_ndcg_rank1_is_1(self):
        self.assertAlmostEqual(bl.ndcg_at_k(1, 10), 1.0)

    def test_ndcg_beyond_k_is_zero(self):
        self.assertEqual(bl.ndcg_at_k(11, 10), 0.0)

    def test_aggregate_metrics_basic(self):
        rows = [
            {"rank": 1, "latency_ms": 100},
            {"rank": None, "latency_ms": 120},
            {"rank": 3, "latency_ms": 90},
        ]
        m = bl.aggregate_metrics(rows)
        self.assertEqual(m["n"], 3)
        self.assertAlmostEqual(m["hit@1"], 1 / 3)
        self.assertAlmostEqual(m["hit@3"], 2 / 3)
        self.assertAlmostEqual(m["mrr@10"], (1.0 + 0 + 1 / 3) / 3)

    def test_aggregate_metrics_empty(self):
        self.assertEqual(bl.aggregate_metrics([]), {"n": 0})

    def test_compare_metrics_deltas(self):
        a = {"hit@1": 0.2, "n": 10, "note": "x"}
        b = {"hit@1": 0.3, "n": 10, "note": "y"}
        delta = bl.compare_metrics(a, b)
        self.assertAlmostEqual(delta["hit@1"], 0.1)
        self.assertEqual(delta["n"], 0)
        self.assertNotIn("note", delta)


class GroupByTests(unittest.TestCase):
    def test_group_by_key(self):
        rows = [{"layer": "web3"}, {"layer": "web3"}, {"layer": "ai-agents"}]
        groups = bl.group_by(rows, lambda r: r["layer"])
        self.assertEqual(len(groups["web3"]), 2)
        self.assertEqual(len(groups["ai-agents"]), 1)


if __name__ == "__main__":
    unittest.main()
