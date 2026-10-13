import json
import sqlite3
import unittest
import uuid
import hashlib
from contextlib import closing
from unittest.mock import patch

from ops import restore_cached_points as legacy, replace_published_labels as snapshots
from ops.tests import test_restore_cached_points as legacy_fixtures
from ops.tests import test_replace_published_labels as snapshot_fixtures
from ops.tests import test_repair_section_labels as base_fixtures
from ops.tests.test_repair_section_labels import SimulatedCrash


class WithdrawalCases:
    tearDown=base_fixtures.SectionLabelRepairTests.tearDown

    def withdraw(self,ids=None,policy=None,parser=None):
        ids=ids or [self.target]
        with patch('pipeline.extractor.should_embed',side_effect=policy or (lambda c:False)):
            return self.module.prepare_withdrawal_plan(self.aid,ids,source_path=self.source_path,
                cache_path=self.cache_path,expected_source_sha256=legacy.labels._sha(self.source_path.read_bytes()),
                expected_cache_sha256=legacy.labels._sha(self.cache_path.read_bytes()),
                state_db=self.state_path,fts_db=self.fts_path,rowmap_db=self.rowmap,journal_path=self.journal,
                qdrant=self.qdrant,expected_policy_sha256=legacy.WITHDRAWAL_POLICY_SHA256,
                origin_journals={pid:self.origin for pid in ids},source_route='latex',
                section_cache_dir=self.section_dir,source_parser=parser or(lambda raw,route:[self.missing]))

    def operation(self,name):
        with patch('pipeline.extractor.should_embed',return_value=False):
            return getattr(self.module,name)(self.journal,qdrant=self.qdrant,
                **({'maintenance':True} if self.module is snapshots else {}))

    def database_image(self):
        with closing(sqlite3.connect(self.fts_path)) as c:
            return {table:list(c.execute('SELECT * FROM '+table+' ORDER BY 1,2')) for table in
                ('chunks','pipeline_fts_points','pipeline_fts_rows','dtox_cached_point_receipts')}

    def test_selective_withdrawal_and_exact_rollback(self):
        points=json.loads(json.dumps(self.qdrant.points));rows=self.database_image();cache=self.cache_path.read_bytes()
        self.withdraw();self.operation('apply_plan')
        self.assertEqual(set(self.qdrant.points),set(points)-{self.target})
        self.assertEqual(self.cache_path.read_bytes(),cache)
        with closing(sqlite3.connect(self.fts_path)) as c:
            self.assertIsNone(c.execute('SELECT row_id FROM dtox_cached_point_receipts WHERE point_id=?',(self.target,)).fetchone())
        self.operation('rollback_plan')
        self.assertEqual(self.qdrant.points,points);self.assertEqual(self.database_image(),rows)

    def test_crash_after_delete_recovers(self):
        before=json.loads(json.dumps(self.qdrant.points));rows=self.database_image();self.withdraw()
        write=legacy._write_removed_points
        def crash(client,collection,removed,forward):
            write(client,collection,removed,forward)
            if forward:raise SimulatedCrash('lost delete response')
        with patch.object(legacy,'_write_removed_points',crash):
            with self.assertRaises(SimulatedCrash):self.operation('apply_plan')
        self.operation('rollback_plan')
        self.assertEqual(self.qdrant.points,before);self.assertEqual(self.database_image(),rows)

    def test_crash_after_fts_commit_recovers(self):
        before=json.loads(json.dumps(self.qdrant.points));rows=self.database_image();self.withdraw()
        verify=self.module._verify
        def crash(plan,client,mode):
            verify(plan,client,mode)
            if mode=='after':raise SimulatedCrash('after FTS commit')
        with patch.object(self.module,'_verify',crash):
            with self.assertRaises(SimulatedCrash):self.operation('apply_plan')
        self.operation('rollback_plan')
        self.assertEqual(self.qdrant.points,before);self.assertEqual(self.database_image(),rows)

    def test_original_frozen_point_rejected(self):
        with self.assertRaisesRegex(legacy.RepairError,'original frozen baseline'):
            self.withdraw([self.pid])
        self.assertFalse(self.journal.exists())

    def test_mixed_included_and_excluded_points_rejected(self):
        with self.assertRaisesRegex(legacy.RepairError,'currently excluded'):
            self.withdraw([self.target,self.pid],policy=lambda c:c['chunk_index']==0)
        self.assertFalse(self.journal.exists())

    def test_accepted_origin_proof_survives_nonreproducing_current_parser(self):
        plan=self.withdraw(parser=lambda raw,route:[])
        removed=plan['paper']['removed'] if self.module is snapshots else plan['removed']
        self.assertEqual(removed[0]['source_proof'],'accepted-source-bound-origin')

    def test_origin_source_mismatch_rejected(self):
        origin=json.loads(self.origin.read_bytes())
        if self.module is snapshots:origin['paper']['addition_proof']['expected_source_sha256']='0'*64
        else:origin['source_sha256']='0'*64
        self.origin.write_bytes(legacy.labels._json_bytes(origin))
        with self.assertRaisesRegex(legacy.RepairError,'origin receipt/source binding'):
            self.withdraw()

    def test_disabled_policy_blocks_deletion(self):
        self.withdraw();before=json.loads(json.dumps(self.qdrant.points))
        with patch('pipeline.extractor.BOILERPLATE_FILTER',False):
            with self.assertRaisesRegex(legacy.RepairError,'exact active boilerplate'):
                self.operation('apply_plan')
        self.assertEqual(self.qdrant.points,before)

    def test_receipt_drift_blocks_deletion(self):
        self.withdraw();before=json.loads(json.dumps(self.qdrant.points))
        with closing(sqlite3.connect(self.fts_path)) as c:
            c.execute("UPDATE dtox_cached_point_receipts SET operation_id='other-operation' WHERE point_id=?",(self.target,));c.commit()
        with self.assertRaisesRegex(legacy.RepairError,'receipt/FTS drift'):
            self.operation('apply_plan')
        self.assertEqual(self.qdrant.points,before)


