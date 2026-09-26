import pathlib
import sqlite3
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Same test-only sys.path / fcntl fixup as test_oai_harvest.py: pipeline/service.py
# imports its sibling modules as bare top-level names, so pipeline/ itself must be
# on sys.path, and fcntl (POSIX-only, used only in service.main()) needs a stub to
# import on Windows dev boxes.
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

# coarse_index.py does a bare `import service` (same convention service.py itself
# uses for extractor/niche_filter -- it only ever runs as a script inside pipeline/,
# where that resolves to this same module). Registering it under the bare name
# first means that import reuses this exact object instead of re-executing
# service.py into a second, distinct module -- otherwise patch.object(service, ...)
# in these tests would silently patch a module coarse_index never sees.
sys.modules.setdefault("service", service)

from pipeline import coarse_index  # noqa: E402


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    service.init_db(conn)
    return conn


def insert_paper(conn, arxiv_id, title="", abstract="", layers="llm-slm", year=2024,
                  niche_score=5, citation_count=None, influential=None, status="done"):
    conn.execute(
        "INSERT INTO papers (arxiv_id, title, abstract, layers, year, niche_score, "
        "citation_count, influential, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (arxiv_id, title, abstract, layers, year, niche_score, citation_count, influential, status),
    )
    conn.commit()


LONG_ABSTRACT = (
    "We study large language models. This sentence pads the text out further. "
    "A third sentence keeps going with more filler words here and there. "
) * 20  # comfortably over ABSTRACT_CHAR_LIMIT (1500)


class SentenceTruncateTests(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(coarse_index.sentence_truncate("short text."), "short text.")

    def test_cuts_at_sentence_boundary(self):
        out = coarse_index.sentence_truncate(LONG_ABSTRACT, limit=200)
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("."))

    def test_falls_back_to_word_boundary_when_no_sentence_break(self):
        text = "word " * 500  # no periods at all
        out = coarse_index.sentence_truncate(text, limit=100)
        self.assertLessEqual(len(out), 100)
        self.assertNotIn(" \n", out)
        self.assertFalse(out.endswith(" "))


class BuildEmbedTextTests(unittest.TestCase):
    def test_uses_title_plus_abstract_when_abstract_is_long_enough(self):
        text = coarse_index.build_embed_text(
            "My Paper",
            "This abstract is long enough to clear the eighty character minimum comfortably and then some.",
            "2401.00001",
        )
        self.assertTrue(text.startswith("My Paper. "))
        self.assertIn("long enough", text)

    def test_falls_back_to_chunk_when_abstract_too_short(self):
        session = MagicMock()
        with patch.object(coarse_index, "fetch_fallback_chunk_text", return_value="Fallback chunk prose text.") as m:
            text = coarse_index.build_embed_text("My Paper", "too short", "2401.00002", session=session)
        m.assert_called_once_with(session, "2401.00002")
        self.assertEqual(text, "My Paper. Fallback chunk prose text.")

    def test_title_only_when_no_abstract_and_no_fallback_chunk(self):
        session = MagicMock()
        with patch.object(coarse_index, "fetch_fallback_chunk_text", return_value=None):
            text = coarse_index.build_embed_text("Just A Title", "", "2401.00003", session=session)
        self.assertEqual(text, "Just A Title")

    def test_no_session_skips_fallback_lookup_entirely(self):
        with patch.object(coarse_index, "fetch_fallback_chunk_text") as m:
            text = coarse_index.build_embed_text("Title Only", "", "2401.00004", session=None)
        m.assert_not_called()
        self.assertEqual(text, "Title Only")


