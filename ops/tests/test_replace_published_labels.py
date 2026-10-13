import json
import sqlite3
import unittest
import ast
import time
from unittest.mock import patch
from pathlib import Path
from contextlib import closing

from ops import replace_published_labels as replacement
from ops.tests import test_repair_section_labels as fixtures
from pipeline.publication import ensure_publication_schema
from pipeline.fts_catalog import ensure_catalog
from ops.tests.test_restore_cached_points import Client


class PublishedLabelReplacementTests(unittest.TestCase):
    def setUp(self):
        fixtures.SectionLabelRepairTests.setUp(self)
        publications = self.root / 'publication_cache'
        publications.mkdir()
        with closing(sqlite3.connect(self.state_path)) as conn:
            for name in ('title', 'year', 'layers', 'source_url'):
                conn.execute('ALTER TABLE papers ADD COLUMN '+name+' TEXT')
            metadata = dict(title='Title', year='2026', layers='llm', source_url='https://example.test/paper')
            conn.execute('UPDATE papers SET title=?,year=?,layers=?,source_url=?', tuple(metadata.values()))
            ensure_publication_schema(conn)
            artifact = dict(points=list(self.qdrant.points.values()), metadata=metadata, cache_path=str(self.cache_path))
            self.parent = replacement._snapshot_revision(artifact, replacement.labels._sha(self.cache_before))
            self.qdrant.points[self.pid]['payload']['publication_revision'] = self.parent
            self.parent_path = publications / 'parent.json'
            self.parent_bytes = replacement._jbytes(artifact)
            self.parent_path.write_bytes(self.parent_bytes)
            conn.execute("INSERT INTO paper_publications(arxiv_id,revision,kind,artifact_path,cache_sha256,state) VALUES (?,?,'snapshot',?,?,'published')", (self.aid, self.parent, str(self.parent_path), replacement.labels._sha(self.cache_before)))
            conn.commit()

    tearDown = fixtures.SectionLabelRepairTests.tearDown

    def prepare(self):
        return replacement.prepare_plan(self.changes, state_db=self.state_path, fts_db=self.fts_path,
            cache_dir=self.cache_dir, section_cache_dir=self.section_dir, journal_path=self.journal, qdrant=self.qdrant,
            source_bytes_by_paper={self.aid: self.source_bytes},
            expected_source_sha256_by_paper={self.aid: replacement.labels._sha(self.source_bytes)},
            source_route_by_paper={self.aid: "paper_markdown"})

    def states(self):
        with closing(sqlite3.connect(self.state_path)) as conn:
            return dict(conn.execute("SELECT revision,state FROM paper_publications WHERE kind='snapshot'"))

    def prepare_addition_fixture(self):
        self.qdrant=Client(list(self.qdrant.points.values()))
        self.missing=dict(self.chunk,chunk_index=1,text='A uniquely reproduced missing body paragraph.',
            section_title='Results',section_type='experiments')
        self.cache_before=replacement._jbytes([self.chunk,self.missing])
        self.cache_path.write_bytes(self.cache_before)
        artifact=json.loads(self.parent_path.read_bytes())
        self.parent=replacement._snapshot_revision(artifact,replacement.labels._sha(self.cache_before))
        artifact['points'][0]['payload']['publication_revision']=self.parent
        self.qdrant.points[self.pid]['payload']['publication_revision']=self.parent
        self.parent_bytes=replacement._jbytes(artifact);self.parent_path.write_bytes(self.parent_bytes)
        with closing(sqlite3.connect(self.state_path))as c:
            c.execute('UPDATE paper_publications SET revision=?,cache_sha256=?',
                (self.parent,replacement.labels._sha(self.cache_before)));c.commit()
        with closing(sqlite3.connect(self.fts_path))as c:
            ensure_catalog(c)
            c.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(self.pid,self.aid,self.rowid))
            c.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(self.aid,self.rowid))
            c.execute("INSERT INTO pipeline_fts_papers VALUES('chunks',?)",(self.aid,))
            c.execute('UPDATE pipeline_fts_scope SET complete=1');c.commit()
        self.source_path=self.root/'addition-source.tex';self.source_path.write_bytes(b'frozen source')
        self.rowmap=self.root/'rowmap.db';stat=self.fts_path.stat()
        with closing(sqlite3.connect(self.rowmap))as c:
            c.execute('CREATE TABLE meta(key PRIMARY KEY,value)')
            c.executemany('INSERT INTO meta VALUES(?,?)',[('complete','1'),('source_identity',json.dumps([str(self.fts_path),stat.st_dev,stat.st_ino]))])
            c.execute('CREATE TABLE rows(rowid INTEGER PRIMARY KEY,point_id,arxiv_id)')
            c.execute('CREATE INDEX rows_point ON rows(point_id)')
            c.execute('INSERT INTO rows VALUES(?,?,?)',(self.rowid,self.pid,self.aid));c.commit()
        self.addition=dict(arxiv_id=self.aid,chunk_indices=[1],source_path=str(self.source_path),
            expected_source_sha256=replacement.labels._sha(self.source_path.read_bytes()),
            expected_cache_sha256=replacement.labels._sha(self.cache_before),source_route='latex',rowmap_db=str(self.rowmap))
        self.recipe=dict(dimension=384,document_prefix='',pooling='CLS/L2',max_length=512,
            model_sha256='a'*64,tokenizer_sha256='b'*64)

    def prepare_addition(self,parser=None):
        return replacement.prepare_plan([],state_db=self.state_path,fts_db=self.fts_path,
            cache_dir=self.cache_dir,section_cache_dir=self.section_dir,journal_path=self.journal,
            qdrant=self.qdrant,cached_additions=self.addition,embedding_recipe=self.recipe,
            embedder=lambda texts:[[1.0]+[0.0]*383 for _ in texts],
            source_parser=parser or(lambda raw,route:[self.missing]))

    def test_published_cached_addition_and_exact_rollback(self):
        self.prepare_addition_fixture()
        before=json.loads(json.dumps(self.qdrant.points));manifest=self.manifest_path.read_bytes()
        plan=self.prepare_addition();replacement.apply_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(len(plan['paper']['points_added']),1)
        self.assertEqual(len(self.qdrant.points),2)
        self.assertEqual({p['payload']['publication_revision']for p in self.qdrant.points.values()},{plan['paper']['revision']})
        self.assertEqual(self.cache_path.read_bytes(),self.cache_before)
        self.assertEqual(self.manifest_path.read_bytes(),manifest)
        self.assertEqual(self.parent_path.read_bytes(),self.parent_bytes)
        with closing(sqlite3.connect(self.fts_path))as c:
            self.assertEqual(c.execute('SELECT COUNT(*)FROM chunks').fetchone(),(2,))
            self.assertEqual(c.execute('SELECT complete FROM pipeline_fts_scope').fetchone(),(1,))
        replacement.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(self.qdrant.points,before)
        with closing(sqlite3.connect(self.fts_path))as c:
            self.assertEqual(c.execute('SELECT COUNT(*)FROM chunks').fetchone(),(1,))
            self.assertEqual(c.execute('SELECT COUNT(*)FROM dtox_cached_point_receipts').fetchone(),(0,))

    def test_published_addition_lost_upsert_response_can_roll_back(self):
        self.prepare_addition_fixture();self.prepare_addition()
        self.qdrant.crash_after_upsert=True
        with self.assertRaises(fixtures.SimulatedCrash):replacement.apply_plan(self.journal,qdrant=self.qdrant)
        replacement.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(len(self.qdrant.points),1)
        self.assertEqual(self.states()[self.parent],'published')

    def test_published_addition_rejects_unproven_source(self):
        self.prepare_addition_fixture()
        with self.assertRaises(replacement.RepairError):self.prepare_addition(lambda raw,route:[])
        self.assertFalse(self.journal.exists())

    def test_published_addition_fts_commit_interruption_and_float_readback(self):
        self.prepare_addition_fixture();self.prepare_addition()
        original=self.qdrant.upsert
        def stored(**kwargs):
            original(**kwargs)
            for p in kwargs['points']:self.qdrant.points[p['id']]['vector'][0]+=8e-9
        self.qdrant.upsert=stored
        verify=replacement._verify
        def crash(plan,client,mode):
            if mode=='after':raise fixtures.SimulatedCrash('after FTS commit')
            return verify(plan,client,mode)
        with patch.object(replacement,'_verify',side_effect=crash):
            with self.assertRaises(fixtures.SimulatedCrash):replacement.apply_plan(self.journal,qdrant=self.qdrant)
        with closing(sqlite3.connect(self.fts_path))as c:
            self.assertEqual(c.execute('SELECT COUNT(*)FROM chunks').fetchone(),(2,))
        replacement.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(len(self.qdrant.points),1)
        with closing(sqlite3.connect(self.fts_path))as c:
            self.assertEqual(c.execute('SELECT COUNT(*)FROM chunks').fetchone(),(1,))

    def test_complete_replacement_and_exact_rollback(self):
        vector = list(self.qdrant.points[self.pid]['vector'])
        plan = self.prepare()
        self.assertEqual(self.qdrant.points[self.pid]['payload']['publication_revision'], self.parent)
        replacement.apply_plan(self.journal, qdrant=self.qdrant)
        new = plan['paper']['revision']
        self.assertEqual(self.states(), {self.parent:'superseded', new:'published'})
        self.assertEqual(self.qdrant.points[self.pid]['payload']['section_type'], 'method')
        self.assertEqual(self.qdrant.points[self.pid]['vector'], vector)
        self.assertEqual(self.parent_path.read_bytes(), self.parent_bytes)
        manifest = json.loads(self.manifest_path.read_bytes())
        self.assertEqual(manifest['cache_sha256'], replacement.labels._sha(self.cache_path.read_bytes()))
        replacement.rollback_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(self.states(), {self.parent:'published', new:'superseded'})
        self.assertEqual(self.cache_path.read_bytes(), self.cache_before)
        self.assertEqual(self.qdrant.points[self.pid]['payload']['publication_revision'], self.parent)
        # Retrying retains the immutable candidate from the deliberate rollback.
        self.journal = self.root / 'retry.json'
        self.prepare()
        replacement.apply_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(self.states()[new],'published')

    def test_crash_after_first_payload_write_can_be_rolled_back(self):
        plan = self.prepare()
        self.qdrant.crash_after_first_write = True
        with self.assertRaises(fixtures.SimulatedCrash):
            replacement.apply_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(json.loads(self.journal.read_bytes())['phase'], 'applying')
        replacement.rollback_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(self.cache_path.read_bytes(), self.cache_before)
        self.assertEqual(self.states()[self.parent], 'published')
        self.assertEqual(self.states()[plan['paper']['revision']], 'superseded')

    def test_parent_payload_drift_blocks_preparation(self):
        self.qdrant.points[self.pid]['payload']['text'] = 'changed'
        with self.assertRaises(replacement.RepairError):
            self.prepare()
        self.assertFalse(self.journal.exists())

    def test_parent_null_bibliographic_live_omission_preserved(self):
        artifact=json.loads(self.parent_path.read_bytes())
        artifact['points'][0]['payload']['venue']=None
        artifact['points'][0]['payload']['citation_count']=None
        artifact['points'][0]['payload']['url']=None
        self.parent=replacement._snapshot_revision(artifact,replacement.labels._sha(self.cache_before))
        artifact['points'][0]['payload']['publication_revision']=self.parent
        self.qdrant.points[self.pid]['payload']['publication_revision']=self.parent
        self.parent_bytes=replacement._jbytes(artifact);self.parent_path.write_bytes(self.parent_bytes)
        with closing(sqlite3.connect(self.state_path))as conn:
            conn.execute('UPDATE paper_publications SET revision=?',(self.parent,));conn.commit()
        vector=list(self.qdrant.points[self.pid]['vector'])
        self.prepare();replacement.apply_plan(self.journal,qdrant=self.qdrant)
        self.assertNotIn('venue',self.qdrant.points[self.pid]['payload'])
        self.assertNotIn('citation_count',self.qdrant.points[self.pid]['payload'])
        self.assertNotIn('url',self.qdrant.points[self.pid]['payload'])
        replacement.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertNotIn('venue',self.qdrant.points[self.pid]['payload'])
        self.assertNotIn('citation_count',self.qdrant.points[self.pid]['payload'])
        self.assertNotIn('url',self.qdrant.points[self.pid]['payload'])
        self.assertEqual(self.qdrant.points[self.pid]['vector'],vector)
        self.assertEqual(self.parent_path.read_bytes(),self.parent_bytes)

    def test_parent_payload_exception_is_only_declared_nullable_fields(self):
        self.assertTrue(replacement._parent_payload_equal({}, {'venue':None}))
        self.assertFalse(replacement._parent_payload_equal({}, {'venue':'Actual venue'}))
        self.assertFalse(replacement._parent_payload_equal({'venue':'Changed'}, {'venue':'Original'}))
        self.assertFalse(replacement._parent_payload_equal({}, {'other':None}))
        self.assertTrue(replacement._parent_payload_equal({}, {'citation_count':None}))
        self.assertFalse(replacement._parent_payload_equal({}, {'citation_count':0}))
        self.assertFalse(replacement._parent_payload_equal({'citation_count':None}, {'citation_count':0}))
        self.assertTrue(replacement._parent_payload_equal({'citation_count':0}, {'citation_count':0}))
        self.assertTrue(replacement._parent_payload_equal({}, {'url':None}))
        self.assertFalse(replacement._parent_payload_equal({}, {'url':'https://example.org/paper'}))
        self.assertFalse(replacement._parent_payload_equal({'url':'https://example.org/new'}, {'url':'https://example.org/old'}))

    def test_complete_inventory_and_filtered_cache_subset(self):
        extra = dict(self.chunk,chunk_index=1,text='Unindexed extra.',section_title='Other')
        self.cache_before = replacement._jbytes([self.chunk,extra])
        self.cache_path.write_bytes(self.cache_before)
        # No canonical manifest: the subset case does not invent section order.
        self.manifest_path.unlink()
        artifact = json.loads(self.parent_path.read_bytes())
        self.parent = replacement._snapshot_revision(artifact,replacement.labels._sha(self.cache_before))
        artifact['points'][0]['payload']['publication_revision'] = self.parent
        self.qdrant.points[self.pid]['payload']['publication_revision'] = self.parent
        self.parent_path.write_bytes(replacement._jbytes(artifact))
        with closing(sqlite3.connect(self.state_path)) as conn:
            conn.execute('UPDATE paper_publications SET revision=?,cache_sha256=?',
                (self.parent,replacement.labels._sha(self.cache_before)))
            conn.commit()
        full_labels = [dict(chunk_index=0,section_title='Misc',section_type='other'),
                       dict(chunk_index=1,section_title='Conclusion',section_type='conclusion')]
        replacement.prepare_plan([],state_db=self.state_path,fts_db=self.fts_path,
            cache_dir=self.cache_dir,section_cache_dir=self.section_dir,journal_path=self.journal,qdrant=self.qdrant,
            complete_fts_rowids_by_paper={self.aid:[self.rowid]},cache_labels_by_paper={self.aid:full_labels},
            expected_indexed_indices_by_paper={self.aid:[0]})
        replacement.apply_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(json.loads(self.cache_path.read_bytes())[1]['section_type'],'conclusion')
        self.assertEqual(len(self.qdrant.points),1)
        replacement.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(self.cache_path.read_bytes(),self.cache_before)

    def test_exact_full_fts_mapping_rejects_duplicate(self):
        with closing(sqlite3.connect(self.fts_path)) as conn:
            conn.execute('INSERT INTO chunks(text,point_id,arxiv_id,section_type) VALUES (?,?,?,?)', ('A useful passage.', self.pid, self.aid, 'other'))
            conn.commit()
        with self.assertRaises(replacement.RepairError):
            self.prepare()

    def test_ordinary_failure_compensates(self):
        self.prepare()
        original = self.qdrant.set_payload
        failed = False
        def failing(**kwargs):
            nonlocal failed
            original(**kwargs)
            if not failed:
                failed = True
                raise RuntimeError('lost payload response')
        self.qdrant.set_payload = failing
        with self.assertRaises(RuntimeError):
            replacement.apply_plan(self.journal, qdrant=self.qdrant)
        self.assertEqual(json.loads(self.journal.read_bytes())['phase'], 'rolled_back')
        self.assertEqual(self.qdrant.points[self.pid]['payload']['publication_revision'], self.parent)

    def test_api_current_snapshot_read_ignores_superseded_history(self):
        tree = ast.parse((Path(__file__).resolve().parents[2]/'api'/'main.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_source_visible')
        with closing(sqlite3.connect(self.state_path)) as conn:
            conn.execute("INSERT INTO paper_publications(arxiv_id,revision,kind,artifact_path,state) VALUES (?,?,'snapshot',?,'superseded')", (self.aid,'old','retained.json'))
            conn.commit()
            scope = dict(time=time,STATE_DB_PATH=str(self.state_path),
                _source_remaining=lambda _:1, _paper_rows=lambda ids:{self.aid:{}},
                _ro_conn=lambda *args,**kwargs:conn, _source_call=lambda deadline,fn:fn(),
                _filter_publication_points=lambda points:points)
            exec(compile(ast.Module(body=[node],type_ignores=[]),'api/main.py','exec'),scope)
            records = [dict(source_id=self.aid,status='done',publication_revision=self.parent)]
            self.assertEqual(scope['_source_visible'](records,1),{self.aid:self.parent})
            conn.execute("UPDATE paper_publications SET state='replacement_staged' WHERE revision='old'")
            conn.commit()
            self.assertEqual(scope['_source_visible'](records,1),{})
