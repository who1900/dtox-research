"""Bounded integration cycles: real local journals/FTS, no external services."""
import copy
import json
from json import loads
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from api import main
from pipeline.publication import (
    PublicationBlocked, prepare_snapshot,
    publish_snapshot, reconcile_publications,
)
from pipeline.tests import test_publication_failures as failure_fixtures
from pipeline.tests.test_publication_preflight import service


def offline_guards(stack):
    for target in ("requests.sessions.Session.request", "urllib.request.urlopen",
                   "socket.socket.connect", "socket.create_connection"):
        stack.enter_context(patch(target, side_effect=AssertionError("network forbidden")))
    stack.enter_context(patch.object(service, "embed_texts_batch",
                                   side_effect=AssertionError("reembedding forbidden")))


@contextmanager
def local_database(path, **options):
    with closing(sqlite3.connect(path, **options)) as conn:
        with conn:
            yield conn


class LocalQdrant:
    def __init__(self, path):
        self.path = path
        self.calls = []
        with local_database(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS points(id TEXT PRIMARY KEY,paper TEXT,body TEXT)")
            conn.execute("CREATE INDEX IF NOT EXISTS points_paper ON points(paper)")

    def seed(self, points):
        with local_database(self.path) as conn:
            conn.executemany("INSERT OR REPLACE INTO points VALUES (?,?,?)",
                             [(p["id"], p["payload"]["arxiv_id"], json.dumps(p)) for p in points])

    def points(self, paper):
        with local_database(self.path) as conn:
            return [json.loads(row[0]) for row in conn.execute(
                "SELECT body FROM points WHERE paper=? ORDER BY id", (paper,))]

    @staticmethod
    def response(result):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": copy.deepcopy(result)}
        return Response()

    def put(self, url, *, params, json, timeout):
        assert url.endswith("/points") and params == {"wait": "true"} and timeout == 60
        assert len(json["points"]) <= service.UPSERT_BATCH_SIZE
        self.calls.append("upsert")
        self.seed(json["points"])
        return self.response({"status": "completed"})

    def post(self, url, *, json, timeout, params=None):
        assert timeout == 60
        if url.endswith("/points"):
            self.calls.append("retrieve")
            assert 0 < len(json["ids"]) <= 200
            with local_database(self.path) as conn:
                found = [conn.execute("SELECT body FROM points WHERE id=?", (pid,)).fetchone()
                         for pid in json["ids"]]
            return self.response([loads(row[0]) for row in found if row])
        must = json["filter"]["must"]
        assert must[0]["key"] == "arxiv_id"
        paper = must[0]["match"]["value"]
        if url.endswith("/points/scroll"):
            self.calls.append("scroll")
            assert json["limit"] == 1
            revision = json["filter"]["must_not"][0]["match"]["value"]
            found = [p for p in self.points(paper)
                     if p["payload"].get("publication_revision") != revision]
            return self.response({"points": found[:1]})
        assert url.endswith("/points/delete") and params == {"wait": "true"}
        self.calls.append("delete")
        tail = must[1]["range"]["gt"] if len(must) > 1 else None
        removed = [p["id"] for p in self.points(paper)
                   if tail is None or p["payload"]["chunk_index"] > tail]
        with local_database(self.path) as conn:
            conn.executemany("DELETE FROM points WHERE id=?", [(pid,) for pid in removed])
        return self.response({"status": "completed"})


def callbacks(conn, session):
    return (
        lambda job, points: service._upsert_chunk_snapshot(session, job["paper_id"], points),
        lambda job, points: service.fts_write(points, [job["paper_id"]],
                                              service._fresh_fts_ids(conn, job["paper_id"])),
        lambda paper, commit=False: service._complete_paper(conn, paper, commit=commit),
        lambda job, points: service._preflight_chunk_snapshot(session, job, points),
    )


def crash_worker(root, job, phase):
    root = Path(root)
    conn = sqlite3.connect(root / "state.db")
    conn.row_factory = sqlite3.Row

    class CrashConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        def commit(self):
            conn.commit()
            row = conn.execute("SELECT completed FROM paper_publications WHERE arxiv_id=? AND revision=?",
                               (job["paper_id"], job["revision"])).fetchone()
            done = set(json.loads(row[0])) if row else set()
            if (phase == "qack" and done == {"preflight", "qdrant"}
                    or phase == "ftsack" and done == {"preflight", "qdrant", "fts"}):
                os._exit(73)

    with ExitStack() as stack:
        offline_guards(stack)
        stack.enter_context(patch.object(service, "FTS_DB_PATH", root / "fts.db"))
        wrapped = CrashConnection()
        qwrite, fwrite, complete, preflight = callbacks(wrapped, LocalQdrant(root / "qdrant.db"))
        def before_visibility(paper, commit=False):
            complete(paper, commit=commit)
            assert conn.in_transaction
            os._exit(73)
        publish_snapshot(wrapped, job, qwrite, fwrite,
                         before_visibility if phase == "visibility" else complete, preflight)
    raise AssertionError("crash boundary was not reached")


class PublicationCycleTests(unittest.TestCase):
    cache = failure_fixtures.PublicationFailureTests.cache
    point = failure_fixtures.PublicationFailureTests.point
    job = failure_fixtures.PublicationFailureTests.job
    journal = failure_fixtures.PublicationFailureTests.journal
    assert_uncompleted = failure_fixtures.PublicationFailureTests.assert_uncompleted
    no_callback = failure_fixtures.PublicationFailureTests.no_callback

    def setUp(self):
        failure_fixtures.PublicationFailureTests.setUp(self)
        self.conn.execute("ALTER TABLE papers ADD COLUMN updated_at TEXT")
        self.conn.executescript(
            "CREATE TABLE paper_index_sync(arxiv_id TEXT PRIMARY KEY,revision TEXT,retry_after REAL DEFAULT 0);"
            "CREATE TABLE paper_index_sync_progress(arxiv_id TEXT PRIMARY KEY,revision TEXT,completed TEXT DEFAULT '[]');"
        )
        with local_database(self.root / "fts.db") as fts:
            fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,"
                        "section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        offline_guards(self.stack)
        self.stack.enter_context(patch.object(service, "FTS_DB_PATH", self.root / "fts.db"))
        self.session = LocalQdrant(self.root / "qdrant.db")
        self.reader = sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True)
        self.addCleanup(self.reader.close)
        self.stack.enter_context(patch.object(main, "STATE_DB_PATH", str(self.database)))
        self.stack.enter_context(patch.object(main, "_ro_conn", return_value=self.reader))

    def add_paper(self, paper):
        self.conn.execute("INSERT INTO papers(arxiv_id,status,parse_status,title,year,layers,source_url) "
                          "VALUES(?,'chunked','ok','Title',2026,'web3','url')", (paper,))
        self.conn.execute("INSERT INTO completion_history VALUES(?,1,0)", (paper,))
        self.cache(paper).write_text('[{"chunk_index":0,"text":"hello"}]', encoding="utf-8")
        self.conn.commit()

    def reconnect(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.database)
        self.conn.row_factory = sqlite3.Row

    def fts_rows(self):
        with local_database(self.root / "fts.db") as fts:
            return fts.execute("SELECT rowid,point_id,text,arxiv_id FROM chunks ORDER BY rowid").fetchall()

    def assert_hidden(self, paper="p"):
        self.assertEqual(main._filter_publication_points(self.session.points(paper)), [])
        self.assert_uncompleted(paper)
        self.assertEqual(self.reader.execute("SELECT status FROM papers WHERE arxiv_id=?", (paper,)).fetchone()[0],
                         "chunked")
        self.assertEqual(self.conn.execute("SELECT * FROM paper_index_sync").fetchall(), [])

    def assert_published(self, job):
        self.assertEqual(self.journal(job["paper_id"])["state"], "published")
        self.assertEqual(main._filter_publication_points(self.session.points(job["paper_id"])),
                         self.session.points(job["paper_id"]))
        self.assertEqual(self.conn.execute("SELECT first_completed FROM completion_totals").fetchone()[0], 1)
        self.assertEqual(len(self.conn.execute("SELECT * FROM paper_index_sync").fetchall()), 1)

    def test_cycle_after_qack_backoff_reopen_and_exact_job_retry(self):
        job = self.job()
        qwrite, fwrite, complete, preflight = callbacks(self.conn, self.session)
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, qwrite, lambda *_: False,
                                                    complete, preflight), 0)
        self.assertEqual(json.loads(self.journal()["completed"]), ["preflight", "qdrant"])
        self.assert_hidden()
        points_before = self.session.points("p")
        self.reconnect()
        with patch("time.time", return_value=1029):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback, self.no_callback,
                                                    self.no_callback, self.no_callback), 0)
        _, fwrite, complete, _ = callbacks(self.conn, self.session)
        with patch("time.time", return_value=1030):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback, fwrite,
                                                    complete, self.no_callback), 1)
        self.assert_published(job)
        self.assertEqual(self.session.points("p"), points_before)
        self.assertEqual(self.session.calls.count("upsert"), 1)
        self.assertEqual(len(self.fts_rows()), 1)
        self.assertTrue(publish_snapshot(self.conn, job, self.no_callback, self.no_callback,
                                         self.no_callback, self.no_callback))
        self.assert_published(job)

    def test_cycle_after_ftsack_sql_failure_retains_owned_rowids_on_retry(self):
        job = self.job()
        qwrite, fwrite, complete, preflight = callbacks(self.conn, self.session)
        def fail(paper, commit=False):
            complete(paper, commit=commit)
            raise sqlite3.OperationalError("fixture completion failure")
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, qwrite, fwrite, fail, preflight), 0)
        rows_before = self.fts_rows()
        self.assertEqual(len(rows_before), 1)
        self.assertEqual(json.loads(self.journal()["completed"]), ["fts", "preflight", "qdrant"])
        self.assert_hidden()
        self.reconnect()
        _, _, complete, _ = callbacks(self.conn, self.session)
        with patch("time.time", return_value=1030):
            self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback, self.no_callback,
                                                    complete, self.no_callback), 1)
        self.assertEqual(self.fts_rows(), rows_before)
        self.assert_published(job)

    def test_process_crash_at_qack(self):
        self.crash_cycle("qack", ["preflight", "qdrant"])

    def test_process_crash_at_ftsack(self):
        self.crash_cycle("ftsack", ["fts", "preflight", "qdrant"])

    def test_process_crash_after_done_writes_before_visibility_commit(self):
        self.crash_cycle("visibility", ["fts", "preflight", "qdrant"])

    def crash_cycle(self, phase, acks):
        job = self.job()
        worker = ("import json,sys; from pipeline.tests.test_publication_cycles import crash_worker; "
                  "crash_worker(sys.argv[1],json.loads(sys.argv[2]),sys.argv[3])")
        result = subprocess.run([sys.executable, "-B", "-c", worker, str(self.root), json.dumps(job), phase],
                                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        self.reconnect()
        self.assertEqual(json.loads(self.journal()["completed"]), acks)
        self.assert_hidden()
        rows_before = self.fts_rows()
        _, fwrite, complete, _ = callbacks(self.conn, self.session)
        self.assertTrue(publish_snapshot(self.conn, job, self.no_callback,
                                         fwrite if phase == "qack" else self.no_callback,
                                         complete, self.no_callback))
        if phase != "qack":
            self.assertEqual(self.fts_rows(), rows_before)
        self.assert_published(job)

    def test_durable_qwrite_without_journal_ack_replays_exact_job(self):
        job = self.job()
        qwrite, fwrite, complete, preflight = callbacks(self.conn, self.session)
        def lost_response(current, points):
            self.assertTrue(qwrite(current, points))
            raise TimeoutError("fixture lost Qdrant ACK")
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, lost_response, fwrite, complete, preflight), 0)
        before = self.session.points("p")
        self.assertEqual(json.loads(self.journal()["completed"]), ["preflight"])
        self.assert_hidden()
        self.reconnect()
        qwrite, fwrite, complete, preflight = callbacks(self.conn, self.session)
        self.assertTrue(publish_snapshot(self.conn, job, qwrite, fwrite, complete, preflight))
        self.assertEqual(self.session.calls.count("retrieve"), 2)
        self.assertEqual(self.session.points("p"), before)
        self.assert_published(job)

    def test_durable_ftswrite_without_journal_ack_replays_exact_owned_rows(self):
        job = self.job()
        qwrite, fwrite, complete, preflight = callbacks(self.conn, self.session)
        def lost_response(current, points):
            self.assertTrue(fwrite(current, points))
            raise TimeoutError("fixture lost FTS ACK")
        with patch("time.time", return_value=1000):
            self.assertEqual(reconcile_publications(self.conn, self.root, qwrite, lost_response, complete, preflight), 0)
        before = self.fts_rows()
        self.assert_hidden()
        self.reconnect()
        _, fwrite, complete, _ = callbacks(self.conn, self.session)
        self.assertTrue(publish_snapshot(self.conn, job, self.no_callback, fwrite, complete, self.no_callback))
        self.assertEqual(self.fts_rows(), before)
        self.assert_published(job)

    def test_partial_qdrant_batch_failure_replays_identical_vectors_and_ids(self):
        second = self.point()
        second["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "p#1"))
        second["payload"].update(chunk_index=1, text="second chunk")
        second["vector"] = [0.4, 0.5]
        self.cache().write_text(json.dumps([{"chunk_index": 0, "text": "hello"},
                                          {"chunk_index": 1, "text": "second chunk"}]), encoding="utf-8")
        job = prepare_snapshot(self.conn, self.root, "p", [self.point(), second], self.cache())
        original_put = self.session.put
        calls = 0
        def partial(url, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("fixture second Qdrant batch failure")
            return original_put(url, **kwargs)
        with patch.object(service, "UPSERT_BATCH_SIZE", 1), patch.object(self.session, "put", side_effect=partial):
            with patch("time.time", return_value=1000):
                self.assertEqual(reconcile_publications(self.conn, self.root, *callbacks(self.conn, self.session)), 0)
        before = self.session.points("p")
        self.assertEqual(len(before), 1)
        self.assertEqual(json.loads(self.journal()["completed"]), ["preflight"])
        self.assert_hidden()
        self.reconnect()
        with patch.object(service, "UPSERT_BATCH_SIZE", 1):
            self.assertTrue(publish_snapshot(self.conn, job, *callbacks(self.conn, self.session)))
        after = self.session.points("p")
        self.assertEqual([point for point in after if point["id"] == before[0]["id"]], before)
        self.assertEqual({tuple(point["vector"]) for point in after}, {(0.2, 0.3), (0.4, 0.5)})
        self.assertEqual(len(self.fts_rows()), 2)
        self.assert_published(job)

    def test_nonembedded_cache_records_and_table_only_snapshot_remain_supported(self):
        self.cache().write_text(json.dumps([{"chunk_index": 0, "text": "hello"},
                                          {"chunk_index": 1, "text": "table", "element_type": "table"}]),
                                encoding="utf-8")
        job = self.job()
        self.assertTrue(publish_snapshot(self.conn, job, *callbacks(self.conn, self.session)))
        self.assert_published(job)
        self.add_paper("table-only")
        self.cache("table-only").write_text('[{"chunk_index":0,"text":"table","element_type":"table"}]',
                                           encoding="utf-8")
        empty = prepare_snapshot(self.conn, self.root, "table-only", [], self.cache("table-only"))
        self.assertTrue(publish_snapshot(self.conn, empty, *callbacks(self.conn, self.session)))
        self.assertEqual(self.journal("table-only")["state"], "published")
        self.assertEqual(self.session.points("table-only"), [])
        self.assertEqual(len(self.fts_rows()), 1)
        self.assertEqual(self.conn.execute("SELECT first_completed FROM completion_totals").fetchone()[0], 2)

    def test_foreign_uuid_revision_and_unmarked_paper_fail_before_mutation(self):
        job = self.job()
        for paper, revision, same_uuid, code in (
                ("foreign", "foreign", True, "legacy_uuid_collision"),
                ("p", "foreign", True, "legacy_uuid_collision"),
                ("p", None, False, "legacy_paper_collision")):
            with self.subTest(paper=paper, revision=revision):
                point = self.point()
                point["id"] = point["id"] if same_uuid else str(uuid.uuid5(uuid.NAMESPACE_URL, "legacy#0"))
                point["payload"]["arxiv_id"] = paper
                if revision is not None:
                    point["payload"]["publication_revision"] = revision
                with local_database(self.session.path) as local:
                    local.execute("DELETE FROM points")
                self.session.seed([point])
                self.conn.execute("UPDATE paper_publications SET state='ready',completed='[]',retry_after=0")
                self.conn.commit()
                before = self.session.points(paper)
                preflight = callbacks(self.conn, self.session)[3]
                self.assertEqual(reconcile_publications(self.conn, self.root, self.no_callback, self.no_callback,
                                                        self.no_callback, preflight), 0)
                self.assertEqual(self.journal()["state"], "blocked")
                self.assertEqual(self.journal()["error_code"], code)
                self.assertEqual(self.session.points(paper), before)
                self.assertEqual(self.fts_rows(), [])
                self.assert_uncompleted()

    def test_metadata_and_cache_drift_at_both_ack_boundaries_hide_revision(self):
        for phase in ("qdrant", "fts"):
            for field, code in (("metadata", "metadata_drift"), ("cache", "cache_hash_mismatch")):
                paper = f"fixture-{phase}-{field}"
                with self.subTest(phase=phase, field=field):
                    self.add_paper(paper)
                    self.job(paper)
                    qwrite, fwrite, _, preflight = callbacks(self.conn, self.session)
                    def drift(current, points):
                        self.assertTrue((qwrite if phase == "qdrant" else fwrite)(current, points))
                        if field == "metadata":
                            self.conn.execute("UPDATE papers SET layers='builder-tech,web3' WHERE arxiv_id=?", (paper,))
                            self.conn.commit()
                        else:
                            self.cache(paper).write_text("changed", encoding="utf-8")
                        return True
                    self.assertEqual(reconcile_publications(
                        self.conn, self.root, drift if phase == "qdrant" else qwrite,
                        drift if phase == "fts" else self.no_callback, self.no_callback, preflight), 0)
                    self.assertEqual(self.journal(paper)["state"], "blocked")
                    self.assertEqual(self.journal(paper)["error_code"], code)
                    self.assertEqual(set(json.loads(self.journal(paper)["completed"])),
                                     {"preflight", "qdrant"} | ({"fts"} if phase == "fts" else set()))
                    self.assert_hidden(paper)

    def test_two_concurrent_stagers_admit_only_one_snapshot_revision(self):
        barrier = threading.Barrier(2)
        def stage(vector):
            with local_database(self.database, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                point = self.point()
                point["vector"] = vector
                barrier.wait(timeout=5)
                try:
                    return prepare_snapshot(conn, self.root, "p", [point], self.cache())["revision"]
                except PublicationBlocked as exc:
                    return exc.code
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(stage, ([0.2, 0.3], [0.8, 0.9])))
        self.assertEqual(results.count("snapshot_conflict"), 1)
        self.assertIn(self.journal()["revision"], results)
        self.assertEqual(len(list((self.root / "publication_cache").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
