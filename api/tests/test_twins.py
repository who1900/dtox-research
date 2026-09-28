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
