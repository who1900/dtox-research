import sqlite3
import unittest

from pipeline.fts_catalog import CatalogBlocked, append_points, ensure_catalog, replace_paper


def memory():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED,section_type UNINDEXED,element_type UNINDEXED,layers UNINDEXED,year UNINDEXED)")
    return db


def pt(pid, paper="p", text="words", **payload):
    return {"id": pid, "payload": {"arxiv_id": paper, "text": text, **payload}}


class NoGlobalScanConnection:
    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        normalized = " ".join(sql.lower().split())
        assert "count(" not in normalized, sql
        if " from chunks" in normalized:
            assert "rowid=?" in normalized or "limit 1" in normalized, sql
        if " from pipeline_fts_points" in normalized:
            assert " where " in normalized and ("point_id=?" in normalized or "arxiv_id=?" in normalized), sql
            assert "group by" not in normalized and "order by" not in normalized, sql
        if any(f" from {table}" in normalized for table in ("pipeline_fts_rows", "pipeline_fts_papers")):
            assert ("table_name=?" in normalized or "table_name='chunks'" in normalized) and "arxiv_id=?" in normalized, sql
            assert "group by" not in normalized and "order by" not in normalized, sql
        if " from pipeline_fts_scope" in normalized:
            assert "where singleton=1" in normalized, sql
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.connection.executemany(sql, parameters)


class FtsCatalogTests(unittest.TestCase):
    def test_replace_and_replay_use_exact_owned_rowids(self):
        db = memory()
        try:
            ensure_catalog(db)
            self.assertTrue(replace_paper(db, "p", [pt("one"), pt("two")], fresh=True))
            rows = db.execute("SELECT point_id,rowid FROM chunks ORDER BY rowid").fetchall()
            self.assertTrue(replace_paper(db, "p", [pt("one"), pt("two")]))
            self.assertEqual(rows, db.execute("SELECT point_id,rowid FROM chunks ORDER BY rowid").fetchall())
            replace_paper(db, "p", [pt("one", text="updated")])
            self.assertEqual([("one", "updated")], db.execute("SELECT point_id,text FROM chunks").fetchall())
        finally:
            db.close()

    def test_duplicate_payload_mismatch_and_foreign_collision_fail_closed(self):
        db = memory()
        try:
            ensure_catalog(db)
            with self.assertRaises(CatalogBlocked):
                replace_paper(db, "p", [pt("x"), pt("x")], fresh=True)
            with self.assertRaises(CatalogBlocked):
                replace_paper(db, "p", [pt("x", paper="other")], fresh=True)
            replace_paper(db, "p", [pt("x")], fresh=True)
            with self.assertRaises(CatalogBlocked):
                replace_paper(db, "q", [pt("x", paper="q")], fresh=True)
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        finally:
            db.close()

    def test_fresh_mismatched_replay_fails_but_identical_replay_succeeds(self):
        db = memory()
        try:
            ensure_catalog(db)
            self.assertTrue(replace_paper(db, "p", [pt("x")], fresh=True))
            self.assertTrue(replace_paper(db, "p", [pt("x")], fresh=True))
            with self.assertRaises(CatalogBlocked):
                replace_paper(db, "p", [pt("x", text="changed")], fresh=True)
            self.assertEqual([("words",)], db.execute("SELECT text FROM chunks").fetchall())
        finally:
            db.close()

    def test_partial_legacy_allows_new_paper_without_global_scan(self):
        db = memory()
        try:
            db.executemany("INSERT INTO chunks(text,point_id,arxiv_id) VALUES ('legacy',?,?)",
                           [(f"old-{i}", f"old-paper-{i}") for i in range(100)])
            ensure_catalog(db)
            guarded = NoGlobalScanConnection(db)
            replace_paper(guarded, "new-paper", [pt("new", "new-paper")], fresh=True)
            self.assertEqual(101, db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        finally:
            db.close()

    def test_append_replay_and_conflict_are_atomic(self):
        db = memory()
        try:
            ensure_catalog(db)
            self.assertEqual(1, append_points(db, [pt("x")]))
            self.assertEqual(0, append_points(db, [pt("x")]))
            with self.assertRaises(CatalogBlocked):
                append_points(db, [pt("y"), pt("x", text="different")])
            self.assertIsNone(db.execute("SELECT 1 FROM pipeline_fts_points WHERE point_id='y'").fetchone())
        finally:
            db.close()

    def test_corrupt_owned_rowid_blocks_before_mutation(self):
        db = memory()
        try:
            ensure_catalog(db)
            replace_paper(db, "p", [pt("x")], fresh=True)
            db.execute("UPDATE pipeline_fts_points SET row_id=row_id+500 WHERE point_id='x'")
            db.commit()
            with self.assertRaises(CatalogBlocked):
                replace_paper(db, "p", [pt("z")])
            self.assertEqual([("x",)], db.execute("SELECT point_id FROM chunks").fetchall())
        finally:
            db.close()

    def test_append_rejects_row_with_foreign_uuid_without_partial_insert(self):
        db = memory()
        try:
            ensure_catalog(db)
            append_points(db, [pt("owned")])
            rid = db.execute("SELECT row_id FROM pipeline_fts_points WHERE point_id='owned'").fetchone()[0]
            db.execute("UPDATE chunks SET point_id='foreign' WHERE rowid=?", (rid,))
            db.commit()
            with self.assertRaises(CatalogBlocked):
                append_points(NoGlobalScanConnection(db), [pt("new"), pt("owned")])
            self.assertEqual([("foreign",)], db.execute("SELECT point_id FROM chunks").fetchall())
            self.assertIsNone(db.execute("SELECT 1 FROM pipeline_fts_points WHERE point_id='new'").fetchone())
        finally:
            db.close()

    def test_append_rejects_incomplete_selected_row_catalog(self):
        db = memory()
        try:
            ensure_catalog(db)
            append_points(db, [pt("owned")])
            db.execute("DELETE FROM pipeline_fts_rows WHERE arxiv_id='p'")
            db.commit()
            with self.assertRaises(CatalogBlocked):
                append_points(NoGlobalScanConnection(db), [pt("owned")])
        finally:
            db.close()

    def test_partial_catalog_cannot_prove_absence_of_unmapped_fts_only_uuid(self):
        db = memory()
        try:
            legacy = db.execute("INSERT INTO chunks(text,point_id,arxiv_id) VALUES ('legacy','candidate','old')").lastrowid
            db.commit()
            ensure_catalog(db)
            replace_paper(NoGlobalScanConnection(db), "new", [pt("candidate", "new")], fresh=True)
            self.assertEqual(("candidate", "old", "legacy"), db.execute(
                "SELECT point_id,arxiv_id,text FROM chunks WHERE rowid=?", (legacy,)).fetchone())
            self.assertEqual([("old",), ("new",)], db.execute(
                "SELECT arxiv_id FROM chunks WHERE point_id='candidate' ORDER BY rowid").fetchall())
            self.assertEqual(0, db.execute("SELECT complete FROM pipeline_fts_scope WHERE singleton=1").fetchone()[0])
            with self.assertRaises(CatalogBlocked):
                append_points(NoGlobalScanConnection(db), [pt("bulk", "another")])
            with self.assertRaises(CatalogBlocked):
                replace_paper(NoGlobalScanConnection(db), "old", [pt("replacement", "old")])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
