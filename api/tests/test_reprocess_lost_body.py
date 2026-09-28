import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_OPS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "ops")
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
import reprocess_lost_body as rlb  # noqa: E402


class FakeQdrant:
    """Stands in for requests.Session: counts and deletes by arxiv_id."""

    def __init__(self, counts=None):
        self.counts = dict(counts or {})
        self.deleted = []

    def post(self, url, json=None, params=None, timeout=None):
        aid = json["filter"]["must"][0]["match"]["value"]
        resp = MagicMock()
        if url.endswith("/points/count"):
            resp.json.return_value = {"result": {"count": self.counts.get(aid, 0)}}
        else:
            self.deleted.append(aid)
            self.counts[aid] = 0
        return resp


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.db = os.path.join(self.dir, "state.db")
        self.fts = os.path.join(self.dir, "fts.db")
        self.list_path = os.path.join(self.dir, "list.json")
        self.progress = os.path.join(self.dir, "progress.jsonl")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, status TEXT, citation_count INTEGER, "
                     "fulltext_source TEXT, extractor_version INTEGER, updated_at TEXT)")
        conn.commit()
        conn.close()
        f = sqlite3.connect(self.fts)
        f.execute("CREATE VIRTUAL TABLE chunks USING fts5(text, point_id UNINDEXED, arxiv_id UNINDEXED)")
        f.commit()
        f.close()

    def conn(self):
        c = rlb.connect(self.db)
        self.addCleanup(c.close)
        return c

    def paper(self, aid, status="done", cites=0, src="latex"):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO papers VALUES (?,?,?,?,?,NULL)", (aid, status, cites, src, 2))
        c.commit()
        c.close()

    def status(self, aid):
        c = sqlite3.connect(self.db)
        r = c.execute("SELECT status, fulltext_source, extractor_version FROM papers WHERE arxiv_id=?",
                      (aid,)).fetchone()
        c.close()
        return r

    def args(self, **kw):
        base = ["--db", self.db, "--fts", self.fts, "--data-dir", self.dir, "--list", self.list_path,
                "--progress", self.progress, "--lock", os.path.join(self.dir, "x.lock"), "--pause", "0"]
        for k, v in kw.items():
            base += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
        return rlb.parse_args(base)

    def write_list(self, items):
        with open(self.list_path, "w") as f:
            json.dump({"items": items}, f)


class BuildListTest(Env):
    def test_sorted_by_citations_then_chunks_and_filtered(self):
        for aid, cites in [("2001.00001", 5), ("2001.00002", 50), ("2001.00003", 50),
                           ("2001.00004", 900), ("2001.00005", 7), ("iacr:2020/1", 999),
                           ("2001.00006v2", 1)]:
            self.paper(aid, cites=cites)
        self.paper("2001.00007", status="chunked", cites=1000)
        self.paper("2001.00008", src="abstract-fallback", cites=1000)
        q = FakeQdrant({"2001.00001": 3, "2001.00002": 30, "2001.00003": 2, "2001.00004": 400,
                        "2001.00005": 39, "2001.00006v2": 40, "iacr:2020/1": 1})
        items, looked = rlb.build_list(self.conn(), q, "u", "c", 40, pause=0,
                                       log=lambda *a, **k: None)
        self.assertEqual([i["id"] for i in items], ["2001.00003", "2001.00002", "2001.00005", "2001.00001"])
        self.assertEqual(looked, 6)

    def test_limit_looks_at_most_cited_only(self):
        for i, aid in enumerate(["2001.00001", "2001.00002", "2001.00003"]):
            self.paper(aid, cites=i)
        items, looked = rlb.build_list(self.conn(), FakeQdrant(), "u", "c", 40, limit=2, pause=0,
                                       log=lambda *a, **k: None)
        self.assertEqual(looked, 2)
        self.assertEqual([i["id"] for i in items], ["2001.00003", "2001.00002"])

    def test_main_writes_list_and_skips_already_reset(self):
        self.paper("2001.00001", cites=9)
        self.paper("2001.00002", cites=1)
        rlb.mark_progress(self.progress, "2001.00001")
        with patch.object(rlb.requests, "Session", lambda: FakeQdrant()):
            rlb.main(["--build-list", "--db", self.db, "--list", self.list_path, "--progress", self.progress,
                      "--lock", os.path.join(self.dir, "x.lock"), "--pause", "0"])
        with open(self.list_path) as f:
            items = json.load(f)["items"]
        self.assertEqual([i["id"] for i in items], ["2001.00002"])


