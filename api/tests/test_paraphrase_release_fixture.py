import json
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from eval import paraphrase_eval as pe


ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "eval" / "paraphrase_release_20261004.json"
FROZEN_SHA256 = "9b666344cd3e137e0fe532046b5a13e59c23c5e5c907628e7f1b1e597d0ab5ff"


def load_fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class ParaphraseReleaseFixtureTests(unittest.TestCase):
    def test_frozen_bytes_match_preregistration_fingerprint(self):
        self.assertEqual(hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(), FROZEN_SHA256)

    def test_preregistered_controlled_holdout_shape_and_layers(self):
        fixture = load_fixture()
        queries = pe.queries_from_fixture(fixture)
        self.assertEqual(fixture["preregistration"]["status"], "frozen_before_live")
        self.assertEqual(len(fixture["groups"]), 8)
        self.assertEqual(len(queries), 24)
        self.assertEqual(len({item["query"] for item in queries}), 24)
        self.assertEqual({item["layer"] for item in queries}, {"llm-slm", "web3", "builder-tech"})
        self.assertEqual(sum(group["layer"] == "web3" for group in fixture["groups"]), 2)
        self.assertFalse(any("vision" in group["id"] or "image" in group["id"] for group in fixture["groups"]))
        for group in fixture["groups"]:
            self.assertEqual(len(group["phrases"]), 3, group["id"])
            self.assertGreaterEqual(len(group["targets"]), 2, group["id"])
            self.assertTrue(group["hard_negatives"], group["id"])
            for paper in group["targets"] + group["hard_negatives"]:
                self.assertTrue(paper["source_url"].startswith("https://arxiv.org/abs/"))
            if group["id"].startswith("release-") and "contract" in group["id"]:
                self.assertEqual(group["layer"], "builder-tech")

    def test_targets_and_exact_queries_are_disjoint_from_fresh_and_dev(self):
        fixture = load_fixture()
        fresh = json.loads((ROOT / "eval" / "paraphrase_fresh_20261004.json").read_text(encoding="utf-8"))
        fresh_targets = {target["canonical_id"] for group in fresh["groups"] for target in group["targets"]}
        release_targets = {target["canonical_id"] for group in fixture["groups"] for target in group["targets"]}
        fresh_queries = {phrase["query"].casefold() for group in fresh["groups"] for phrase in group["phrases"]}
        release_queries = {phrase["query"].casefold() for group in fixture["groups"] for phrase in group["phrases"]}
        dev_targets, dev_queries = set(), set()
        for path in (ROOT / "eval" / "bench").glob("*.jsonl"):
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("split") != "dev":
                    continue
                dev_queries.add(str(row.get("query", "")).casefold())
                for key in ("target", "source_id"):
                    if row.get(key):
                        dev_targets.add(str(row[key]))
        self.assertFalse(release_targets & fresh_targets)
        self.assertFalse(release_queries & fresh_queries)
        self.assertFalse(release_targets & dev_targets)
        self.assertFalse(release_queries & dev_queries)

    def test_scorer_accepts_any_valid_anchor_and_flags_mechanism_negative(self):
        fixture = load_fixture()
        records = []
        for group in fixture["groups"]:
            for index, phrase in enumerate(group["phrases"]):
                target = group["targets"][index % len(group["targets"])]["canonical_id"]
                negative = group["hard_negatives"][0]["id"]
                records.append({
                    "group_id": group["id"],
                    "phrase_id": phrase["id"],
                    "results": [{"arxiv_id": target}, {"arxiv_id": negative}],
                    "error": None,
                    "partial": False,
                })
        metrics = pe.score(fixture, records)
        self.assertTrue(metrics["all_groups_pass"])
        self.assertEqual(metrics["negative_flag_count"], 0)
        self.assertEqual(metrics["operational_invalid_count"], 0)


if __name__ == "__main__":
    unittest.main()
