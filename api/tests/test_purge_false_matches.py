import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

_OPS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "ops")
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
import purge_false_matches as pfm  # noqa: E402

PHYS = ("Neutrons at 5 MeV", "Measured MeV neutron spectra in nuclear physics.")
REAL = ("MEV extraction on Ethereum", "Searchers and MEV on Ethereum.")
MIXED = ("Stark effect and MeV beams", "A large language model agent for beam physics.")


class FakeQdrant:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, params=None, timeout=None):
        self.calls.append((url.rsplit("/collections/", 1)[1], json))
        return MagicMock()


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.db = os.path.join(d, "state.db")
        self.pfts = os.path.join(d, "papers_fts.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT, abstract TEXT, "
                     "layers TEXT, status TEXT, niche_score INT, matched_terms TEXT, "
                     "citation_count INT, updated_at TEXT)")
        conn.commit()
        conn.close()
        f = sqlite3.connect(self.pfts)
        f.execute("CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id UNINDEXED, title, abstract, "
                  "layers UNINDEXED, year UNINDEXED)")
        f.commit()
        f.close()
        self.args = pfm.parse_args(["--db", self.db, "--paper-fts", self.pfts,
                                    "--qdrant", "http://q", "--progress", os.path.join(d, "p.jsonl"),
                                    "--lock", os.path.join(d, "l.lock")])

    def add(self, pid, text, layers, status="done", terms=("mev",), cit=0):
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?)",
                  (pid, text[0], text[1], layers, status, 5, json.dumps(list(terms)), cit, "old"))
        c.commit()
        c.close()
        f = sqlite3.connect(self.pfts)
        f.execute("INSERT INTO papers_fts (arxiv_id, title, abstract, layers, year) VALUES (?,?,?,?,2024)",
                  (pid, text[0], text[1], layers))
        f.commit()
        f.close()

    def row(self, pid):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM papers WHERE arxiv_id=?", (pid,)).fetchone()
        c.close()
        return r

    def fts_ids(self):
        f = sqlite3.connect(self.pfts)
        out = [r[0] for r in f.execute("SELECT arxiv_id FROM papers_fts")]
        f.close()
        return out

    def run_purge(self, apply=True, conn=None):
        self.args.apply = apply
        q = FakeQdrant()
        c = conn or pfm.connect(self.db)
        try:
            pfm.run(c, q, self.args, log=lambda *a, **k: None)
        finally:
            c.close()
        return q


class Tests(Env):
    def test_protected_and_kept_untouched(self):
        for pid in ("eip:1", "simd:2", "wp:3", "gh:x:1", "iacr:2023/1255"):
            self.add(pid, PHYS, "web3")
        self.add("2401.00001", PHYS, "web3", cit=500)
        q = self.run_purge()
        for pid in ("eip:1", "simd:2", "wp:3", "gh:x:1", "iacr:2023/1255"):
            self.assertEqual(self.row(pid)["status"], "done")
        self.assertEqual(self.row("2401.00001")["status"], "off_niche")
        self.assertTrue(all(c[1]["filter"]["must"][0]["match"]["value"] == "2401.00001" for c in q.calls))

    def test_keep_flag(self):
        self.add("2401.00001", PHYS, "web3")
        self.args.keep = ["2401.00001"]
        self.run_purge()
        self.assertEqual(self.row("2401.00001")["status"], "done")

    def test_empty_layers_delete(self):
        self.add("2401.00001", PHYS, "web3")
        self.add("2401.00002", REAL, "web3")
        q = self.run_purge()
        r = self.row("2401.00001")
        self.assertEqual((r["status"], r["layers"]), ("off_niche", ""))
        self.assertEqual(json.loads(r["matched_terms"]), [])
        self.assertEqual(self.fts_ids(), ["2401.00002"])
        self.assertEqual(self.row("2401.00002")["status"], "done")
        self.assertEqual([c[0] for c in q.calls],
                         ["%s/points/delete" % c for c in pfm.COLLECTIONS])

    def test_partial_trim(self):
        self.add("2401.00003", MIXED, "web3,llm-slm,ai-agents", terms=("stark", "large language model"))
        q = self.run_purge()
        r = self.row("2401.00003")
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["layers"], "ai-agents,llm-slm")
        self.assertEqual(len(q.calls), 3)
        self.assertTrue(all(c[0].endswith("/points/payload") and c[1]["payload"] == {"layers": ["ai-agents", "llm-slm"]}
                            for c in q.calls))
        f = sqlite3.connect(self.pfts)
        self.assertEqual(f.execute("SELECT layers FROM papers_fts").fetchone()[0], "ai-agents,llm-slm")
        f.close()

    def test_dry_run_changes_nothing(self):
        self.add("2401.00001", PHYS, "web3")
        self.add("2401.00003", MIXED, "web3,llm-slm,ai-agents", terms=("stark",))
        before = (self.row("2401.00001")[:], self.row("2401.00003")[:], self.fts_ids())
        q = self.run_purge(apply=False)
        self.assertEqual(q.calls, [])
        self.assertEqual((self.row("2401.00001")[:], self.row("2401.00003")[:], self.fts_ids()), before)
        self.assertFalse(os.path.exists(self.args.progress))

    def test_commit_per_paper(self):
        for i in range(3):
            self.add(f"2401.0000{i}", PHYS, "web3")
        conn = pfm.connect(self.db)
        seen = []
        orig = pfm.apply_one

        def spy(c, *a, **k):
            r = orig(c, *a, **k)
            other = sqlite3.connect(self.db)
            seen.append(other.execute("SELECT COUNT(*) FROM papers WHERE status='off_niche'").fetchone()[0])
            other.close()
            return r
        pfm.apply_one = spy
        try:
            self.run_purge(conn=conn)
        finally:
            pfm.apply_one = orig
        self.assertEqual(seen, [1, 2, 3])

    def test_resume_skips_done_and_status_guard(self):
        self.add("2401.00001", PHYS, "web3")
        self.add("2401.00002", PHYS, "web3")
        self.run_purge()
        q = self.run_purge()
        self.assertEqual(q.calls, [])
        self.assertEqual(len(pfm.load_progress(self.args.progress)), 2)

    def test_lowercase_only_not_candidate(self):
        self.add("2401.00009", REAL, "web3", terms=("ethereum",))
        c = pfm.connect(self.db)
        try:
            self.assertEqual(pfm.find_candidates(c), [])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
