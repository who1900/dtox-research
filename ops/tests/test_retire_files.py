import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing

from ops.corpus_quality import retire_files
from pipeline.originals import SCHEMA


class RetireFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.repair = self.root / 'repair'
        self.originals = self.root / 'originals'
        self.out = self.root / 'archive'
        for path in (self.data, self.repair, self.originals, self.out):
            path.mkdir()
        self._state()
        (self.originals / 'blobs').mkdir()
        (self.originals / 'private').mkdir()
        self._originals()

    def tearDown(self):
        self.tmp.cleanup()

    def _state(self):
        with closing(sqlite3.connect(self.data / 'state.db')) as conn, conn:
            conn.execute('CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,status TEXT)')
            conn.executemany('INSERT INTO papers VALUES(?,?)', [('excluded', 'excluded'), ('retained', 'done')])
            conn.execute('CREATE TABLE paper_publications(arxiv_id TEXT,revision TEXT,kind TEXT,artifact_path TEXT,cache_sha256 TEXT,state TEXT)')

    def _originals(self):
        with closing(sqlite3.connect(self.originals / 'manifest.sqlite3')) as conn, conn:
            for statement in SCHEMA:
                conn.execute(statement)
            shared = hashlib.sha256(b'shared').hexdigest()
            only = hashlib.sha256(b'only').hexdigest()
            excluded_shared = hashlib.sha256(b'excluded-shared').hexdigest()
            (self.originals / 'blobs' / shared).write_bytes(b'shared')
            (self.originals / 'blobs' / only).write_bytes(b'only')
            (self.originals / 'blobs' / excluded_shared).write_bytes(b'excluded-shared')
            conn.executemany('INSERT INTO blobs VALUES(?,?)', [(shared, 6), (only, 4), (excluded_shared, 15)])
            conn.executemany('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?)', [
                (1, 'excluded', 'https://x/e', 'https://x/e', 'PDF', 'v1', '2026-01-01T00:00:00Z', shared),
                (2, 'retained', 'https://x/r', 'https://x/r', 'PDF', 'v1', '2026-01-01T00:00:00Z', shared),
                (3, 'excluded', 'https://x/e2', 'https://x/e2', 'PDF', 'v1', '2026-01-01T00:00:00Z', only),
                (4, 'excluded', 'https://x/e3', 'https://x/e3', 'PDF', 'v1', '2026-01-01T00:00:00Z', excluded_shared),
            ])
            conn.execute("INSERT INTO acquisition_queue VALUES('excluded','b1','{}','acquisition_pending',1)")
            conn.execute("INSERT INTO transitions VALUES(1,'excluded','b1','acquisition_pending','x','2026-01-01T00:00:00Z','{}',1)")

    def plan(self):
        (self.data / 'chunk_cache').mkdir()
        (self.data / 'chunk_cache' / 'excluded.json').write_text('[]')
        return retire_files.plan_files(['excluded'], self.data, self.repair, self.originals, self.out)

    def test_removes_excluded_files_and_exclusive_cas_only(self):
        plan = self.plan()
        result = retire_files.delete_files(plan)
        self.assertFalse((self.data / 'chunk_cache' / 'excluded.json').exists())
        with closing(sqlite3.connect(self.originals / 'manifest.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT canonical_id FROM originals ORDER BY id').fetchall(), [('retained',)])
            hashes = {r[0] for r in conn.execute('SELECT sha256 FROM blobs')}
            self.assertIn(hashlib.sha256(b'shared').hexdigest(), hashes)
            self.assertNotIn(hashlib.sha256(b'only').hexdigest(), hashes)
        self.assertTrue((self.originals / 'blobs' / hashlib.sha256(b'shared').hexdigest()).exists())
        self.assertFalse((self.originals / 'blobs' / hashlib.sha256(b'only').hexdigest()).exists())
        self.assertEqual(result['cas_blobs_deleted'], 2)

    def test_safe_id_collision_fails_closed(self):
        with closing(sqlite3.connect(self.data / 'state.db')) as conn, conn:
            conn.executemany('INSERT INTO papers VALUES(?,?)', [('ab:c', 'excluded'), ('ab/c', 'excluded')])
        with self.assertRaises(retire_files.RetirementFileError):
            retire_files.plan_files(['ab:c'], self.data, self.repair, self.originals, self.out)

    def test_shared_publication_artifact_is_retained(self):
        pub = self.data / 'publication_cache'
        pub.mkdir()
        artifact = pub / 'shared.json'
        artifact.write_text('{}')
        with closing(sqlite3.connect(self.data / 'state.db')) as conn, conn:
            conn.executemany('INSERT INTO paper_publications VALUES(?,?,?,?,?,?)', [
                ('excluded', 'a', 'snapshot', str(artifact), None, 'superseded'),
                ('retained', 'b', 'snapshot', str(artifact), None, 'published'),
            ])
        plan = self.plan()
        self.assertFalse(any(f['path'] == str(artifact.resolve()) for f in plan['files']))
        retire_files.delete_files(plan)
        self.assertTrue(artifact.exists())

    def test_excluded_journal_artifact_becomes_deletion_candidate(self):
        pub = self.data / 'publication_cache'
        pub.mkdir()
        artifact = pub / 'old-publication.json'
        artifact.write_text('{"old":true}')
        journal = self.repair / 'journals' / 'excluded.json.gz'
        journal.parent.mkdir()
        import gzip
        old = artifact.read_bytes()
        payload = dict(schema=1, kind='published-label-replacement', operation='whole-document',
                       phase='applied', paper=dict(arxiv_id='excluded',
                       retained_artifacts=[dict(path=str(artifact), sha256=hashlib.sha256(old).hexdigest())]))
        journal.write_bytes(gzip.compress(json.dumps(payload).encode()))
        with closing(sqlite3.connect(self.repair / 'whole-document-replacement.sqlite')) as conn, conn:
            conn.execute('CREATE TABLE targets(paper_id TEXT,status TEXT,journal_path TEXT)')
            conn.execute('INSERT INTO targets VALUES(?,?,?)', ('excluded', 'applied', str(journal)))
        plan = self.plan()
        self.assertTrue(any(f['path'] == str(artifact.resolve()) and f['kind'] == 'publication_cache'
                            for f in plan['files']))
        retire_files.delete_files(plan)
        self.assertFalse(artifact.exists())

    def test_pending_queue_may_select_terminal_deferred_journal(self):
        journal = self.repair / 'journals' / 'excluded.json'
        journal.parent.mkdir()
        journal.write_text(json.dumps(dict(schema=1, kind='published-label-replacement',
                                           operation='whole-document', phase='deferred',
                                           paper=dict(arxiv_id='excluded', retained_artifacts=[]))))
        with closing(sqlite3.connect(self.repair / 'whole-document-replacement.sqlite')) as conn, conn:
            conn.execute('CREATE TABLE targets(paper_id TEXT,status TEXT,journal_path TEXT)')
            conn.execute('INSERT INTO targets VALUES(?,?,?)', ('excluded', 'pending', str(journal)))
        plan = self.plan()
        self.assertTrue(any(f['path'] == str(journal.resolve()) and f['kind'] == 'journal'
                            for f in plan['files']))

    def test_missing_pending_journal_is_recorded_as_provisional(self):
        missing = self.repair / 'journals' / 'not-created.json.gz'
        (self.repair / 'journals').mkdir()
        with closing(sqlite3.connect(self.repair / 'whole-document-replacement.sqlite')) as conn, conn:
            conn.execute('CREATE TABLE targets(paper_id TEXT,status TEXT,journal_path TEXT)')
            conn.execute('INSERT INTO targets VALUES(?,?,?)', ('excluded', 'pending', str(missing)))
        plan = self.plan()
        self.assertEqual(plan['missing_provisional_journals'], [
            dict(paper_id='excluded', path=str(missing.resolve()), status='pending')])
        self.assertFalse(any(f['kind'] == 'journal' for f in plan['files']))

    def test_missing_deferred_journal_is_recorded_as_provisional(self):
        missing = self.repair / 'journals' / 'not-created.json'
        (self.repair / 'journals').mkdir()
        with closing(sqlite3.connect(self.repair / 'whole-document-replacement.sqlite')) as conn, conn:
            conn.execute('CREATE TABLE targets(paper_id TEXT,status TEXT,journal_path TEXT)')
            conn.execute('INSERT INTO targets VALUES(?,?,?)', ('excluded', 'deferred', str(missing)))
        plan = self.plan()
        self.assertEqual(plan['missing_provisional_journals'][0]['status'], 'deferred')

    def test_missing_applied_retained_journal_stops_planning(self):
        missing = self.repair / 'journals' / 'applied.json.gz'
        (self.repair / 'journals').mkdir()
        with closing(sqlite3.connect(self.repair / 'whole-document-replacement.sqlite')) as conn, conn:
            conn.execute('CREATE TABLE targets(paper_id TEXT,status TEXT,journal_path TEXT)')
            conn.execute('INSERT INTO targets VALUES(?,?,?)', ('retained', 'applied', str(missing)))
        with self.assertRaises(retire_files.RetirementFileError):
            self.plan()

    def test_migrated_cache_root_symlink_is_supported(self):
        physical = self.root / 'migrated-chunks'
        physical.mkdir()
        (physical / 'excluded.json').write_text('[]')
        (self.data / 'chunk_cache').symlink_to(physical, target_is_directory=True)
        plan = retire_files.plan_files(['excluded'], self.data, self.repair, self.originals, self.out)
        self.assertEqual(plan['files'][0]['path'], str((physical / 'excluded.json').resolve()))
        retire_files.delete_files(plan)
        self.assertFalse((physical / 'excluded.json').exists())

    def test_cas_referenced_only_by_excluded_ids_can_be_removed(self):
        with closing(sqlite3.connect(self.data / 'state.db')) as conn, conn:
            conn.execute("INSERT INTO papers VALUES('also-excluded','excluded')")
        with closing(sqlite3.connect(self.originals / 'manifest.sqlite3')) as conn, conn:
            excluded_shared = hashlib.sha256(b'excluded-shared').hexdigest()
            conn.execute('INSERT INTO originals VALUES(?,?,?,?,?,?,?,?)',
                         (5, 'also-excluded', 'https://x/other', 'https://x/other', 'PDF', 'v1',
                          '2026-01-01T00:00:00Z', excluded_shared))
        plan = retire_files.plan_files(['excluded', 'also-excluded'], self.data,
                                       self.repair, self.originals, self.out)
        retire_files.delete_files(plan)
        self.assertFalse((self.originals / 'blobs' / hashlib.sha256(b'excluded-shared').hexdigest()).exists())
        self.assertTrue((self.originals / 'blobs' / hashlib.sha256(b'shared').hexdigest()).exists())


if __name__ == '__main__':
    unittest.main()
