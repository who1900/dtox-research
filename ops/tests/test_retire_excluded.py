from contextlib import contextmanager
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ops.corpus_quality import retire_excluded as retirement


@contextmanager
def database(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class Client:
    def __init__(self):
        self.points = {c: {aid: dict(id=aid, payload={'arxiv_id': aid}, vector=[1.0, 0.25])
                           for aid in ('excluded', 'retained')} for c in retirement.COLLECTIONS}
        self.deletes = []

    def scroll(self, collection_name, scroll_filter, **kwargs):
        aid = scroll_filter['must'][0]['match']['value']
        return [copy.deepcopy(p) for p in self.points[collection_name].values()
                if p['payload']['arxiv_id'] == aid], None

    def count(self, collection_name, exact):
        return {'count': len(self.points[collection_name])}

    def delete(self, collection_name, points_selector, wait):
        assert wait
        self.deletes.append((collection_name, points_selector))
        for pid in points_selector:
            del self.points[collection_name][pid]


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {family: self.root / (family + '.sqlite') for family in retirement.TABLES}
        with database(self.paths['state']) as conn:
            conn.executescript("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT);"
                               "INSERT INTO papers VALUES('excluded','excluded'),('retained','complete');"
                               "CREATE TABLE paper_publications(arxiv_id TEXT,revision TEXT, artifact BLOB);"
                               "INSERT INTO paper_publications VALUES('excluded','old',x'00FF'),('retained','live',x'AB');")
        for family, table in [('fts', 'chunks'), ('papers_fts', 'papers_fts')]:
            with database(self.paths[family]) as conn:
                conn.execute('CREATE VIRTUAL TABLE ' + table + ' USING fts5(arxiv_id UNINDEXED,point_id UNINDEXED,text)')
                conn.executemany('INSERT INTO ' + table + ' VALUES(?,?,?)',
                                 [('excluded', 'excluded', 'retire content'), ('retained', 'retained', 'keep content')])
                # Deliberately no ownership entries for raw excluded FTS rows.
                conn.execute('CREATE TABLE pipeline_fts_points(point_id TEXT,arxiv_id TEXT,row_id INTEGER)')
                if family == 'fts':
                    conn.execute('CREATE TABLE dtox_cached_point_receipts(operation_id TEXT,point_id TEXT,row_id INTEGER)')
                    conn.execute("INSERT INTO dtox_cached_point_receipts VALUES('op','excluded',1)")
        self.client = Client()
        self.output = self.root / 'archive'

    def export(self):
        return retirement.export_indexes(['excluded'], self.paths, self.client, self.output)

    def test_roundtrip_preserves_vectors_blob_and_retained_content(self):
        manifest = self.export()
        self.assertEqual(retirement.verify_archive(self.output), manifest)
        self.assertEqual(retirement._read_points(self.output, 'papers_fulltext')['excluded']['vector'], [1.0, .25])
        state_image = next(x for x in manifest['tables'] if x['table'] == 'paper_publications')
        with database(self.output / 'rows.sqlite') as conn:
            self.assertEqual(conn.execute('SELECT artifact FROM ' + state_image['archive']).fetchone()[0], b'\x00\xff')
        retained = {c: copy.deepcopy(self.client.points[c]['retained']) for c in retirement.COLLECTIONS}
        retirement.delete_indexes(self.output, self.client, maintenance=True)
        for c in retirement.COLLECTIONS:
            self.assertEqual(self.client.points[c], {'retained': retained[c]})
        with database(self.paths['state']) as conn:
            self.assertEqual(conn.execute("SELECT status FROM papers WHERE arxiv_id='excluded'").fetchone(), ('excluded',))
            self.assertEqual(conn.execute('SELECT arxiv_id FROM paper_publications').fetchall(), [('retained',)])
        for family, table in [('fts', 'chunks'), ('papers_fts', 'papers_fts')]:
            with database(self.paths[family]) as conn:
                self.assertEqual(conn.execute('SELECT rowid,* FROM ' + table).fetchall(), [(2, 'retained', 'retained', 'keep content')])
        with database(self.paths['fts']) as conn:
            self.assertEqual(conn.execute('SELECT * FROM dtox_cached_point_receipts').fetchall(), [])

    def test_foreign_point_or_vector_drift_prevents_all_deletion(self):
        self.export()
        self.client.points['papers_fulltext']['excluded']['payload']['arxiv_id'] = 'retained'
        with self.assertRaises(retirement.RetirementError):
            retirement.delete_indexes(self.output, self.client, maintenance=True)
        self.assertEqual(self.client.deletes, [])

    def test_sql_drift_prevents_all_deletion(self):
        self.export()
        with database(self.paths['fts']) as conn:
            conn.execute("UPDATE chunks SET text='changed' WHERE rowid=1")
        with self.assertRaisesRegex(retirement.RetirementError, 'SQL preimage drift'):
            retirement.delete_indexes(self.output, self.client, maintenance=True)
        self.assertEqual(self.client.deletes, [])

    def test_vector_drift_prevents_all_deletion(self):
        self.export()
        self.client.points['papers_coarse_base']['excluded']['vector'][0] = 2.0
        with self.assertRaisesRegex(retirement.RetirementError, 'Qdrant preimage drift'):
            retirement.delete_indexes(self.output, self.client, maintenance=True)
        self.assertEqual(self.client.deletes, [])

    def test_corrupt_archive_and_missing_gate_fail_before_deletion(self):
        self.export()
        with self.assertRaisesRegex(retirement.RetirementError, 'maintenance'):
            retirement.delete_indexes(self.output, self.client)
        with (self.output / 'papers_fulltext.jsonl.gz').open('ab') as stream:
            stream.write(b'corruption')
        with self.assertRaisesRegex(retirement.RetirementError, 'hash mismatch'):
            retirement.delete_indexes(self.output, self.client, maintenance=True)
        self.assertEqual(self.client.deletes, [])


if __name__ == '__main__':
    unittest.main()
