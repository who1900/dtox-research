"""_score_specific_papers: two-phase retrieval (id+score+section_type only in
phase 1, full payload fetched only for the per-paper winner in phase 2).

Mirrors the pattern in test_two_phase_search.py's FakeQdrant, but answers the
arxiv_id-filtered /points/search call this function issues plus the /points
batch-fetch phase 2 makes.
"""
import unittest
from unittest.mock import MagicMock, patch

from api import main


def _hit(point_id, score, arxiv_id, section_type="method"):
    return {"id": point_id, "score": score,
            "payload": {"arxiv_id": arxiv_id, "section_type": section_type}}


def _point(point_id, arxiv_id, **extra):
    payload = {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}", "text": "full body text",
              "section_type": extra.pop("section_type", "method")}
    payload.update(extra)
    return {"id": point_id, "payload": payload}


class FakeQdrant:
    def __init__(self, search_hits, points_by_id):
        self.search_hits = search_hits
        self.points_by_id = points_by_id
        self.search_calls = []
        self.points_calls = []

    def post(self, url, json=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/points/search"):
            self.search_calls.append(json)
            resp.json = MagicMock(return_value={"result": self.search_hits[: json["limit"]]})
            return resp
        if url.endswith("/points"):
            self.points_calls.append(json)
            ids = json["ids"]
            points = [self.points_by_id[i] for i in ids if i in self.points_by_id]
            resp.json = MagicMock(return_value={"result": points})
            return resp
        raise AssertionError(f"unexpected URL {url}")


class ScoreSpecificPapersTests(unittest.TestCase):
    def setUp(self):
        main.qdrant_result_cache.data.clear()

    def _call(self, fake, paper_ids, **kwargs):
        with patch.object(main.requests, "post", side_effect=fake.post):
            return main._score_specific_papers("query text", paper_ids,
                                               vector=[0.1, 0.2, 0.3], **kwargs)

    def test_phase1_requests_only_id_and_section_type_no_full_payload(self):
        hits = [_hit("c1", 0.9, "p1")]
        points = {"c1": _point("c1", "p1")}
        fake = FakeQdrant(hits, points)

        self._call(fake, ["p1"])

        self.assertEqual(len(fake.search_calls), 1)
        sent = fake.search_calls[0]
        self.assertEqual(sent["with_payload"], ["arxiv_id", "section_type"])
        # limit stays len(paper_ids) * 8, unchanged semantics
        self.assertEqual(sent["limit"], 8)

    def test_phase2_fetches_only_the_winning_point_per_paper(self):
        # p1 has two chunk candidates; only the higher-scoring one should be
        # fetched in phase 2, not every phase-1 candidate.
        hits = [_hit("c1", 0.90, "p1"), _hit("c2", 0.70, "p1"), _hit("c3", 0.80, "p2")]
        points = {
            "c1": _point("c1", "p1", text="winner text"),
            "c2": _point("c2", "p1", text="loser text"),
            "c3": _point("c3", "p2", text="p2 text"),
        }
        fake = FakeQdrant(hits, points)

        results = self._call(fake, ["p1", "p2"])

        self.assertEqual(len(fake.points_calls), 1, "one batch fetch, not per-paper")
        requested_ids = set(fake.points_calls[0]["ids"])
        self.assertEqual(requested_ids, {"c1", "c3"})
        by_id = {r["arxiv_id"]: r for r in results}
        self.assertEqual(by_id["p1"]["text"], "winner text")
        self.assertEqual(by_id["p1"]["score"], 0.90)

    def test_survey_section_still_loses_to_non_survey_regardless_of_score(self):
        # same selection rule as before: a survey/context section chunk never
        # outranks a non-survey chunk for the same paper, even at a higher score.
        main.SURVEY_SECTIONS  # sanity: attribute exists
        hits = [
            _hit("c1", 0.95, "p1", section_type="introduction"),  # survey section
            _hit("c2", 0.50, "p1", section_type="method"),
        ]
        points = {
            "c1": _point("c1", "p1", section_type="introduction", text="survey text"),
            "c2": _point("c2", "p1", section_type="method", text="method text"),
        }
        fake = FakeQdrant(hits, points)

        results = self._call(fake, ["p1"])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "method text")

    def test_no_candidates_returns_empty_without_phase2_call(self):
        fake = FakeQdrant([], {})
        results = self._call(fake, ["p1"])
        self.assertEqual(results, [])
        self.assertEqual(len(fake.points_calls), 0)

    def test_result_shape_matches_full_payload_fields(self):
        hits = [_hit("c1", 0.9, "p1")]
        points = {"c1": _point("c1", "p1", year=2024, venue="NeurIPS", citation_count=5,
                               repos=["org/repo"], terms=["grpo"])}
        fake = FakeQdrant(hits, points)

        results = self._call(fake, ["p1"])

        self.assertEqual(len(results), 1)
        r = results[0]
        for field in ("arxiv_id", "source", "url", "title", "section_type", "section_title",
                     "element_type", "year", "repos", "terms", "text", "score", "layers",
                     "venue", "citation_count", "fulltext"):
            self.assertIn(field, r)
        self.assertEqual(r["arxiv_id"], "p1")
        self.assertEqual(r["year"], 2024)
        self.assertEqual(r["venue"], "NeurIPS")


if __name__ == "__main__":
    unittest.main()
