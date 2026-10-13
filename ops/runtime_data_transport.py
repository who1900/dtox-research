"""Explicit per-component send/receive/relay; never activates, retries or deletes.

Deploy this and export_runtime_sqlite_snapshot.py together. Config extends the
snapshot config: mount_ids has ten paths; publication_receipt/publication_sha256,
snapshot_receipt/snapshot_receipt_sha256, stream_id (fresh 32 hex), and limits
(snapshot_bindings maps the two snapshot names to verified bytes/sha256).
(component -> bytes, entries, manifest_bytes). Relay additionally needs
source_config/destination_config and source_job/destination_job absolute paths.
Each job directory must be new. stdin/stdout are binary framed transport only.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import threading
import traceback

try:
    from . import export_runtime_sqlite_snapshot as snapshot
except ImportError:
    import export_runtime_sqlite_snapshot as snapshot

ROOT = Path('/opt/dtox-research')
CAPTURE = Path('/opt/dtox-source-recovery-20261012/sqlite-capture-jobs-v1/capture-1')
COMPONENTS = {name: ROOT / name for name in
              ('latex_cache', 'fulltext_cache', 'chunk_cache', 'section_cache', 'publication_cache', 'fts.db')}
COMPONENTS.update({'state.sqlite': CAPTURE / 'state.sqlite', 'papers.sqlite': CAPTURE / 'papers.sqlite'})
OLD_POST = '4051b4d39ddd52be4a72e0cd182f2118303d3505e03f99c451da849b2780d540'
PUB_RECEIPT = '6255ffeb0c4334f9cf113e0324aa0f19f5672ec9f100f1f71d1a827f84eaf8ed'
CHUNK = 1024 * 1024
ENTRY_OVERHEAD = 6144
RESERVE = 10 * 1024 ** 3
HEADER, TERMINAL = '_transport/header.json', '_transport/manifest.json'
SSH, KEY = 'C:/Windows/System32/OpenSSH/ssh.exe', 'C:/Users/User/.ssh/id_ed25519_claude_dtox'


def need(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def decoded(data):
    return json.loads(data, object_pairs_hook=unique_keys)


def config(path, component):
    raw = snapshot.plain(path).read_bytes()
    c = decoded(raw)
    need(component in COMPONENTS, 'component outside exact allowlist')
    need(re.fullmatch('[0-9a-f]{32}', c['stream_id']), 'fresh stream_id required')
    limits = c['limits'][component]
    for key, maximum in [('bytes', 64 * 1024 ** 3), ('entries', 1000000), ('manifest_bytes', 256 * CHUNK)]:
        need(type(limits[key]) is int and 0 < limits[key] <= maximum, 'invalid explicit limit')
    if component in ('state.sqlite', 'papers.sqlite'):
        need(limits['bytes'] <= (4 if component == 'state.sqlite' else 1) * 1024 ** 3, 'snapshot cap exceeds approved bound')
    return c, hashlib.sha256(raw).hexdigest(), limits


def isolation(c):
    pub = str(ROOT / 'publication_cache')
    need(c['isolation_sha256'] == OLD_POST and c['publication_sha256'] == PUB_RECEIPT, 'unapproved receipt pins')
    need(set(c['mount_ids']) == snapshot.MOUNTS | {pub} and c['mount_ids'][pub] == 1449, 'ten exact mounts required')
    base = dict(c, mount_ids={k: v for k, v in c['mount_ids'].items() if k != pub})
    actual = snapshot.isolation(base)
    need(snapshot.digest(c['publication_receipt']) == PUB_RECEIPT, 'publication receipt mismatch')
    mounts = [line.split(' - ', 1)[0].split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    matches = [f for f in mounts if f[4] == pub]
    need(len(matches) == 1 and int(matches[0][0]) == 1449 and 'ro' in matches[0][5].split(','), 'publication mount changed')
    actual['mount_ids'] = dict(c['mount_ids'])
    return actual


def fingerprint(path):
    p = snapshot.plain(path)
    s = p.lstat()
    kind = 'file' if stat.S_ISREG(s.st_mode) else 'directory' if stat.S_ISDIR(s.st_mode) else None
    need(kind is not None and (kind != 'file' or s.st_nlink == 1), 'links/special files refused')
    return [kind, s.st_size, s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns]


def inventory(path, limits):
    result = {'payload': fingerprint(path)}
    if result['payload'][0] == 'directory':
        def failed(error):
            raise error
        for parent, dirs, files in os.walk(path, followlinks=False, onerror=failed):
            dirs.sort(); files.sort()
            for name in dirs + files:
                p = Path(parent) / name
                result['payload/' + p.relative_to(path).as_posix()] = fingerprint(p)
                need(len(result) <= limits['entries'], 'entry limit exceeded')
    need(len(result) <= limits['entries'] and sum(v[1] for v in result.values() if v[0] == 'file') <= limits['bytes'], 'inventory exceeds cap')
    return result


def safe_name(name):
    p = PurePosixPath(name)
    need(name == p.as_posix() and not p.is_absolute() and '\\' not in name and ':' not in name
         and '..' not in p.parts and p.parts and p.parts[0] == 'payload'
         and len(name.encode()) <= 4096 and not any(ord(x) < 32 or ord(x) == 127 for x in name), 'unsafe member path')
    return name


class HashReader:
    def __init__(self, stream):
        self.stream, self.hash, self.count = stream, hashlib.sha256(), 0

    def read(self, size):
        data = self.stream.read(size)
        self.hash.update(data); self.count += len(data)
        return data


def json_member(archive, name, value, limit):
    data = encoded(value)
    need(len(data) <= limit, 'manifest limit exceeded')
    info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
    archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(data).hexdigest()


def expected_snapshot(c, component):
    if component not in ('state.sqlite', 'papers.sqlite'):
        return None
    need(snapshot.plain(c['snapshot_receipt']) == CAPTURE / 'receipt.json', 'snapshot receipt outside allowlist')
    need(snapshot.digest(c['snapshot_receipt']) == c['snapshot_receipt_sha256'], 'snapshot receipt mismatch')
    receipt = decoded(Path(c['snapshot_receipt']).read_bytes())
    need(receipt['status'] == 'COMPLETE', 'snapshot job not complete')
    entry = receipt['snapshots']['state' if component == 'state.sqlite' else 'papers']
    need(entry['path'] == str(COMPONENTS[component]) and entry['integrity'] == ['ok'], 'snapshot not verified')
    binding = {'sha256': entry['sha256'], 'bytes': entry['bytes']}
    need(binding == c['snapshot_bindings'][component], 'snapshot config binding mismatch')
    return binding


def send(c, component, limits, stream, audit=None):
    before = isolation(c)
    path = COMPONENTS[component]
    planned = inventory(path, limits)
    expected = expected_snapshot(c, component)
    header = {'component': component, 'stream_id': c['stream_id'], 'inventory': planned,
              'isolation_before': before, 'expected_snapshot': expected}
    if audit:
        durable(audit / 'source-before.json', header)
    records = {}
    with tarfile.open(fileobj=stream, mode='w|', format=tarfile.GNU_FORMAT) as archive:
        json_member(archive, HEADER, header, limits['manifest_bytes'])
        for name, fp in planned.items():
            safe_name(name)
            info = tarfile.TarInfo(name); info.mode = 0o700 if fp[0] == 'directory' else 0o600
            if fp[0] == 'directory':
                info.type = tarfile.DIRTYPE
                archive.addfile(info); records[name] = {'type': 'directory'}
                continue
            source = path if name == 'payload' else path / name[len('payload/'):]
            need(fingerprint(source) == fp, 'source changed before read')
            fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
            with os.fdopen(fd, 'rb') as raw:
                s = os.fstat(raw.fileno())
                need(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, 'opened file not regular/single-link')
                need([s.st_size, s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns] == fp[1:], 'opened inode changed')
                reader = HashReader(raw); info.size = fp[1]
                archive.addfile(info, reader)
                need(reader.count == fp[1] and not raw.read(1), 'source size changed')
                records[name] = {'type': 'file', 'bytes': reader.count, 'sha256': reader.hash.hexdigest()}
            need(fingerprint(source) == fp, 'source changed while reading')
        current = inventory(path, limits)
        if audit:
            durable(audit / 'source-after.json', current)
        need(current == planned, 'source inventory changed')
        after = isolation(c)
        need(after == before, 'isolation changed')
        if expected:
            need(records['payload']['sha256'] == expected['sha256'] and records['payload']['bytes'] == expected['bytes'], 'snapshot binding mismatch')
        manifest = {'component': component, 'stream_id': c['stream_id'], 'records': records,
                    'isolation_after': after, 'source_metadata_unchanged': True}
        manifest_sha = json_member(archive, TERMINAL, manifest, limits['manifest_bytes'])
    stream.flush()
    return {'component': component, 'stream_id': c['stream_id'], 'manifest_sha256': manifest_sha}


def exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        data = stream.read(size - len(chunks))
        need(bool(data), 'disconnected/truncated stream')
        chunks.extend(data)
    return bytes(chunks)


class Frames:
    def __init__(self, stream):
        self.stream, self.remaining, self.commit = stream, 0, None

    def read(self, size):
        if self.commit is not None:
            return b''
        if not self.remaining:
            self.remaining = struct.unpack('!I', exact(self.stream, 4))[0]
            need(self.remaining <= CHUNK, 'frame too large')
            if not self.remaining:
                length = struct.unpack('!I', exact(self.stream, 4))[0]
                need(0 < length <= 65536, 'invalid commit size')
                self.commit = decoded(exact(self.stream, length))
                need(not self.stream.read(1), 'trailing protocol bytes')
                return b''
        data = exact(self.stream, min(size, self.remaining))
        self.remaining -= len(data)
        return data


class PaddingReader:
    def __init__(self, stream, maximum):
        self.stream, self.count, self.tail, self.padding = stream, 0, b'', None
        self.maximum = maximum

    def read(self, size):
        data = self.stream.read(size)
        if self.padding is not None:
            need(not any(data), 'nonzero data after terminal member')
        self.count += len(data); self.tail = (self.tail + data)[-32768:]
        need(self.count <= self.maximum, 'archive wire cap exceeded')
        return data

    def finish(self, start):
        need(self.count - len(self.tail) <= start, 'padding escaped verification window')
        need(not any(self.tail[max(0, start - (self.count - len(self.tail))):]), 'nonzero buffered trailing data')
        self.padding = start
        while self.read(CHUNK):
            pass
        need(self.count >= start + 1024 and self.count % 512 == 0, 'missing tar end markers')


def read_json(archive, member, limit):
    need(member.isfile() and member.size <= limit, 'invalid metadata member')
    data = archive.extractfile(member).read(limit + 1)
    need(len(data) == member.size, 'truncated metadata member')
    return decoded(data), hashlib.sha256(data).hexdigest()


class StrictTarInfo(tarfile.TarInfo):
    def _proc_member(self, archive):
        need(self.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.GNUTYPE_LONGNAME), 'special/extension refused')
        if self.type == tarfile.GNUTYPE_LONGNAME:
            need(0 < self.size <= 4097, 'long name exceeds bound')
        return super()._proc_member(archive)


def receive(c, component, limits, stream, job):
    framed = Frames(stream)
    reader = PaddingReader(framed, limits['bytes'] + limits['entries'] * ENTRY_OVERHEAD + 2 * limits['manifest_bytes'] + 10240)
    records = {}; total = 0
    with tarfile.open(fileobj=reader, mode='r|', bufsize=10240, tarinfo=StrictTarInfo) as archive:
        first = archive.next()
        need(first is not None and first.name == HEADER, 'header missing')
        header, _ = read_json(archive, first, limits['manifest_bytes'])
        need(header['component'] == component and header['stream_id'] == c['stream_id'], 'wrong stream binding')
        need(header['isolation_before'] == {k: c[k] for k in ('boot_id', 'mount_ids', 'mount_namespace', 'api_identity')}, 'unexpected sender isolation pins')
        need(header['expected_snapshot'] == c.get('snapshot_bindings', {}).get(component), 'unexpected snapshot binding')
        planned = header['inventory']
        need(0 < len(planned) <= limits['entries'], 'inventory count cap')
        for name, fp in planned.items():
            safe_name(name)
            need(isinstance(fp, list) and len(fp) == 6 and fp[0] in ('file', 'directory')
                 and all(type(v) is int and v >= 0 for v in fp[1:]), 'invalid inventory metadata')
        admitted = sum(v[1] for v in planned.values() if v[0] == 'file')
        need(admitted <= limits['bytes'] and shutil.disk_usage(job).free >= RESERVE + admitted + 2 * limits['manifest_bytes'], 'admission/reserve failed')
        durable(job / 'source-header.json', header)
        terminal = None
        while True:
            member = archive.next()
            if member is None:
                break
            if member.name == TERMINAL:
                terminal, manifest_sha = read_json(archive, member, limits['manifest_bytes'])
                end = ((member.offset_data + member.size + 511) // 512) * 512
                need(archive.next() is None, 'member after terminal')
                break
            name = safe_name(member.name)
            need(name in planned and name not in records and len(records) < limits['entries'], 'extra/duplicate file')
            fp = planned[name]
            need(not member.pax_headers and member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE), 'link/special/header refused')
            target = job / name
            snapshot.plain(target)
            if member.isdir():
                need(fp[0] == 'directory' and member.size == 0, 'directory mismatch')
                target.mkdir(mode=0o700)
                snapshot.sync_directory(target.parent)
                records[name] = {'type': 'directory'}
            else:
                need(fp[0] == 'file' and member.size == fp[1], 'file size mismatch')
                total += member.size
                need(total <= limits['bytes'] and shutil.disk_usage(job).free >= RESERVE + member.size, 'byte cap/reserve failed')
                h = hashlib.sha256(); source = archive.extractfile(member); remaining = member.size
                with target.open('xb') as output:
                    snapshot.sync_directory(target.parent)
                    try:
                        while remaining:
                            data = source.read(min(CHUNK, remaining)); need(bool(data), 'truncated file')
                            need(shutil.disk_usage(job).free >= RESERVE + len(data), 'reserve breached')
                            output.write(data); h.update(data); remaining -= len(data)
                    finally:
                        output.flush(); os.fsync(output.fileno())
                records[name] = {'type': 'file', 'bytes': member.size, 'sha256': h.hexdigest()}
        need(terminal is not None and set(records) == set(planned), 'terminal/members missing')
        need(terminal['component'] == component and terminal['stream_id'] == c['stream_id']
             and terminal['records'] == records and terminal['source_metadata_unchanged'] is True
             and terminal['isolation_after'] == header['isolation_before'], 'manifest/hash/isolation mismatch')
        if header['expected_snapshot']:
            e = header['expected_snapshot']
            need(records['payload']['sha256'] == e['sha256'] and records['payload']['bytes'] == e['bytes'], 'snapshot mismatch')
        need(shutil.disk_usage(job).free >= RESERVE + len(encoded(terminal)), 'manifest reserve breached')
        durable(job / 'manifest.json', terminal)
        reader.finish(end)
    expected_commit = {'sender_exit': 0, 'component': component, 'stream_id': c['stream_id'], 'manifest_sha256': manifest_sha}
    need(framed.commit == expected_commit, 'sender exit0 commit missing/mismatched')
    for parent, dirs, files in os.walk(job / 'payload', topdown=False):
        snapshot.sync_directory(Path(parent))
    snapshot.sync_directory(job)
    if component in ('fts.db', 'state.sqlite', 'papers.sqlite'):
        db = sqlite3.connect((job / 'payload').as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)
        try:
            db.execute('PRAGMA query_only=ON')
            need([r[0] for r in db.execute('PRAGMA integrity_check')] == ['ok'], 'destination SQLite integrity failed')
        finally:
            db.close()
    return dict(expected_commit, bytes=total, entries=len(records), status='COMPLETE')


def job_run(mode, c, component, limits, job, config_sha):
    job = snapshot.plain(job)
    need(not any(job == p or p in job.parents for p in (ROOT, CAPTURE)), 'audit/staging overlaps source')
    job.mkdir(mode=0o700); snapshot.sync_directory(job.parent)
    receipt = {'status': 'ERROR', 'mode': mode, 'component': component, 'config_sha256': config_sha}
    durable(job / 'intent.json', receipt)
    try:
        receipt['result'] = send(c, component, limits, sys.stdout.buffer, audit=job) if mode == 'send' else receive(c, component, limits, sys.stdin.buffer, job)
        receipt['status'] = 'COMPLETE'
    except BaseException:
        receipt['error'] = traceback.format_exc()
    durable(job / 'receipt.json', receipt)
    if receipt['status'] == 'COMPLETE':
        print(json.dumps(receipt['result']), file=sys.stderr if mode == 'send' else sys.stdout, flush=True)
    return 0 if receipt['status'] == 'COMPLETE' else 1


class Capture:
    def __init__(self, pipe):
        self.data = bytearray(); self.overflow = False; self.error = None
        def drain():
            try:
                while True:
                    block = pipe.read(65536)
                    if not block:
                        break
                    self.overflow |= len(self.data) + len(block) > CHUNK
                    self.data.extend(block[:max(0, CHUNK - len(self.data))])
            except BaseException:
                self.error = traceback.format_exc()
        self.thread = threading.Thread(target=drain, daemon=True); self.thread.start()

    def finish(self):
        self.thread.join(); need(not self.overflow and self.error is None, 'diagnostic output cap/read error')
        return bytes(self.data)


def durable(path, value):
    if os.name != 'nt':
        return snapshot.durable_json(path, value)
    # Windows relay: file fsync only; remote Linux receipts fsync directories too.
    with path.open('x', encoding='utf-8') as output:
        json.dump(value, output, sort_keys=True); output.flush(); os.fsync(output.fileno())


def command(c, component, host, mode, config_path, remote_job):
    need(component in COMPONENTS and mode in ('send', 'receive'), 'invalid command role/component')
    need(len(c['stream_id']) == 32 and all(x in '0123456789abcdef' for x in c['stream_id']), 'invalid stream ID')
    need(all(PurePosixPath(p).is_absolute() and '..' not in PurePosixPath(p).parts for p in (c['remote_module'], config_path, remote_job)), 'absolute remote paths required')
    role = 'src' if mode == 'send' else 'dst'
    slug = component.replace('_', '-').replace('.', '-')
    args = (['sudo', '-n'] if mode == 'receive' else []) + [
        'systemd-run', '--scope', '--quiet',
        f'--unit=dtox-runtime-transport-{role}-{slug}-{c["stream_id"]}',
        '-p', 'CPUQuota=200%', '-p', 'MemoryMax=2G', '-p', 'MemorySwapMax=0',
        '-p', 'RuntimeMaxSec=infinity']
    if mode == 'receive':
        args += ['/usr/bin/setpriv', '--reuid=1001', '--regid=1001', '--clear-groups', '--no-new-privs']
    args += ['/usr/bin/python3', c['remote_module'], mode, '--config', config_path,
             '--component', component, '--job-dir', remote_job]
    return [SSH, '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-i', KEY, host, shlex.join(args)]


def relay(c, component, limits, job, config_sha):
    job = snapshot.plain(job); job.mkdir(mode=0o700)
    receipt = {'status': 'ERROR', 'component': component, 'config_sha256': config_sha}
    durable(job / 'intent.json', receipt)
    src = dst = None; captures = []
    try:
        dst = subprocess.Popen(command(c, component, 'ubuntu@92.5.127.67', 'receive', c['destination_config'], c['destination_job']), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        dst_out, dst_err = Capture(dst.stdout), Capture(dst.stderr); captures.extend([dst_out, dst_err])
        src = subprocess.Popen(command(c, component, 'root@161.97.126.166', 'send', c['source_config'], c['source_job']), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        src_err = Capture(src.stderr); captures.append(src_err)
        wire = 0
        while True:
            block = src.stdout.read(CHUNK)
            if not block:
                break
            wire += len(block)
            need(wire <= limits['bytes'] + limits['entries'] * ENTRY_OVERHEAD + 2 * limits['manifest_bytes'] + 10240, 'wire cap exceeded')
            dst.stdin.write(struct.pack('!I', len(block))); dst.stdin.write(block)
        code = src.wait(); diagnostic = src_err.finish()
        need(code == 0, 'sender failed; no commit')
        done = decoded(diagnostic)
        need(done['component'] == component and done['stream_id'] == c['stream_id'], 'sender completion binding mismatch')
        commit = encoded(dict(done, sender_exit=0))
        dst.stdin.write(struct.pack('!II', 0, len(commit))); dst.stdin.write(commit); dst.stdin.close()
        need(dst.wait() == 0, 'receiver failed')
        result = decoded(dst_out.finish()); dst_err.finish()
        need(result['status'] == 'COMPLETE' and all(result[k] == v for k, v in decoded(commit).items()), 'receiver ACK mismatch')
        receipt.update(status='COMPLETE', result=result, wire_bytes=wire)
    except BaseException:
        receipt['error'] = traceback.format_exc()
    finally:
        for process in (src, dst):
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
        for capture in captures:
            capture.thread.join()
        for process in (src, dst):
            if process is not None:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe and not pipe.closed:
                        pipe.close()
        receipt['diagnostics'] = [{'output': bytes(x.data).decode(errors='replace'), 'error': x.error, 'overflow': x.overflow} for x in captures]
    durable(job / 'receipt.json', receipt)
    return 0 if receipt['status'] == 'COMPLETE' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['send', 'receive', 'relay'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--component', choices=COMPONENTS, required=True)
    parser.add_argument('--job-dir', required=True)
    args = parser.parse_args()
    c, sha, limits = config(args.config, args.component)
    raise SystemExit(relay(c, args.component, limits, args.job_dir, sha) if args.mode == 'relay'
                     else job_run(args.mode, c, args.component, limits, args.job_dir, sha))