class LegacyWithdrawalTests(WithdrawalCases,unittest.TestCase):
    def setUp(self):
        legacy_fixtures.RestoreCachedPointsTests.setUp(self)
        with closing(sqlite3.connect(self.fts_path)) as c:
            c.execute("UPDATE chunks SET element_type='prose',layers='' WHERE point_id=?",(self.pid,));c.commit()
        self.origin=self.journal
        legacy_fixtures.RestoreCachedPointsTests.prepare(self)
        legacy.apply_plan(self.origin,qdrant=self.qdrant)
        with closing(sqlite3.connect(self.fts_path)) as c:c.execute('UPDATE pipeline_fts_scope SET complete=1');c.commit()
        self.target=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aid+'#1'))
        self.journal=self.root/'withdrawal.json';self.module=legacy


class SnapshotWithdrawalTests(WithdrawalCases,unittest.TestCase):
    prepare_addition_fixture=snapshot_fixtures.PublishedLabelReplacementTests.prepare_addition_fixture
    prepare_addition=snapshot_fixtures.PublishedLabelReplacementTests.prepare_addition
    def setUp(self):
        snapshot_fixtures.PublishedLabelReplacementTests.setUp(self)
        self.prepare_addition_fixture()
        canonical=self.parent_path.parent/(hashlib.sha256(self.aid.encode()).hexdigest()+'-'+self.parent+'.json')
        self.parent_path.rename(canonical);self.parent_path=canonical
        with closing(sqlite3.connect(self.state_path)) as c:
            c.execute('UPDATE paper_publications SET artifact_path=?',(str(canonical),));c.commit()
        self.origin=self.journal
        self.prepare_addition();snapshots.apply_plan(self.origin,qdrant=self.qdrant)
        self.target=str(uuid.uuid5(uuid.NAMESPACE_URL,self.aid+'#1'))
        self.journal=self.root/'withdrawal.json';self.module=snapshots

    def test_current_accepted_labels_preserved(self):
        self.manifest_path.unlink()
        with closing(sqlite3.connect(self.fts_path)) as c:
            rid=c.execute('SELECT row_id FROM pipeline_fts_points WHERE point_id=?',(self.target,)).fetchone()[0]
        label_journal=self.root/'new-labels.json'
        snapshots.prepare_plan([dict(arxiv_id=self.aid,point_id=self.target,rowid=rid,
            section_title='Revised',section_type='method')],state_db=self.state_path,fts_db=self.fts_path,
            cache_dir=self.cache_dir,section_cache_dir=self.section_dir,journal_path=label_journal,qdrant=self.qdrant,
            expected_indexed_indices_by_paper={self.aid:[0,1]})
        snapshots.apply_plan(label_journal,qdrant=self.qdrant)
        points=json.loads(json.dumps(self.qdrant.points));cache=self.cache_path.read_bytes()
        self.withdraw();self.operation('apply_plan');self.operation('rollback_plan')
        self.assertEqual(self.qdrant.points,points);self.assertEqual(self.cache_path.read_bytes(),cache)

    def test_snapshot_withdrawal_requires_maintenance(self):
        self.withdraw()
        with self.assertRaisesRegex(legacy.RepairError,'maintenance'):
            snapshots.apply_plan(self.journal,qdrant=self.qdrant)


if __name__=='__main__':unittest.main()
