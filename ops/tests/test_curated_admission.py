"""Offline checks for bounded rejected-paper admission and scoped reconciliation."""
import contextlib
import types
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops.corpus_quality import curated_admission as admission
from pipeline import publication


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.base=self.root/'queue'
        c=sqlite3.connect(self.root/'state.db')
        c.executescript("CREATE TABLE papers(arxiv_id TEXT PRIMARY KEY,title TEXT,status TEXT,twin_of TEXT,fulltext_source TEXT,parse_status TEXT);"
                        "CREATE TABLE completion_history(arxiv_id TEXT,first_known INTEGER,completed INTEGER);"
                        "CREATE TABLE paper_publications(arxiv_id TEXT);CREATE TABLE paper_index_sync(arxiv_id TEXT);")
        c.executemany("INSERT INTO papers VALUES(?,?,'rejected',NULL,NULL,NULL)",[('2405.00549','Paper one'),('2310.11331','Paper two')]);c.commit();c.close()
        f=sqlite3.connect(self.root/'papers_fts.db');f.execute('CREATE VIRTUAL TABLE papers_fts USING fts5(arxiv_id UNINDEXED,title)');f.close()
        self.runtime=dict(fresh=Mock(FreshSourceError=ValueError,FreshSourceDependencyError=ImportError))

    def test_preparation_preserves_state_and_keeps_valid_document_after_one_defer(self):
        before=(self.root/'state.db').read_bytes()
        def acquire(pid,*args,**kwargs):
            if pid=='2310.11331':raise admission.worker.DeferredPaper('source_not_approved')
            return dict(body_quality_candidate=True),dict()
        with patch.object(admission.worker,'_load_runtime',return_value=self.runtime), patch.object(admission,'_no_owned_indexes'), patch.object(admission.worker,'_source_binding'), patch.object(admission.worker,'_acquire_one',side_effect=acquire):
            result=admission.prepare(self.base,self.root,['2405.00549','2310.11331'])
        self.assertEqual(result['prepared'],['2405.00549'])
        self.assertEqual(result['deferred'][0]['paper_id'],'2310.11331')
        self.assertFalse(result['corpus_writes'])
        self.assertEqual(before,(self.root/'state.db').read_bytes())
        self.assertEqual(len(json.loads((self.base/'queue.json').read_text())['rows']),1)

    def test_new_paper_preparation_pins_metadata_without_creating_state(self):
        metadata=dict(paper_id='wp:alpenglow',title='Solana Alpenglow Consensus',year=2026,authors=['Author'],abstract='Abstract',fresh_url='https://publisher.example/paper.pdf',edition='sha256:'+'a'*64,publisher_url='https://publisher.example/research')
        manifest=self.root/'new.json';manifest.write_text(json.dumps([metadata]))
        with patch.object(admission.worker,'_load_runtime',return_value=self.runtime),patch.object(admission,'_no_owned_indexes'),patch.object(admission.worker,'_source_binding'),patch.object(admission.worker,'_acquire_one',return_value=(dict(body_quality_candidate=True),{})) as acquire:
            result=admission.prepare(self.base,self.root,[],manifest)
        self.assertEqual(result['prepared'],['wp:alpenglow'])
        self.assertEqual(acquire.call_args.kwargs['source_request']['fresh_version'],metadata['edition'])
        c=sqlite3.connect(self.root/'state.db');self.addCleanup(c.close)
        self.assertIsNone(c.execute("SELECT 1 FROM papers WHERE arxiv_id='wp:alpenglow'").fetchone())
        receipt=json.loads((self.base/'wp:alpenglow.json').read_text())
        self.assertEqual(receipt['declared_metadata'],metadata)

    def test_quality_rejection_is_deferred_and_empty_queue_is_no_work(self):
        with patch.object(admission.worker,'_load_runtime',return_value=self.runtime),patch.object(admission,'_no_owned_indexes'),patch.object(admission.worker,'_source_binding'),patch.object(admission.worker,'_acquire_one',return_value=(dict(body_quality_candidate=False),{})):
            result=admission.prepare(self.base,self.root,['2405.00549'])
        self.assertEqual(result['prepared'],[])
        self.assertTrue(admission.run(self.base,self.root,None)['no_work'])

    def test_alias_inventory_uses_declared_root_with_separately_located_state(self):
        physical=self.root/'physical';physical.mkdir()
        (self.root/'state.db').rename(physical/'state.db')
        conn=sqlite3.connect(physical/'state.db');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
        self.assertEqual(admission._duplicate_aliases(conn,'2405.00549','Paper one',self.root),[])

    def test_previous_completion_is_never_reset(self):
        c=sqlite3.connect(self.root/'state.db');c.execute("INSERT INTO completion_history VALUES('2405.00549',0,1)");c.commit();c.close()
        with patch.object(admission.worker,'_load_runtime',return_value=self.runtime),patch.object(admission.worker,'_acquire_one') as acquire:
            with self.assertRaisesRegex(admission.worker.WorkerError,'state ownership'):
                admission.prepare(self.base,self.root,['2405.00549'])
            acquire.assert_not_called()

    def test_shared_dependency_failure_does_not_become_document_skip(self):
        with patch.object(admission.worker,'_load_runtime',return_value=self.runtime),patch.object(admission,'_no_owned_indexes'),patch.object(admission.worker,'_acquire_one',side_effect=ImportError('native unavailable')):
            with self.assertRaises(ImportError):admission.prepare(self.base,self.root,['2405.00549'])
        self.assertFalse((self.base/'queue.json').exists())

    def _cache_failure_resume(self, new=False):
        from pipeline.tests.test_parse_safety import ParseSafetyTests
        fixture=ParseSafetyTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        root=fixture.root;base=root/'admission';base.mkdir()
        fixture.conn.execute("UPDATE papers SET status='rejected',fulltext_source=NULL,parse_status=NULL")
        fixture.conn.execute('DELETE FROM completion_history');fixture.conn.commit()
        state=dict(fixture.paper())
        if new:
            fixture.conn.execute('DELETE FROM papers');fixture.conn.commit()
            fixture.paper_id='wp:alpenglow'
            state=dict(arxiv_id=fixture.paper_id,title='Solana Alpenglow Consensus',year=2026,
                       abstract='Verified paper abstract',source_url='https://publisher.example/paper.pdf',layers='web3',status='new')
        for path in (fixture.source_path,fixture.cache_path,fixture.section_path):path.unlink()
        raw=fixture.source_bytes;source=base/'source.tex';source.write_bytes(raw)
        result=dict(canonical_id=fixture.paper_id,source_path=str(source),source_sha256=admission.hashlib.sha256(raw).hexdigest(),
                    chunks=fixture.chunks,sections=fixture.canonical_sections,parser_mode='pylatexenc',
                    source_route='latex',source_binding=dict(original_kind='rawTexarchive'))
        if new:
            result.update(parser_mode='markdown',source_route='paper_markdown',source_binding=dict(original_kind='PDF'))
        digest=admission._write_once(base/(fixture.paper_id+'.json'),dict(paper_id=fixture.paper_id,state=state,result=result))
        admission._write_once(base/'queue.json',dict(schema=1,root=str(root),rows=[dict(paper_id=fixture.paper_id,receipt=fixture.paper_id+'.json',sha256=digest)]))
        fixture.worker.get_conn=lambda:fixture.conn
        fixture.worker.requests=Mock();fixture.worker.embed_texts_batch=Mock(return_value=[[0.0]*384])
        cache=Mock(side_effect=OSError('injected cache publication failure'));fixture.worker._write_parse_cache=cache
        def embedded(*args,**kwargs):
            fixture.conn.execute("UPDATE papers SET status='done'");fixture.conn.execute('UPDATE completion_history SET completed=1');fixture.conn.commit()
        fixture.worker.embed_step=Mock(side_effect=embedded)
        runtime=dict(document_cache=Mock(),worker=Mock())
        with patch.object(admission.worker,'_locks',return_value=contextlib.nullcontext()),patch.object(admission.worker,'_load_runtime',return_value=runtime),patch.object(admission.worker,'_runtime_preflight'),patch.object(admission,'_service',return_value=fixture.worker),patch.object(admission,'_no_owned_indexes'),patch.object(admission,'_duplicate_aliases',return_value=[]),patch.dict('sys.modules',{'coarse_index':types.SimpleNamespace(embed_coarse_batch=lambda *args:[[0.0]*768])}):
            # The runner normally owns its connection; keep this fixture connection for restart.
            proxy=Mock(wraps=fixture.conn);proxy.close=Mock();fixture.worker.get_conn=lambda:proxy
            with self.assertRaisesRegex(OSError,'cache publication failure'):
                admission.run(base,root,'http://127.0.0.1:8090/embed_batch')
            self.assertEqual(fixture.paper()['status'],'fulltext_fetched')
            self.assertEqual(tuple(fixture.conn.execute('SELECT first_known,completed FROM completion_history').fetchone()),(1,0))
            cache.side_effect=None
            report=admission.run(base,root,'http://127.0.0.1:8090/embed_batch')
            self.assertEqual(report['pending'],[])
            if new:
                self.assertEqual(fixture.paper()['fulltext_source'],'source-pdf')
                self.assertEqual(fixture.paper()['parser_mode'],'markdown')
            self.assertEqual(fixture.conn.execute('SELECT count(*) FROM completion_history').fetchone()[0],1)

    def test_cache_failure_after_rejected_admission_resumes_without_resetting_ownership(self):
        self._cache_failure_resume()

    def test_cache_failure_after_new_paper_transaction_resumes_same_canonical_id(self):
        self._cache_failure_resume(new=True)

    def test_unpinned_endpoint_and_credential_url_rejected_before_import(self):
        for endpoint in (None,'http://example.com/embed_batch','http://user:pass@127.0.0.1:8090/embed_batch'):
            with self.subTest(endpoint=endpoint),self.assertRaises(admission.worker.WorkerError):
                admission._service(self.root,{},endpoint)


