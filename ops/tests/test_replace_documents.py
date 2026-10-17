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
    def test_api_startup_check_runs_as_direct_script_outside_repository(self):
        import os
        import subprocess
        import sys
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-B', str(Path(worker.__file__).resolve()),
                                     'api-startup-check', '--base', directory],
                                    cwd=directory, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_coarse_layer_reconciliation_preserves_vector_and_rejects_title_drift(self):
        state = dict(title='Verified', year=2026, layers='ai-agents,builder-tech')
        before = dict(id='coarse-id', vector=[1.0, 0.0],
                      payload=dict(title='Verified', year=2026, layers=['ai-agents'], abstract='Unchanged'))
        after = worker._coarse_metadata_candidate(state, before)
        self.assertEqual(after['vector'], before['vector'])
        self.assertEqual(after['id'], before['id'])
        self.assertEqual(after['payload']['abstract'], 'Unchanged')
        self.assertEqual(after['payload']['layers'], ['ai-agents', 'builder-tech'])
        self.assertEqual(before['payload']['layers'], ['ai-agents'])
        self.assertIsNone(worker._coarse_metadata_candidate(state, after))
        with self.assertRaises(worker.DeferredPaper):
            worker._coarse_metadata_candidate(dict(state, title='Different'), before)

    def test_pmlr_http_input_keeps_identity_and_declared_url(self):
        state = dict(arxiv_id='pmlr:v235/paper24a', title='A verified paper',
                     source_url='http://proceedings.mlr.press/v235/paper24a/paper24a.pdf')
        request = worker._fresh_request(state, object())
        self.assertEqual(request['fresh_url'],
                         'https://proceedings.mlr.press/v235/paper24a/paper24a.pdf')
        self.assertEqual(request['declared_source_url'], state['source_url'])
        self.assertEqual(request['paper_id'], state['arxiv_id'])

    def test_generic_http_input_requests_tls_at_the_declared_path(self):
        state = dict(arxiv_id='oa:W123', title='A verified paper',
                     source_url='http://example.org/papers/one.pdf')
        request = worker._fresh_request(state, object())
        self.assertEqual(request['fresh_url'], 'https://example.org/papers/one.pdf')
        self.assertEqual(request['declared_source_url'], state['source_url'])
        self.assertEqual(request['title'], state['title'])

    def test_rejected_pdf_keeps_actual_unicode_diagnostics_in_existing_ledger(self):
        class Fresh:
            class FreshSourceError(Exception): pass
            class FreshSourceDependencyError(FreshSourceError): pass
            @staticmethod
            def acquire_and_parse(*args, **kwargs):
                return dict(body_quality_candidate=False, source_sha256='source-sha',
                            source_binding={'raw_sha256': 'raw-sha'},
                            diagnostics=dict(body_quality_reason='replacement_characters_present',
                                             replacement_characters=2, page_count=4))
        runtime = dict(fresh=Fresh, document_cache=types.SimpleNamespace(safe_id=lambda x:x))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(worker.DeferredPaper) as error:
                worker._acquire_one('acl:paper', {'arxiv_id': 'acl:paper'}, runtime, directory)
        with patch.object(worker, '_update') as update:
            worker._record_error('base', 'acl:paper', error.exception, {})
        saved = update.call_args.kwargs['metrics']['source_diagnostics'][0]
        self.assertEqual(saved['raw_sha256'], 'raw-sha')
        self.assertEqual(saved['diagnostics']['replacement_characters'], 2)

    def test_replacement_does_not_inherit_stale_fulltext_bibliography(self):
        state = dict(title='Verified title', year=2026, abstract='Current abstract',
                     layers='ai-agents,builder-tech')
        original = dict(title='Old title', year=2025, abstract='Old abstract',
                        layers=['ai-agents'], citation_count=17)
        points = {'p': {'payload': original}}
        metadata = worker._candidate_metadata(state, points, {'layers': ['ai-agents', 'builder-tech']})
        self.assertEqual(metadata['layers'], ['ai-agents', 'builder-tech'])
        self.assertEqual((metadata['title'], metadata['year'], metadata['abstract']),
                         ('Verified title', 2026, 'Current abstract'))
        self.assertEqual(metadata['citation_count'], 17)
        self.assertEqual(original['layers'], ['ai-agents'])
        metadata['layers'].append('other')
        self.assertEqual(state['layers'], 'ai-agents,builder-tech')

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
    def test_interrupted_run_uses_matching_controller_stop_time_for_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            conn = worker._connect(worker.db_path(base))
            try:
                worker._schema(conn)
                conn.executemany("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,created_at,updated_at) VALUES(?,0,0,0,?,'now','now')",
                                 [(f'p{i}', 'applied' if i <= 10 else 'pending') for i in range(1, 21)])
                conn.commit()
            finally:
                conn.close()
            (base / 'bulk-controller-status.json').write_text(json.dumps({
                'worker_pid': 321, 'phase': 'bulk_interrupted', 'time': 200.0}), encoding='utf-8')
            run = {'run_id': 'r1', 'target_ids': [f'p{i}' for i in range(1, 21)], 'started_epoch': 100.0,
                   'pid': 321, 'finished': False, 'phase_seconds': {}}
            with patch.object(worker.os, 'kill', side_effect=ProcessLookupError), \
                 patch.object(worker.time, 'time', side_effect=[1000.0, 2000.0]):
                first = worker.status_snapshot(base, run)['run']
                snapshot = worker.status_snapshot(base, run)['run']

            self.assertEqual(snapshot['phase'], 'interrupted')
            self.assertEqual(snapshot['elapsed_seconds'], 100.0)
            self.assertEqual(snapshot['elapsed_seconds'], first['elapsed_seconds'])
            self.assertEqual(snapshot['throughput_docs_per_minute'], 6.0)
            self.assertEqual(snapshot['considered_docs_per_minute'], 6.0)
            self.assertIsNone(snapshot['eta_seconds'])

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
            with patch.object(worker, '_wait_for_api'), self.assertRaises(KeyboardInterrupt):
                worker._apply_batch([('p1', journal, {})], runtime, base, worker_state)
            self.assertTrue(worker_state.api_active)
            self.assertEqual(Owner._load(journal)['phase'], 'rolled_back')
            self.assertEqual(worker._get_targets(base, ('failed',))[0]['paper_id'], 'p1')



