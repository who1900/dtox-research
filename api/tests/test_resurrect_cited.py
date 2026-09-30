import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

_OPS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "ops")
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
import resurrect_cited as rc  # noqa: E402


def make_db(path=":memory:"):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT, year INTEGER, layers TEXT, "
                 "status TEXT NOT NULL, passed INTEGER, updated_at TEXT)")
    conn.execute("CREATE TABLE citations (src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, dst))")
    return conn


def add(conn, pid, status, layers="", title=None):
    conn.execute("INSERT INTO papers (arxiv_id, title, year, layers, status) VALUES (?,?,?,?,?)",
                 (pid, title or pid, 2022, layers, status))


def cite(conn, src, dst):
    conn.execute("INSERT INTO citations (src, dst) VALUES (?,?)", (src, dst))


def seed(conn):
    # done citers: 3 web3, 1 llm-slm, 1 web3+builder-tech
    for i, layers in enumerate(["web3", "web3", "web3", "llm-slm", "web3,builder-tech", "ai-agents"]):
        add(conn, f"c{i}", "done", layers)
    add(conn, "LVR", "off_niche")          # cited by 3 web3 + 1 llm-slm -> web3
    add(conn, "REJ", "rejected")           # cited by 3 (web3, web3, llm-slm) -> web3
    add(conn, "FEW", "rejected")           # only 2 citers
    add(conn, "SPLIT", "off_niche")        # 4 layers, 1 vote each: nothing over half
    add(conn, "LIVE", "discovered")        # not off_niche/rejected: never a candidate
    add(conn, "PENDING", "quality_checked")
    add(conn, "notdone", "chunked", "web3")  # a citer that is not done does not count
    for s in ("c0", "c1", "c2", "c3"):
        cite(conn, s, "LVR")
    for s in ("c0", "c1", "c3"):
        cite(conn, s, "REJ")
    for s in ("c0", "c1"):
        cite(conn, s, "FEW")
    cite(conn, "c3", "SPLIT")
    cite(conn, "c4", "SPLIT")
    cite(conn, "c5", "SPLIT")
    for s in ("c0", "c1", "c2"):
        cite(conn, s, "LIVE")
    cite(conn, "notdone", "FEW")
    cite(conn, "notdone", "SPLIT")
    conn.commit()


class MajorityTests(unittest.TestCase):
    def test_more_than_half(self):
        self.assertEqual(rc.majority_layers({"web3": 3, "llm-slm": 1}, 4), ["web3"])
        self.assertEqual(rc.majority_layers({"web3": 3, "builder-tech": 2, "llm-slm": 1}, 4), ["web3"])

    def test_two_layers_both_over_half(self):
        self.assertEqual(rc.majority_layers({"web3": 4, "builder-tech": 3}, 5), ["builder-tech", "web3"])

    def test_no_majority_takes_most_common(self):
        self.assertEqual(rc.majority_layers({"web3": 2, "llm-slm": 1, "builder-tech": 1}, 4), ["web3"])
        self.assertEqual(rc.majority_layers({"b": 1, "a": 1}, 2 + 1), ["a"])  # tie: by name

    def test_empty(self):
        self.assertEqual(rc.majority_layers({}, 3), [])


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        seed(self.conn)

    def test_threshold_and_status(self):
        got = {c["id"]: c for c in rc.find_candidates(self.conn, min_citers=3)}
        self.assertEqual(set(got), {"LVR", "REJ", "SPLIT"})
        self.assertNotIn("FEW", got)      # 2 done citers (the third is not done)
        self.assertNotIn("LIVE", got)     # wrong status
        self.assertEqual(got["LVR"]["citers"], 4)
        self.assertEqual(got["REJ"]["citers"], 3)
        got2 = {c["id"] for c in rc.find_candidates(self.conn, min_citers=2)}
        self.assertEqual(got2, {"LVR", "REJ", "FEW", "SPLIT"})

    def test_layers_by_majority(self):
        got = {c["id"]: c["layers"] for c in rc.find_candidates(self.conn, min_citers=2)}
        self.assertEqual(got["LVR"], ["web3"])
        self.assertEqual(got["REJ"], ["web3"])
        self.assertEqual(got["SPLIT"], ["ai-agents"])  # 1 vote each, tie -> first by name

    def test_layer_filter_and_order(self):
        got = rc.find_candidates(self.conn, min_citers=2, layers=["web3"])
        self.assertEqual([c["id"] for c in got], ["LVR", "REJ", "FEW"])  # most cited first
        self.assertEqual(rc.find_candidates(self.conn, min_citers=2, layers=["llm-slm"]), [])


class ResurrectTests(unittest.TestCase):
    def test_apply_skip_gate(self):
        conn = make_db()
        seed(conn)
        cands = rc.find_candidates(conn, min_citers=3)
        n = rc.resurrect(conn, cands, gate="skip", log=lambda *_: None)
        self.assertEqual(n, 3)
        for pid in ("LVR", "REJ"):
            row = conn.execute("SELECT status, layers, passed FROM papers WHERE arxiv_id=?", (pid,)).fetchone()
            self.assertEqual((row["status"], row["layers"], row["passed"]), ("quality_checked", "web3", 1))
        self.assertEqual(conn.execute("SELECT status FROM papers WHERE arxiv_id='FEW'").fetchone()[0], "rejected")
        # second run finds nothing: the papers left off_niche/rejected
        self.assertEqual(rc.find_candidates(conn, min_citers=3), [])

    def test_gate_keep_and_limit(self):
        conn = make_db()
        seed(conn)
        cands = rc.find_candidates(conn, min_citers=3)
        n = rc.resurrect(conn, cands, gate="keep", limit=1, log=lambda *_: None)
        self.assertEqual(n, 1)
        row = conn.execute("SELECT status, passed FROM papers WHERE arxiv_id='LVR'").fetchone()
        self.assertEqual((row["status"], row["passed"]), ("discovered", None))
        self.assertEqual(conn.execute("SELECT status FROM papers WHERE arxiv_id='REJ'").fetchone()[0], "rejected")

    def test_status_changed_meanwhile_is_left_alone(self):
        conn = make_db()
        seed(conn)
        cands = rc.find_candidates(conn, min_citers=3)
        conn.execute("UPDATE papers SET status='done' WHERE arxiv_id='LVR'")
        conn.commit()
        self.assertEqual(rc.resurrect(conn, cands, log=lambda *_: None), 2)
        self.assertEqual(conn.execute("SELECT status FROM papers WHERE arxiv_id='LVR'").fetchone()[0], "done")


class DryRunTests(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "state.db")
            conn = make_db(db)
            seed(conn)
            conn.close()
            before = pathlib.Path(db).read_bytes()
            rc_main = rc.main(["--db", db, "--lock", os.path.join(d, "x.lock"), "--min-citers", "3"])
            self.assertEqual(rc_main, 0)
            self.assertEqual(pathlib.Path(db).read_bytes(), before)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT status FROM papers WHERE arxiv_id='LVR'").fetchone()[0], "off_niche")
            conn.close()

    def test_apply_writes(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "state.db")
            conn = make_db(db)
            seed(conn)
            conn.close()
            self.assertEqual(rc.main(["--db", db, "--lock", os.path.join(d, "x.lock"), "--apply",
                                      "--layers", "web3"]), 0)
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT status FROM papers WHERE arxiv_id='LVR'").fetchone()[0],
                             "quality_checked")
            conn.close()

    def test_unknown_layer_rejected(self):
        self.assertEqual(rc.main(["--layers", "nope", "--db", ":memory:"]), 2)


if __name__ == "__main__":
    unittest.main()
