import contextlib
import json
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path

from ops.corpus_quality import replace_documents as worker


class BatchTests(unittest.TestCase):
    def test_preserves_exact_existing_text_and_encodes_only_changed_text(self):
        paper = 'paper'
        old_id = str(uuid.uuid5(uuid.NAMESPACE_URL, paper + '#0'))
        old_vector = [384 ** -0.5] * 384
        existing = {old_id: dict(id=old_id, vector=old_vector,
                    payload=dict(arxiv_id=paper, text='unchanged'))}
        targets = [dict(chunk_index=0, text='unchanged'), dict(chunk_index=1, text='new')]
        calls, metrics = [], {}
        def encode(texts):
            calls.append(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]
        with patch.object(worker, '_embedder', return_value=encode):
            embed, reuse = worker._preserving_embedder({}, metrics, paper, targets, existing)
            vectors = embed(['unchanged', 'new'])
        self.assertEqual(calls, [['new']])
        self.assertEqual(vectors[0], old_vector)
        self.assertEqual(reuse, {old_id: old_id})
        self.assertEqual((metrics['reused_vectors'], metrics['newly_embedded_vectors']), (1, 1))

    def test_embedding_batches_preserve_order_and_cap_requests_at_64(self):
        values = list(range(129))
        batches = list(worker.batched(values))
        self.assertEqual([len(batch) for batch in batches], [64, 64, 1])
        self.assertEqual([item for batch in batches for item in batch], values)

    def test_successful_source_acquisition_records_phase_metrics(self):
        class Fresh:
            class FreshSourceError(Exception):
                pass

            class FreshSourceDependencyError(FreshSourceError):
                pass

            @staticmethod
            def acquire_and_parse(request, stage, **kwargs):
                return {'body_quality_candidate': True,
                        'diagnostics': {'fetch_seconds': 1.25, 'parse_seconds': 2.5,
                                        'chunk_build_seconds': .5}}

        runtime = {'fresh': Fresh, 'document_cache': types.SimpleNamespace(safe_id=lambda value: value)}
        with tempfile.TemporaryDirectory() as directory:
            result, metrics = worker._acquire_one(
                'acl:W12-1234', {'arxiv_id': 'acl:W12-1234'}, runtime, directory)
        self.assertIn('diagnostics', result)
        self.assertAlmostEqual(metrics['fetch_seconds'], 1.25)
        self.assertAlmostEqual(metrics['convert_seconds'], 3.0)
        self.assertIn('acquire_parse_seconds', metrics)

    def test_arxiv_falls_back_from_quality_rejected_html_and_tex_to_pdf(self):
        class Fresh:
            class FreshSourceError(Exception):
                pass

            class FreshSourceDependencyError(FreshSourceError):
                pass

            @staticmethod
            def resolve_arxiv_version(paper_id, timeout):
                return {'fresh_version': 'v2', 'title': 'A paper'}

            def __init__(self):
                self.routes = []

            def acquire_and_parse(self, request, stage, **kwargs):
                route = request['fresh_kind']
                self.routes.append(route)
                if route == 'HTML':
                    raise self.FreshSourceError('structured HTML parse failed')
                if route == 'rawTexarchive':
                    return {'body_quality_candidate': False,
                            'diagnostics': {'body_quality_reason': 'replacement_characters_present'}}
                return {'body_quality_candidate': True, 'diagnostics': {'parse_seconds': 2.0}}

        fresh = Fresh()
        runtime = {'fresh': fresh, 'document_cache': types.SimpleNamespace(safe_id=lambda value: value)}
        with tempfile.TemporaryDirectory() as directory:
            result, metrics = worker._acquire_one(
                '2411.07975', {'arxiv_id': '2411.07975'}, runtime, directory)
        self.assertTrue(result['body_quality_candidate'])
        self.assertEqual(fresh.routes, ['HTML', 'rawTexarchive', 'PDF'])
        self.assertEqual(metrics['source_route_attempts'], fresh.routes)
        self.assertEqual([item['route'] for item in metrics['source_route_rejections']],
                         ['HTML', 'rawTexarchive'])


class RecoveryTests(unittest.TestCase):
    def test_interrupted_apply_is_rolled_back_and_verified_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'paper.json'
            journal.write_text(json.dumps({'phase': 'applying'}), encoding='utf-8')
            calls = []

            class Owner:
                @staticmethod
                def _load(path):
                    return json.loads(Path(path).read_text(encoding='utf-8'))

                @staticmethod
                def rollback_plan(path, *, qdrant, maintenance=False):
                    calls.append(('rollback', maintenance))
                    Path(path).write_text(json.dumps({'phase': 'rolled_back'}), encoding='utf-8')

                @staticmethod
                def _verify_document(plan, qdrant, mode):
                    calls.append(('verify', mode, plan['phase']))

            @contextlib.contextmanager
            def gate_factory(ids):
                gate = {'safe_to_restore': False}
                yield gate

            updates = []
            worker.recover_journals(
                [{'paper_id': 'p1', 'status': 'applying', 'journal_path': str(journal)}],
                owner=Owner, qdrant=object(), gate_factory=gate_factory,
                update=lambda paper_id, status, **kwargs: updates.append((paper_id, status, kwargs)))

            self.assertIn(('rollback', True), calls)
            self.assertIn(('verify', 'before', 'rolled_back'), calls)
            self.assertEqual(updates[0][0:2], ('p1', 'failed'))
            self.assertTrue(any(call[0] == 'verify' and call[1] == 'before' for call in calls))

    def test_catastrophic_batch_exception_rolls_back_then_restores_api(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            journal = base / 'p1.json'
            journal.write_text(json.dumps({'phase': 'prepared'}), encoding='utf-8')
            conn = worker._connect(worker.db_path(base))
            try:
                worker._schema(conn)
                conn.execute("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,created_at,updated_at) VALUES('p1',0,0,0,'prepared','now','now')")
                conn.commit()
            finally:
                conn.close()

            class Owner:
                RepairError = ValueError

                @staticmethod
                def _load(path):
                    return json.loads(Path(path).read_text(encoding='utf-8'))

                @staticmethod
                def _verify_document(plan, qdrant, mode):
                    if mode == 'before' and plan['phase'] not in ('prepared', 'rolled_back'):
                        raise AssertionError('before-image verification requires a safe phase')

                @staticmethod
                def apply_plan(path, *, qdrant, maintenance=False):
                    Path(path).write_text(json.dumps({'phase': 'applying'}), encoding='utf-8')
                    raise KeyboardInterrupt()

                @staticmethod
                def rollback_plan(path, *, qdrant, maintenance=False):
                    Path(path).write_text(json.dumps({'phase': 'rolled_back'}), encoding='utf-8')

            class FakeWorker:
                api_active = True

                def systemctl(self, action, unit, *args):
                    if action == 'show':
                        return 'active' if unit == worker.API_UNIT and self.api_active else 'inactive'
                    if action == 'stop':
                        self.api_active = False
                    elif action == 'start':
                        self.api_active = True

                def health(self):
                    return None

            runtime = {'owner': Owner, 'client': object()}
            worker_state = FakeWorker()
            with self.assertRaises(KeyboardInterrupt):
                worker._apply_batch([('p1', journal, {})], runtime, base, worker_state)
            self.assertTrue(worker_state.api_active)
            self.assertEqual(Owner._load(journal)['phase'], 'rolled_back')
            self.assertEqual(worker._get_targets(base, ('failed',))[0]['paper_id'], 'p1')


if __name__ == '__main__':
    unittest.main()
