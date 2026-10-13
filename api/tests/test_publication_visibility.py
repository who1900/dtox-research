"""Publication visibility gate tests use only temporary, read-only state DBs."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from api import main


class PublicationVisibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "state.db")
        self.fts_db = None
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE paper_publications(arxiv_id TEXT, revision TEXT, kind TEXT, state TEXT, retry_after REAL, "
                     "PRIMARY KEY(arxiv_id,revision))")
        conn.execute("CREATE INDEX idx_publications_kind_state_retry "
                     "ON paper_publications(kind,state,retry_after)")
        conn.commit()
        conn.close()
        self.path_patch = patch.object(main, "STATE_DB_PATH", self.db)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self._drop_connection()
        self.addCleanup(self._drop_connection)

    def _drop_connection(self):
        conns = getattr(main._ro_conn_local, "conns", {})
        for path in (self.db, self.fts_db):
            if not path:
                continue
            conn = conns.pop(path, None)
            if conn is not None:
                conn.close()

    def publish(self, paper_id, revision, kind="snapshot", state="published"):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT OR REPLACE INTO paper_publications(arxiv_id,revision,kind,state) VALUES (?,?,?,?)",
                     (paper_id, revision, kind, state))
        conn.commit()
        conn.close()

    @staticmethod
    def point(paper_id, revision=None, point_id=None):
        payload = {"arxiv_id": paper_id, "text": paper_id}
        if revision is not None:
            payload["publication_revision"] = revision
        return {"id": point_id or paper_id, "payload": payload, "score": 0.9}

    def test_pending_and_partial_hidden_exact_published_visible(self):
        self.publish("visible", "r1")
        self.publish("pending", "r2", state="staged")
        self.publish("partial", "r3", state="publishing")
        points = [self.point("pending", "r2"), self.point("partial", "r3"),
                  self.point("visible", "r1")]
        self.assertEqual([p["id"] for p in main._filter_publication_points(points)], ["visible"])

    def test_wrong_revision_and_non_snapshot_kind_are_blocked(self):
        self.publish("paper", "right")
        self.publish("paper", "not-snapshot", kind="source")
        points = [self.point("paper", "wrong"), self.point("paper", "not-snapshot"),
                  self.point("paper", "right")]
        self.assertEqual([p["payload"]["publication_revision"]
                          for p in main._filter_publication_points(points)], ["right"])

    def test_legacy_points_do_not_open_database(self):
        with patch.object(main, "_ro_conn", side_effect=AssertionError("unexpected DB call")) as read_db:
            points = [self.point("legacy-a"), self.point("legacy-b")]
            self.assertEqual(main._filter_publication_points(points), points)
        read_db.assert_not_called()

    def test_unavailable_database_hides_marked_but_keeps_legacy(self):
        points = [self.point("legacy"), self.point("new", "pending")]
        with patch.object(main, "_ro_conn", side_effect=sqlite3.OperationalError("offline")):
            self.assertEqual(main._filter_publication_points(points), [points[0]])

    def test_poisoned_marker_without_exact_identity_is_hidden(self):
        self.publish("good", "r1")
        points = [self.point("legacy"), {"id": "poison", "payload": {
            "publication_revision": {"unexpected": "object"}}},
            {"id": "empty", "payload": {"arxiv_id": "paper", "publication_revision": ""}},
            self.point("good", "r1")]
        self.assertEqual([point["id"] for point in main._filter_publication_points(points)],
                         ["legacy", "good"])

    def test_cached_raw_result_becomes_visible_after_publication(self):
        main.qdrant_result_cache.data.clear()
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"result": [self.point("paper", "rev1")]}
        vector = [0.1, 0.2]
        with patch.object(main.requests, "post", return_value=response) as post:
            self.assertEqual(main.qdrant_search(vector, None, 5), [])
            self.publish("paper", "rev1")
            self.assertEqual([p["id"] for p in main.qdrant_search(vector, None, 5)], ["paper"])
        post.assert_called_once()
        main.qdrant_result_cache.data.clear()

    def test_grouped_stage_b_hides_pending_and_keeps_published_and_legacy(self):
        self.publish("published", "r1")
        self.publish("pending", "r2", state="staged")
        hits = [self.point("pending", "r2"), self.point("published", "r1"),
                self.point("legacy")]
        with patch.object(main, "HIER_GROUPED_SEARCH", True), \
                patch.object(main, "grouped_hits", return_value=hits):
            result = main._hier_stage_b_hits([0.1], [], ["pending", "published", "legacy"], 10, 1.0)
        self.assertEqual([hit["payload"]["arxiv_id"] for hit in result],
                         ["published", "legacy"])

    def test_narrow_search_and_scroll_payload_include_gate_fields(self):
        with patch.object(main.requests, "post") as post:
            resp = MagicMock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"result": []}
            post.return_value = resp
            main.qdrant_search([1], None, 2, with_payload=["section_type"])
        fields = post.call_args.kwargs["json"]["with_payload"]
        self.assertEqual(set(fields), {"section_type", "arxiv_id", "publication_revision"})

    def test_scroll_limit_caps_raw_seen_not_visible_results(self):
        pages = [
            {"points": [self.point("hidden", "pending"), self.point("visible", "ok")],
             "next_page_offset": "next"},
            {"points": [self.point("legacy")], "next_page_offset": "more"},
        ]
        responses = []
        for page in pages:
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.json.return_value = {"result": page}
            responses.append(response)
        self.publish("visible", "ok")
        with patch.object(main.requests, "post", side_effect=responses) as post:
            result = main.qdrant_scroll_by_arxiv("any", limit=3, page=2,
                                                  payload_fields=["chunk_index"])
        self.assertEqual([p["id"] for p in result], ["visible", "legacy"])
        self.assertEqual([call.kwargs["json"]["limit"] for call in post.call_args_list], [2, 1])
        self.assertTrue(all({"arxiv_id", "publication_revision"}.issubset(
            call.kwargs["json"]["with_payload"]["include"]) for call in post.call_args_list))

    def test_publication_journal_query_batches_at_most_200_pairs(self):
        points = [self.point(f"p{i}", f"r{i}") for i in range(401)]
        class RecordingConnection:
            def __init__(self):
                self.calls = []

            def execute(self, sql, params):
                self.calls.append((sql, params))
                class Result:
                    @staticmethod
                    def fetchall():
                        return []
                return Result()

        conn = RecordingConnection()
        with patch.object(main, "_ro_conn", return_value=conn):
            self.assertEqual(main._filter_publication_points(points), [])
        self.assertEqual(len(conn.calls), 3)
        self.assertLessEqual(max(len(params) // 2 for _, params in conn.calls), 200)

    def test_journal_lookup_uses_exact_composite_primary_key_without_table_scan(self):
        conn = main._ro_conn(self.db)
        pairs = [(f"paper{i}", f"revision{i}") for i in range(200)]
        points = [self.point(paper_id, revision) for paper_id, revision in pairs]
        main._filter_publication_points(points)
        sql = ("SELECT arxiv_id, revision FROM paper_publications WHERE " + " OR ".join(
            "(arxiv_id=? AND revision=? AND kind='snapshot' AND state='published')"
            for _ in pairs))
        params = [value for pair in pairs for value in pair]
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        details = " ".join(str(row[3]).upper() for row in plan)
        self.assertNotIn("SCAN PAPER_PUBLICATIONS", details)
        self.assertIn("SEARCH PAPER_PUBLICATIONS", details)
        self.assertRegex(details, r"SEARCH PAPER_PUBLICATIONS USING .*\(ARXIV_ID=\? AND REVISION=\?\)")

    def test_lexical_fallback_requires_done_metadata_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.db")
            fts_path = str(Path(directory) / "fts.db")
            self.fts_db = fts_path
            state = sqlite3.connect(state_path)
            state.execute("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT,title TEXT,"
                          "citation_count INTEGER,venue TEXT,niche_score INTEGER,matched_terms TEXT,source_url TEXT)")
            statuses = (("staged", "staged"), ("ready", "quality_checked"),
                        ("chunked", "chunked"), ("done", "done"))
            state.executemany("INSERT INTO papers(arxiv_id,status,title) VALUES (?,?,?)",
                              [(pid, status, pid) for pid, status in statuses])
            state.commit()
            state.close()
            fts = sqlite3.connect(fts_path)
            fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text, arxiv_id UNINDEXED, "
                         "section_type, element_type, layers, year UNINDEXED)")
            fts.executemany("INSERT INTO chunks VALUES ('visibility evidence',?,'method','text','web3',2025)",
                            [(pid,) for pid, _ in statuses])
            fts.commit()
            fts.close()
            state_patch = patch.object(main, "STATE_DB_PATH", state_path)
            fts_patch = patch.object(main, "FTS_DB_PATH", fts_path)
            state_patch.start()
            fts_patch.start()
            try:
                self._drop_connection()
                with patch.object(main, "_fts_query", return_value='"visibility"'):
                    results = main._lexical_fallback_results(
                        "visibility", [pid for pid, _ in statuses], 10)
                self.assertEqual([row["arxiv_id"] for row in results], ["done"])

                real_ro_conn = main._ro_conn
                cached_state = getattr(main._ro_conn_local, "conns", {}).pop(state_path, None)
                if cached_state is not None:
                    cached_state.close()
                def fail_state(path, *args, **kwargs):
                    if path == state_path:
                        raise sqlite3.OperationalError("state unavailable")
                    return real_ro_conn(path, *args, **kwargs)
                with patch.object(main, "_ro_conn", side_effect=fail_state), \
                        patch.object(main, "_fts_query", return_value='"visibility"'):
                    self.assertEqual(main._lexical_fallback_results("visibility", ["done"], 10), [])
            finally:
                conns = getattr(main._ro_conn_local, "conns", {})
                for path in (state_path, fts_path):
                    cached = conns.pop(path, None)
                    if cached is not None:
                        cached.close()
                fts_patch.stop()
                state_patch.stop()

    def test_canonical_section_manifest_reader_requires_done(self):
        with patch.object(main, "_ro_conn") as read_state, \
                patch("pipeline.document_cache.load_section_manifest") as load_manifest:
            read_state.return_value.execute.return_value.fetchone.return_value = ("chunked",)
            manifest, reason = main._load_canonical_section_manifest("paper")
        self.assertIsNone(manifest)
        self.assertEqual(reason, "paper_not_done")
        load_manifest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
