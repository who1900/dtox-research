import collections
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from api import main

_OPS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "ops")
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
import build_twins  # noqa: E402

TITLE = "InjecAgent: Benchmarking Indirect Prompt Injections in Tool-Integrated Agents"


class BuildTwinsTest(unittest.TestCase):
    def test_acl_copy_points_at_arxiv(self):
        got = build_twins.build([("acl:2024.findings-acl.624", TITLE, 2024),
                                 ("2403.02691", TITLE, 2024)])
        self.assertEqual(got, {"2403.02691": "2403.02691",
                               "acl:2024.findings-acl.624": "2403.02691"})

    def test_title_match_ignores_case_and_punctuation(self):
        got = build_twins.build([("2407.19572", "MEV Mitigation: A Comprehensive Survey of Layer-2 Chains", 2024),
                                 ("oa:W1", "mev mitigation a comprehensive survey of layer 2 chains", 2024)])
        self.assertEqual(got.get("oa:W1"), "2407.19572")

    def test_short_titles_never_merge(self):
        self.assertEqual(build_twins.build([("1.1", "Introduction", 2020),
                                            ("acl:x", "Introduction", 2020)]), {})

    def test_years_far_apart_stay_separate(self):
        self.assertEqual(build_twins.build([("2001.00001", TITLE, 2020),
                                            ("acl:x", TITLE, 2024)]), {})

    def test_two_arxiv_records_are_not_merged(self):
        self.assertEqual(build_twins.build([("2001.00001", TITLE, 2020),
                                            ("2001.00002", TITLE, 2020)]), {})


FLASH_ARXIV = "Flash Boys 2.0: Frontrunning, Transaction Reordering, and Consensus Instability in Decentralized Exchanges"


