import io
import json
from pathlib import Path
import struct
import sqlite3
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ops import runtime_data_transport as m

RAW = b'raw bytes\x00\xff\r\n\x1aafter'


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'; self.source.mkdir()
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / ('a' * 134 + '.json')).write_bytes(RAW)
        (self.source / 'empty').write_bytes(b'')
        self.limits = {'bytes': 100000, 'entries': 20, 'manifest_bytes': 20000}
        self.c = {'stream_id': 'a' * 32, 'boot_id': 'boot', 'mount_ids': {'mount': 1},
                  'mount_namespace': 'mnt:1', 'api_identity': {'pid': 1}}
        self.pins = {k: self.c[k] for k in ('boot_id', 'mount_ids', 'mount_namespace', 'api_identity')}
        self._real_isolation = m.isolation
        for p in (patch.dict(m.COMPONENTS, {'chunk_cache': self.source}),
                  patch.object(m, 'isolation', return_value=self.pins), patch.object(m, 'RESERVE', 0)):
            p.start(); self.addCleanup(p.stop)
        if m.os.name == 'nt':
            p = patch.object(m.snapshot, 'sync_directory'); p.start(); self.addCleanup(p.stop)

    def sent(self):
        output = io.BytesIO()
        done = m.send(self.c, 'chunk_cache', self.limits, output)
        return output.getvalue(), done

    def wire(self, raw, done, commit=True):
        output = io.BytesIO()
        for start in range(0, len(raw), 701):
            block = raw[start:start + 701]
            output.write(struct.pack('!I', len(block))); output.write(block)
        if commit:
            data = m.encoded(dict(done, sender_exit=0))
            output.write(struct.pack('!II', 0, len(data))); output.write(data)
        return output.getvalue()

    def received(self, wire):
        job = self.root / ('receive-' + str(len(list(self.root.iterdir())))); job.mkdir()
        self.last_job = job
        return m.receive(self.c, 'chunk_cache', self.limits, io.BytesIO(wire), job)

    def rewrite(self, raw, transform):
        members = []
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            for info in archive:
                data = archive.extractfile(info).read() if info.isfile() else b''
                members.append((info, data))
        members = transform(members)
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:', format=tarfile.GNU_FORMAT) as archive:
            for info, data in members:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data) if info.isfile() else None)
        return output.getvalue()

    def test_correct_long_names_and_single_hash_read(self):
        raw, done = self.sent()
        self.assertEqual(self.received(self.wire(raw, done))['status'], 'COMPLETE')
        for p in self.source.rglob('*'):
            if p.is_file():
                self.assertEqual(p.read_bytes(), (self.last_job / 'payload' / p.relative_to(self.source)).read_bytes())
        reader = m.HashReader(io.BytesIO(b'one pass'))
        self.assertEqual(reader.read(3) + reader.read(100), b'one pass')
        self.assertEqual(reader.count, 8)

    def test_disconnect_and_missing_sender_commit(self):
        raw, done = self.sent()
        for wire in (self.wire(raw, done, False), self.wire(raw, done)[:777]):
            with self.subTest(length=len(wire)), self.assertRaises((ValueError, tarfile.TarError)):
                self.received(wire)

    def test_truncated_tar_and_nonzero_padding(self):
        raw, done = self.sent()
        for changed in (raw[:512], raw.rstrip(b'\0'), raw[:-1] + b'X'):
            with self.subTest(length=len(changed)), self.assertRaises((ValueError, tarfile.TarError)):
                self.received(self.wire(changed, done))

    def test_hash_mismatch(self):
        raw, done = self.sent()
        changed = raw.replace(b'raw bytes\x00\xff', b'bad bytes\x00\xff')
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.received(self.wire(changed, done))

    def test_duplicate_header_terminal_and_commit_keys_rejected(self):
        raw, done = self.sent()
        for name in (m.HEADER, m.TERMINAL):
            def duplicate(xs):
                return [(info, b'{"component":"chunk_cache",' + data[1:] if info.name == name else data)
                        for info, data in xs]
            with self.subTest(member=name), self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
                self.received(self.wire(self.rewrite(raw, duplicate), done))
        commit = b'{"sender_exit":1,' + m.encoded(dict(done, sender_exit=0))[1:]
        wire = self.wire(raw, done, False) + struct.pack('!II', 0, len(commit)) + commit
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            self.received(wire)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            m.decoded('{"inventory":{"payload":1,"payload":2}}')

    def test_fsync_failure_closes_fd_and_preserves_partial(self):
        raw, done = self.sent()
        original = m.os.fsync
        failed = []
        def fsync(fd):
            if not failed and m.os.fstat(fd).st_size == len(RAW):
                failed.append(fd)
                raise OSError('fixture fsync failure')
            return original(fd)
        job = self.root / 'fsync-failure'
        with patch.object(m.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(self.wire(raw, done)))), \
             patch.object(m.os, 'fsync', side_effect=fsync):
            self.assertEqual(m.job_run('receive', self.c, 'chunk_cache', self.limits, job, 'configsha'), 1)
        self.assertEqual(len(failed), 1)
        with self.assertRaises(OSError):
            m.os.fstat(failed[0])
        self.assertEqual(next((job / 'payload/nested').iterdir()).read_bytes(), RAW)
        receipt = json.loads((job / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'ERROR')
        self.assertIn('fixture fsync failure', receipt['error'])

    def test_extra_missing_duplicate_and_post_terminal(self):
        raw, done = self.sent()
        transforms = [lambda xs: xs[:-2] + xs[-1:],
                      lambda xs: xs[:2] + [xs[1]] + xs[2:],
                      lambda xs: xs + [xs[1]]]
        def extra(xs):
            info = tarfile.TarInfo('payload/extra')
            return xs[:-1] + [(info, b'extra')] + xs[-1:]
        transforms.append(extra)
        for transform in transforms:
            with self.subTest(transform=transform), self.assertRaises((ValueError, tarfile.TarError)):
                self.received(self.wire(self.rewrite(raw, transform), done))

    def test_link_special_traversal(self):
        raw, done = self.sent()
        for kind, name in ((tarfile.SYMTYPE, 'payload/link'), (tarfile.LNKTYPE, 'payload/hard'),
                           (tarfile.FIFOTYPE, 'payload/fifo'), (tarfile.REGTYPE, '../escape')):
            def change(xs):
                info = tarfile.TarInfo(name); info.type = kind; info.linkname = '/outside'
                return xs[:1] + [(info, b'')] + xs[1:]
            with self.subTest(kind=kind), self.assertRaises((ValueError, tarfile.TarError)):
                self.received(self.wire(self.rewrite(raw, change), done))
        self.assertFalse((self.root / 'escape').exists())

    def test_reserve_and_caps(self):
        raw, done = self.sent()
        with patch.object(m.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), self.assertRaises(ValueError):
            self.received(self.wire(raw, done))
        for limits in (dict(self.limits, bytes=1), dict(self.limits, entries=1), dict(self.limits, manifest_bytes=1)):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                m.send(self.c, 'chunk_cache', limits, io.BytesIO())

    def test_metadata_change_prevents_completion(self):
        original = m.inventory
        calls = 0
        def changed(path, limits):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.source / 'new').write_bytes(b'new')
            return original(path, limits)
        with patch.object(m, 'inventory', side_effect=changed), self.assertRaisesRegex(ValueError, 'inventory changed'):
            self.sent()

    def test_wrong_identity_and_nonzero_commit(self):
        raw, done = self.sent()
        for bad in (dict(done, stream_id='b' * 32), dict(done, sender_exit=1)):
            data = m.encoded(dict(bad, sender_exit=bad.get('sender_exit', 0)))
            wire = self.wire(raw, done, False) + struct.pack('!II', 0, len(data)) + data
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.received(wire)

    def test_failure_receipt_preserves_partial_and_exclusive_job(self):
        raw, done = self.sent()
        wire = self.wire(raw.replace(b'raw bytes\x00\xff', b'bad bytes\x00\xff'), done)
        job = self.root / 'job'
        with patch.object(m.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(wire))):
            self.assertEqual(m.job_run('receive', self.c, 'chunk_cache', self.limits, job, 'configsha'), 1)
        self.assertEqual(json.loads((job / 'receipt.json').read_text())['status'], 'ERROR')
        self.assertTrue(list((job / 'payload/nested').iterdir()))
        with self.assertRaises(FileExistsError):
            m.job_run('receive', self.c, 'chunk_cache', self.limits, job, 'configsha')

    def test_source_link_refused(self):
        try:
            (self.source / 'link').symlink_to(self.source / 'empty')
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaises(ValueError):
            self.sent()

    def test_single_sqlite_file_receiver_integrity(self):
        db = self.root / 'fts.db'
        conn = sqlite3.connect(db)
        try:
            conn.execute('CREATE TABLE chunks (text TEXT)'); conn.commit()
        finally:
            conn.close()
        output = io.BytesIO()
        with patch.dict(m.COMPONENTS, {'fts.db': db}):
            done = m.send(self.c, 'fts.db', self.limits, output)
        job = self.root / 'sqlite-receive'; job.mkdir()
        with patch.object(m.sqlite3, 'connect', wraps=sqlite3.connect) as connect:
            self.assertEqual(m.receive(self.c, 'fts.db', self.limits, io.BytesIO(self.wire(output.getvalue(), done)), job)['status'], 'COMPLETE')
            self.assertEqual(connect.call_args.args[0], (job / 'payload').as_uri() + '?mode=ro&immutable=1')
        self.assertFalse((job / 'payload-wal').exists())
        self.assertFalse((job / 'payload-shm').exists())

    def test_publication_and_old_isolation_pins(self):
        # setUp mocks the network-facing guard; exercise the real function here.
        real = self._real_isolation
        pub = str(m.ROOT / 'publication_cache')
        c = dict(self.c, isolation_sha256=m.OLD_POST, publication_sha256=m.PUB_RECEIPT,
                 publication_receipt='/receipt', mount_ids=dict.fromkeys(m.snapshot.MOUNTS, 1))
        c['mount_ids'][pub] = 1449
        with patch.object(m.snapshot, 'isolation', return_value=dict(self.pins)), \
             patch.object(m.snapshot, 'digest', return_value=m.PUB_RECEIPT), \
             patch.object(Path, 'read_text', return_value=f'1449 1 8:1 /cache {pub} ro,relatime - ext4 /dev/a rw\n'):
            self.assertEqual(real(c)['mount_ids'][pub], 1449)
            with self.assertRaises(ValueError):
                real(dict(c, publication_sha256='bad'))
            with patch.object(Path, 'read_text', return_value=f'1449 1 8:1 /cache {pub} rw - ext4 /dev/a rw\n'), self.assertRaises(ValueError):
                real(c)

    def test_native_scope_command(self):
        c = dict(self.c, remote_module="/code v2/module's.py")
        for mode, role, host in [('send', 'src', 'root@161.97.126.166'),
                                 ('receive', 'dst', 'ubuntu@92.5.127.67')]:
            with patch.object(m.subprocess, 'Popen') as popen:
                argv = m.command(c, 'fts.db', host, mode, '/config space/a.json', "/jobs/it's here")
                popen.assert_not_called()
            self.assertEqual(argv[:-1], [m.SSH, '-T', '-o', 'BatchMode=yes', '-o',
                                         'ConnectTimeout=15', '-i', m.KEY, host])
            expected = (['sudo', '-n'] if mode == 'receive' else []) + [
                'systemd-run', '--scope', '--quiet',
                f'--unit=dtox-runtime-transport-{role}-fts-db-{c["stream_id"]}',
                '-p', 'CPUQuota=200%', '-p', 'MemoryMax=2G', '-p', 'MemorySwapMax=0',
                '-p', 'RuntimeMaxSec=infinity']
            if mode == 'receive':
                expected += ['/usr/bin/setpriv', '--reuid=1001', '--regid=1001',
                             '--clear-groups', '--no-new-privs']
            expected += ['/usr/bin/python3', c['remote_module'], mode, '--config',
                         '/config space/a.json', '--component', 'fts.db', '--job-dir', "/jobs/it's here"]
            self.assertEqual(m.shlex.split(argv[-1]), expected)

    def test_relay_commits_only_after_sender_exit_zero(self):
        raw, done = self.sent()
        class Keep(io.BytesIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        class Process:
            def __init__(self, stdout, stderr, code):
                self.stdout = io.BytesIO(stdout); self.stderr = io.BytesIO(stderr)
                self.stdin = Keep(); self.returncode = None; self.code = code
            def wait(self):
                self.returncode = self.code; return self.code
            def poll(self):
                return self.returncode
            def kill(self):
                self.code = -1
        c = dict(self.c, remote_module='/ops/runtime_data_transport.py', source_config='/source.json',
                 destination_config='/dest.json', source_job='/source/job', destination_job='/dest/job')
        for code in (0, 1):
            dst = Process(m.encoded(dict(done, sender_exit=0, status='COMPLETE')), b'', 0)
            src = Process(raw, m.encoded(done), code)
            with patch.object(m.subprocess, 'Popen', side_effect=[dst, src]):
                self.assertEqual(m.relay(c, 'chunk_cache', self.limits, self.root / f'relay-{code}', 'sha'), code)
            data = io.BytesIO(dst.stdin.saved); committed = False
            while data.tell() < len(dst.stdin.saved):
                length = struct.unpack('!I', data.read(4))[0]
                if length == 0:
                    committed = True; break
                data.read(length)
            self.assertEqual(committed, code == 0)


if __name__ == '__main__':
    unittest.main()
