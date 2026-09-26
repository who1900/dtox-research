"""papers_fts.db: the small article-level lexical index (title+abstract) that
answers exact-term queries even when the 9.2M-row chunk-level fts.db misses
its own timeout on the slow disk. Covers pipeline/build_paper_fts.py (build,
atomic rebuild, incremental upsert) and the api/main.py wiring behind
PAPER_LEXICAL (_paper_bm25, the RRF fuse with the chunk channel, and the
HIER_SEARCH stage-A shortlist union). All sqlite in tmp dirs, no network.
"""
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from pipeline import build_paper_fts  # noqa: E402
from api import main  # noqa: E402


def make_state_db(path, papers):
    """papers: list of dicts with arxiv_id, title, abstract, layers, year, status."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT, abstract TEXT, "
        "layers TEXT, year INTEGER, status TEXT)"
    )
    for p in papers:
        conn.execute(
            "INSERT INTO papers (arxiv_id, title, abstract, layers, year, status) "
            "VALUES (?,?,?,?,?,?)",
            (p["arxiv_id"], p.get("title", ""), p.get("abstract", ""),
             p.get("layers", "llm-slm"), p.get("year", 2024), p.get("status", "done")),
        )
    conn.commit()
    conn.close()


def query_fts(out_path, match):
    conn = sqlite3.connect(out_path)
    rows = conn.execute(
        "SELECT arxiv_id FROM papers_fts WHERE papers_fts MATCH ? "
        "ORDER BY bm25(papers_fts, 0, 3.0, 1.0, 0, 0)",
        (match,),
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


SAMPLE_PAPERS = [
    {"arxiv_id": "2401.00001", "title": "GRPO: Group Relative Policy Optimization for LLMs",
     "abstract": "We introduce a reinforcement learning method for language models."},
    {"arxiv_id": "2401.00002", "title": "A Survey of Transformer Architectures",
     "abstract": "This survey covers attention mechanisms and their variants, including "
                 "a brief mention of GRPO among many other training recipes."},
    {"arxiv_id": "2401.00003", "title": "Durable Nonces on Solana",
     "abstract": "We describe how durable transaction nonces avoid blockhash expiry."},
    {"arxiv_id": "2401.00004", "title": "Unrelated Paper About Gardening",
     "abstract": "This paper has nothing to do with machine learning or blockchains."},
    {"arxiv_id": "2401.00005", "title": "Discovered But Not Done",
     "abstract": "Should never appear in papers_fts.", "status": "discovered"},
]


class BuildPaperFtsTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmpdir.name, "state.db")
        self.out_path = os.path.join(self.tmpdir.name, "papers_fts.db")
        make_state_db(self.state_path, SAMPLE_PAPERS)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_rebuild_indexes_only_done_papers(self):
        n = build_paper_fts.rebuild(self.state_path, self.out_path)
        self.assertEqual(n, 4)  # excludes the status='discovered' row
        conn = sqlite3.connect(self.out_path)
        count = conn.execute("SELECT COUNT(*) FROM papers_fts").fetchone()[0]
        conn.close()
        self.assertEqual(count, 4)

    def test_exact_term_search_finds_the_right_paper(self):
        build_paper_fts.rebuild(self.state_path, self.out_path)
        hits = query_fts(self.out_path, "durable AND nonces")
        self.assertEqual(hits, ["2401.00003"])

    def test_title_weighted_more_than_abstract(self):
        # GRPO appears in 2401.00001's title and only in passing in
        # 2401.00002's abstract -- the title hit must rank first.
        build_paper_fts.rebuild(self.state_path, self.out_path)
        hits = query_fts(self.out_path, "grpo")
        self.assertEqual(hits[0], "2401.00001")

    def test_rebuild_is_atomic_on_failure(self):
        # pre-existing index must survive a rebuild that errors out partway
        with open(self.out_path, "w") as f:
            f.write("sentinel: old index content")
        bad_state_path = os.path.join(self.tmpdir.name, "missing.db")
        # points at a state.db with no `papers` table at all
        sqlite3.connect(bad_state_path).close()
        with self.assertRaises(sqlite3.OperationalError):
            build_paper_fts.rebuild(bad_state_path, self.out_path)
        with open(self.out_path) as f:
            self.assertEqual(f.read(), "sentinel: old index content")
        # no leftover temp files beside it
        leftovers = [p for p in os.listdir(self.tmpdir.name) if ".tmp-" in p]
        self.assertEqual(leftovers, [])

    def test_rebuild_replaces_old_index_wholesale(self):
        build_paper_fts.rebuild(self.state_path, self.out_path)
        # add a paper and rebuild again; the new file must reflect it, and be
        # a clean swap (no stray temp files left around)
        conn = sqlite3.connect(self.state_path)
        conn.execute(
            "INSERT INTO papers (arxiv_id, title, abstract, layers, year, status) "
            "VALUES ('2401.00006','New Paper About GRPO','more text','llm-slm',2025,'done')"
        )
        conn.commit()
        conn.close()
        build_paper_fts.rebuild(self.state_path, self.out_path)
        hits = query_fts(self.out_path, "grpo")
        self.assertIn("2401.00006", hits)
        leftovers = [p for p in os.listdir(self.tmpdir.name) if ".tmp-" in p]
        self.assertEqual(leftovers, [])

    def test_stats_reports_count_and_size(self):
        build_paper_fts.rebuild(self.state_path, self.out_path)
        n = build_paper_fts.stats(self.out_path)
        self.assertEqual(n, 4)

    def test_stats_on_missing_index_does_not_raise(self):
        missing = os.path.join(self.tmpdir.name, "does-not-exist.db")
        self.assertEqual(build_paper_fts.stats(missing), 0)


class UpsertPapersTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmpdir.name, "state.db")
        self.out_path = os.path.join(self.tmpdir.name, "papers_fts.db")
        make_state_db(self.state_path, SAMPLE_PAPERS)
        build_paper_fts.rebuild(self.state_path, self.out_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_upsert_adds_a_new_done_paper(self):
        conn = sqlite3.connect(self.state_path)
        conn.execute(
            "INSERT INTO papers (arxiv_id, title, abstract, layers, year, status) "
            "VALUES ('2401.00007','Fresh Paper About GRPO','text','llm-slm',2025,'done')"
        )
        conn.commit()
        conn.close()
        n = build_paper_fts.upsert_papers(["2401.00007"], self.state_path, self.out_path)
        self.assertEqual(n, 1)
        self.assertIn("2401.00007", query_fts(self.out_path, "grpo"))

    def test_upsert_is_delete_then_insert_not_a_duplicate(self):
        conn = sqlite3.connect(self.state_path)
        conn.execute("UPDATE papers SET title='Renamed Nonce Paper' WHERE arxiv_id='2401.00003'")
        conn.commit()
        conn.close()
        build_paper_fts.upsert_papers(["2401.00003"], self.state_path, self.out_path)
        conn = sqlite3.connect(self.out_path)
        rows = conn.execute(
            "SELECT title FROM papers_fts WHERE arxiv_id='2401.00003'"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Renamed Nonce Paper")

    def test_upsert_empty_list_is_a_noop(self):
        self.assertEqual(build_paper_fts.upsert_papers([], self.state_path, self.out_path), 0)


class PaperFtsServiceHookTests(unittest.TestCase):
    """pipeline/service.py's _sync_paper_fts: same shape as _sync_coarse_index --
    off by default, never allowed to raise into the ingest pipeline."""

    def setUp(self):
        import types
        if "fcntl" not in sys.modules:
            try:
                import fcntl  # noqa: F401
            except ImportError:
                stub = types.ModuleType("fcntl")
                stub.flock = lambda *a, **kw: None
                stub.LOCK_EX = 2
                stub.LOCK_NB = 4
                sys.modules["fcntl"] = stub
        from pipeline import service
        self.service = service

    def test_noop_when_disabled(self):
        with patch.object(self.service, "PAPER_FTS_ENABLED", False), \
             patch("builtins.__import__") as import_mock:
            self.service._sync_paper_fts(["2401.00001"])
        import_mock.assert_not_called()

    def test_noop_for_empty_id_list(self):
        with patch.object(self.service, "PAPER_FTS_ENABLED", True):
            self.service._sync_paper_fts([])  # would raise if it did real work

    def test_exception_is_swallowed_as_warning(self):
        import types
        fake = types.ModuleType("build_paper_fts")
        fake.upsert_papers = MagicMock(side_effect=RuntimeError("disk full"))
        with patch.object(self.service, "PAPER_FTS_ENABLED", True), \
             patch.dict(sys.modules, {"build_paper_fts": fake}), \
             patch.object(self.service.log, "warning") as warn_mock:
            self.service._sync_paper_fts(["2401.00001"])  # must not raise
        warn_mock.assert_called_once()
        fake.upsert_papers.assert_called_once()

    def test_calls_upsert_papers_when_enabled(self):
        import types
        fake = types.ModuleType("build_paper_fts")
        fake.upsert_papers = MagicMock(return_value=1)
        with patch.object(self.service, "PAPER_FTS_ENABLED", True), \
             patch.dict(sys.modules, {"build_paper_fts": fake}):
            self.service._sync_paper_fts(["2401.00001"])
        fake.upsert_papers.assert_called_once_with(["2401.00001"])


class PaperBm25Tests(unittest.TestCase):
    """api/main._paper_bm25 against a real papers_fts.db."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmpdir.name, "state.db")
        self.out_path = os.path.join(self.tmpdir.name, "papers_fts.db")
        make_state_db(self.state_path, SAMPLE_PAPERS)
        build_paper_fts.rebuild(self.state_path, self.out_path)
        main._ro_conn_local.conns = {}
        self.patcher = patch.object(main, "PAPER_FTS_PATH", self.out_path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        for conn in main._ro_conn_local.conns.values():
            conn.close()
        main._ro_conn_local.conns = {}
        self.tmpdir.cleanup()

    def test_finds_exact_term(self):
        hits = main._paper_bm25("durable nonce")
        self.assertEqual(hits, ["2401.00003"])

    def test_empty_query_returns_empty(self):
        self.assertEqual(main._paper_bm25(""), [])

    def test_missing_index_file_returns_empty_not_raise(self):
        main._ro_conn_local.conns = {}
        with patch.object(main, "PAPER_FTS_PATH", os.path.join(self.tmpdir.name, "nope.db")):
            self.assertEqual(main._paper_bm25("grpo"), [])


class RunSearchPaperLexicalOffTests(unittest.TestCase):
    """PAPER_LEXICAL=0 (default): _run_search must behave exactly as before --
    _paper_bm25 must never even be called."""

    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()

    def _dense_hit(self):
        return {"score": 0.9,
                "payload": {"arxiv_id": "1234.5678", "title": "Dense Hit",
                           "section_type": "method"}}

    def test_paper_bm25_never_invoked(self):
        body = main.SearchBody(query="grpo durable nonce", hybrid=True, limit=3)
        with patch.object(main, "PAPER_LEXICAL", False), \
             patch.object(main, "_bm25_candidates", return_value=["1234.5678"]), \
             patch.object(main, "_paper_bm25") as paper_bm25_mock, \
             patch.object(main, "embed_query", return_value=[0.1, 0.2]), \
             patch.object(main, "two_phase_dense_search",
                          return_value=([self._dense_hit()], [], [], [])), \
             patch.object(main, "_paper_facts", return_value={}), \
             patch.object(main, "_lookup_note", return_value=None):
            result = main._run_search(body)
        paper_bm25_mock.assert_not_called()
        self.assertNotIn("lexical_channel", result)
        self.assertEqual(result["results"][0]["arxiv_id"], "1234.5678")


class RunSearchPaperLexicalOnTests(unittest.TestCase):
    """PAPER_LEXICAL=1: RRF of _paper_bm25 (weight 1.0) and _bm25_candidates
    (weight 0.6), and the three lexical_channel states."""

    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()

    def _dense_hit(self, arxiv_id="1234.5678", score=0.9):
        return {"score": score, "payload": {"arxiv_id": arxiv_id, "title": "Dense Hit",
                                            "section_type": "method"}}

    @staticmethod
    def _score_stub(query, paper_ids, layer=None, vector=None, timeout=45, constraints=None):
        """Stand-in for _score_specific_papers: hydrate each lexical-only id
        into a minimal, real result dict instead of hitting Qdrant."""
        return [{"arxiv_id": pid, "score": 0.5, "title": f"lexical {pid}",
                "section_type": "method", "text": "", "terms": [], "repos": [],
                "layers": [], "source": main.SOURCE_LABELS["arxiv"],
                "url": None, "venue": None, "citation_count": None,
                "fulltext": False}
                for pid in paper_ids]

    def _run(self, **overrides):
        body = main.SearchBody(query="grpo", hybrid=True, limit=5)
        patches = [
            patch.object(main, "embed_query", return_value=[0.1, 0.2]),
            patch.object(main, "two_phase_dense_search",
                        return_value=([self._dense_hit()], [], [], [])),
            patch.object(main, "_paper_facts", return_value={}),
            patch.object(main, "_lookup_note", return_value=None),
            patch.object(main, "PAPER_LEXICAL", True),
            patch.object(main, "_score_specific_papers", side_effect=self._score_stub),
            patch.object(main, "_lexical_fallback_results", return_value=[]),
        ]
        for key, value in overrides.items():
            patches.append(patch.object(main, key, value))
        ctxs = [p.__enter__() for p in patches]
        try:
            return main._run_search(body)
        finally:
            for p in reversed(patches):
                p.__exit__(None, None, None)

    def test_both_channels_fuse(self):
        result = self._run(
            _paper_bm25=MagicMock(return_value=["extra:paper"]),
            _bm25_candidates=MagicMock(return_value=["extra:chunk"]),
        )
        self.assertEqual(result.get("lexical_channel"), "paper+chunk")
        arxiv_ids = {r["arxiv_id"] for r in result["results"]}
        self.assertIn("extra:paper", arxiv_ids)
        self.assertIn("extra:chunk", arxiv_ids)

    def test_chunk_channel_skipped_reports_paper_only(self):
        def slow_bm25(*a, **kw):
            time.sleep(0.2)
            return ["extra:chunk"]

        result = self._run(
            LEXICAL_QUERY_TIMEOUT=0.01,
            _paper_bm25=MagicMock(return_value=["extra:paper"]),
            _bm25_candidates=slow_bm25,
        )
        self.assertEqual(result.get("lexical_channel"), "paper")
        arxiv_ids = {r["arxiv_id"] for r in result["results"]}
        self.assertIn("extra:paper", arxiv_ids)

    def test_both_channels_skipped_reports_skipped(self):
        def slow(*a, **kw):
            time.sleep(0.2)
            return ["x"]

        result = self._run(
            LEXICAL_QUERY_TIMEOUT=0.01,
            _paper_bm25=slow,
            _bm25_candidates=slow,
        )
        self.assertEqual(result.get("lexical_channel"), "skipped")
        # dense results still returned even though both lexical channels timed out
        self.assertEqual(result["results"][0]["arxiv_id"], "1234.5678")


def _hit(point_id, score, payload=None):
    return {"id": point_id, "score": score, "payload": payload or {}}


def _coarse_hit(arxiv_id, cosine, in_citations=0, layers=("web3",)):
    return _hit(f"coarse:{arxiv_id}", cosine,
               {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}", "in_citations": in_citations,
                "layers": list(layers)})


