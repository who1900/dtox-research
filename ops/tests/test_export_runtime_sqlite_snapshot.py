import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('snapshot', Path(__file__).parents[1] / 'export_runtime_sqlite_snapshot.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'source.db'
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('CREATE TABLE papers (value TEXT)')
        self.conn.execute("INSERT INTO papers VALUES ('committed WAL')")
        self.conn.commit()
        self.output = self.root / 'copy.sqlite'
        p = patch.object(m, 'RESERVE', 0)
        p.start()
        self.addCleanup(p.stop)
        if m.os.name == 'nt':
            p = patch.object(m, 'sync_directory')
            p.start()
            self.addCleanup(p.stop)

    def test_committed_wal_schema_hash_source_unchanged(self):
        paths = (self.db, Path(str(self.db) + '-wal'))
        before = [p.read_bytes() for p in paths]
        self.conn.execute("INSERT INTO papers VALUES ('uncommitted')")
        result = m.backup(self.db, self.output, 1024 ** 2, {'papers'})
        checked = sqlite3.connect(self.output)
        try:
            self.assertEqual(checked.execute('SELECT * FROM papers').fetchall(), [('committed WAL',)])
        finally:
            checked.close()
        self.assertEqual(before, [p.read_bytes() for p in paths])
        self.assertEqual(result['sha256'], m.digest(self.output))
        self.assertEqual(result['integrity'], ['ok'])

    def test_existing_output_preserved(self):
        self.output.write_bytes(b'evidence')
        with self.assertRaises(FileExistsError):
            m.backup(self.db, self.output, 1024 ** 2, {'papers'})
        self.assertEqual(self.output.read_bytes(), b'evidence')

    def test_size_and_reserve_reject_before_output(self):
        with self.assertRaises(ValueError):
            m.backup(self.db, self.output, 1, {'papers'})
        with patch.object(m, 'RESERVE', 10 ** 30), self.assertRaises(ValueError):
            m.backup(self.db, self.output, 1024 ** 2, {'papers'})
        self.assertFalse(self.output.exists())

    def test_schema_error_preserves_snapshot(self):
        with self.assertRaises(ValueError):
            m.backup(self.db, self.output, 1024 ** 2, {'missing'})
        self.assertTrue(self.output.exists())

    def test_progress_reserve_abort_preserves_output(self):
        from types import SimpleNamespace
        with patch.object(m.shutil, 'disk_usage', side_effect=[SimpleNamespace(free=10 ** 12), SimpleNamespace(free=0)]), \
             self.assertRaises(ValueError):
            m.backup(self.db, self.output, 1024 ** 2, {'papers'})
        self.assertTrue(self.output.exists())

    def test_complete_two_database_pack(self):
        with patch.object(m, 'DATABASES', {name: (self.db, 1024 ** 2, {'papers'}) for name in ('state', 'papers')}), \
             patch.object(m, 'isolation', return_value={'verified': True}), \
             patch.object(m, 'durable_json', self.windows_json):
            self.assertEqual(m.run(self.config(), self.root / 'job'), 0)
        receipt = json.loads((self.root / 'job/receipt.json').read_text())
        self.assertEqual(receipt['status'], 'COMPLETE')
        self.assertEqual(set(receipt['snapshots']), {'state', 'papers'})
        self.assertEqual(receipt['source_before'], receipt['source_after'])

    def config(self):
        p = self.root / 'config.json'
        p.write_text('{}')
        return p

    def test_interruption_preserves_intent_partial_and_error(self):
        job = self.root / 'job'
        def interrupted(db, output, *args):
            output.write_bytes(b'partial evidence')
            raise KeyboardInterrupt('interrupted')
        with patch.object(m, 'DATABASES', {'state': (self.db, 1024 ** 2, {'papers'})}), \
             patch.object(m, 'isolation', return_value={'verified': True}), \
             patch.object(m, 'backup', side_effect=interrupted), patch.object(m, 'durable_json', self.windows_json):
            self.assertEqual(m.run(self.config(), job), 1)
        self.assertTrue((job / 'intent.json').exists())
        self.assertEqual((job / 'state.sqlite').read_bytes(), b'partial evidence')
        self.assertIn('KeyboardInterrupt', json.loads((job / 'receipt.json').read_text())['error'])
        with self.assertRaises(FileExistsError):
            m.run(self.root / 'config.json', job)

    @staticmethod
    def windows_json(path, value):
        with path.open('x') as stream:
            json.dump(value, stream)

    def test_post_isolation_failure_cannot_complete(self):
        with patch.object(m, 'DATABASES', {'state': (self.db, 1024 ** 2, {'papers'})}), \
             patch.object(m, 'isolation', side_effect=[{}, ValueError('mount changed')]), \
             patch.object(m, 'durable_json', self.windows_json):
            self.assertEqual(m.run(self.config(), self.root / 'job'), 1)
        self.assertEqual(json.loads((self.root / 'job/receipt.json').read_text())['status'], 'ERROR')

    def test_mount_boot_code_receipt_pins(self):
        receipt = self.root / 'isolation.json'
        receipt.write_text('{}')
        c = dict(isolation_receipt=str(receipt), isolation_sha256=m.digest(receipt),
                 api_source=str(receipt), api_sha256=m.digest(receipt), boot_id='boot',
                 mount_namespace='mnt:[1]', mount_ids={'/dataset': 42})
        def proc(path, *args, **kwargs):
            return 'boot' if str(path).endswith('boot_id') else '42 1 8:1 /dataset /dataset ro - ext4 /dev/a rw\n'
        with patch.object(m, 'ROOT', self.root), patch.object(m, 'MOUNTS', {'/dataset'}), patch.object(Path, 'read_text', proc), \
             patch.object(m.os, 'readlink', return_value='mnt:[1]'), \
             patch.object(m, 'api_identity', return_value=m.API_IDENTITY):
            c['api_identity'] = dict(m.API_IDENTITY)
            self.assertEqual(m.isolation(c)['mount_ids'], {'/dataset': 42})
            for key, bad in [('boot_id', 'other'), ('api_sha256', 'bad'),
                             ('isolation_sha256', 'bad'), ('mount_ids', {'/dataset': 43}),
                             ('mount_namespace', 'other')]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    m.isolation(dict(c, **{key: bad}))
            with patch.object(Path, 'read_text', return_value='42 1 8:1 /dataset /dataset rw - ext4 /dev/a rw\n'), \
                 self.assertRaises(ValueError):
                m.isolation(c)

    def test_api_identity_and_reused_pid_rejected(self):
        from types import SimpleNamespace
        expected = dict(m.API_IDENTITY)
        def proc(start):
            return f"{expected['pid']} (api (worker)) " + ' '.join(['S'] + ['0'] * 18 + [str(start)])
        result = SimpleNamespace(stdout=f"MainPID={expected['pid']}\nInvocationID={expected['invocation_id']}\n")
        with patch.object(Path, 'read_text', return_value=proc(expected['starttime'])), \
             patch.object(m.subprocess, 'run', return_value=result) as command:
            self.assertEqual(m.api_identity(expected), expected)
            self.assertEqual(command.call_args.args[0][1:3], ['show', 'dtox-research-api.service'])
            for stdout in ('MainPID=1\nInvocationID=other\n',
                           f"MainPID={expected['pid']}\nInvocationID=other\n"):
                with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(stdout=stdout)), self.assertRaises(ValueError):
                    m.api_identity(expected)
            with patch.object(Path, 'read_text', side_effect=[proc(expected['starttime']), proc(expected['starttime'] + 1)]), \
                 self.assertRaises(ValueError):
                m.api_identity(expected)
            with patch.object(m.subprocess, 'run', side_effect=FileNotFoundError()), self.assertRaises(FileNotFoundError):
                m.api_identity(expected)
        with self.assertRaises(ValueError):
            m.api_identity(dict(expected, pid=1))

    def test_source_schema_preflight_before_any_snapshot(self):
        self.assertTrue(m.source_schema(self.db, {'papers'}))
        with patch.object(m, 'DATABASES', {'state': (self.db, 1024 ** 2, {'missing'})}), \
             patch.object(m, 'isolation', return_value={}), patch.object(m, 'durable_json', self.windows_json), \
             patch.object(m, 'backup') as backup:
            self.assertEqual(m.run(self.config(), self.root / 'job'), 1)
            backup.assert_not_called()
        self.assertFalse((self.root / 'job/state.sqlite').exists())

    def test_traversal_and_symlink_refused(self):
        with self.assertRaises(ValueError):
            m.plain(self.root / '..' / 'escape')
        link = self.root / 'link'
        try:
            link.symlink_to(self.db)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaises(ValueError):
            m.metadata(link)


if __name__ == '__main__':
    unittest.main()
