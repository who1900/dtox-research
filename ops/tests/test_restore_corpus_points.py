import json
import sqlite3
import unittest
import uuid
import ast
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import restore_corpus_points as driver
from ops import restore_cached_points as restore
from ops.tests import test_restore_cached_points as fixtures
from pipeline.fts_catalog import ensure_catalog
from pipeline.publication import ensure_publication_schema


class RestoreCorpusPointsTests(unittest.TestCase):
    def setUp(self):
        fixtures.RestoreCachedPointsTests.setUp(self)
        self.fulltext=self.root/'fulltext';self.fulltext.mkdir()
        self.latex=self.root/'latex';self.latex.mkdir()
        self.aids=[self.aid,'2401.00002']
        aid2=self.aids[1];pid2=str(uuid.uuid5(uuid.NAMESPACE_URL,aid2+'#0'))
        p=json.loads(json.dumps(self.qdrant.points[self.pid]));p['id']=pid2;p['payload']['arxiv_id']=aid2
        self.qdrant.points[pid2]=p
        with closing(sqlite3.connect(self.state_path))as conn:
            conn.execute("INSERT INTO papers VALUES(?,'done')",(aid2,));conn.commit()
        with closing(sqlite3.connect(self.fts_path))as conn:
            rid=conn.execute('INSERT INTO chunks(text,point_id,arxiv_id,section_type)VALUES(?,?,?,?)',(self.chunk['text'],pid2,aid2,'other')).lastrowid
            conn.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(pid2,aid2,rid))
            conn.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(aid2,rid));conn.commit()
        with closing(sqlite3.connect(self.rowmap))as conn:
            conn.execute('INSERT INTO rows VALUES(?,?,?)',(rid,pid2,aid2));conn.commit()
        self.census=self.root/'census.db';self.projection=self.root/'projection.db'
        with closing(sqlite3.connect(self.census))as conn:
            conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            conn.execute("INSERT INTO meta VALUES('run_complete','1')")
            conn.execute('CREATE TABLE papers(paper_id TEXT PRIMARY KEY,input_fingerprint TEXT,cache_sha256 TEXT,source_sha256 TEXT,source_location TEXT,fulltext_source TEXT,parser_mode TEXT)')
            for aid in self.aids:
                (self.cache_dir/(aid+'.json')).write_bytes(self.cache_before)
                (self.fulltext/(aid+'.tex')).write_bytes(self.source_path.read_bytes())
                conn.execute('INSERT INTO papers VALUES(?,?,?,?,?,?,?)',(aid,'fingerprint-'+aid,restore.labels._sha(self.cache_before),restore.labels._sha(self.source_path.read_bytes()),'fulltext_cache','latex','pylatexenc'))
            conn.commit()
        self.make_projection()
        self.args=SimpleNamespace(census_db=self.census,projection_db=self.projection,rowmap_db=self.rowmap,
            state_db=self.state_path,fts_db=self.fts_path,cache_dir=self.cache_dir,fulltext_cache_dir=self.fulltext,
            latex_cache_dir=self.latex,run_db=self.root/'run.sqlite',journal_dir=self.root/'journals',
            max_seconds=60,batch_size=20,retry_failed=False,label_run_db=None)

    tearDown=fixtures.RestoreCachedPointsTests.tearDown

    def make_projection(self):
        with closing(sqlite3.connect(self.projection))as conn:
            conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            conn.executemany('INSERT INTO meta VALUES(?,?)',[('run_complete','1'),('census_identity',driver._identity(self.census)),('rowmap_identity',driver._identity(self.rowmap))])
            conn.execute('CREATE TABLE papers(paper_id TEXT PRIMARY KEY,census_fingerprint TEXT,cache_sha256 TEXT,cache_backed_missing_current_count INTEGER,cache_backed_missing_current_indices_json TEXT,projection_status TEXT)')
            for aid in self.aids:
                conn.execute('INSERT INTO papers VALUES(?,?,?,?,?,?)',(aid,'fingerprint-'+aid,restore.labels._sha(self.cache_before),1,'[1]','cache_backed_current_projection_missing'))
            conn.commit()

    def run_driver(self,parser=None):
        return driver.run(self.args,self.qdrant,self.embed,self.recipe,source_parser=parser or(lambda raw,route:[self.missing]))

    def publish_first_paper(self):
        """Make the first candidate a published snapshot with one cached gap."""
        aid=self.aids[0]
        metadata=dict(title='A test paper',year='2026',layers='llm',source_url='https://example.test/paper')
        point=self.qdrant.points[self.pid]
        point['payload'].update(metadata)
        with closing(sqlite3.connect(self.state_path))as conn:
            for name in metadata:
                conn.execute('ALTER TABLE papers ADD COLUMN '+name+' TEXT')
            conn.execute('UPDATE papers SET title=?,year=?,layers=?,source_url=? WHERE arxiv_id=?',(*metadata.values(),aid))
            ensure_publication_schema(conn)
            artifact=dict(points=[json.loads(json.dumps(point))],metadata=metadata,cache_path=str(self.cache_path.resolve()))
            parent=restore.snapshots._snapshot_revision(artifact,restore.labels._sha(self.cache_before))
            point['payload']['publication_revision']=parent
            artifact['points'][0]['payload']['publication_revision']=parent
            parent_path=self.root/'published-parent.json'
            parent_path.write_bytes(restore.labels._json_bytes(artifact))
            conn.execute("INSERT INTO paper_publications(arxiv_id,revision,kind,artifact_path,cache_sha256,state) VALUES(?,?,'snapshot',?,?,'published')",
                (aid,parent,str(parent_path),restore.labels._sha(self.cache_before)))
            conn.commit()
        with closing(sqlite3.connect(self.fts_path))as conn:
            ensure_catalog(conn)
            conn.execute('UPDATE pipeline_fts_scope SET complete=1 WHERE singleton=1')
            conn.execute("INSERT OR IGNORE INTO pipeline_fts_papers VALUES('chunks',?)",(aid,))
            conn.execute("INSERT OR IGNORE INTO pipeline_fts_papers VALUES('chunks',?)",(self.aids[1],))
            conn.commit()
        with closing(sqlite3.connect(self.projection))as conn:
            conn.execute('UPDATE papers SET cache_backed_missing_current_count=0,cache_backed_missing_current_indices_json=? WHERE paper_id=?',('[]',self.aids[1]))
            conn.execute("UPDATE meta SET value=? WHERE key='census_identity'",(driver._identity(self.census),))
            conn.commit()
        self.args.publication_maintenance=nullcontext
        self.args.section_cache_dir=self.section_dir
        return parent

    def assert_reader_exposes_published(self,point):
        api_path=Path(__file__).resolve().parents[2]/'api'/'main.py'
        tree=ast.parse(api_path.read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_filter_publication_points')
        with closing(sqlite3.connect(self.state_path))as conn:
            scope={'sqlite3':sqlite3,'STATE_DB_PATH':str(self.state_path),'_ro_conn':lambda _:conn}
            exec(compile(ast.Module(body=[node],type_ignores=[]),str(api_path),'exec'),scope)
            self.assertEqual(scope['_filter_publication_points']([point]),[point])

    def test_published_cache_gap_uses_snapshot_and_reader_visibility_then_resumes_without_embedding(self):
        parent=self.publish_first_paper()
        result=self.run_driver()
        with closing(sqlite3.connect(self.args.run_db))as conn:
            detail=conn.execute('SELECT detail FROM targets').fetchone()
        self.assertEqual(result['added_this_run'],1,(result,detail))
        self.assertEqual(self.calls,1)
        point_id=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aids[0]+'#1'))
        point=self.qdrant.points[point_id]
        with closing(sqlite3.connect(self.state_path))as conn:
            published=conn.execute("SELECT revision FROM paper_publications WHERE arxiv_id=? AND kind='snapshot' AND state='published'",(self.aids[0],)).fetchone()[0]
        self.assertNotEqual(published,parent)
        self.assertEqual(point['payload']['publication_revision'],published)
        self.assert_reader_exposes_published(point)
        result=self.run_driver()
        self.assertEqual(result['added_this_run'],0)
        self.assertEqual(result['verified_points'],1)
        self.assertEqual(self.calls,1)

    def test_interrupted_published_addition_rolls_back_then_resumes(self):
        self.publish_first_paper()
        self.qdrant.crash_after_upsert=True
        with self.assertRaises(fixtures.fixtures.SimulatedCrash):
            self.run_driver()
        point_id=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aids[0]+'#1'))
        self.assertIn(point_id,self.qdrant.points)
        journal=next(self.args.journal_dir.glob('*/*.json'))
        self.assertEqual(json.loads(journal.read_bytes())['phase'],'applying')
        result=self.run_driver()
        self.assertEqual(result['verified_points'],1)
        self.assertEqual(result['added_this_run'],1)
        self.assertEqual(len([p for p in self.qdrant.points.values() if p['payload']['arxiv_id']==self.aids[0]]),2)
        self.assertEqual(self.calls,2)

    def test_all_candidate_papers_then_completed_skip(self):
        result=self.run_driver()
        self.assertEqual(result['added_this_run'],2)
        self.assertEqual(result['verified_points'],2)
        self.assertEqual(result['pending_points'],0)
        self.assertEqual(result['unresolved_points'],0)
        self.assertEqual(self.calls,2)
        result=self.run_driver(lambda raw,route:(_ for _ in()).throw(AssertionError('completed paper parsed again')))
        self.assertEqual(result['added_this_run'],0)
        self.assertEqual(result['verified_points'],2)
        self.assertEqual(self.calls,2)
        paths=list(self.args.journal_dir.glob('*/*.json'))
        self.assertEqual(len(paths),2)

    def test_source_subset_then_full_run_reuses_ledger(self):
        with closing(sqlite3.connect(self.census))as conn:
            conn.execute("UPDATE papers SET fulltext_source='pdf' WHERE paper_id=?",(self.aids[1],));conn.commit()
        with closing(sqlite3.connect(self.projection))as conn:
            conn.execute("UPDATE meta SET value=? WHERE key='census_identity'",(driver._identity(self.census),));conn.commit()
        self.args.allowed_sources=['latex']
        result=self.run_driver()
        self.assertEqual(result['allowed_sources'],['latex'])
        self.assertEqual(result['candidate_papers'],1)
        self.assertEqual(result['added_this_run'],1)
        with closing(sqlite3.connect(self.args.run_db))as conn:
            self.assertEqual(conn.execute('SELECT DISTINCT paper_id FROM targets').fetchall(),[(self.aids[0],)])
        self.args.allowed_sources=None
        result=self.run_driver()
        self.assertIsNone(result['allowed_sources'])
        self.assertEqual(result['candidate_papers'],2)
        self.assertEqual(result['added_this_run'],1)
        self.assertEqual(result['verified_points'],2)
        self.assertEqual(self.calls,2)

    def test_unproven_targets_defer_then_explicit_retry(self):
        result=self.run_driver(lambda raw,route:[])
        self.assertEqual(result['unresolved_points'],2);self.assertEqual(self.calls,0)
        result=self.run_driver();self.assertEqual(result['added_this_run'],0)
        self.args.retry_failed=True
        result=self.run_driver();self.assertEqual(result['added_this_run'],2)

    def test_changed_source_split_restores_original_text_and_identity(self):
        larger=dict(self.missing,text='Source paragraph before. '+self.missing['text']+' Source paragraph after.')
        result=self.run_driver(lambda raw,route:[larger])
        self.assertEqual(result['verified_points'],2)
        for aid in self.aids:
            pid=str(uuid.uuid5(uuid.NAMESPACE_URL,aid+'#1'))
            self.assertEqual(self.qdrant.points[pid]['payload']['text'],self.missing['text'])

    def test_interrupted_operation_is_recovered_before_dispatch(self):
        self.qdrant.crash_after_upsert=True
        with self.assertRaises(fixtures.fixtures.SimulatedCrash):self.run_driver()
        result=self.run_driver()
        self.assertEqual(result['verified_points'],2)
        self.assertEqual(len(self.qdrant.points),4)

    def test_verified_label_receipt_allows_only_label_changed_cache(self):
        self.args.label_run_db=self.root/'label-run.sqlite'
        with closing(sqlite3.connect(self.args.label_run_db))as conn:
            conn.execute('CREATE TABLE papers(paper_id TEXT PRIMARY KEY,status TEXT,journal_path TEXT)')
            for aid in self.aids:
                chunks=json.loads(self.cache_before);chunks[1]['section_title']='Verified results'
                after=restore.labels._json_bytes(chunks)
                (self.cache_dir/(aid+'.json')).write_bytes(after)
                import base64
                journal=self.root/(aid+'.label.json')
                journal.write_text(json.dumps(dict(kind='section-label-repair',phase='applied',papers=[dict(arxiv_id=aid,cache_before=base64.b64encode(self.cache_before).decode(),cache_after=base64.b64encode(after).decode())])))
                conn.execute('INSERT INTO papers VALUES(?,?,?)',(aid,'verified',str(journal)))
            conn.commit()
        result=self.run_driver()
        self.assertEqual(result['added_this_run'],2)
        for aid in self.aids:
            pid=str(uuid.uuid5(uuid.NAMESPACE_URL,aid+'#1'))
            self.assertEqual(self.qdrant.points[pid]['payload']['section_title'],'Verified results')

    def test_budget_does_not_dispatch_new_operation(self):
        with patch.object(driver.time,'monotonic',side_effect=[0,100,100]):
            result=self.run_driver()
        self.assertEqual(result['added_this_run'],0)
        self.assertEqual(result['pending_points'],2)

    def test_two_batches_same_paper_keep_previously_added_baseline(self):
        extra=dict(self.missing,chunk_index=2,text='A separate source-proven table: precision 0.9 and recall 0.8.')
        cache=restore.labels._json_bytes([self.chunk,self.missing,extra])
        self.cache_path.write_bytes(cache)
        with closing(sqlite3.connect(self.census))as conn:
            conn.execute('UPDATE papers SET cache_sha256=?WHERE paper_id=?',(restore.labels._sha(cache),self.aid));conn.commit()
        with closing(sqlite3.connect(self.projection))as conn:
            conn.execute("UPDATE meta SET value=?WHERE key='census_identity'",(driver._identity(self.census),))
            conn.execute('UPDATE papers SET cache_sha256=?,cache_backed_missing_current_count=2,cache_backed_missing_current_indices_json=?WHERE paper_id=?',(restore.labels._sha(cache),'[1,2]',self.aid));conn.commit()
        self.args.batch_size=1
        result=self.run_driver(lambda raw,route:[self.missing,extra])
        self.assertEqual(result['added_this_run'],3)
        self.assertEqual(result['verified_points'],3)
        self.assertEqual(result['unresolved_points'],0)
        self.assertEqual(len(self.qdrant.points),5)

    def test_managed_paper_is_explicitly_deferred(self):
        with closing(sqlite3.connect(self.state_path))as conn:
            conn.execute('CREATE TABLE paper_active(arxiv_id TEXT PRIMARY KEY)')
            conn.execute('INSERT INTO paper_active VALUES(?)',(self.aids[1],));conn.commit()
        result=self.run_driver()
        self.assertEqual(result['added_this_run'],1)
        self.assertEqual(result['unresolved_points'],1)
        self.assertEqual(result['statuses']['deferred'],1)
