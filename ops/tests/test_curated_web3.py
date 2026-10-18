import json
import tempfile
import unittest
from pathlib import Path
import sys
import types
from unittest.mock import patch

from ops.corpus_quality import curated_web3 as curated
from ops.corpus_quality import replace_documents as worker


class CuratedWeb3Tests(unittest.TestCase):
    def manifest(self, path):
        path.write_text(json.dumps({
            'schema': 1,
            'sources': [{
                'paper_id': 'iacr:2016/260',
                'title': 'On the Size of Pairing-based Non-interactive Arguments',
                'fresh_url': 'https://example.org/groth16.pdf',
                'edition': 'sha256:' + ('a' * 64),
            }],
        }), encoding='utf-8')
        return path

    def test_manifest_is_identity_bound_and_drift_blocks_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'
            root.mkdir()
            manifest = self.manifest(Path(directory) / 'manifest.json')
            base = Path(directory) / 'run'
            with patch.object(curated, '_validate_targets'):
                curated.initialize(base, manifest, root)
                manifest.write_text(manifest.read_text(encoding='utf-8') + '\n', encoding='utf-8')
                with self.assertRaisesRegex(worker.WorkerError, 'manifest hash changed'):
                    curated._stored_requests(base, manifest, root)

    def test_run_uses_shared_repair_gate_and_curated_source_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'
            root.mkdir()
            manifest = self.manifest(Path(directory) / 'manifest.json')
            base = Path(directory) / 'run'
            with patch.object(curated, '_validate_targets'):
                curated.initialize(base, manifest, root)
            with patch.object(curated, '_validate_targets'), \
                    patch.object(curated, '_wait_for_repair_complete'), \
                    patch.object(curated.worker, 'run', return_value={'ok': True}) as run:
                result = curated.run(base, manifest, root=root, wait_for_lock=True)
            self.assertEqual(result, {'ok': True})
            kwargs = run.call_args.kwargs
            self.assertEqual(kwargs['gate_base'], worker.BASE)
            self.assertTrue(kwargs['wait_for_lock'])
            self.assertEqual(kwargs['papers'], ['iacr:2016/260'])
            request = kwargs['source_requests']['iacr:2016/260']
            self.assertEqual(request['fresh_kind'], 'PDF')
            self.assertEqual(request['fresh_url'], 'https://example.org/groth16.pdf')
            self.assertEqual(request['source_label'], 'source-pdf')

    def test_wait_for_lock_blocks_on_shared_sibling_locks(self):
        acquired = []
        fake_fcntl = types.SimpleNamespace(
            LOCK_EX=2, LOCK_NB=4,
            flock=lambda stream, flags: acquired.append((Path(stream.name).name, flags)))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / 'curated'
            with patch.dict(sys.modules, {'fcntl': fake_fcntl}):
                with worker._locks(base, wait=True):
                    pass
        self.assertEqual([name for name, _ in acquired], [
            'labels-worker.lock', 'points-worker.lock', 'whole-document-replacement.lock'])
        self.assertTrue(all(flags == fake_fcntl.LOCK_EX for _, flags in acquired))

    def test_wait_for_lock_respects_restart_gap_until_bulk_pass_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            repair_base = Path(directory)
            (repair_base / 'bulk-controller-status.json').write_text(json.dumps({
                'phase': 'bulk_running', 'status': 'running'}), encoding='utf-8')
            (repair_base / 'maintenance-gate.json').write_text(json.dumps({
                'phase': 'restored', 'safe_to_restore': True,
                'attention_required': False}), encoding='utf-8')
            with patch.object(curated.worker, 'BASE', repair_base):
                calls = []
                def complete_after_wait(seconds):
                    calls.append(seconds)
                    (repair_base / 'bulk-controller-status.json').write_text(json.dumps({
                        'phase': 'bulk_complete', 'status': 'pass_complete'}), encoding='utf-8')
                curated._wait_for_repair_complete(complete_after_wait)
                self.assertEqual(calls, [10])
                self.assertTrue(curated._repair_idle())


if __name__ == '__main__':
    unittest.main()