class ScopedPublicationTests(unittest.TestCase):
    def test_embed_scope_excludes_other_chunked_papers_and_scopes_retry(self):
        from pipeline.tests.test_parse_safety import ParseSafetyTests
        fixture=ParseSafetyTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        self.assertEqual(fixture.parse(),1)
        fixture.worker.embed_step(fixture.conn,fixture.session,paper_ids=['other'])
        self.assertEqual(fixture.paper()['status'],'chunked')
        fixture.upsert.assert_not_called()
        fixture.worker._reconcile_index_sync.assert_called_once_with(fixture.conn,fixture.session,paper_ids=('other',))

    def test_scoped_reconciliation_never_visits_another_pending_publication(self):
        c=sqlite3.connect(':memory:');self.addCleanup(c.close);c.row_factory=sqlite3.Row
        publication.ensure_publication_schema(c)
        c.executemany("INSERT INTO paper_publications(arxiv_id,revision,kind,state,retry_after,artifact_path,cache_sha256) VALUES(?,?,'snapshot','ready',0,'path','hash')",[('ours','a'),('other','b')]);c.commit()
        with patch.object(publication,'publish_snapshot',return_value=True) as publish:
            self.assertEqual(publication.reconcile_publications(c,'.',Mock(),Mock(),Mock(),Mock(),limit=10,paper_ids=['ours']),1)
        self.assertEqual(publish.call_args.args[1]['paper_id'],'ours')
        with patch.object(publication,'publish_snapshot') as publish:
            self.assertEqual(publication.reconcile_publications(c,'.',Mock(),Mock(),Mock(),Mock(),paper_ids=[]),0)
            publish.assert_not_called()


if __name__=='__main__':unittest.main()
