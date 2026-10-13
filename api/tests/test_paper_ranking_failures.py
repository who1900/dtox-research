from concurrent.futures import TimeoutError as FuturesTimeoutError
import importlib.util
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from api import main

if importlib.util.find_spec("httpx") is not None:
    from fastapi.testclient import TestClient
else:
    TestClient = None

HAS_HTTPX = TestClient is not None


ROWS = {
    "a": {"arxiv_id": "a", "title": "Paper A", "year": 2024, "layers": ["web3"]},
    "b": {"arxiv_id": "b", "title": "Paper B", "year": 2023, "layers": ["web3"]},
    "named": {"arxiv_id": "named", "title": "Named Paper", "year": 2022,
              "layers": ["web3"]},
}
TWINS = {"members": {}, "canonical": {}}


def _rank(dense, lexical, *, named=None):
    with patch.object(main, "COARSE_EMBED_URL", "http://embed"), \
         patch.object(main, "_coarse_paper_search", side_effect=dense), \
         patch.object(main, "_paper_bm25", side_effect=lexical), \
         patch.object(main, "_paper_rows",
                      side_effect=lambda ids: {pid: dict(ROWS[pid]) for pid in ids if pid in ROWS}), \
         patch.object(main, "_twins", return_value=TWINS), \
         patch.object(main, "_exact_matches", return_value=named or []):
        return main._rank_papers("ranking failure test", "web3", 2020, 2025, 10)


def _timeout(*args, **kwargs):
    raise FuturesTimeoutError()


def _db_error(*args, **kwargs):
    raise main.sqlite3.OperationalError("database unavailable")


