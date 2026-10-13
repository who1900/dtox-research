import json
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from ops import apply_missing_prose_repair as engine
from ops import plan_missing_prose_repair as planner
from ops import repair_prose_corpus as runner
from ops.tests import test_apply_missing_prose_repair as fixtures
from ops.tests import test_plan_missing_prose_repair as plans
from ops.tests import test_repair_prose_corpus as corpus
from pipeline import document_cache as dc


class SectionRepairSafetyTests(unittest.TestCase):
    def completed(self):
        fixture = fixtures.AdditiveRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.record_completed_fixture()
        return fixture

    def test_bounded_protected_change_or_omission_cannot_enter_manifest(self):
        source = plans.source_for(("Method", plans.prose("body")))
        old = [plans.chunk("Method", "    protected A\n", element_type="code")]
        plan_fixture = plans.MissingProseRepairPlanTests()
        plan_fixture.setUp()
        section = planner._source_sections(source)[0][0]
        for protected in ([plans.chunk("Method", "protected B", element_type="code")], []):
            bounded = {section["bounded_source"]: [plans.chunk("Method", plans.prose("body"))] + protected}
            with self.subTest(protected=protected):
                result = plan_fixture.plan(source, old, bounded=bounded, full=old)
                self.assertEqual(result["status"], "blocked")
                self.assertIsNone(result["section_manifest"])
                self.assertTrue(any("bounded protected sequence" in s for s in result["blockers"]))

    def test_planner_manifest_uses_unsplit_sink_not_appended_cache(self):
        before, middle, after = (plans.prose(label) for label in ("before", "middle", "after"))
        source = plans.source_for(("Method", before + " " + middle + " " + after))
        old = [plans.chunk("Method", before + " " + after)]
        sink_text = "    canonical before\ncanonical middle\ncanonical after\n"

        def parse(text, section_sink=None):
            if section_sink is not None:
                section_sink.append({"section_title": "Method", "section_type": "other", "text": sink_text})
                return [plans.chunk("Method", before + " " + middle + " " + after)], "pylatexenc"
            return [], "pylatexenc"

        with patch.object(planner.extractor, "build_chunks", side_effect=parse) as parsed:
            result = planner.build_plan("fixture", source, old)
        self.assertEqual(result["status"], "dry_run_only")
        self.assertEqual(parsed.call_count, 2)
        self.assertEqual(result["section_manifest"]["sections"][0]["text"], sink_text)
        self.assertEqual(result["section_manifest"]["coverage"], "examined_sections")
        self.assertEqual(result["additions"][0]["chunk_index"], 1)
        self.assertEqual(old[0]["text"], before + " " + after)

    def test_v2_receipt_does_not_authorize_publication_with_unknown_old_fts(self):
        fixture = self.completed()
        journal = engine._load_journal(fixture.journal_path)
        journal["version"] = 2
        journal["section_manifest"] = dc.make_section_manifest(
            journal["paper_id"], [{"section_title": "Method", "section_type": "method", "text": "canonical"}],
            fixture.cache_path.read_bytes(), fixture.source, "pylatexenc", "rev")
        journal["section_manifest_before_sha256"] = None
        journal["digest"] = engine._immutable_digest(journal)
        engine._atomic_json(fixture.journal_path, journal)
        with closing(sqlite3.connect(fixture.fts_path)) as conn, conn:
            conn.execute("UPDATE dtox_prose_repair_receipts SET digest=?", (journal["digest"],))
        before = fixture.journal_path.read_bytes(), fixture.cache_path.read_bytes()
        with patch.object(engine, "_fts_preflight", side_effect=AssertionError("no FTS scan")), \
                patch.object(fixture.backend, "scroll", side_effect=AssertionError("no network")):
            with self.assertRaises(engine.RepairBlocked) as caught:
                fixture.apply("resume")
        self.assertEqual(caught.exception.code, "fts_identity_unknown")
        self.assertEqual((fixture.journal_path.read_bytes(), fixture.cache_path.read_bytes()), before)
        self.assertFalse((fixture.root / "section_cache").exists())

    def test_unknown_identity_has_no_fts_scan(self):
        fixture = fixtures.AdditiveRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        journal = engine._load_journal(fixture.journal_path)
        statements = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with patch.object(engine.sqlite3, "connect", side_effect=traced):
            with self.assertRaises(engine.RepairBlocked) as caught:
                with engine._fts_preflight(fixture.fts_path, journal):
                    self.fail("unknown identity was accepted")
        self.assertEqual(caught.exception.code, "fts_identity_unknown")
        sql = " ".join(statements).lower()
        self.assertNotIn("count(", sql)
        self.assertNotIn(" join ", sql)
        self.assertNotIn(" match ", sql)
        self.assertNotIn("from chunks", sql)

    def test_done_v1_scoped_validation_does_not_prove_old_fts_rows(self):
        fixture = self.completed()
        with closing(sqlite3.connect(fixture.fts_path)) as conn, conn:
            conn.execute("UPDATE chunks SET text='changed same-paper legacy row' WHERE point_id='legacy-id'")
        result = fixture.apply("resume")
        self.assertFalse(result["legacy_fts_baseline_verified"])
        self.assertEqual(result["verification_scope"], "recorded_cache_qdrant_and_new_fts_receipt")
        self.assertTrue(result["read_only"])
        self.assertFalse((fixture.root / "section_cache").exists())

    def test_unknown_sqlite_code_and_english_timeout_message_are_not_retryable(self):
        fixture = corpus.CorpusRunnerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.paper("a")
        fixture.run_corpus()
        with patch.object(fixture.engine, "apply_or_resume", side_effect=sqlite3.OperationalError("interrupted")):
            fixture.run_corpus("apply")
        self.assertEqual(fixture.rows()["a"]["state"], "manual_attention")
        fixture.engine.failure = corpus.FakeRepairBlocked("time budget timeout interrupted")
        fixture.run_corpus("apply")
        self.assertEqual(fixture.rows()["a"]["state"], "manual_attention")

    def test_missing_inputs_count_as_uninspected_without_changing_paper_status(self):
        fixture = corpus.CorpusRunnerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.paper("missing-source", source=False)
        fixture.paper("missing-cache", cache=False)
        fixture.paper("oa:W1")
        fixture.paper("outside", kind="pdf")
        with patch("builtins.print"):
            result = fixture.run_corpus("worker")
        self.assertEqual(result["reason"], "frontier_drained_with_unresolved")
        self.assertEqual(result["uninspected_count"], 3)
        self.assertEqual(result["unresolved_count"], 3)
        self.assertEqual(result["safe_outside_scope_count"], 1)
        with closing(sqlite3.connect(fixture.root / "state.db")) as state:
            self.assertEqual({r[0] for r in state.execute("SELECT status FROM papers")}, {"done"})