class JournalCompressionTests(unittest.TestCase):
    def test_temp_write_failure_retains_exact_raw_and_next_document_continues(self):
        import gzip
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            conn = worker._connect(worker.db_path(base))
            worker._schema(conn)
            raw_by_paper = {}
            for paper in ('first', 'next'):
                path = base / (paper + '.json')
                raw = json.dumps({'phase': 'applied', 'paper': {'arxiv_id': paper}}).encode()
                path.write_bytes(raw)
                raw_by_paper[paper] = raw
                conn.execute("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,journal_path,created_at,updated_at) VALUES(?,0,0,0,'applied',?,'now','now')",
                             (paper, str(path)))
            conn.commit()
            conn.close()
            def load(path):
                raw = Path(path).read_bytes()
                return json.loads(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw)
            def atomic(path, content):
                if Path(path).name.startswith('first'):
                    raise PermissionError('compressed temporary write failed')
                Path(path).write_bytes(content)
            owner = types.SimpleNamespace(_load=load, labels=types.SimpleNamespace(_atomic=atomic))
            runtime = dict(owner=owner)
            results = []
            with patch.object(worker, 'write_status'):
                for paper in ('first', 'next'):
                    results.append(worker._compress_applied_journal(
                        base, paper, base / (paper + '.json'), runtime, {}))
            self.assertEqual(results, [base / 'first.json', base / 'next.json.gz'])
            self.assertEqual((base / 'first.json').read_bytes(), raw_by_paper['first'])
            self.assertEqual(gzip.decompress((base / 'next.json.gz').read_bytes()), raw_by_paper['next'])
            rows = {row['paper_id']: row for row in worker._get_targets(base, ('applied',))}
            self.assertEqual(rows['first']['journal_path'], str(base / 'first.json'))
            self.assertIn('PermissionError:', json.loads(rows['first']['metrics_json'])['compression_deferred'])
            self.assertEqual(rows['next']['journal_path'], str(base / 'next.json.gz'))

    def test_compression_failure_without_selected_applied_receipt_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            owner = types.SimpleNamespace(_load=lambda path: {'phase': 'prepared'})
            runtime = dict(owner=owner)
            with patch.object(worker, '_get_targets', return_value=[dict(journal_path=str(base / 'missing'), metrics_json='{}')]):
                with self.assertRaisesRegex(worker.WorkerError, 'accepted journal changed'):
                    worker._compress_applied_journal(base, 'paper', base / 'missing', runtime, {})


