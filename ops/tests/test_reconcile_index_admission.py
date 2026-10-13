import json
from pathlib import Path
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from ops.corpus_quality import reconcile_index_admission as runner
from ops.tests import test_withdraw_cached_points as fixtures
from ops.tests.test_repair_section_labels import SimulatedCrash


class ReconcileIndexAdmissionTests(unittest.TestCase):
    def setUp(self):
        fixtures.LegacyWithdrawalTests.setUp(self)
        self.global_ledger=self.root/'point-run.sqlite';self.audit_db=self.root/'audit.sqlite';self.census=self.root/'census.sqlite'
        with closing(sqlite3.connect(self.global_ledger)) as c:
            c.execute('CREATE TABLE targets(paper_id TEXT,chunk_index INTEGER,status TEXT,attempt INTEGER,journal_path TEXT,detail TEXT,PRIMARY KEY(paper_id,chunk_index))')
            c.execute("INSERT INTO targets VALUES(?,1,'verified',2,?,'accepted origin')",(self.aid,str(self.origin)));c.commit()
        cache_sha=runner.legacy.labels._sha(self.cache_path.read_bytes());source_sha=runner.legacy.labels._sha(self.source_path.read_bytes())
        canonical=self.root/(self.aid+'.tex');canonical.write_bytes(self.source_path.read_bytes())
        with closing(sqlite3.connect(self.census)) as c:
            c.execute('CREATE TABLE papers(paper_id TEXT,source_location TEXT,cache_sha256 TEXT,source_sha256 TEXT,fulltext_source TEXT,parser_mode TEXT)')
            c.execute('INSERT INTO papers VALUES(?,?,?,?,?,?)',(self.aid,'fulltext_cache',cache_sha,source_sha,'pdf','latex'));c.commit()
        with closing(sqlite3.connect(self.audit_db)) as c:
            c.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
            c.executemany('INSERT INTO meta VALUES(?,?)',[('full_run_complete','0'),('scoped_run_complete','1')])
            c.execute('CREATE TABLE papers(paper_id TEXT,cache_sha256 TEXT,source_sha256 TEXT,policy_sha256 TEXT)')
            c.execute('INSERT INTO papers VALUES(?,?,?,?)',(self.aid,cache_sha,source_sha,runner.audit.POLICY_SHA256))
            c.execute('CREATE TABLE candidates(paper_id TEXT,chunk_index INTEGER,ledger_status TEXT,outcome TEXT,receipt_state TEXT,source_occurrences INTEGER,text_sha256 TEXT,element_type TEXT)')
            c.execute("INSERT INTO candidates VALUES(?,1,'verified','EXCLUDED','valid',1,?,?)",(self.aid,runner.legacy.labels._sha(self.missing['text'].encode()),self.missing['element_type']));c.commit()
        self.args=SimpleNamespace(base=self.root,audit_db=self.audit_db,census_db=self.census,point_run_db=self.global_ledger,
            rowmap_db=self.rowmap,state_db=self.state_path,fts_db=self.fts_path,cache_dir=self.cache_dir,
            section_cache_dir=self.section_dir,fulltext_cache_dir=self.root,latex_cache_dir=self.root,label_run_db=None,
            operations_db=self.root/'operations.sqlite',status_file=self.root/'controller-status.json',paper=[self.aid],canary=True,max_papers=1)
        self.worker=SimpleNamespace(UNIT='test-api',systemctl=lambda *args:'inactive',health=lambda:None)

    tearDown=fixtures.base_fixtures.SectionLabelRepairTests.tearDown

    def execute(self,**kwargs):
        with patch('pipeline.extractor.should_embed',return_value=False):
            return runner.run(self.args,self.qdrant,self.worker,**kwargs)

    def test_crash_between_owner_apply_and_global_mark_recovers_without_second_delete(self):
        baseline=json.loads(json.dumps(self.qdrant.points[self.pid]))
        def fault(phase):raise SimulatedCrash(phase)
        with patch.object(self.qdrant,'delete',wraps=self.qdrant.delete) as deletion:
            with self.assertRaises(SimulatedCrash):self.execute(fault=fault)
            self.assertNotIn(self.target,self.qdrant.points)
            with closing(sqlite3.connect(self.global_ledger)) as c:
                self.assertEqual(c.execute('SELECT status,attempt FROM targets').fetchone(),('verified',2))
            result=self.execute()
            self.assertEqual(deletion.call_count,1)
            self.assertEqual(result['operations'],{'excluded':1})
            self.execute()  # terminal operation verifies absence without reapplying
            self.assertEqual(deletion.call_count,1)
        self.assertEqual(self.qdrant.points[self.pid],baseline)
        with closing(sqlite3.connect(self.global_ledger)) as c:
            status,attempt,journal,detail=c.execute('SELECT status,attempt,journal_path,detail FROM targets').fetchone()
            self.assertEqual((status,attempt),('excluded',3))
            self.assertEqual(json.loads(detail)['origin_journal'],str(self.origin))
            self.assertNotEqual(journal,str(self.origin))
        with closing(sqlite3.connect(self.args.operations_db)) as c:
            before=json.loads(c.execute('SELECT before_targets FROM operations').fetchone()[0])
            self.assertEqual((before[0]['status'],before[0]['attempt'],before[0]['detail']),('verified',2,'accepted origin'))

    def test_incomplete_audit_is_rejected_before_primary_write(self):
        with closing(sqlite3.connect(self.audit_db)) as c:c.execute("UPDATE meta SET value='0'");c.commit()
        with self.assertRaisesRegex(RuntimeError,'completed admission audit'):
            self.execute()
        self.assertIn(self.target,self.qdrant.points)

    def test_global_target_change_before_apply_prevents_deletion(self):
        prepare=runner.prepare_operation
        def changed(*args,**kwargs):
            operation=prepare(*args,**kwargs)
            with closing(sqlite3.connect(self.global_ledger)) as c:
                c.execute("UPDATE targets SET status='failed'");c.commit()
            return operation
        with patch.object(runner,'prepare_operation',changed):result=self.execute()
        self.assertEqual(result['operations'],{'failed':1})
        self.assertIn(self.target,self.qdrant.points)

    def test_source_unverified_exclusion_is_never_selected(self):
        with closing(sqlite3.connect(self.audit_db)) as c:
            c.execute("UPDATE candidates SET outcome='EXCLUDED_SOURCE_UNVERIFIED'");c.commit()
        result=self.execute()
        self.assertEqual(result['operations'],{})
        self.assertIn(self.target,self.qdrant.points)

    def test_own_recovery_precedes_new_audit_completion_gate(self):
        with self.assertRaises(SimulatedCrash):self.execute(fault=lambda phase:(_ for _ in ()).throw(SimulatedCrash(phase)))
        with closing(sqlite3.connect(self.audit_db)) as c:c.execute("UPDATE meta SET value='0'");c.commit()
        with self.assertRaisesRegex(RuntimeError,'completed admission audit'):self.execute()
        with closing(sqlite3.connect(self.global_ledger)) as c:
            self.assertEqual(c.execute('SELECT status FROM targets').fetchone(),('excluded',))

    def test_legacy_recovery_required_closes_and_later_restores_reader_gate(self):
        state={'active':True}
        def systemctl(*args):
            if args[0]=='show':return 'active' if args[1]=='test-api' and state['active'] else 'inactive'
            state['active']=args[0]=='start'
        self.worker.systemctl=systemctl
        with patch.object(runner,'mark_global',side_effect=RuntimeError('interrupted ledger boundary')):
            with self.assertRaises(runner.RecoveryRequired):self.execute()
        self.assertFalse(state['active'])
        marker=self.root/'index-admission-maintenance.json'
        self.assertTrue(json.loads(marker.read_text())['previously_active'])
        self.execute()
        self.assertTrue(state['active'])
        self.assertEqual(json.loads(marker.read_text())['phase'],'restored')


if __name__=='__main__':unittest.main()
