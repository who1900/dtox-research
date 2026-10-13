import copy
import json
import math
import unittest

from api.semantic_rank_experiment import build_candidates, rerank_candidates


def hit(pid, score=0.8, text="substantive passage", **metadata):
    payload = {"arxiv_id": pid, "title": "Title " + pid, "text": text,
               "section_title": "Methods", "section_type": "method"}
    payload.update(metadata)
    return {"id": pid + str(score), "score": score, "payload": payload}


def canonical(pid):
    return pid.removeprefix("alias-")


def boilerplate(row):
    return row["payload"].get("section_title") == "References"


class SemanticRankExperimentTests(unittest.TestCase):
    def build(self, hits, **kwargs):
        return build_candidates(hits, canonical, boilerplate, **kwargs)

    def test_missing_text_and_boilerplate_only_papers_are_excluded(self):
        rows = self.build([hit("missing", text=""),
                           hit("junk", text="refs", section_title="References"),
                           hit("good")])
        self.assertEqual([row["arxiv_id"] for row in rows], ["good"])

    def test_aliases_dedupe_distinct_passages_and_keep_original_provenance(self):
        source = [hit("alias-paper", .2, "same"), hit("paper", .1, "same"),
                  hit("paper", .05, "different", section_title="Results")]
        rows = self.build(source, fusion_ranks={"paper": {"dense": 4, "lex": 8}})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["canonical_id"], "paper")
        self.assertEqual(rows[0]["channel_ranks"], {"dense": 4, "lex": 8})
        self.assertEqual([p["text"] for p in rows[0]["passages"]], ["same", "different"])
        self.assertEqual([p["score"] for p in rows[0]["passages"]], [.2, .05])
        self.assertEqual(rows[0]["paper_rank"], 1)

    def test_rank_four_low_cosine_survives_and_caps_hold(self):
        hits = [hit("a", .9), hit("b", .8), hit("c", .7), hit("d", .01)]
        rows = self.build(hits)
        self.assertEqual(rows[3]["arxiv_id"], "d")
        self.assertEqual(rows[3]["score"], .01)
        many = self.build([hit(str(i)) for i in range(30)], limit=200)
        self.assertEqual(len(many), 20)
        capped = self.build([hit("p", text=str(i)) for i in range(5)], passages_per_paper=50)
        self.assertEqual(len(capped), 1)
        self.assertLessEqual(len(capped) * len(capped[0]["passages"]), 40)

    def test_missing_title_is_reported_and_invalid_score_is_not_invented(self):
        row = hit("p", float("nan"))
        row["payload"].pop("title")
        candidate = self.build([row])[0]
        self.assertEqual(candidate["metadata_status"], "missing_title")
        self.assertIsNone(candidate["score"])

    def test_build_does_not_mutate_hits(self):
        source = [hit("p")]
        before = copy.deepcopy(source)
        self.build(source)
        self.assertEqual(source, before)

    def test_invalid_source_scores_become_none_and_output_is_finite_json(self):
        hits = [hit("bool", True), hit("str", "0.2"), hit("nan", float("nan")),
                hit("inf", float("inf"))]
        rows = self.build(hits)
        self.assertTrue(all(row["score"] is None for row in rows))
        self.assertTrue(all(row["passages"][0]["score"] is None for row in rows))
        self.assertTrue(all(row["provenance"][0]["score"] is None for row in rows))
        json.dumps(rows, allow_nan=False)

    def test_title_and_passage_caps_follow_whitespace_normalization(self):
        long_title = " Title  " + "x" * 300
        long_text = " alpha\n\t beta " + "z" * 1400
        rows = self.build([hit("p", text=long_text, title=long_title),
                           hit("p", text="alpha beta " + "z" * 1400)])
        candidate = rows[0]
        self.assertEqual(len(candidate["title"]), 256)
        self.assertEqual(len(candidate["passages"][0]["text"]), 1200)
        self.assertEqual(len(candidate["passages"]), 1)

    def test_missing_title_and_empty_pairs_return_no_scorable_fallback(self):
        no_title = hit("p")
        no_title["payload"].pop("title")
        candidate = self.build([no_title])
        calls = []
        report = rerank_candidates("q", candidate,
                                   lambda *args: calls.append(args) or [])
        self.assertEqual(report["status"], "fallback")
        self.assertEqual(report["reason"], "no_scorable_pairs")
        self.assertFalse(report["reranked"])
        self.assertIs(report["candidates"], candidate)
        self.assertEqual(calls, [])

        report = rerank_candidates("q", [{"title": "T", "passages": []}],
                                   lambda *args: calls.append(args) or [])
        self.assertEqual(report["reason"], "no_scorable_pairs")
        self.assertEqual(calls, [])

    def test_nonpositive_channel_ranks_are_discarded(self):
        rows = self.build([hit("p", fusion_ranks={"dense": 0, "lex": -1,
                                                    "graph": 1, "other": 2})])
        self.assertEqual(rows[0]["channel_ranks"], {"graph": 1, "other": 2})

    def test_scorer_called_once_and_max_pair_logit_ranks_deterministically(self):
        candidates = self.build([hit("a", text="first"), hit("a", text="second"), hit("b")])
        calls = []

        def scorer(query, pairs):
            calls.append((query, pairs))
            return [-3, 2, 2]

        result = rerank_candidates("q", candidates, scorer)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "q")
        self.assertEqual([row["arxiv_id"] for row in result["candidates"]], ["a", "b"])
        self.assertEqual(result["candidates"][0]["semantic_logit"], 2)
        self.assertFalse(result["evidence"])

    def test_malformed_and_nonfinite_scores_fallback_unchanged(self):
        candidates = self.build([hit("p")])
        for scores in ([1, 2], [True], ["1"], [math.inf], [float("nan")]):
            result = rerank_candidates("q", candidates, lambda *_: scores)
            self.assertEqual(result["status"], "fallback")
            self.assertIs(result["candidates"], candidates)
            self.assertFalse(result["reranked"])

    def test_scorer_exception_fallback_and_negative_scores_are_not_evidence(self):
        candidates = self.build([hit("p")])

        def fail(*_):
            raise RuntimeError("offline")

        fallback = rerank_candidates("q", candidates, fail)
        self.assertEqual(fallback["candidates"], candidates)
        self.assertEqual(fallback["reason"], "scorer_failure")
        self.assertEqual(fallback["error_type"], "RuntimeError")
        self.assertNotIn("offline", json.dumps(fallback))
        ranked = rerank_candidates("q", candidates, lambda *_: [-8])
        self.assertEqual(ranked["candidates"][0]["semantic_logit"], -8)
        self.assertTrue(ranked["candidates"][0]["verification_required"])
        self.assertFalse(ranked["evidence"])

    def test_semantic_rank_is_actual_one_based_sorted_rank(self):
        candidates = self.build([hit("a", text="a1"), hit("a", text="a2"),
                                 hit("b", text="b")])
        report = rerank_candidates("q", candidates, lambda *_: [1, 4, -2])
        ranked = report["candidates"]
        self.assertEqual([row["arxiv_id"] for row in ranked], ["a", "b"])
        self.assertEqual([row["semantic_rank"] for row in ranked], [1, 2])
        self.assertEqual([row["paper_rank"] for row in ranked], [1, 2])


if __name__ == "__main__":
    unittest.main()
