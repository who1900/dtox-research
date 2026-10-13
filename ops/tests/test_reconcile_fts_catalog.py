import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager


@contextmanager
def database(path):
    conn=sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()

from ops import reconcile_fts_catalog as repair
from pipeline.fts_catalog import ensure_catalog


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.fts=self.root/'fts.db';self.rowmap=self.root/'map.db'
        with database(self.fts)as c:
            c.execute('CREATE VIRTUAL TABLE chunks USING fts5(text,point_id UNINDEXED,arxiv_id UNINDEXED)')
            c.executemany('INSERT INTO chunks VALUES(?,?,?)',[('original one','p1','paper1'),('original two','p2','absent-state-paper'),('restored','p3','paper1')])
            ensure_catalog(c)
            c.execute("INSERT INTO pipeline_fts_points VALUES('p3','paper1',3)")
            c.execute("INSERT INTO pipeline_fts_rows VALUES('chunks','paper1',3)")
            c.execute('CREATE TABLE dtox_cached_point_receipts(operation_id,point_id,row_id,PRIMARY KEY(operation_id,point_id))')
            c.execute('CREATE INDEX receipts_point ON dtox_cached_point_receipts(point_id,row_id)')
            c.execute("INSERT INTO dtox_cached_point_receipts VALUES('accepted','p3',3)")
        stat=self.fts.stat()
        with database(self.rowmap)as c:
            c.execute('CREATE TABLE meta(key PRIMARY KEY,value)')
            c.executemany('INSERT INTO meta VALUES(?,?)',[('complete','1'),('source_identity',json.dumps([str(self.fts.resolve()),stat.st_dev,stat.st_ino]))])
            c.execute('CREATE TABLE rows(rowid INTEGER PRIMARY KEY,point_id,arxiv_id)')
            c.executemany('INSERT INTO rows VALUES(?,?,?)',[(1,'p1','paper1'),(2,'p2','absent-state-paper')])
        self.map_bytes=self.rowmap.read_bytes()

    def tearDown(self):self.temp.cleanup()

    def run_repair(self,**kwargs):
        return repair.run(self.fts,self.rowmap,writers_frozen=True,**kwargs)

    def test_resume_and_complete_without_content_changes(self):
        with database(self.fts)as c:before=c.execute('SELECT rowid,*FROM chunks').fetchall()
        with patch.object(repair.time,'monotonic',side_effect=[0,0,100]):
            result=self.run_repair(batch_size=1,max_seconds=1)
        self.assertEqual(result['phase'],'filling')
        with database(self.fts)as c:
            self.assertEqual(c.execute('SELECT complete FROM pipeline_fts_scope').fetchone(),(0,))
            self.assertEqual(c.execute('SELECT COUNT(*)FROM pipeline_fts_papers').fetchone(),(0,))
        self.assertEqual(self.run_repair(batch_size=1)['phase'],'complete')
        with database(self.fts)as c:
            self.assertEqual(c.execute('SELECT rowid,*FROM chunks').fetchall(),before)
            self.assertEqual(c.execute('SELECT complete FROM pipeline_fts_scope').fetchone(),(1,))
            self.assertEqual(c.execute('SELECT COUNT(*)FROM pipeline_fts_rows').fetchone(),(3,))
            self.assertEqual(c.execute('SELECT COUNT(*)FROM pipeline_fts_papers').fetchone(),(2,))
        self.assertEqual(self.rowmap.read_bytes(),self.map_bytes)

    def test_unreceipted_row_blocks_and_batch_rolls_back(self):
        with database(self.fts)as c:c.execute('DELETE FROM dtox_cached_point_receipts')
        with self.assertRaises(repair.ReconciliationBlocked):self.run_repair()
        with database(self.fts)as c:
            self.assertEqual(c.execute('SELECT COUNT(*)FROM pipeline_fts_points').fetchone(),(1,))
            self.assertEqual(c.execute('SELECT complete FROM pipeline_fts_scope').fetchone(),(0,))

    def test_duplicate_point_across_batches_rejected(self):
        with database(self.fts)as c:c.execute("UPDATE chunks SET point_id='p1' WHERE rowid=2")
        with self.assertRaises(repair.ReconciliationBlocked):self.run_repair(batch_size=1)

    def test_orphan_existing_ownership_blocks_completion(self):
        with database(self.fts)as c:c.execute("INSERT INTO pipeline_fts_rows VALUES('chunks','wrong-paper',1)")
        with self.assertRaises(repair.ReconciliationBlocked):self.run_repair()
        with database(self.fts)as c:self.assertEqual(c.execute('SELECT complete FROM pipeline_fts_scope').fetchone(),(0,))

    def test_wrong_file_provenance_rejected(self):
        with database(self.rowmap)as c:c.execute("UPDATE meta SET value='[]' WHERE key='source_identity'")
        with self.assertRaises(repair.ReconciliationBlocked):self.run_repair()

    def test_explicit_freeze_required(self):
        with self.assertRaises(repair.ReconciliationBlocked):repair.run(self.fts,self.rowmap)


if __name__=='__main__':unittest.main()