class FeedTest(Env):
    def setUp(self):
        super().setUp()
        self.log = []

    def run_feed(self, q, **kw):
        return rlb.feed(self.conn(), q, self.args(**kw), log=self.log.append)

    def seed_paper_files(self, aid):
        for sub, name in [("fulltext_cache", f"{aid}.tex"), ("chunk_cache", f"{aid}.json"),
                          (os.path.join("latex_cache", aid), "source.tex")]:
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
            with open(os.path.join(self.dir, sub, name), "w") as f:
                f.write("x")

    def test_queue_full_resets_nothing(self):
        for i in range(3):
            self.paper(f"2002.0000{i}", status="quality_checked")
        self.paper("2001.00001")
        self.write_list([{"id": "2001.00001", "citations": 1, "chunks": 2}])
        q = FakeQdrant()
        self.assertEqual(self.run_feed(q, apply=True, target=3), 0)
        self.assertEqual(q.deleted, [])
        self.assertEqual(self.status("2001.00001")[0], "done")

    def test_queue_counts_all_three_statuses(self):
        self.paper("2002.00001", status="quality_checked")
        self.paper("2002.00002", status="fulltext_fetched")
        self.paper("2002.00003", status="chunked")
        self.paper("2002.00004", status="deferred")
        self.assertEqual(rlb.queue_size(self.conn()), 3)

    def test_dry_run_changes_nothing(self):
        self.paper("2001.00001")
        self.seed_paper_files("2001.00001")
        self.write_list([{"id": "2001.00001", "citations": 1, "chunks": 2}])
        q = FakeQdrant()
        self.assertEqual(self.run_feed(q), 1)
        self.assertEqual(q.deleted, [])
        self.assertEqual(self.status("2001.00001")[0], "done")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "fulltext_cache", "2001.00001.tex")))
        self.assertFalse(os.path.exists(self.progress))

    def test_apply_resets_and_cleans(self):
        self.paper("2001.00001")
        self.seed_paper_files("2001.00001")
        f = sqlite3.connect(self.fts)
        f.executemany("INSERT INTO chunks (text, point_id, arxiv_id) VALUES (?,?,?)",
                      [("a", "p1", "2001.00001"), ("b", "p2", "2001.00001"), ("c", "p3", "2001.00002")])
        f.commit()
        f.close()
        self.write_list([{"id": "2001.00001", "citations": 1, "chunks": 2}])
        q = FakeQdrant()
        self.assertEqual(self.run_feed(q, apply=True), 1)
        self.assertEqual(q.deleted, ["2001.00001"])
        self.assertEqual(self.status("2001.00001"), ("quality_checked", None, None))
        for sub in ("fulltext_cache/2001.00001.tex", "chunk_cache/2001.00001.json", "latex_cache/2001.00001"):
            self.assertFalse(os.path.exists(os.path.join(self.dir, sub)), sub)
        f = sqlite3.connect(self.fts)
        self.assertEqual(f.execute("SELECT arxiv_id FROM chunks").fetchall(), [("2001.00002",)])
        f.close()
        self.assertEqual(rlb.load_progress(self.progress), {"2001.00001"})

    def test_continues_by_progress_and_respects_batch(self):
        ids = [f"2001.0000{i}" for i in range(1, 6)]
        for aid in ids:
            self.paper(aid)
        self.write_list([{"id": a, "citations": 10 - i, "chunks": 1} for i, a in enumerate(ids)])
        q = FakeQdrant()
        self.run_feed(q, apply=True, batch=2)
        self.assertEqual(q.deleted, ids[:2])
        # the two now sit in the queue; a bigger target lets the next run go on after them
        self.run_feed(q, apply=True, batch=2, target=4)
        self.assertEqual(q.deleted, ids[:4])

    def test_target_minus_queue_limits_reset(self):
        self.paper("2002.00001", status="chunked")
        ids = ["2001.00001", "2001.00002", "2001.00003"]
        for aid in ids:
            self.paper(aid)
        self.write_list([{"id": a, "citations": 1, "chunks": 1} for a in ids])
        q = FakeQdrant()
        self.run_feed(q, apply=True, target=3)
        self.assertEqual(q.deleted, ids[:2])

    def test_commit_per_paper_leaves_no_open_transaction(self):
        ids = ["2001.00001", "2001.00002"]
        for aid in ids:
            self.paper(aid)
        self.write_list([{"id": a, "citations": 1, "chunks": 1} for a in ids])
        conn = self.conn()
        seen = []
        db = self.db

        class Probe(FakeQdrant):
            def post(self, url, **kw):
                # a second writer must be able to get in between papers
                other = sqlite3.connect(db, timeout=0.2)
                other.execute("UPDATE papers SET citation_count=citation_count WHERE arxiv_id='2001.00002'")
                other.commit()
                other.close()
                seen.append(conn.in_transaction)
                return super().post(url, **kw)

        rlb.feed(conn, Probe(), self.args(apply=True), log=lambda *a: None)
        self.assertEqual(seen, [False, False])
        self.assertFalse(conn.in_transaction)
        self.assertEqual(rlb.load_progress(self.progress), set(ids))

    def test_paper_no_longer_done_is_skipped(self):
        self.paper("2001.00001", status="chunked")
        self.write_list([{"id": "2001.00001", "citations": 1, "chunks": 1}])
        q = FakeQdrant()
        self.assertEqual(self.run_feed(q, apply=True, target=5), 0)
        self.assertEqual(q.deleted, [])
        self.assertEqual(self.status("2001.00001")[0], "chunked")


@unittest.skipUnless(hasattr(rlb.fcntl, "flock") and os.name == "posix", "real flock is POSIX only")
class LockTest(Env):
    def test_second_holder_is_refused(self):
        path = os.path.join(self.dir, "l.lock")
        with rlb.single_instance(path) as first:
            self.assertTrue(first)
            with rlb.single_instance(path) as second:
                self.assertFalse(second)
        with rlb.single_instance(path) as again:
            self.assertTrue(again)


if __name__ == "__main__":
    unittest.main()