class PaperRankingFailuresTest(unittest.TestCase):
    def test_lexical_timeout_keeps_dense_results_and_marks_partial(self):
        dense = [{"arxiv_id": "a"}, {"arxiv_id": "b"}]
        pool, diagnostics = _rank(lambda *args, **kwargs: dense, _timeout)

        assert [row["arxiv_id"] for row in pool] == ["a", "b"]
        assert diagnostics == {
            "dense": {"status": "ok", "error": None},
            "lexical": {"status": "failed", "error": "TimeoutError"},
        }

    def test_dense_failure_keeps_lexical_results(self):
        def dense_failure(*args, **kwargs):
            raise HTTPException(status_code=503, detail="Qdrant unavailable")

        pool, diagnostics = _rank(dense_failure, lambda *args, **kwargs: ["b", "a"])

        assert [row["arxiv_id"] for row in pool] == ["b", "a"]
        assert diagnostics["dense"] == {"status": "failed", "error": "Qdrant unavailable"}
        assert diagnostics["lexical"] == {"status": "ok", "error": None}

    def test_embedding_http_failure_is_reported_as_dense_failure(self):
        with patch.object(main, "COARSE_EMBED_URL", ""), \
             patch.object(main, "embed_query",
                           side_effect=HTTPException(status_code=502, detail="embed unavailable")), \
             patch.object(main, "_coarse_paper_search") as dense_search, \
             patch.object(main, "_paper_bm25", return_value=["a"]), \
             patch.object(main, "_paper_rows", return_value={"a": dict(ROWS["a"])}), \
             patch.object(main, "_twins", return_value=TWINS), \
             patch.object(main, "_exact_matches", return_value=[]):
            pool, diagnostics = main._rank_papers("query", "web3", 2020, 2025, 10)

        assert [row["arxiv_id"] for row in pool] == ["a"]
        assert diagnostics["dense"] == {"status": "failed", "error": "embed unavailable"}
        dense_search.assert_not_called()

    def test_both_channels_failed_without_named_match_raise_managed_503(self):
        def dense_failure(*args, **kwargs):
            raise HTTPException(status_code=503, detail="Qdrant unavailable")

        try:
            _rank(dense_failure, _timeout)
        except HTTPException as exc:
            assert exc.status_code == 503
            assert exc.detail["channels"]["dense"]["status"] == "failed"
            assert exc.detail["channels"]["lexical"]["status"] == "failed"
        else:
            raise AssertionError("both failed ranking channels must not return an empty success")

    def test_named_match_remains_available_with_explicit_degradation(self):
        def dense_failure(*args, **kwargs):
            raise HTTPException(status_code=503, detail="Qdrant unavailable")

        pool, diagnostics = _rank(dense_failure, _db_error,
                                  named=[("named", "exact title")])

        assert [row["arxiv_id"] for row in pool] == ["named"]
        assert diagnostics["dense"]["status"] == "failed"
        assert diagnostics["lexical"]["status"] == "failed"

    def test_valid_empty_channels_are_not_treated_as_failure(self):
        pool, diagnostics = _rank(lambda *args, **kwargs: [], lambda *args, **kwargs: [])

        assert pool == []
        assert diagnostics is None

    def test_successful_ranking_order_and_filters_are_unchanged(self):
        dense = [{"arxiv_id": "a"}, {"arxiv_id": "b"}]
        lexical = ["b"]
        expected = sorted(
            main.reciprocal_rank_fusion(["a", "b"], lexical,
                                        weights=[main.HIER_W_DENSE, main.HIER_W_LEX]),
            key=lambda pid: -main.reciprocal_rank_fusion(
                ["a", "b"], lexical, weights=[main.HIER_W_DENSE, main.HIER_W_LEX]
            )[pid],
        )
        with patch.object(main, "COARSE_EMBED_URL", "http://embed"), \
             patch.object(main, "_coarse_paper_search", return_value=dense) as dense_search, \
             patch.object(main, "_paper_bm25", return_value=lexical) as lexical_search, \
             patch.object(main, "_paper_rows",
                          side_effect=lambda ids: {pid: dict(ROWS[pid]) for pid in ids if pid in ROWS}), \
             patch.object(main, "_twins", return_value=TWINS), \
             patch.object(main, "_exact_matches", return_value=[]):
            pool, diagnostics = main._rank_papers("query", "web3", 2020, 2025, 10)

        assert [row["arxiv_id"] for row in pool] == expected
        assert [row["relevance_rank"] for row in pool] == list(range(1, len(pool) + 1))
        assert diagnostics is None
        assert dense_search.call_args.args[1:5] == ("web3", 2020, 2025, 10)
        lexical_search.assert_called_once_with("query", "web3", 2020, 2025, limit=10)

    @unittest.skipUnless(HAS_HTTPX, "FastAPI TestClient route test requires optional httpx")
    def test_route_returns_503_when_both_channels_fail(self):
        def dense_failure(*args, **kwargs):
            raise HTTPException(status_code=503, detail="Qdrant unavailable")

        app_client = TestClient(main.app)
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "COARSE_EMBED_URL", "http://embed"), \
             patch.object(main, "_coarse_paper_search", side_effect=dense_failure), \
             patch.object(main, "_paper_bm25", side_effect=_timeout), \
             patch.object(main, "_paper_rows", return_value={}), \
             patch.object(main, "_exact_matches", return_value=[]):
            response = app_client.post("/v1/papers", json={"query": "ranking failure test"})

        assert response.status_code == 503
        assert response.json()["detail"]["channels"]["dense"]["status"] == "failed"
        assert response.json()["detail"]["channels"]["lexical"]["status"] == "failed"

    def test_lexical_only_partial_note_does_not_claim_terms_only_when_dense_survived(self):
        diagnostics = {
            "dense": {"status": "ok", "error": None},
            "lexical": {"status": "failed", "error": "TimeoutError"},
        }
        row = dict(ROWS["a"], relevance_rank=1)
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_rank_papers", return_value=([row], diagnostics)), \
             patch.object(main, "_paper_card", return_value={"id": "a"}):
            out = main.find_papers(main.PapersBody(query="ranking failure test"))

        assert out["partial"] is True
        assert out["channel_diagnostics"] == diagnostics
        assert "dense similarity only" in out["note"]
        assert "terms only" not in out["note"]

    def test_facets_returns_partial_channel_diagnostics(self):
        diagnostics = {
            "dense": {"status": "ok", "error": None},
            "lexical": {"status": "failed", "error": "TimeoutError"},
        }
        row = dict(ROWS["a"], relevance_rank=1)
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_rank_papers", return_value=([row], diagnostics)), \
             patch.object(main, "_facet_source", return_value=[]), \
             patch.object(main, "_paper_card", return_value={"id": "a"}):
            out = main.paper_facets(main.FacetsBody(query="ranking failure test"))

        assert out["partial"] is True
        assert out["channel_diagnostics"] == diagnostics
        assert "dense similarity only" in out["note"]

    @unittest.skipUnless(HAS_HTTPX, "FastAPI TestClient route test requires optional httpx")
    def test_facets_route_returns_503_when_both_channels_fail(self):
        app_client = TestClient(main.app)
        detail = {
            "message": "paper ranking unavailable: dense and lexical channels failed",
            "channels": {
                "dense": {"status": "failed", "error": "Qdrant unavailable"},
                "lexical": {"status": "failed", "error": "TimeoutError"},
            },
        }
        with patch.object(main, "auth_and_limit"), \
             patch.object(main, "_rank_papers", side_effect=HTTPException(status_code=503, detail=detail)):
            response = app_client.post("/v1/facets", json={"query": "ranking failure test"})

        assert response.status_code == 503
        assert response.json()["detail"]["channels"] == detail["channels"]
