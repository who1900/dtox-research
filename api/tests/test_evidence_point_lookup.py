import sqlite3
import unittest
from unittest.mock import patch

from api import main


class EvidencePointLookupTests(unittest.TestCase):
    def _db(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT, year INTEGER, "
            "venue TEXT, citation_count INTEGER, influential INTEGER, layers TEXT, "
            "abstract TEXT, fulltext_source TEXT, source_url TEXT, parser_mode TEXT, "
            "parse_status TEXT, status TEXT)")
        conn.execute("CREATE INDEX idx_papers_status ON papers(status)")
        conn.executemany(
            "INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"paper-{i}", f"Title {i}", 2024, None, i, 0, "web3", "abstract",
              "latex", f"https://example.test/{i}", "pylatexenc", "ok",
              "done" if i < 20 else ("deferred" if i == 20 else "invalid"))
             for i in range(5000)])
        conn.execute("ANALYZE")
        self.addCleanup(conn.close)
        return conn

    def test_lookup_uses_primary_key_and_matches_done_filtered_rows(self):
        conn = self._db()
        ids = ["paper-1", "paper-2", "paper-2", "", "paper-20", "missing"]
        statements = []
        conn.set_trace_callback(statements.append)
        with patch.object(main, "_ro_conn", return_value=conn):
            actual = main._paper_rows(ids)

        placeholders = ",".join("?" * len(set(pid for pid in ids if pid)))
        unique_ids = list(dict.fromkeys(pid for pid in ids if pid))
        old_rows = conn.execute(
            f"SELECT {main.PAPER_COLUMNS} FROM papers WHERE status='done' "
            f"AND arxiv_id IN ({placeholders})", unique_ids).fetchall()
        new_rows = conn.execute(
            f"SELECT {main.PAPER_COLUMNS}, status FROM papers "
            f"WHERE arxiv_id IN ({placeholders})", unique_ids).fetchall()
        new_done_rows = [row[:-1] for row in new_rows if row[-1] == "done"]
        self.assertEqual(set(old_rows), set(new_done_rows))
        self.assertEqual(set(actual), {"paper-1", "paper-2"})
        self.assertEqual(actual["paper-2"]["fulltext_source"], "latex")
        self.assertEqual(actual["paper-2"]["parser_mode"], "pylatexenc")
        self.assertEqual(actual["paper-2"]["parse_status"], "ok")

        select = next(statement for statement in statements
                      if statement.lstrip().upper().startswith("SELECT"))
        self.assertIn("WHERE arxiv_id IN", select)
        self.assertNotIn("WHERE status", select)

        plan = conn.execute(
            f"EXPLAIN QUERY PLAN SELECT {main.PAPER_COLUMNS}, status FROM papers "
            f"WHERE arxiv_id IN ({placeholders})", unique_ids).fetchall()
        plan_text = " ".join(row[-1] for row in plan)
        self.assertIn("sqlite_autoindex_papers_1", plan_text)
        self.assertNotIn("idx_papers_status", plan_text)

    def test_empty_ids_do_not_query(self):
        with patch.object(main, "_ro_conn") as connection:
            self.assertEqual(main._paper_rows([None, "", None]), {})
        connection.assert_not_called()

    def test_legacy_schema_fallback_projects_missing_metadata_as_null(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT, year INTEGER, "
            "venue TEXT, citation_count INTEGER, influential INTEGER, layers TEXT, "
            "abstract TEXT, status TEXT);"
            "CREATE INDEX idx_papers_status ON papers(status);"
            "INSERT INTO papers VALUES ('legacy-1','Legacy title',2020,NULL,3,0,'web3','a','done');"
            "INSERT INTO papers VALUES ('legacy-pending','Pending title',2021,NULL,0,0,'web3','b','pending');")
        with patch.object(main, "_ro_conn", return_value=conn), \
                patch.object(main, "_ro_conn_drop") as drop:
            rows = main._paper_rows(["legacy-1", "legacy-pending"])

        drop.assert_called_once_with(main.STATE_DB_PATH)
        self.assertEqual(set(rows), {"legacy-1"})
        self.assertEqual(rows["legacy-1"]["title"], "Legacy title")
        self.assertIsNone(rows["legacy-1"]["fulltext_source"])
        self.assertIsNone(rows["legacy-1"]["source_url"])
        self.assertIsNone(rows["legacy-1"]["parser_mode"])
        self.assertIsNone(rows["legacy-1"]["parse_status"])

    def test_intermediate_schema_fallback_preserves_stored_source(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.executescript(
            "CREATE TABLE papers (arxiv_id TEXT PRIMARY KEY, title TEXT, year INTEGER, "
            "venue TEXT, citation_count INTEGER, influential INTEGER, layers TEXT, "
            "abstract TEXT, fulltext_source TEXT, source_url TEXT, status TEXT);"
            "INSERT INTO papers VALUES ('legacy-md','Doc',2024,NULL,0,0,'web3','body',"
            "'latex','https://example.test/doc.md','done');")
        with patch.object(main, "_ro_conn", return_value=conn), \
                patch.object(main, "_ro_conn_drop"):
            rows = main._paper_rows(["legacy-md"])

        self.assertEqual(rows["legacy-md"]["fulltext_source"], "latex")
        self.assertIsNone(rows["legacy-md"]["parser_mode"])
        self.assertIsNone(rows["legacy-md"]["parse_status"])


if __name__ == "__main__":
    unittest.main()