class TwinRulesTest(unittest.TestCase):
    def test_arxiv_id_in_source_url(self):
        got = build_twins.build([("2309.06180", "Paged attention serving", 2023, None, None),
                                 ("oa:W1", "Paged attention serving systems", 2023, None,
                                  "https://arxiv.org/pdf/2309.06180v2")])
        self.assertEqual(got["oa:W1"], "2309.06180")

    def test_arxiv_doi_in_source_url(self):
        got = build_twins.build([("2603.07716", "SoK about extractable value", 2026, None, None),
                                 ("oa:W2", "SoK about extractable value", 2026, None,
                                  "https://doi.org/10.48550/arXiv.2603.07716")])
        self.assertEqual(got["oa:W2"], "2603.07716")

    def test_url_of_a_different_paper_is_not_a_twin(self):
        self.assertEqual(build_twins.build([("2309.06180", "Paged attention serving", 2023, "x", None),
                                            ("oa:W1", "Something else entirely different", 2023, "y",
                                             "https://arxiv.org/pdf/2309.06180v2")]), {})

    def test_shared_task_papers_stay_apart(self):
        self.assertEqual(build_twins.build([
            ("acl:a", "Team A at SemEval-2026 Task 11: Neuro-Symbolic Syllogistic Reasoning", 2026),
            ("acl:b", "Team B at SemEval-2026 Task 11: Neuro-Symbolic Syllogistic Reasoning", 2026)]), {})

    def test_url_pointing_outside_corpus_is_ignored(self):
        self.assertEqual(build_twins.build([("2309.06180", "Paged attention serving", 2023, None, None),
                                            ("oa:W1", "Something else entirely different", 2023, None,
                                             "https://arxiv.org/abs/2401.00001")]), {})

    def test_identical_abstract(self):
        ab = "Blockchains, and specifically smart contracts, have promised to create fair and transparent trading ecosystems. " * 3
        got = build_twins.build([("1904.05234", "Alpha beta gamma", 2019, ab, None),
                                 ("oa:W3", "Completely different title words", 2020, ab.replace(". ", "."), None)])
        self.assertEqual(got["oa:W3"], "1904.05234")

    def test_fuzzy_whitepaper_title(self):
        got = build_twins.build([("1904.05234", FLASH_ARXIV, 2019),
                                 ("wp:flashbots-mev",
                                  "Flash Boys 2.0: Frontrunning, Transaction Reordering and Consensus Instability", 2019)])
        self.assertEqual(got["wp:flashbots-mev"], "1904.05234")

    def test_fuzzy_subtitle_variant(self):
        got = build_twins.build([("acl:x", "Efficient Memory Management for Large Language Model Serving", 2023),
                                 ("2309.06180", "Efficient Memory Management for Large Language Model Serving: PagedAttention", 2023)])
        self.assertEqual(got.get("acl:x"), "2309.06180")

    def test_part_one_and_two_stay_apart(self):
        self.assertEqual(build_twins.build([
            ("acl:a", "Learning Robust Representations for Language Models Part I", 2022),
            ("oa:b", "Learning Robust Representations for Language Models Part II", 2022)]), {})

    def test_version_numbers_stay_apart(self):
        self.assertEqual(build_twins.build([
            ("acl:a", "Scaling Neural Machine Translation Systems v2", 2022),
            ("oa:b", "Scaling Neural Machine Translation Systems v3", 2022)]), {})

    def test_fuzzy_respects_years(self):
        self.assertEqual(build_twins.build([
            ("acl:a", "Efficient Memory Management for Large Language Model Serving", 2020),
            ("oa:b", "Efficient Memory Management for Large Language Model Serving Systems", 2024)]), {})

    def test_surveys_of_different_topics_stay_apart(self):
        self.assertEqual(build_twins.build([
            ("acl:a", "A Survey of Large Language Model Agents and Planning", 2024),
            ("oa:b", "A Survey of Large Language Model Reasoning and Planning", 2024)]), {})

    def test_fuzzy_never_merges_two_arxiv(self):
        self.assertEqual(build_twins.build([
            ("2001.00001", "Efficient Memory Management for Large Language Model Serving", 2023),
            ("2001.00002", "Efficient Memory Management for Large Language Model Serving Systems", 2023)]), {})

    def test_transitive_chain_never_holds_two_arxiv(self):
        got = build_twins.build([
            ("2001.00001", "Efficient Memory Management for Large Language Model Serving", 2023),
            ("2001.00002", "Efficient Memory Management for Large Language Model Serving Systems", 2023),
            ("oa:W1", "Efficient Memory Management for Large Language Model Serving Systems", 2023)])
        self.assertEqual(sum(1 for k in got if k[0].isdigit()), 1)
        self.assertEqual(len(got), 2)

    def test_group_size_is_capped(self):
        rows = [("2001.00001", TITLE, 2024)] + [(f"oa:W{i}", TITLE, 2024) for i in range(30)]
        got = build_twins.build(rows)
        sizes = collections.Counter(got.values())
        self.assertLessEqual(max(sizes.values()), build_twins.MAX_GROUP)


class CollapseTwinsTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"canonical": {"2403.02691": "2403.02691",
                                     "acl:2024.findings-acl.624": "2403.02691"}}, f)
        main._twins_state.update(checked=0.0, mtime=None, canonical={}, members={})

    def tearDown(self):
        os.remove(self.path)
        main._twins_state.update(checked=0.0, mtime=None, canonical={}, members={})

    def test_second_copy_is_dropped_and_named(self):
        with patch.object(main, "TWINS_PATH", self.path):
            out = main._collapse_twins([{"arxiv_id": "acl:2024.findings-acl.624"},
                                        {"arxiv_id": "2310.00001"},
                                        {"arxiv_id": "2403.02691"}])
        self.assertEqual([r["arxiv_id"] for r in out], ["acl:2024.findings-acl.624", "2310.00001"])
        self.assertEqual(out[0]["twins"], ["2403.02691"])
        self.assertNotIn("twins", out[1])

    def test_missing_file_means_no_grouping(self):
        with patch.object(main, "TWINS_PATH", self.path + ".absent"):
            out = main._collapse_twins([{"arxiv_id": "a"}, {"arxiv_id": "a2"}])
        self.assertEqual(len(out), 2)
        self.assertEqual(main.canonical_id("acl:2024.findings-acl.624"), "acl:2024.findings-acl.624")


if __name__ == "__main__":
    unittest.main()