class FetchFallbackChunkTests(unittest.TestCase):
    def test_skips_related_work_appendix_conclusion_and_non_prose(self):
        session = MagicMock()
        session.post.return_value.status_code = 200
        session.post.return_value.raise_for_status = lambda: None
        session.post.return_value.json.return_value = {
            "result": {
                "points": [
                    {"payload": {"text": "related work text", "section_type": "related_work",
                                 "element_type": "prose", "chunk_index": 0}},
                    {"payload": {"text": "a table", "section_type": "method",
                                 "element_type": "table", "chunk_index": 1}},
                    {"payload": {"text": "the real method prose", "section_type": "method",
                                 "element_type": "prose", "chunk_index": 2}},
                ]
            }
        }
        out = coarse_index.fetch_fallback_chunk_text(session, "2401.00005")
        self.assertEqual(out, "the real method prose")

    def test_returns_none_on_request_error(self):
        import requests
        session = MagicMock()
        session.post.side_effect = requests.RequestException("boom")
        self.assertIsNone(coarse_index.fetch_fallback_chunk_text(session, "2401.00006"))


class PointIdAndSourceTests(unittest.TestCase):
    def test_point_id_is_deterministic(self):
        a = coarse_index.coarse_point_id("2401.00007")
        b = coarse_index.coarse_point_id("2401.00007")
        self.assertEqual(a, b)

    def test_point_id_differs_from_chunk_point_id(self):
        coarse_id = coarse_index.coarse_point_id("2401.00007")
        chunk_id = str(service.uuid.uuid5(service.NAMESPACE_URL, "2401.00007#0"))
        self.assertNotEqual(coarse_id, chunk_id)

    def test_source_for_prefixes(self):
        self.assertEqual(coarse_index.source_for("iacr:2024/123"), "iacr")
        self.assertEqual(coarse_index.source_for("eip:1559"), "eip")
        self.assertEqual(coarse_index.source_for("2401.00007"), "arxiv")


class InCitationsTests(unittest.TestCase):
    """in_citations = in-corpus in-degree from state.db's citations table."""

    def setUp(self):
        self.conn = make_conn()
        for aid in ("A", "B", "C", "D"):
            insert_paper(self.conn, aid, title=aid)
        # A is cited by B, C, D (in-degree 3); B is cited by C (in-degree 1);
        # D has no incoming citations.
        self.conn.executemany(
            "INSERT INTO citations (src, dst) VALUES (?,?)",
            [("B", "A"), ("C", "A"), ("D", "A"), ("C", "B")],
        )
        self.conn.commit()

    def test_fetch_all_group_by(self):
        counts = coarse_index.fetch_in_citations_all(self.conn)
        self.assertEqual(counts.get("A"), 3)
        self.assertEqual(counts.get("B"), 1)
        self.assertNotIn("D", counts)  # never a dst, absent rather than 0

    def test_fetch_for_scoped_batch(self):
        counts = coarse_index.fetch_in_citations_for(self.conn, ["A", "D"])
        self.assertEqual(counts.get("A"), 3)
        self.assertNotIn("D", counts)

    def test_fetch_for_empty_list(self):
        self.assertEqual(coarse_index.fetch_in_citations_for(self.conn, []), {})

    def test_build_payload_carries_in_citations_citation_count_influential(self):
        insert_paper(self.conn, "E", title="Paper E", citation_count=42, influential=7)
        row = self.conn.execute(
            f"SELECT {coarse_index.DONE_ROW_COLUMNS} FROM papers WHERE arxiv_id='E'"
        ).fetchone()
        payload = coarse_index.build_payload(row, in_citations=9)
        self.assertEqual(payload["in_citations"], 9)
        self.assertEqual(payload["citation_count"], 42)
        self.assertEqual(payload["influential"], 7)

    def test_build_payload_defaults_missing_citation_fields_to_none_or_zero(self):
        row = self.conn.execute(
            f"SELECT {coarse_index.DONE_ROW_COLUMNS} FROM papers WHERE arxiv_id='D'"
        ).fetchone()
        payload = coarse_index.build_payload(row)  # in_citations omitted -> 0
        self.assertEqual(payload["in_citations"], 0)
        self.assertIsNone(payload["citation_count"])
        self.assertIsNone(payload["influential"])