class ContinuousStageTests(unittest.TestCase):
    @contextlib.contextmanager
    def stages(self, base, acquire, candidate, prepare, apply):
        owner = types.SimpleNamespace(prepare_document_replacement=prepare)
        runtime = dict(owner=owner, document_cache=types.SimpleNamespace(safe_id=lambda value: value),
                       paths={}, client=object(), worker=object())
        with contextlib.ExitStack() as stack:
            for name, value in (('_check_storage', lambda *a: None),
                                ('_state_row', lambda *a: {}), ('_update', lambda *a, **kw: None),
                                ('_get_targets', lambda *a: []),
                                ('_advance_rowid_floors', lambda *a: None),
                                ('_acquire_one', acquire), ('_prepare_candidate', candidate),
                                ('_apply_batch', apply)):
                stack.enter_context(patch.object(worker, name, side_effect=value))
            stack.enter_context(patch.object(worker, 'PREFETCH_WORKERS', 2))
            stack.enter_context(patch.object(worker, 'CANDIDATE_WORKERS', 2))
            stack.enter_context(patch.object(worker, 'MAX_IN_FLIGHT', 4))
            yield runtime

    def test_slow_source_does_not_block_publication_or_continuous_refill(self):
        import threading
        from unittest.mock import Mock
        release = threading.Event()
        main_thread = threading.get_ident()
        active, published, peak = set(), [], [0]
        lock = threading.Lock()
        def acquire(paper, *args):
            with lock:
                active.add(paper)
                peak[0] = max(peak[0], len(active))
            if paper == 'slow' and not release.wait(5):
                raise AssertionError('slow source blocked the independent pipeline')
            return {}, {}
        def prepare(paper, **kwargs):
            self.assertEqual(threading.get_ident(), main_thread)
            return {'phase': 'prepared'}
        def apply(plans, *args):
            self.assertEqual(threading.get_ident(), main_thread)
            for paper, _, _ in plans:
                with lock:
                    active.remove(paper)
                published.append(paper)
                if paper == 'fast6':
                    release.set()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            records = [dict(paper_id=paper) for paper in ['slow'] + [f'fast{i}' for i in range(7)]]
            with self.stages(base, acquire, lambda *a: {}, prepare, apply) as runtime:
                worker._run_stages(records, runtime, base, base, {})
        self.assertTrue(release.is_set())
        self.assertLess(published.index('fast6'), published.index('slow'))
        self.assertEqual(set(published), {r['paper_id'] for r in records})
        self.assertLessEqual(peak[0], 4)

    def test_preparation_orders_existing_cache_sizes_with_prepared_first(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            cache = types.SimpleNamespace(safe_id=lambda value: value)
            for paper, size in [('small', 10), ('large', 900), ('tie-b', 80),
                                ('tie-a', 80), ('ready', 1), ('empty', 0)]:
                (cache_dir / (paper + '.json')).write_bytes(b'x' * size)
            records = [dict(paper_id=p, status='prepared' if p == 'ready' else 'pending')
                       for p in ('small', 'tie-b', 'empty', 'large', 'ready', 'tie-a', 'absent')]
            ordered = worker._order_preparation(records, cache_dir, cache)
            self.assertEqual([row['paper_id'] for row in ordered],
                             ['ready', 'large', 'tie-a', 'tie-b', 'small', 'absent', 'empty'])
            self.assertEqual(records[0]['paper_id'], 'small')
        with patch.object(Path, 'lstat', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                worker._order_preparation(records, cache_dir, cache)

    def test_source_completion_feeds_candidates_while_writer_is_blocked(self):
        import threading
        entered_writer, candidate_during_write = threading.Event(), threading.Event()
        published = []
        def acquire(paper, *args):
            if paper == 'next' and not entered_writer.wait(5):
                raise AssertionError('first document never reached writer')
            return {}, {}
        def candidate(paper, *args):
            if paper == 'next':
                self.assertTrue(entered_writer.is_set())
                candidate_during_write.set()
            return {}
        def apply(plans, *args):
            for paper, _, _ in plans:
                if paper == 'first':
                    entered_writer.set()
                    self.assertTrue(candidate_during_write.wait(5),
                                    'candidate dispatch stopped while writer was busy')
                published.append(paper)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with self.stages(base, acquire, candidate,
                             lambda *a, **kw: {'phase': 'prepared'}, apply) as runtime:
                worker._run_stages([dict(paper_id=p) for p in ('first', 'next')],
                                   runtime, base, base, {})
        self.assertEqual(published, ['first', 'next'])

    def test_unknown_source_and_candidate_defects_are_local_but_storage_stops(self):
        from unittest.mock import Mock
        import errno
        for stage in ('source', 'candidate'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as directory:
                def acquire(paper, *args):
                    if paper == 'bad' and stage == 'source':
                        raise ValueError('source input defect')
                    return {}, {}
                def candidate(paper, *args):
                    if paper == 'bad' and stage == 'candidate':
                        raise ValueError('candidate input defect')
                    return {}
                applied = []
                def apply(plans, *args):
                    applied.extend(paper for paper, _, _ in plans)
                with self.stages(Path(directory), acquire, candidate,
                                 lambda *a, **kw: {'phase': 'prepared'}, apply) as runtime, \
                        patch.object(worker, '_record_error') as error:
                    worker._run_stages([dict(paper_id=p) for p in ('bad', 'good')],
                                       runtime, Path(directory), Path(directory), {})
                    self.assertEqual(applied, ['good'])
                    self.assertEqual(error.call_args.args[1], 'bad')
        with tempfile.TemporaryDirectory() as directory:
            failure = Mock(side_effect=OSError(errno.ENOSPC, 'disk full'))
            apply = Mock()
            with self.stages(Path(directory), failure, lambda *a: {}, Mock(), apply) as runtime:
                with self.assertRaises(OSError):
                    worker._run_stages([dict(paper_id='bad')], runtime,
                                       Path(directory), Path(directory), {})
            apply.assert_not_called()

    def test_prepared_journal_survives_local_prepare_exception(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            journal = base / 'journal.json'
            def prepare(*args, **kwargs):
                journal.write_text('{}')
                raise ValueError('post-journal input defect')
            apply = Mock()
            with self.stages(base, lambda *a: ({}, {}), lambda *a: {}, prepare, apply) as runtime, \
                    patch.object(worker, '_journal_path', return_value=journal), \
                    patch.object(worker, '_update') as update:
                runtime['owner']._load = lambda path: {'phase': 'prepared'}
                worker._run_stages([dict(paper_id='paper')], runtime, base, base, {})
            self.assertEqual(update.call_args.args[2], 'prepared')
            self.assertIn('ValueError:', update.call_args.kwargs['reason'])
            apply.assert_not_called()

    def test_candidate_transport_retries_without_hiding_invalid_inputs(self):
        with patch.object(worker, '_prepare_candidate', side_effect=[TimeoutError('Qdrant busy'), {}]) as build, \
                patch.object(worker.time, 'sleep') as sleep:
            result, metrics = worker._candidate_one('paper', {}, {}, {}, Path('.'), {})
            self.assertEqual(result, {})
            self.assertEqual(build.call_count, 2)
            sleep.assert_called_once_with(5)
        with patch.object(worker, '_prepare_candidate', side_effect=ValueError('bad input')), \
                patch.object(worker.time, 'sleep') as sleep:
            with self.assertRaises(ValueError):
                worker._candidate_one('paper', {}, {}, {}, Path('.'), {})
            sleep.assert_not_called()

    def test_only_explicitly_trusted_matching_terminal_skips_recovery_readback(self):
        from unittest.mock import Mock
        for trusted, status, expected_readback in ((True, 'applied', False),
                                                    (False, 'applied', True),
                                                    (True, 'applying', True)):
            with self.subTest(trusted=trusted, status=status), tempfile.TemporaryDirectory() as directory:
                journal = Path(directory) / 'journals' / 'paper.json'
                journal.parent.mkdir()
                journal.write_text('{}')
                owner = types.SimpleNamespace(_load=Mock(return_value={'phase': 'applied'}),
                                              _verify_document=Mock())
                update, gate = Mock(), Mock()
                worker.recover_journals([dict(paper_id='paper', status=status, journal_path=str(journal))],
                                        owner=owner, qdrant=object(), gate_factory=gate, update=update,
                                        trusted_terminal=trusted)
                self.assertEqual(owner._verify_document.called, expected_readback)
                self.assertEqual(update.called, expected_readback)
                gate.assert_not_called()
                if not expected_readback:
                    self.assertEqual(owner._load.call_count, 1)

    def test_chained_document_failure_keeps_root_cause(self):
        try:
            try:
                raise ValueError('bad source')
            except ValueError as cause:
                raise RuntimeError('parse failed') from cause
        except RuntimeError as error:
            self.assertEqual(worker._error_reason(error),
                             'RuntimeError:parse failed <- ValueError:bad source')


class FaultPolicyTests(unittest.TestCase):
    def runtime(self):
        from ops.corpus_quality import fresh_sources
        return {'fresh': fresh_sources}

    def test_source_transport_retries_then_succeeds(self):
        from unittest.mock import Mock
        call = Mock(side_effect=[TimeoutError('slow'), ConnectionRefusedError('down'), 'ok'])
        waits = []
        self.assertEqual(worker._source_attempt(call, self.runtime(), waits.append), 'ok')
        self.assertEqual(waits, [5, 15])
        self.assertEqual(call.call_count, 3)

    def test_four_unrelated_transport_failures_each_defer(self):
        from unittest.mock import Mock
        for _ in range(4):
            call = Mock(side_effect=TimeoutError('slow'))
            with self.assertRaises(worker.DeferredPaper):
                worker._source_attempt(call, self.runtime(), lambda _: None)
            self.assertEqual(call.call_count, 3)

    def test_run_continues_after_four_source_transport_failures(self):
        import requests
        from concurrent.futures import Future
        class ImmediatePool:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def submit(self, call, *args):
                future = Future()
                try:
                    future.set_result(call(*args))
                except Exception as exc:
                    future.set_exception(exc)
                return future
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            conn = worker._connect(worker.db_path(base))
            worker._schema(conn)
            conn.executemany("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,created_at,updated_at) VALUES(?,0,0,0,'pending','now','now')",
                             [(f'p{i}',) for i in range(4)])
            conn.commit()
            conn.close()
            runtime = dict(self.runtime(), owner=object(), client=object(), worker=object(),
                           document_cache=types.SimpleNamespace(safe_id=lambda value: value), paths={'cache_dir': base})
            with patch.object(worker, '_locks', return_value=contextlib.nullcontext()), \
                    patch.object(worker, '_load_runtime', return_value=runtime), \
                    patch.object(worker, '_runtime_preflight'), patch.object(worker, '_rowid_floors', return_value={}), \
                    patch.object(worker, '_order_preparation', side_effect=lambda rows, cache_dir, cache: rows), \
                    patch.object(worker, '_check_storage'), \
                    patch.object(worker, 'ThreadPoolExecutor', ImmediatePool), \
                    patch.object(worker.os, 'kill', return_value=None), \
                    patch.object(worker, '_state_row', return_value={}), \
                    patch.object(worker, '_acquire_one', side_effect=requests.exceptions.ReadTimeout('slow')) as acquire:
                result = worker.run(base, base)
            self.assertEqual(acquire.call_count, 4)
            self.assertEqual(result['statuses'], {'deferred': 4})

    def test_document_prepare_errors_defer_and_continue(self):
        from concurrent.futures import Future
        from pipeline import document_cache
        from unittest.mock import Mock
        class ImmediatePool:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def submit(self, call, *args):
                result = Future()
                result.set_result(call(*args))
                return result
        for error in (document_cache.ManifestInvalid('symlinked document path'), RuntimeError('unknown defect')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                conn = worker._connect(worker.db_path(base))
                worker._schema(conn)
                conn.executemany("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,created_at,updated_at) VALUES(?,0,0,0,'pending','now','now')",
                                 [('a-invalid',), ('b-valid',)])
                conn.commit()
                conn.close()
                prepare = Mock(side_effect=lambda paper_id, **kwargs: (_ for _ in ()).throw(error)
                               if paper_id == 'a-invalid' else {'phase': 'prepared'})
                owner = types.SimpleNamespace(RepairError=type('RepairError', (Exception,), {}),
                                              prepare_document_replacement=prepare)
                runtime = dict(self.runtime(), owner=owner, client=object(), worker=object(),
                               document_cache=document_cache, paths={'cache_dir': base})
                applied = []
                def apply(plans, runtime, base, service):
                    for paper_id, journal, metrics in plans:
                        applied.append(paper_id)
                        worker._update(base, paper_id, 'applied', metrics=metrics)
                with patch.object(worker, '_locks', return_value=contextlib.nullcontext()), \
                        patch.object(worker, '_load_runtime', return_value=runtime), \
                        patch.object(worker, '_runtime_preflight'), patch.object(worker, '_rowid_floors', return_value={}), \
                    patch.object(worker, '_order_preparation', side_effect=lambda rows, cache_dir, cache: rows), \
                        patch.object(worker, '_check_storage'), patch.object(worker, 'ThreadPoolExecutor', ImmediatePool), \
                        patch.object(worker.os, 'kill', return_value=None), patch.object(worker, '_state_row', return_value={}), \
                        patch.object(worker, '_acquire_one', return_value=({}, {})), \
                        patch.object(worker, '_prepare_candidate', return_value={}), \
                        patch.object(worker, '_advance_rowid_floors'), patch.object(worker, '_compress_applied_journal'), \
                        patch.object(worker, '_apply_batch', side_effect=apply):
                    result = worker.run(base, base)
                    self.assertEqual(result['statuses'], {'applied': 1, 'deferred': 1})
                    self.assertEqual(applied, ['b-valid'])
                    self.assertEqual(prepare.call_count, 2)
                    self.assertIn(type(error).__name__ + ':',
                                  worker._get_targets(base, ('deferred',))[0]['reason'])

    def test_http_retry_after_is_bounded_and_404_not_retried(self):
        from unittest.mock import Mock
        from ops.corpus_quality import fresh_sources
        response = types.SimpleNamespace(status_code=429, headers={'Retry-After': '999'})
        error = fresh_sources._http_failure('HTTP 429', response)
        waits = []
        call = Mock(side_effect=[error, 'ok'])
        self.assertEqual(worker._source_attempt(call, self.runtime(), waits.append), 'ok')
        self.assertEqual(waits, [60])
        response.status_code = 404
        error = fresh_sources._http_failure('non-200', response)
        call = Mock(side_effect=error)
        with self.assertRaises(fresh_sources.FreshSourceError):
            worker._source_attempt(call, self.runtime(), waits.append)
        self.assertEqual(call.call_count, 1)

    def test_unknown_dependency_and_cancellation_do_not_retry(self):
        from unittest.mock import Mock
        from ops.corpus_quality import fresh_sources
        for error in (ValueError('parse bug'), fresh_sources.FreshSourceDependencyError('HTTP 503'),
                      KeyboardInterrupt()):
            call = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                worker._source_attempt(call, self.runtime(), lambda _: self.fail('unexpected sleep'))
            self.assertEqual(call.call_count, 1)

    def test_shared_service_fault_classification(self):
        import urllib.error
        for error in (TimeoutError(), urllib.error.HTTPError('url', 503, 'down', {}, None),
                      urllib.error.URLError(ConnectionRefusedError())):
            self.assertIsInstance(worker._shared_failure('outage', error), worker.RetryableWorkerError)
        for error in (ValueError(), OSError('disk'),
                      urllib.error.HTTPError('url', 400, 'bad', {}, None)):
            self.assertNotIsInstance(worker._shared_failure('fatal', error), worker.RetryableWorkerError)

    def test_embedding_outage_retries_invalid_json_and_vectors_defer(self):
        runtime = {'recipe': {'endpoint': 'http://127.0.0.1:99/embed_batch'}}
        for error in (TimeoutError(), ValueError('invalid JSON')):
            with patch.object(worker.urllib.request, 'urlopen', side_effect=error):
                with self.assertRaises(worker.RetryableWorkerError if isinstance(error, TimeoutError)
                                       else worker.DeferredPaper):
                    worker._embedder(runtime, {})(['text'])
        for value in ({'vectors': []}, {'vectors': [[0.0] * 384]}):
            with patch.object(worker.urllib.request, 'urlopen', return_value=contextlib.nullcontext(None)), \
                    patch.object(worker.json, 'load', return_value=value):
                with self.assertRaises(worker.DeferredPaper):
                    worker._embedder(runtime, {})(['text'])

    def test_health_preflight_classifies_outage_and_invalid_receipt(self):
        with patch.object(worker.urllib.request, 'urlopen', side_effect=ConnectionRefusedError()):
            with self.assertRaises(worker.RetryableWorkerError):
                worker._check_health('http://localhost/health', 'service')
        with patch.object(worker.urllib.request, 'urlopen', return_value=contextlib.nullcontext(None)), \
                patch.object(worker.json, 'load', return_value={}):
            with self.assertRaises(worker.WorkerError) as caught:
                worker._check_health('http://localhost/health', 'service')
        self.assertNotIsInstance(caught.exception, worker.RetryableWorkerError)

    def test_api_restore_wait_retries_only_known_transient_health(self):
        waits = []
        with patch.object(worker, '_check_health', side_effect=[worker.RetryableWorkerError('down'), None]) as check:
            worker._wait_for_api(waits.append)
            self.assertEqual(check.call_count, 2)
            self.assertEqual(waits, [1])
        waits.clear()
        with patch.object(worker, '_check_health', side_effect=worker.WorkerError('invalid JSON')) as check:
            with self.assertRaises(worker.WorkerError):
                worker._wait_for_api(waits.append)
            self.assertEqual(check.call_count, 1)
            self.assertEqual(waits, [])
        with patch.object(worker, '_check_health', side_effect=worker.RetryableWorkerError('down')) as check:
            with self.assertRaises(worker.RetryableWorkerError):
                worker._wait_for_api(waits.append)
            self.assertEqual(check.call_count, 30)
            self.assertEqual(len(waits), 29)

    def test_delayed_pass_selects_only_transport_and_persists_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            conn = worker._connect(worker.db_path(base))
            worker._schema(conn)
            conn.executemany("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,reason,created_at,updated_at) VALUES(?,0,0,0,'deferred',?,'now','now')",
                             [('timeout', 'HTTPError:HTTP Error 503:down'), ('transport', 'DeferredPaper:source_transport_retries_exhausted:Timeout'),
                              ('parser', 'FreshSourceError:invalid HTML'), ('missing', 'FreshSourceError:HTTP 404')])
            conn.commit()
            conn.close()
            with patch.object(worker, 'write_status'):
                selected = worker._transport_retry_targets(base)
                self.assertEqual(set(selected), {'timeout', 'transport'})
                self.assertEqual(worker._transport_retry_targets(base), [])
                worker._update(base, 'timeout', 'staging', metrics={'fetch_seconds': 2})
                worker._update_metrics(base, 'transport', {'fetch_seconds': 3})
            for row in worker._get_targets(base, ('pending', 'staging', 'deferred'), papers=selected):
                self.assertEqual(json.loads(row['metrics_json'])['source_transport_retry_passes'], 1)
            self.assertEqual(worker._transport_retry_targets(base), [])

    def test_managed_cli_runs_one_delayed_transport_pass(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(worker, 'run', return_value={'statuses': {'deferred': 2}}) as run, \
                patch.object(worker, '_transport_retry_targets', return_value=['retry']), \
                patch.object(worker.time, 'sleep') as sleep:
            self.assertEqual(worker.main(['run', '--managed', '--base', directory]), 0)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs['papers'], ['retry'])
            self.assertNotIn('retry_deferred', run.call_args.kwargs)
            sleep.assert_called_once_with(60)
            value = json.loads((Path(directory) / 'bulk-controller-status.json').read_text())
            self.assertEqual(value['deferred_documents'], 2)
            self.assertFalse(value['whole_corpus_quality_complete'])

    def test_api_startup_check_accepts_only_missing_or_verified_safe_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maintenance-gate.json'
            with patch.object(worker, '_load_runtime', side_effect=AssertionError('runtime loaded')):
                self.assertEqual(worker.main(['api-startup-check', '--base', directory]), 0)
                for value, expected in (({'phase': 'closed', 'safe_to_restore': True, 'attention_required': False}, 0),
                                        ({'phase': 'restored', 'safe_to_restore': True, 'attention_required': False}, 0),
                                        ({'phase': 'closed', 'safe_to_restore': False, 'attention_required': False}, 1),
                                        ({'phase': 'closed', 'safe_to_restore': True, 'attention_required': True}, 1),
                                        ({'phase': 'unknown', 'safe_to_restore': True, 'attention_required': False}, 1),
                                        ({}, 1), ([], 1), (None, 1)):
                    path.write_text(json.dumps(value))
                    self.assertEqual(worker.main(['api-startup-check', '--base', directory]), expected)
                path.write_text('{invalid')
                self.assertEqual(worker.main(['api-startup-check', '--base', directory]), 1)

    def test_gate_persists_verified_state_before_api_start_and_preserves_transient_health(self):
        for health_error in (None, worker.RetryableWorkerError('booting'), ValueError('bad health')):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                class Service:
                    api_active = True
                    def systemctl(self, action, unit, *args):
                        if action == 'show':
                            return 'active' if unit == worker.API_UNIT and self.api_active else 'inactive'
                        if action == 'stop':
                            self.api_active = False
                        elif action == 'start':
                            self.assert_safe = worker.api_startup_check(base)
                            if self.assert_safe != 0:
                                raise AssertionError('API startup blocked by unpersisted verification')
                            self.api_active = True
                    def health(self):
                        if health_error is not None:
                            raise health_error
                service = Service()
                def restore():
                    with patch.object(worker, '_wait_for_api', side_effect=health_error), \
                            worker.maintenance_gate(base, service, ['p']) as gate:
                        gate['safe_to_restore'] = True
                if health_error is None:
                    restore()
                else:
                    with self.assertRaises(worker.RetryableWorkerError if isinstance(health_error, worker.RetryableWorkerError)
                                           else ValueError):
                        restore()
                marker = json.loads((base / 'maintenance-gate.json').read_text())
                self.assertEqual(service.assert_safe, 0)
                self.assertTrue(marker['safe_to_restore'])
                self.assertEqual(marker['attention_required'], isinstance(health_error, ValueError))
                self.assertEqual(marker['phase'], 'restored' if health_error is None else 'closed')

    def test_managed_cli_exit_and_lifecycle_classification(self):
        for error, code, status in ((worker.RetryableWorkerError('service down'), 75, 'retry_wait'),
                                    (worker.WorkerError('integrity'), 78, 'needs_attention'),
                                    (ValueError('unknown bug'), 78, 'needs_attention'),
                                    (MemoryError('resource shortage'), 75, 'retry_wait'),
                                    (OSError(worker.errno.EMFILE, 'descriptors exhausted'), 75, 'retry_wait')):
            with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'run', side_effect=error):
                self.assertEqual(worker.main(['run', '--managed', '--base', directory]), code)
                value = json.loads((Path(directory) / 'bulk-controller-status.json').read_text())
                self.assertEqual(value['worker_pid'], worker.os.getpid())
                self.assertEqual(int((Path(directory) / 'bulk-controller.pid').read_text()), worker.os.getpid())
                self.assertEqual(value['phase'], 'bulk_interrupted')
                self.assertEqual(value['status'], status)
                self.assertIsInstance(value['time'], float)
                self.assertIn(str(error), value['cause'])
        with tempfile.TemporaryDirectory() as directory, patch.object(worker, 'run', return_value={}), \
                patch.object(worker, '_transport_retry_targets', return_value=[]):
            self.assertEqual(worker.main(['run', '--managed', '--base', directory]), 0)
            value = json.loads((Path(directory) / 'bulk-controller-status.json').read_text())
            self.assertEqual((value['phase'], value['status']), ('bulk_complete', 'pass_complete'))
        with patch.object(worker, 'run', side_effect=worker.RetryableWorkerError('down')):
            with self.assertRaises(worker.RetryableWorkerError):
                worker.main(['run'])


if __name__ == '__main__':
    unittest.main()
