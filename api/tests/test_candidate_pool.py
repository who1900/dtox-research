import copy
import unittest

from api.candidate_pool import build_candidate_pool, finite_score, rows_as_hits


def _hit(paper_id, score, text, channels=None, ranks=None):
    return {"score": score, "payload": {
        "arxiv_id": paper_id, "title": paper_id, "text": text,
        "section_type": "method", "section_title": "Methods",
        "layers": ["web3"], "channels": channels, "fusion_ranks": ranks,
    }}


def _from_hit(hit):
    payload = hit["payload"]
    return {"arxiv_id": payload["arxiv_id"], "text": payload["text"],
            "score": hit.get("score"), "channels": payload.get("channels") or [],
            "fusion_ranks": payload.get("fusion_ranks") or {}}


class CandidatePoolTests(unittest.TestCase):
    def test_canonical_dedupe_keeps_preferred_content_and_merges_only_provenance(self):
        primary = [{"arxiv_id": "primary", "score": 0.9, "text": "primary",
                    "channels": ["dense"], "fusion_ranks": {"dense": 1}}]
        precut = [{"arxiv_id": "alias-a", "score": 0.7, "text": "preferred snippet",
                   "channels": ["lexical"], "fusion_ranks": {"lex": 2}}]
        hits = [_hit("alias-b", 0.61, "other snippet", ["graph"],
                     {"graph": 1, "dense": 4}), _hit("raw", 0.6, "raw")]
        original = copy.deepcopy((primary, precut, hits))
        rows, summary = build_candidate_pool(
            primary, precut, hits, lambda pid: "alias" if pid.startswith("alias-") else pid,
            _from_hit)

        self.assertEqual([row["arxiv_id"] for row in rows],
                         ["primary", "alias-a", "raw"])
        self.assertEqual(rows[1]["text"], "preferred snippet")
        self.assertEqual(rows[1]["score"], 0.7)
        self.assertEqual(rows[1]["channels"], ["lexical", "graph", "dense"])
        self.assertEqual(rows[1]["fusion_ranks"], {"lex": 2, "graph": 1, "dense": 4})
        self.assertEqual((primary, precut, hits), original)
        self.assertEqual(summary["observed_candidate_count"], 2)
        self.assertEqual(summary["returned_unique_count"], 1)
        self.assertEqual(rows_as_hits(rows)[1]["score"], 0.7)

    def test_score_rejects_bool_nonfinite_and_non_numbers(self):
        for value in (True, False, float("nan"), float("inf"), "0.9", None):
            with self.subTest(value=value):
                self.assertIsNone(finite_score(value))
        self.assertEqual(finite_score(0.7), 0.7)

    def test_bad_scores_are_never_synthesized_or_treated_as_floors(self):
        rows, _ = build_candidate_pool(
            [], [], [_hit("bool", True, "bool"), _hit("nan", float("nan"), "nan")],
            lambda pid: pid, _from_hit)
        self.assertEqual([row["score"] for row in rows], [None, None])


if __name__ == "__main__":
    unittest.main()
