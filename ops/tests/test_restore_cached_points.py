import json
import sqlite3
import unittest
import math
import struct
from contextlib import closing
from unittest.mock import patch

from ops import restore_cached_points as restore
from ops.tests import test_repair_section_labels as fixtures
from pipeline.fts_catalog import ensure_catalog


class Client(fixtures.FakeQdrant):
    def retrieve(self,*,collection_name,ids,**kwargs):
        if collection_name=='papers_coarse_base':return []
        return [self.points[i]for i in ids if i in self.points]
    def upsert(self,*,collection_name,points,wait=True):
        for point in points:
            p=json.loads(json.dumps(point))
            self.points[str(p['id'])]=p
            if getattr(self,'crash_after_upsert',False):
                self.crash_after_upsert=False
                raise fixtures.SimulatedCrash('lost upsert response')
    def delete(self,*,collection_name,points_selector,wait=True):
        for pid in points_selector:self.points.pop(str(pid),None)


class RestoreCachedPointsTests(unittest.TestCase):
    def setUp(self):
        fixtures.SectionLabelRepairTests.setUp(self)
        self.manifest_path.unlink()
        self.missing=dict(chunk_index=1,text='Accuracy results: baseline 42.1; proposed method 51.3. Dataset sizes: training 1000; evaluation 200.',
            section_title='Results',section_type='experiments',element_type='table')
        self.cache_path.write_bytes(restore.labels._json_bytes([self.chunk,self.missing]))
        self.cache_before=self.cache_path.read_bytes()
        self.source_path=self.root/'source.tex';self.source_path.write_bytes(b'preserved source input')
        self.qdrant=Client(list(self.qdrant.points.values()))
        self.rowmap=self.root/'rowmap.sqlite'
        with closing(sqlite3.connect(self.fts_path))as conn:
            ensure_catalog(conn)
            conn.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(self.pid,self.aid,self.rowid))
            conn.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(self.aid,self.rowid));conn.commit()
        stat=self.fts_path.stat()
        with closing(sqlite3.connect(self.rowmap))as conn:
            conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            conn.executemany('INSERT INTO meta VALUES(?,?)',[('complete','1'),('source_identity',json.dumps([str(self.fts_path),stat.st_dev,stat.st_ino]))])
            conn.execute('CREATE TABLE rows(rowid INTEGER PRIMARY KEY,point_id TEXT,arxiv_id TEXT)')
            conn.execute('CREATE INDEX by_point ON rows(point_id)')
            conn.execute('INSERT INTO rows VALUES(?,?,?)',(self.rowid,self.pid,self.aid));conn.commit()
        self.recipe=dict(dimension=384,document_prefix='',pooling='CLS/L2',model_sha256='a'*64,tokenizer_sha256='b'*64,max_length=512)
        self.calls=0
        def embed(texts):
            self.calls+=1
            return [[1.0]+[0.0]*383 for _ in texts]
        self.embed=embed
    tearDown=fixtures.SectionLabelRepairTests.tearDown

    def prepare(self,parser=None):
        return restore.prepare_plan(self.aid,[1],source_path=self.source_path,cache_path=self.cache_path,
            expected_source_sha256=restore.labels._sha(self.source_path.read_bytes()),expected_cache_sha256=restore.labels._sha(self.cache_before),
            state_db=self.state_path,fts_db=self.fts_path,rowmap_db=self.rowmap,journal_path=self.journal,qdrant=self.qdrant,
            embedder=self.embed,embedding_recipe=self.recipe,source_route='latex',source_parser=parser or(lambda raw,route:[self.missing]))

    def test_exact_cached_restore_and_rollback(self):
        baseline=json.loads(json.dumps(self.qdrant.points))
        plan=self.prepare();restore.apply_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(self.calls,1)
        self.assertEqual(self.cache_path.read_bytes(),self.cache_before)
        self.assertEqual(len(self.qdrant.points),2)
        with closing(sqlite3.connect(self.fts_path))as conn:
            self.assertEqual(conn.execute('SELECT complete FROM pipeline_fts_scope').fetchone(),(0,))
            self.assertEqual(conn.execute('SELECT COUNT(*)FROM chunks').fetchone(),(2,))
        restore.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(self.qdrant.points,baseline)
        with closing(sqlite3.connect(self.fts_path))as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*)FROM chunks').fetchone(),(1,))
            self.assertEqual(conn.execute('SELECT COUNT(*)FROM dtox_cached_point_receipts').fetchone(),(0,))

    def test_float32_cosine_storage_allows_apply_and_rollback(self):
        vector=[math.sin(i+1) for i in range(384)]
        norm=math.sqrt(sum(x*x for x in vector));vector=[x/norm for x in vector]
        self.embed=lambda texts:[vector[:] for _ in texts]
        original=self.qdrant.upsert
        def rounded(**kwargs):
            original(**kwargs)
            for point in kwargs['points']:
                p=self.qdrant.points[point['id']]
                p['vector']=[struct.unpack('f',struct.pack('f',x))[0]for x in p['vector']]
        self.qdrant.upsert=rounded
        self.prepare();restore.apply_plan(self.journal,qdrant=self.qdrant)
        restore.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(len(self.qdrant.points),1)

    def test_candidate_tolerance_rejects_real_drift_and_nonfinite(self):
        expected={'id':'point','payload':{'text':'exact'},'vector':[1.0]+[0.0]*383}
        for change in ['payload','id','dimension','nan','component','distributed']:
            actual=json.loads(json.dumps(expected))
            if change=='payload':actual['payload']['text']='changed'
            elif change=='id':actual['id']='other'
            elif change=='dimension':actual['vector'].pop()
            elif change=='nan':actual['vector'][1]=float('nan')
            elif change=='component':actual['vector'][1]=2e-6
            else:actual['vector'][1:]=[8e-8]*383
            with self.subTest(change=change):self.assertFalse(restore._candidate_equal(actual,expected))

    def test_lost_upsert_response_resumable_rollback(self):
        self.prepare();self.qdrant.crash_after_upsert=True
        with self.assertRaises(fixtures.SimulatedCrash):restore.apply_plan(self.journal,qdrant=self.qdrant)
        restore.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(len(self.qdrant.points),1)
        self.assertEqual(self.calls,1)

    def test_source_proof_unique_and_exact(self):
        with self.assertRaises(restore.RepairError):self.prepare(lambda raw,route:[])
        with self.assertRaises(restore.RepairError):self.prepare(lambda raw,route:[self.missing,self.missing])
        self.assertEqual(self.calls,0)

    def test_source_alignment_accepts_exact_substring_with_matching_element(self):
        larger=dict(self.missing,text='Source prefix. '+self.missing['text']+' Source suffix.')
        plan=self.prepare(lambda raw,route:[larger])
        self.assertEqual(plan['points'][0]['payload']['text'],self.missing['text'])
        self.assertEqual(self.calls,1)

    def test_source_alignment_accepts_cached_interval_across_exact_overlap(self):
        from pipeline import extractor
        overlap=max(int(extractor.CHUNK_CHAR_LIMIT*extractor.CHUNK_OVERLAP_RATIO),1)
        left='prefix-'+('A'*(overlap+20))+'CROSS'
        right=left[-overlap:]+'-suffix'
        target=left[-(overlap+10):]+'-suffix'
        self.missing['text']=target
        self.cache_before=restore.labels._json_bytes([self.chunk,self.missing])
        self.cache_path.write_bytes(self.cache_before)
        parts=[
            dict(self.missing,chunk_index=10,text=left,part_index=0,part_total=2),
            dict(self.missing,chunk_index=11,text=right,part_index=1,part_total=2),
        ]
        canonical=left+right[len(left[-overlap:]):]
        plan=self.prepare(lambda raw,route:(parts,[dict(section_title='Results',section_type='experiments',
            element_type='table',text=canonical)]))
        self.assertEqual(plan['points'][0]['payload']['text'],target)
        self.assertEqual(self.calls,1)

    def test_unsplit_proof_counts_physical_occurrences_and_rejects_real_repeats(self):
        source_text='prefix '+self.missing['text']+' suffix'
        chunks=[dict(self.missing,text='prefix '+self.missing['text'][:30]),
                dict(self.missing,text=self.missing['text'][-30:]+' suffix')]
        one=[dict(section_title='Results',section_type='experiments',element_type='table',text=source_text)]
        self.assertTrue(restore.source_proven(self.missing,chunks,one))
        two=one+[dict(section_title='Appendix',section_type='experiments',element_type='table',text=source_text)]
        self.assertFalse(restore.source_proven(self.missing,chunks,two))

    def test_unsplit_element_type_must_match(self):
        code=[dict(section_title='Results',section_type='experiments',element_type='code',text=self.missing['text'])]
        self.assertFalse(restore.source_proven(self.missing,[],code))

    def test_default_parser_provides_typed_unsplit_view_without_changing_chunks(self):
        from pipeline import extractor
        body=' '.join('Measured result '+str(i)+' with preserved source text.'for i in range(70))
        source=('# Results\n\n'+body+'\n\n```python\nprint(42)\n```\n').encode()
        chunks,elements=restore.parse_source_views(source,'paper_markdown')
        expected,_=extractor.build_chunks_markdown(source.decode(),document_kind='paper')
        self.assertEqual(chunks,expected)
        self.assertEqual({e['element_type']for e in elements},{'prose','code'})
        prose=next(e for e in elements if e['element_type']=='prose')
        self.assertEqual(prose['text'],body.strip())
        target=dict(prose,text=body[1500:1900])
        self.assertTrue(restore.source_proven(target,chunks,elements))

    def test_source_alignment_rejects_ambiguous_type_and_normalized_text(self):
        repeated=dict(self.missing,text=self.missing['text']+' '+self.missing['text'])
        with self.assertRaises(restore.RepairError):self.prepare(lambda raw,route:[repeated])
        wrong_type=dict(self.missing,element_type='prose')
        with self.assertRaises(restore.RepairError):self.prepare(lambda raw,route:[wrong_type])
        near=dict(self.missing,text=self.missing['text'].replace(';',':',1))
        with self.assertRaises(restore.RepairError):self.prepare(lambda raw,route:[near])
        self.assertEqual(self.calls,0)

    def test_crash_after_fts_commit_uses_atomic_receipt_for_rollback(self):
        self.prepare()
        original=restore._phase
        def interrupted(path,plan,phase):
            if phase=='applied':raise fixtures.SimulatedCrash('lost completion response')
            return original(path,plan,phase)
        with patch.object(restore,'_phase',interrupted):
            with self.assertRaises(fixtures.SimulatedCrash):restore.apply_plan(self.journal,qdrant=self.qdrant)
        with closing(sqlite3.connect(self.fts_path))as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*)FROM dtox_cached_point_receipts').fetchone(),(1,))
        restore.rollback_plan(self.journal,qdrant=self.qdrant)
        self.assertEqual(len(self.qdrant.points),1)
        with closing(sqlite3.connect(self.fts_path))as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*)FROM chunks').fetchone(),(1,))

    def test_collision_blocks_embedding(self):
        import uuid
        pid=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aid+'#1'))
        with closing(sqlite3.connect(self.rowmap))as conn:
            conn.execute('INSERT INTO rows VALUES(?,?,?)',(99,pid,'another-paper'));conn.commit()
        with self.assertRaises(restore.RepairError):self.prepare()
        self.assertEqual(self.calls,0)

    def test_new_fragment_preserves_paper_provenance_and_numeric_year(self):
        provenance=dict(fulltext_source='latex',source_url='https://example.test/source',
            source_sha256='c'*64,acquisition={'kind':'original','edition':'unknown'},
            license='CC-BY',extraction={'parser':'verified'})
        self.qdrant.points[self.pid]['payload'].update(provenance,year=2020,
            source_locator={'page':1},fragment_id='old-fragment')
        self.missing['source_locator']={'page':7,'table':3}
        self.cache_before=restore.labels._json_bytes([self.chunk,self.missing])
        self.cache_path.write_bytes(self.cache_before)
        plan=self.prepare();restore.apply_plan(self.journal,qdrant=self.qdrant)
        p=self.qdrant.points[plan['points'][0]['id']]['payload']
        for key,value in provenance.items():self.assertEqual(p[key],value)
        self.assertEqual(p['source_locator'],{'page':7,'table':3})
        self.assertNotIn('fragment_id',p)
        with closing(sqlite3.connect(self.fts_path))as conn:
            self.assertEqual(conn.execute('SELECT year FROM chunks WHERE point_id=?',(plan['points'][0]['id'],)).fetchone(),(2020,))