def _chunk_hit(point_id, arxiv_id, score):
    return _hit(point_id, score, {"arxiv_id": arxiv_id, "title": f"paper {arxiv_id}", "text": "body"})


class FakeHierQdrant:
    def __init__(self, coarse_hits, stage_b_hits, global_hits, points_by_id):
        self.coarse_hits = coarse_hits
        self.stage_b_hits = stage_b_hits
        self.global_hits = global_hits
        self.points_by_id = points_by_id
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith(f"/collections/{main.COARSE_COLLECTION}/points/search"):
            resp.json = MagicMock(return_value={"result": self.coarse_hits[: json["limit"]]})
            return resp
        if url.endswith("/points/search"):
            must = (json.get("filter") or {}).get("must") or []
            has_arxiv_filter = any(c.get("key") == "arxiv_id" for c in must)
            hits = self.stage_b_hits if has_arxiv_filter else self.global_hits
            resp.json = MagicMock(return_value={"result": hits[: json["limit"]]})
            return resp
        if url.endswith("/points"):
            ids = json["ids"]
            points = [self.points_by_id[i] for i in ids if i in self.points_by_id]
            resp.json = MagicMock(return_value={"result": points})
            return resp
        raise AssertionError(f"unexpected URL {url}")


class HierPaperLexicalShortlistTests(unittest.TestCase):
    """HIER_SEARCH + PAPER_LEXICAL: stage-A shortlist must include an article
    the lexical channel found even though the coarse dense search never
    surfaced it, provided stage B finds it real chunk evidence."""

    def setUp(self):
        main.qdrant_result_cache.data.clear()
        main.search_cache.data.clear()
        self.vector = [0.1, 0.2, 0.3]
        for name, value in (("HIER_SEARCH", True), ("PAPER_LEXICAL", True),
                           ("HIER_GLOBAL_GRACE", 0.2)):
            p = patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(main, "embed_query", return_value=self.vector)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(main, "_paper_facts", return_value={})
        p.start()
        self.addCleanup(p.stop)

    def _body(self, **kwargs):
        defaults = dict(query="lexical shortlist test", layer="web3", hybrid=False,
                        diagnose=False, dedupe=True, min_score=0.0, limit=8)
        defaults.update(kwargs)
        return main.SearchBody(**defaults)

    def test_lexical_only_paper_reaches_final_results_via_stage_b(self):
        # p1 is the only coarse hit; "lexpaper" is found only by _paper_bm25
        # and is not in the coarse shortlist at all, but stage B (searched
        # over the union of both) finds it real chunk evidence.
        coarse = [_coarse_hit("p1", 0.80)]
        chunks = [_chunk_hit("c1", "p1", 0.70), _chunk_hit("c2", "lexpaper", 0.95)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        with patch.object(main, "_paper_bm25", return_value=["lexpaper"]), \
             patch.object(main.requests, "post", side_effect=fake.post):
            result = main._run_search(self._body())

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertIn("lexpaper", ids)

        stage_b_calls = [
            j for (u, j) in fake.calls
            if u.endswith("/points/search")
            and any(c.get("key") == "arxiv_id" for c in (j.get("filter") or {}).get("must", []))
        ]
        self.assertEqual(len(stage_b_calls), 1)
        arxiv_clause = next(c for c in stage_b_calls[0]["filter"]["must"] if c["key"] == "arxiv_id")
        self.assertEqual(set(arxiv_clause["match"]["any"]), {"p1", "lexpaper"})

    def test_lexical_paper_with_no_stage_b_evidence_is_dropped(self):
        coarse = [_coarse_hit("p1", 0.80)]
        chunks = [_chunk_hit("c1", "p1", 0.70)]  # nothing for "lexpaper"
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        with patch.object(main, "_paper_bm25", return_value=["lexpaper"]), \
             patch.object(main.requests, "post", side_effect=fake.post):
            result = main._run_search(self._body())

        ids = [r["arxiv_id"] for r in result["results"]]
        self.assertNotIn("lexpaper", ids)
        self.assertIn("p1", ids)

    def test_disabled_by_default_does_not_call_paper_bm25(self):
        coarse = [_coarse_hit("p1", 0.80)]
        chunks = [_chunk_hit("c1", "p1", 0.70)]
        points = {h["id"]: h for h in chunks}
        fake = FakeHierQdrant(coarse, chunks, [], points)

        with patch.object(main, "PAPER_LEXICAL", False), \
             patch.object(main, "_paper_bm25") as paper_bm25_mock, \
             patch.object(main.requests, "post", side_effect=fake.post):
            main._run_search(self._body())
        paper_bm25_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
