import os
import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

# Same import scaffolding as test_spec_niche.py: pipeline/ on sys.path and a
# no-op fcntl stub on platforms without it.
_PIPELINE_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "pipeline")
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        stub = types.ModuleType("fcntl")
        stub.flock = lambda *a, **kw: None
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        sys.modules["fcntl"] = stub

from pipeline import service  # noqa: E402

_OPS_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "ops")
if _OPS_DIR not in sys.path:
    sys.path.insert(0, _OPS_DIR)
import refresh_moved_eips as rme  # noqa: E402

STUB = ("---\neip: 4337\ncategory: ERC\nstatus: Moved\n---\n\n"
        "This file was moved to https://github.com/ethereum/ercs/blob/master/ERCS/erc-4337.md\n")
ERC = ("---\neip: 4337\ntitle: Account Abstraction Using Alt Mempool\n"
       "description: An account abstraction proposal without consensus-layer changes\n"
       "status: Draft\ncreated: 2021-09-29\ncategory: ERC\n---\n\n## Abstract\n"
       + "A UserOperation is sent to an alternative mempool. " * 30)


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    service.init_db(conn)
    return conn


def fake_response(text, status=200):
    r = MagicMock()
    r.status_code = status
    r.content = text.encode()
    return r


def read_file(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class MovedStubTests(unittest.TestCase):
    def test_stub_is_recognised(self):
        self.assertTrue(service.is_moved_stub(STUB))
        self.assertTrue(rme.is_moved_stub(STUB))

    def test_real_texts_are_not_stubs(self):
        self.assertFalse(service.is_moved_stub(ERC))
        self.assertFalse(rme.is_moved_stub(ERC))
        self.assertFalse(service.is_moved_stub(""))
        self.assertFalse(service.is_moved_stub(None))

    def test_long_text_mentioning_the_move_is_not_a_stub(self):
        text = ("---\nstatus: Final\n---\n" + "x" * 900
                + "\nThis file was moved to https://github.com/ethereum/ercs/x")
        self.assertFalse(service.is_moved_stub(text))


class ErcSourceTests(unittest.TestCase):
    def test_new_cursor_key_and_config(self):
        cfg = service.SPEC_SOURCES["erc@all"]
        self.assertIn("eip@all", service.SPEC_SOURCES)
        self.assertEqual(cfg["prefix"], "eip:")
        self.assertTrue(cfg["list_url"].endswith("/repos/ethereum/ERCs/contents/ERCS"))
        self.assertTrue(cfg["raw_base"].endswith("/ethereum/ERCs/master/ERCS/"))
        self.assertEqual(cfg["file_re"].match("erc-4337.md").group(1), "4337")
        self.assertIsNone(cfg["file_re"].match("eip-1.md"))
        self.assertIn("erc@all", [qk for _, qk in service.ALL_QUERY_KEYS])

    def _fetch(self, key, files):
        def fake_urlopen(req, timeout=None):
            resp = MagicMock()
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda *a: False
            resp.read.return_value = files[req.full_url.rsplit("/", 1)[1]].encode()
            return resp
        with patch.object(service, "spec_list_files", return_value=sorted(files)), \
                patch.object(service.urllib.request, "urlopen", fake_urlopen), \
                patch.object(service.time, "sleep"):
            return service.fetch_spec_batch(key, 0, 40)

    def test_erc_entry_keeps_eip_id_and_eips_url(self):
        entries, total = self._fetch("erc@all", {"erc-4337.md": ERC})
        self.assertEqual(total, 1)
        e = entries[0]
        self.assertEqual(e["arxiv_id"], "eip:4337")
        self.assertEqual(e["url"], "https://eips.ethereum.org/EIPS/eip-4337")
        self.assertEqual(e["year"], 2021)
        self.assertEqual(e["title"], "Account Abstraction Using Alt Mempool [Draft]")
        self.assertTrue(e["abstract"].startswith("Status: Draft. "))
        self.assertEqual(e["full_text"], ERC)

    def test_eip_source_drops_stubs_but_keeps_real_eips(self):
        real = "---\ntitle: Fee market\nstatus: Final\ncreated: 2019-04-13\n---\n\n## Abstract\nx\n"
        entries, total = self._fetch("eip@all", {"eip-1559.md": real, "eip-4337.md": STUB})
        self.assertEqual(total, 2)
        self.assertEqual([e["arxiv_id"] for e in entries], ["eip:1559"])

    def test_withdrawn_erc_is_skipped(self):
        entries, _ = self._fetch("erc@all", {"erc-1.md": ERC.replace("status: Draft", "status: Withdrawn")})
        self.assertEqual(entries, [])

    def test_stagnant_and_review_are_kept_with_status_mark(self):
        for status, label in (("Stagnant", "Stagnant"), ("Review", "Review"), ("Last Call", "Last Call")):
            text = ERC.replace("status: Draft", f"status: {status}")
            entries, _ = self._fetch("eip@all", {"eip-7547.md": text})
            self.assertEqual(len(entries), 1, status)
            self.assertTrue(entries[0]["title"].endswith(f"[{label}]"), status)

    def test_final_eip_title_is_not_marked(self):
        real = "---" + chr(10) + "title: Fee market" + chr(10) + "status: Final" + chr(10) + "created: 2019-04-13" + chr(10) + "---" + chr(10) + chr(10) + "## Abstract" + chr(10) + "x" + chr(10)
        entries, _ = self._fetch("eip@all", {"eip-1559.md": real})
        self.assertEqual(entries[0]["title"], "Fee market")
        self.assertEqual(service.SPEC_SKIP_STATUSES, {"withdrawn"})

    def test_missing_title_falls_back_to_erc_label(self):
        entries, _ = self._fetch("erc@all", {"erc-9.md": "---\nstatus: Final\n---\n\nbody\n"})
        self.assertEqual(entries[0]["title"], "ERC-9")


class CacheNeverDowngradedTests(unittest.TestCase):
    def test_erc_text_beats_stub_in_either_order(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(service, "LATEX_CACHE_DIR", pathlib.Path(d)):
                self.assertTrue(service.write_spec_cache("eip:4337", STUB))
                self.assertTrue(service.write_spec_cache("eip:4337", ERC))
                self.assertFalse(service.write_spec_cache("eip:4337", STUB))
                self.assertEqual(service.read_latex_cache("eip:4337"), ERC)

    def test_new_erc_is_admitted_as_web3(self):
        conn = make_conn()
        service.upsert_discovered(conn, "eip:7683", "Cross Chain Intents", 2024, "web3",
                                  abstract="Standard for cross chain order settlement.")
        row = conn.execute("SELECT status, layers FROM papers WHERE arxiv_id='eip:7683'").fetchone()
        self.assertEqual(row["status"], "discovered")
        self.assertIn("web3", row["layers"].split(","))


class RefreshScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        for sub in ("fulltext_cache", "chunk_cache", "latex_cache"):
            os.makedirs(os.path.join(self.d, sub))
        fts = sqlite3.connect(os.path.join(self.d, "no.db"))
        fts.execute("CREATE VIRTUAL TABLE chunks USING fts5(text, point_id UNINDEXED, arxiv_id UNINDEXED)")
        fts.execute("INSERT INTO chunks VALUES ('published indexed body','old-point','eip:4337')")
        fts.commit()
        fts.close()
        self.conn = make_conn()
        self._add("eip:4337", "EIP-4337", STUB)
        self._add("eip:1559", "Fee market", "---\nstatus: Final\n---\n" + "real " * 200)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _add(self, aid, title, text):
        self.conn.execute("INSERT INTO papers (arxiv_id, title, layers, status, fulltext_source) "
                          "VALUES (?,?,'web3','done','spec-markdown')", (aid, title))
        sid = aid.replace(":", "_")
        with open(os.path.join(self.d, "fulltext_cache", sid + ".tex"), "w") as f:
            f.write(text)
        with open(os.path.join(self.d, "chunk_cache", sid + ".json"), "w") as f:
            f.write("[]")
        self.conn.commit()

    def _args(self, apply):
        return rme.parse_args(["--data-dir", self.d, "--fts", os.path.join(self.d, "no.db"),
                               "--progress", os.path.join(self.d, "p.jsonl"), "--pause", "0",
                               "--db", ":memory:"] + (["--apply"] if apply else []))

    def _session(self, erc_text=ERC, status=200):
        s = MagicMock()
        s.get.return_value = fake_response(erc_text, status)
        return s

    def test_only_stubs_are_candidates(self):
        self.assertEqual([c["arxiv_id"] for c in rme.find_candidates(self.conn, self.d)], ["eip:4337"])

    def test_candidate_found_even_after_latex_cache_was_overwritten(self):
        source_path = os.path.join(self.d, "latex_cache", "eip_4337", "source.tex")
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        with open(source_path,
                  "w", encoding="utf-8") as f:
            f.write(ERC)  # erc@all harvest got there first
        self.assertEqual([c["arxiv_id"] for c in rme.find_candidates(self.conn, self.d)], ["eip:4337"])

    def test_usable_rules(self):
        self.assertTrue(rme.erc_usable(ERC)[0])
        self.assertFalse(rme.erc_usable(STUB)[0])
        self.assertFalse(rme.erc_usable(None)[0])
        self.assertTrue(rme.erc_usable(ERC.replace("status: Draft", "status: Stagnant"))[0])
        self.assertFalse(rme.erc_usable(ERC.replace("status: Draft", "status: Withdrawn"))[0])

    def test_dry_run_writes_nothing(self):
        s = self._session()
        rme.run(self.conn, s, self._args(False), log=lambda *a: None)
        s.post.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT status FROM papers WHERE arxiv_id='eip:4337'").fetchone()[0], "done")
        self.assertTrue(os.path.exists(os.path.join(self.d, "fulltext_cache", "eip_4337.tex")))
        self.assertFalse(os.path.exists(os.path.join(self.d, "p.jsonl")))

    def test_apply_stages_exact_erc_and_preserves_published_state(self):
        s = self._session()
        row_before = tuple(self.conn.execute("SELECT * FROM papers WHERE arxiv_id='eip:4337'").fetchone())
        totals_before = tuple(self.conn.execute("SELECT * FROM completion_totals").fetchone())
        history_before = [tuple(row) for row in self.conn.execute(
            "SELECT * FROM completion_history ORDER BY arxiv_id").fetchall()]
        fulltext_before = read_file(os.path.join(self.d, "fulltext_cache", "eip_4337.tex"))
        chunk_before = read_file(os.path.join(self.d, "chunk_cache", "eip_4337.json"))
        fts = sqlite3.connect(os.path.join(self.d, "no.db"))
        fts_before = fts.execute("SELECT text,point_id,arxiv_id FROM chunks ORDER BY rowid").fetchall()
        fts.close()
        self.assertEqual(rme.run(self.conn, s, self._args(True), log=lambda *a: None), 1)
        self.assertEqual(tuple(self.conn.execute(
            "SELECT * FROM papers WHERE arxiv_id='eip:4337'").fetchone()), row_before)
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM completion_totals").fetchone()), totals_before)
        self.assertEqual([tuple(row) for row in self.conn.execute(
            "SELECT * FROM completion_history ORDER BY arxiv_id").fetchall()], history_before)
        self.assertEqual(read_file(os.path.join(self.d, "fulltext_cache", "eip_4337.tex")), fulltext_before)
        self.assertEqual(read_file(os.path.join(self.d, "chunk_cache", "eip_4337.json")), chunk_before)
        self.assertEqual(read_file(os.path.join(self.d, "fulltext_cache", "eip_1559.tex")),
                         "---\nstatus: Final\n---\n" + "real " * 200)
        fts = sqlite3.connect(os.path.join(self.d, "no.db"))
        self.assertEqual(fts.execute("SELECT text,point_id,arxiv_id FROM chunks ORDER BY rowid").fetchall(),
                         fts_before)
        fts.close()
        artifact = self.conn.execute(
            "SELECT revision,artifact_path,cache_sha256,state FROM paper_publications "
            "WHERE arxiv_id='eip:4337'").fetchone()
        metadata = {**rme.erc_fields(ERC), "source": "erc@all"}
        source_bytes = ERC.encode("utf-8")
        revision = hashlib.sha256(
            source_bytes + b"\0" + json.dumps(metadata, sort_keys=True, separators=(",", ":"),
                                                 ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        self.assertEqual((artifact[0], artifact[2], artifact[3]),
                         (revision, hashlib.sha256(source_bytes).hexdigest(), "staged"))
        with open(artifact[1], encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"source": ERC, "metadata": metadata})
        s.post.assert_not_called()
        finished, pending = rme.load_state(os.path.join(self.d, "p.jsonl"))
        self.assertEqual(finished, set())
        self.assertEqual(pending, {"eip:4337"})

    def test_unfetchable_erc_is_left_alone(self):
        s = self._session("", 404)
        self.assertEqual(rme.run(self.conn, s, self._args(True), log=lambda *a: None), 0)
        self.assertEqual(self.conn.execute("SELECT status FROM papers WHERE arxiv_id='eip:4337'").fetchone()[0], "done")
        s.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
