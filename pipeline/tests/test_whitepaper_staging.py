import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline.whitepapers import apply_source


TEXT = "A validated whitepaper passage. " * 100


class WhitepaperStagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(
            "CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,title TEXT,year INTEGER,layers TEXT,status TEXT,"
            "passed INTEGER,abstract TEXT,fulltext_source TEXT,updated_at TEXT,niche_score REAL);"
            "CREATE TABLE completion_history(arxiv_id TEXT PRIMARY KEY,first_known INTEGER,completed INTEGER);"
        )
        self.fulltext = self.root / "fulltext_cache" / "wp-fixture.tex"
        self.secondary = self.root / "latex_cache" / "wp-fixture" / "source.tex"
        self.conn.commit()

    def apply(self, paper="wp:fixture", source=TEXT, dry_run=False):
        return apply_source(self.conn, self.root, self.fulltext, paper, "Fixture", 2026,
                            "https://example.test/source", source, dry_run=dry_run,
                            secondary_cache_path=self.secondary)

    def seed(self, status):
        self.conn.execute("INSERT INTO papers(arxiv_id,title,year,layers,status,fulltext_source) "
                          "VALUES ('wp:fixture','Original',2020,'old',?, 'old-source')", (status,))
        self.conn.execute("INSERT INTO completion_history VALUES ('wp:fixture',1,0)")
        self.conn.commit()

    def test_done_paper_only_stages_immutable_source(self):
        self.seed("done")
        self.fulltext.parent.mkdir()
        self.fulltext.write_text("old fulltext", encoding="utf-8")
        self.secondary.parent.mkdir(parents=True)
        self.secondary.write_text("old latex", encoding="utf-8")
        before = tuple(self.conn.execute("SELECT title,year,layers,status,fulltext_source FROM papers").fetchone())
        result = self.apply()
        self.assertEqual(result["status"], "staged")
        self.assertEqual(tuple(self.conn.execute("SELECT title,year,layers,status,fulltext_source FROM papers").fetchone()), before)
        self.assertEqual(self.fulltext.read_text(encoding="utf-8"), "old fulltext")
        self.assertEqual(self.secondary.read_text(encoding="utf-8"), "old latex")
        self.assertEqual(tuple(self.conn.execute("SELECT kind,state FROM paper_publications").fetchone()), ("source", "staged"))

    def test_existing_not_done_is_busy_and_does_not_overwrite(self):
        self.seed("fulltext_fetched")
        self.fulltext.parent.mkdir()
        self.fulltext.write_text("old", encoding="utf-8")
        before = tuple(self.conn.execute("SELECT title,status FROM papers").fetchone())
        self.assertEqual(self.apply()["status"], "busy")
        self.assertEqual(tuple(self.conn.execute("SELECT title,status FROM papers").fetchone()), before)
        self.assertEqual(self.fulltext.read_text(encoding="utf-8"), "old")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='paper_publications'").fetchone())

    def test_new_source_and_first_known_state_commit_together(self):
        result = self.apply()
        self.assertEqual(result["status"], "acquired")
        self.assertTrue(result["first_acquisition"])
        self.assertEqual(tuple(self.conn.execute("SELECT status,fulltext_source FROM papers").fetchone()),
                         ("fulltext_fetched", "whitepaper-pdf"))
        self.assertEqual(tuple(self.conn.execute("SELECT first_known,completed FROM completion_history").fetchone()), (1, 0))
        self.assertEqual(self.fulltext.read_text(encoding="utf-8"), TEXT)
        self.assertEqual(self.secondary.read_text(encoding="utf-8"), TEXT)

    def test_dry_run_performs_no_database_or_cache_writes(self):
        result = self.apply(dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        self.assertFalse(self.fulltext.exists())
        self.assertFalse(self.secondary.exists())

    def test_new_acquisition_rollback_removes_caches_and_is_not_published(self):
        self.conn.execute("CREATE TRIGGER reject_history BEFORE INSERT ON completion_history "
                          "BEGIN SELECT RAISE(ABORT,'blocked fixture'); END")
        self.conn.commit()
        result = self.apply()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["published"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        self.assertFalse(self.fulltext.exists())
        self.assertFalse(self.secondary.exists())

    def test_blocked_done_source_staging_is_failed_not_published(self):
        self.seed("done")
        with patch("pipeline.publication.stage_source", side_effect=RuntimeError("fixture blocked")):
            result = self.apply()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["published"])
        self.assertEqual(self.conn.execute("SELECT status FROM papers").fetchone()[0], "done")


if __name__ == "__main__":
    unittest.main()