class BackfillSkipsAlreadyIndexedTests(unittest.TestCase):
    def test_skips_papers_already_in_papers_coarse(self):
        conn = make_conn()
        insert_paper(conn, "2401.00010", title="Already indexed",
                     abstract="Has a long enough abstract for the pipeline to embed directly without any fallback.")
        insert_paper(conn, "2401.00011", title="Needs indexing",
                     abstract="Also has a long enough abstract for the pipeline to embed directly without fallback.")

        with patch.object(coarse_index, "ensure_coarse_collection"), \
             patch.object(coarse_index, "get_indexed_arxiv_ids", return_value={"2401.00010"}), \
             patch.object(service, "embed_texts_batch", return_value=[[0.0] * 384]) as embed_mock, \
             patch.object(coarse_index, "upsert_batch") as upsert_mock:
            done = coarse_index.backfill(conn, limit=None, dry_run=False)

        self.assertEqual(done, 1)
        embed_mock.assert_called_once()
        (_, texts), _ = embed_mock.call_args
        self.assertEqual(len(texts), 1)
        upsert_mock.assert_called_once()
        points = upsert_mock.call_args[0][1]
        self.assertEqual(points[0]["payload"]["arxiv_id"], "2401.00011")

    def test_dry_run_does_not_embed_or_upsert(self):
        conn = make_conn()
        insert_paper(conn, "2401.00012", title="X", abstract="Y" * 100)
        with patch.object(coarse_index, "ensure_coarse_collection"), \
             patch.object(coarse_index, "get_indexed_arxiv_ids", return_value=set()), \
             patch.object(service, "embed_texts_batch") as embed_mock, \
             patch.object(coarse_index, "upsert_batch") as upsert_mock:
            total = coarse_index.backfill(conn, dry_run=True)
        self.assertEqual(total, 1)
        embed_mock.assert_not_called()
        upsert_mock.assert_not_called()


class ServiceHookTests(unittest.TestCase):
    """service._sync_coarse_index: the embed pipeline must never break because
    of the coarse index, and must stay a no-op unless explicitly enabled."""

    def test_noop_when_disabled(self):
        with patch.object(service, "COARSE_INDEX_ENABLED", False), \
             patch("builtins.__import__") as import_mock:
            service._sync_coarse_index(MagicMock(), ["2401.00013"])
        # coarse_index module must never even be imported when the flag is off
        import_mock.assert_not_called()

    def test_noop_for_empty_id_list(self):
        with patch.object(service, "COARSE_INDEX_ENABLED", True):
            # would raise if it tried to do real work; empty list must short-circuit
            service._sync_coarse_index(MagicMock(), [])

    def test_exception_in_coarse_index_is_swallowed_as_warning(self):
        fake_coarse_index = types.ModuleType("coarse_index")
        fake_coarse_index.upsert_papers = MagicMock(side_effect=RuntimeError("qdrant down"))
        with patch.object(service, "COARSE_INDEX_ENABLED", True), \
             patch.dict(sys.modules, {"coarse_index": fake_coarse_index}), \
             patch.object(service.log, "warning") as warn_mock:
            service._sync_coarse_index(MagicMock(), ["2401.00014"])  # must not raise
        warn_mock.assert_called_once()
        fake_coarse_index.upsert_papers.assert_called_once()

    def test_calls_coarse_index_upsert_papers_when_enabled(self):
        fake_coarse_index = types.ModuleType("coarse_index")
        fake_coarse_index.upsert_papers = MagicMock(return_value=1)
        conn = MagicMock()
        with patch.object(service, "COARSE_INDEX_ENABLED", True), \
             patch.dict(sys.modules, {"coarse_index": fake_coarse_index}):
            service._sync_coarse_index(conn, ["2401.00015"])
        fake_coarse_index.upsert_papers.assert_called_once_with(conn, ["2401.00015"])


if __name__ == "__main__":
    unittest.main()
