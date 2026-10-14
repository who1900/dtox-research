import json
import gzip
import re
import sqlite3
import unittest
import uuid
from contextlib import closing
from unittest.mock import patch

from ops import replace_published_labels as owner
from ops.tests import test_replace_published_labels as fixtures
from ops.tests.test_repair_section_labels import SimulatedCrash
from pipeline import document_cache


class WholeDocumentTests(unittest.TestCase):
    def setUp(self):
        fixtures.OrphanAdoptionTests.setUp(self)
        (self.root/'publication_cache').mkdir()
        self.cache_path.write_bytes(b'corrupt old cache\xff')
        self.canonical_source.write_bytes(b'old damaged source')
        self.manifest_path.write_bytes(b'old corrupt manifest')
        with closing(sqlite3.connect(self.state_path)) as c:
            cols=list(self.state_after)
            c.execute('INSERT INTO papers('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',list(self.state_after.values()))
            c.execute('CREATE TABLE citations(arxiv_id TEXT, citation TEXT)')
            c.execute('INSERT INTO citations VALUES(?,?)',(self.aid,'original citation'))
            c.execute('CREATE TABLE judgments(arxiv_id TEXT, verdict TEXT)')
            c.execute('INSERT INTO judgments VALUES(?,?)',(self.aid,'original judgment'));c.commit()
        self.qdrant.coarse[self.coarse['id']]=json.loads(json.dumps(self.coarse))
        obsolete=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aid+'#2'))
        self.qdrant.points[obsolete]=dict(id=obsolete,vector=[384**-0.5]*384,payload=dict(self.chunk,arxiv_id=self.aid,chunk_index=2))
        with closing(sqlite3.connect(self.fts_path)) as c:
            rid=c.execute('INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers) VALUES(?,?,?,?,?,?)',
                (self.chunk['text'],obsolete,self.aid,'other','prose','')).lastrowid
            c.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(obsolete,self.aid,rid))
            c.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(self.aid,rid));c.commit()
        self.newchunk=dict(self.chunk,chunk_index=0,text='Fresh canonical body with enough descriptive words to demonstrate complete replacement and preserve content.')
        self.newid=self.pid
        self.point=dict(id=self.newid,vector=[384**-0.5]*384,payload=dict(self.newchunk,arxiv_id=self.aid,title='Title',layers=[],year=None))
        cache=owner._jbytes([self.newchunk])
        self.candidate=dict(source_path=str(self.source_path),source_sha256=owner.labels._sha(self.source_bytes),
            source_route='spec_markdown',source_binding=dict(url=self.state_after['source_url'],version='v2',retrieved_at='2026-10-14T00:00:00Z'),
            chunks=[self.newchunk],manifest=document_cache.make_section_manifest(self.aid,[self.newchunk],cache,self.source_bytes,'markdown','test'),
            points=[self.point],embedding_recipe=dict(dimension=384,document_prefix='',pooling='CLS/L2',max_length=512,model_sha256='a'*64,tokenizer_sha256='b'*64))
    tearDown=fixtures.OrphanAdoptionTests.tearDown

    def prepare(self,**kwargs):
        return owner.prepare_document_replacement(self.aid,candidate=self.candidate,state_db=self.state_path,fts_db=self.fts_path,
            paper_fts_db=self.pf,cache_dir=self.cache_dir,section_cache_dir=self.section_dir,fulltext_cache_dir=self.fulltext_dir,
            publication_cache_dir=self.root/'publication_cache',journal_path=self.journal,qdrant=self.qdrant,
            source_parser=lambda raw,route:([self.newchunk],[self.newchunk]),**kwargs)

    def snapshot(self):
        sql={}
        for path,tables in ((self.state_path,('papers','citations','judgments','paper_publications')),
                (self.fts_path,('chunks','pipeline_fts_points','pipeline_fts_rows')),
                (self.pf,('papers_fts','pipeline_fts_rows','pipeline_fts_papers'))):
            with closing(sqlite3.connect(path)) as c:
                for table in tables:sql[str(path)+table]=c.execute('SELECT rowid,* FROM '+table+' ORDER BY rowid').fetchall()
        return dict(sql=sql,points=json.loads(json.dumps(self.qdrant.points)),coarse=json.loads(json.dumps(self.qdrant.coarse)),
            files=[p.read_bytes() for p in (self.cache_path,self.manifest_path,self.canonical_source)])

    def test_success_coherent_complete_replacement_and_exact_rollback(self):
        before=self.snapshot();self.prepare()
        with self.assertRaises(owner.RepairError):owner.apply_plan(self.journal,qdrant=self.qdrant)
        owner.apply_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        self.assertEqual(set(self.qdrant.points),{self.newid})
        self.assertEqual(json.loads(self.cache_path.read_bytes()),[self.newchunk])
        with closing(sqlite3.connect(self.fts_path)) as c:
            self.assertEqual(c.execute('SELECT point_id,text FROM chunks').fetchall(),[(self.newid,self.newchunk['text'])])
        with closing(sqlite3.connect(self.state_path)) as c:
            self.assertEqual(c.execute('SELECT state FROM paper_publications').fetchone(),('published',))
            self.assertEqual(c.execute('SELECT citation FROM citations').fetchone(),('original citation',))
            self.assertEqual(c.execute('SELECT verdict FROM judgments').fetchone(),('original judgment',))
        owner.rollback_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        self.assertEqual(self.snapshot(),before)

    def test_preflight_drift_and_foreign_uuid_reject_before_writes(self):
        original=self.qdrant.points[self.newid]
        self.qdrant.points[self.newid]=dict(self.point,payload=dict(self.point['payload'],arxiv_id='foreign'))
        with self.assertRaises(owner.RepairError):self.prepare()
        self.assertFalse(self.journal.exists())
        self.qdrant.points[self.newid]=original
        self.prepare();before=self.snapshot();self.source_path.write_bytes(b'drift')
        with self.assertRaises(owner.RepairError):owner.apply_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        self.assertEqual(self.snapshot(),before)

    def test_interrupted_apply_restores_exact_surfaces(self):
        before=self.snapshot();self.prepare()
        original=owner.labels._atomic
        def crash(path,data):
            if str(path)==str(self.cache_path):raise SimulatedCrash('after complete Qdrant/SQL transitions')
            return original(path,data)
        with patch.object(owner.labels,'_atomic',crash):
            with self.assertRaises(SimulatedCrash):owner.apply_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        owner.rollback_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        self.assertEqual(self.snapshot(),before)

    def test_complete_owner_uses_bounded_fts_rowids(self):
        traced=[];connect=owner.labels._connect
        def capture(*args,**kwargs):
            conn=connect(*args,**kwargs);conn.set_trace_callback(traced.append);return conn
        with patch.object(owner.labels,'_connect',capture):
            self.prepare()
            owner.apply_plan(self.journal,qdrant=self.qdrant,maintenance=True)
            owner.rollback_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        statements=[s.strip().lower() for s in traced if not s.lstrip().startswith('--')]
        direct=[s for s in statements if re.search(r'\b(?:from|into) (?:chunks|papers_fts)\b',s)]
        self.assertTrue(direct)
        for statement in direct:
            if statement.startswith('insert'):continue
            if 'order by rowid desc limit 1' in statement:continue
            self.assertIn('where rowid=',statement,statement)
        for statement in statements:
            if re.search(r'(?:from|delete from) pipeline_fts_(?:rows|papers)\b',statement):
                self.assertIn('table_name=',statement,statement)
        with closing(sqlite3.connect(self.fts_path)) as c:
            bounded=c.execute('EXPLAIN QUERY PLAN SELECT point_id,arxiv_id FROM chunks WHERE rowid=?',(self.rowid,)).fetchall()
            endpoint=c.execute('EXPLAIN QUERY PLAN SELECT rowid FROM chunks ORDER BY rowid DESC LIMIT 1').fetchall()
            self.assertTrue(any('INDEX 0:=' in row[3] for row in bounded),bounded)
            self.assertTrue(any('INDEX 192:' in row[3] for row in endpoint),endpoint)

    def test_two_prepared_documents_allocate_nonoverlapping_rows(self):
        first_id=self.aid
        self.newchunk['chunk_index']=5
        self.point['id']=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aid+'#5'))
        self.point['payload']['chunk_index']=5
        self.candidate['manifest']=document_cache.make_section_manifest(self.aid,[self.newchunk],owner._jbytes([self.newchunk]),self.source_bytes,'markdown','test')
        first_journal=self.journal
        first=self.prepare()
        floor={'chunks':max(r[0] for r in first['paper']['fts_after']),
               'papers_fts':max(r[0] for r in first['paper']['paper_fts_after'])}
        self.aid='2401.00002';self.journal=self.root/'second.json'
        with closing(sqlite3.connect(self.state_path)) as c:
            values=dict(self.state_after,arxiv_id=self.aid)
            c.execute('INSERT INTO papers('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',list(values.values()));c.commit()
        cp=json.loads(json.dumps(self.coarse));cp['id']=str(uuid.uuid5(uuid.NAMESPACE_URL,'coarse:'+self.aid));cp['payload']['arxiv_id']=self.aid
        self.qdrant.coarse[cp['id']]=cp
        self.point['id']=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aid+'#5'));self.point['payload']['arxiv_id']=self.aid
        self.candidate['manifest']=document_cache.make_section_manifest(self.aid,[self.newchunk],owner._jbytes([self.newchunk]),self.source_bytes,'markdown','test')
        second=self.prepare(rowid_floors=floor)
        self.assertGreater(second['paper']['fts_after'][0][0],floor['chunks'])
        self.assertGreater(second['paper']['paper_fts_after'][0][0],floor['papers_fts'])
        owner.apply_plan(first_journal,qdrant=self.qdrant,maintenance=True)
        owner.apply_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        with closing(sqlite3.connect(self.fts_path)) as c:
            self.assertEqual(set(c.execute('SELECT arxiv_id FROM chunks').fetchall()),{(first_id,),(self.aid,)})

    def test_embedding_reuse_is_exact_same_paper_baseline(self):
        original=self.qdrant.points[self.pid]
        self.newchunk['text']=original['payload']['text']
        self.point['payload']['text']=self.newchunk['text']
        self.point['vector']=list(original['vector'])
        self.candidate['manifest']=document_cache.make_section_manifest(self.aid,[self.newchunk],owner._jbytes([self.newchunk]),self.source_bytes,'markdown','test')
        self.candidate['embedding_recipe']['scope']='newly_embedded_points'
        self.candidate['embedding_reuse']={self.pid:self.pid}
        self.point['vector'][0]+=0.0000001
        with self.assertRaises(owner.RepairError):self.prepare()
        self.point['vector']=list(original['vector'])
        self.candidate['embedding_reuse']={self.pid:'foreign-origin'}
        with self.assertRaises(owner.RepairError):self.prepare()
        self.candidate['embedding_reuse']={self.pid:self.pid}
        plan=self.prepare()
        provenance=plan['paper']['artifact']['metadata']['embedding_provenance']
        self.assertEqual(provenance['newly_embedded_point_ids'],[])
        self.assertTrue(provenance['reused_vector_recipe_unverified'])
        owner.apply_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        self.assertEqual(self.qdrant.points[self.pid]['vector'],original['vector'])

    def test_compressed_applied_journal_rolls_back_exactly(self):
        before=self.snapshot();self.prepare()
        owner.apply_plan(self.journal,qdrant=self.qdrant,maintenance=True)
        compressed=self.journal.with_suffix('.json.gz')
        compressed.write_bytes(gzip.compress(self.journal.read_bytes(),compresslevel=1,mtime=0))
        self.journal.unlink()
        owner.rollback_plan(compressed,qdrant=self.qdrant,maintenance=True)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(compressed.read_bytes()[:2],b'\x1f\x8b')
        self.assertEqual(owner._load(compressed)['phase'],'rolled_back')


if __name__=='__main__':unittest.main()
