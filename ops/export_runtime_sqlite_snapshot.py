"""Two-DB capture only; no transfer, checkpoint, cleanup or automatic retry.

CLI: --config FILE --job-dir NEW_ABSOLUTE_DIRECTORY. Config keys:
isolation_receipt, isolation_sha256, boot_id, mount_ids (exact path -> ID),
api_source, api_sha256, api_identity, mount_namespace. Pins come from MAIN's approved receipt.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import traceback
from datetime import datetime, timezone

GIB = 1024 ** 3
RESERVE = 10 * GIB
API_IDENTITY = {'pid': 3687787, 'starttime': 766380260,
                'invocation_id': '6cd85377d3cf44e6bf5909edcb2d3ec2'}
ROOT = Path('/opt/dtox-research')
DATABASES = {'state': (ROOT / 'state.db', 4 * GIB, {'papers', 'citations', 'harvest_cursor'}),
             'papers': (ROOT / 'papers_fts.db', GIB, {'papers_fts'})}
MOUNTS = {str(ROOT / name) for name in ('latex_cache', 'fulltext_cache', 'chunk_cache',
          'section_cache', 'state.db', 'state.db-wal', 'fts.db', 'papers_fts.db', 'papers_fts.db-wal')}


def plain(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('absolute non-traversing path required')
    for part in (*reversed(path.parents), path):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('symlink/reparse path refused')
    return path


def digest(path):
    with plain(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def metadata(path):
    path = plain(path)
    try:
        s = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
        raise ValueError('source must be a single-link regular file')
    return {k: getattr(s, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}


def sources():
    return {str(p): metadata(p) for db, _, _ in DATABASES.values()
            for p in (db, Path(str(db) + '-wal'))}


def api_identity(expected):
    if expected != API_IDENTITY:
        raise ValueError('API identity differs from approved frozen identity')
    pid = expected['pid']
    def starttime():
        value = Path(f'/proc/{pid}/stat').read_text()
        if int(value.split(' ', 1)[0]) != pid:
            raise ValueError('API stat PID mismatch')
        return int(value.rsplit(')', 1)[1].split()[19])
    before = starttime()
    result = subprocess.run(['/usr/bin/systemctl', 'show', 'dtox-research-api.service',
                             '--property=MainPID', '--property=InvocationID'],
                            check=True, capture_output=True, text=True, timeout=10)
    properties = dict(line.split('=', 1) for line in result.stdout.splitlines())
    actual = {'pid': int(properties['MainPID']), 'starttime': before,
              'invocation_id': properties['InvocationID']}
    if actual != expected or starttime() != before:
        raise ValueError('live API PID/starttime/invocation changed')
    return actual


def isolation(c):
    if digest(c['isolation_receipt']) != c['isolation_sha256']:
        raise ValueError('isolation receipt hash mismatch')
    if digest(c['api_source']) != c['api_sha256']:
        raise ValueError('API code hash mismatch')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    namespace = os.readlink('/proc/self/ns/mnt')
    if namespace != c['mount_namespace'] or namespace != os.readlink('/proc/1/ns/mnt'):
        raise ValueError('not approved host mount namespace')
    if boot != c['boot_id'] or set(c['mount_ids']) != MOUNTS:
        raise ValueError('boot or exact mount allowlist mismatch')
    found = {}
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, _ = line.split(' - ', 1)
        fields = left.split()
        if fields[4] in MOUNTS:
            if fields[4] in found or 'ro' not in fields[5].split(','):
                raise ValueError('ambiguous/non-readonly mount')
            found[fields[4]] = int(fields[0])
    if found != c['mount_ids'] or plain(ROOT / 'fts.db-wal').exists():
        raise ValueError('mount IDs changed or unexpected FTS WAL')
    return {'boot_id': boot, 'mount_ids': found, 'mount_namespace': namespace,
            'api_identity': api_identity(c['api_identity'])}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_json(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def schema(conn):
    return conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()


def source_schema(db, tables):
    conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=0)
    try:
        conn.execute('PRAGMA query_only=ON')
        actual = schema(conn)
        if not tables <= {r[1] for r in actual if r[0] == 'table'}:
            raise ValueError('required source table missing')
        return actual
    finally:
        conn.close()


def backup(db, output, cap, tables):
    before = [metadata(db), metadata(Path(str(db) + '-wal'))]
    if before[0] is None or sum(x['st_size'] for x in before if x) > cap:
        raise ValueError('source DB+WAL exceeds snapshot bound or DB missing')
    if shutil.disk_usage(output.parent).free < RESERVE + cap:
        raise ValueError('insufficient reserve plus conservative snapshot bound')
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    sync_directory(output.parent)
    source = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=0)
    try:
        source.execute('PRAGMA query_only=ON')
        expected = schema(source)
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        if source.execute('PRAGMA page_count').fetchone()[0] * page_size > cap:
            raise ValueError('logical DB exceeds byte bound')
        def progress(status, remaining, total):
            if status not in (sqlite3.SQLITE_OK, sqlite3.SQLITE_DONE):
                raise RuntimeError('backup busy/error; no retry')
            if total * page_size > cap or output.stat().st_size > cap:
                raise ValueError('snapshot byte bound exceeded')
            if shutil.disk_usage(output.parent).free < RESERVE + 128 * page_size:
                raise ValueError('free-space reserve breached')
        target = sqlite3.connect(str(output), timeout=0)
        try:
            source.backup(target, pages=128, progress=progress, sleep=0)
        finally:
            target.close()
    finally:
        source.close()
    with output.open('r+b') as stream:
        os.fsync(stream.fileno())
    checked = sqlite3.connect(output.as_uri() + '?mode=ro', uri=True, timeout=0)
    try:
        checked.execute('PRAGMA query_only=ON')
        integrity = [r[0] for r in checked.execute('PRAGMA integrity_check')]
        actual = schema(checked)
        if integrity != ['ok'] or actual != expected or not tables <= {r[1] for r in actual if r[0] == 'table'}:
            raise ValueError('integrity/schema verification failed')
        if checked.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('foreign key verification failed')
    finally:
        checked.close()
    if before != [metadata(db), metadata(Path(str(db) + '-wal'))]:
        raise ValueError('source metadata changed')
    return {'path': str(output), 'bytes': output.stat().st_size, 'sha256': digest(output),
            'integrity': integrity, 'schema': actual, 'source_before_after': before}


def run(config, job):
    job = plain(job)
    if job == ROOT or ROOT in job.parents:
        raise ValueError('job directory cannot be in source tree')
    job.mkdir(mode=0o700)
    sync_directory(job.parent)
    receipt = {'status': 'ERROR', 'started_at': datetime.now(timezone.utc).isoformat(),
               'snapshots': {}, 'no_delete': True, 'no_retry': True}
    try:
        raw = plain(config).read_bytes()
        c = json.loads(raw)
        receipt.update(config_sha256=hashlib.sha256(raw).hexdigest(), pins=c)
        durable_json(job / 'intent.json', receipt)
        receipt['isolation_before'] = isolation(c)
        receipt['source_before'] = sources()
        receipt['source_schema'] = {name: source_schema(db, tables)
                                    for name, (db, _, tables) in DATABASES.items()}
        durable_json(job / 'capture-start.json', receipt)
        for name, (db, cap, tables) in DATABASES.items():
            receipt['snapshots'][name] = backup(db, job / (name + '.sqlite'), cap, tables)
            durable_json(job / (name + '.receipt.json'), receipt['snapshots'][name])
        receipt['source_after'] = sources()
        receipt['isolation_after'] = isolation(c)
        if receipt['source_before'] != receipt['source_after']:
            raise ValueError('dataset changed across capture interval')
        receipt['status'] = 'COMPLETE'
    except BaseException:
        receipt['error'] = traceback.format_exc()
        try:
            receipt['source_after_error'] = sources()
        except BaseException:
            receipt['source_after_error'] = traceback.format_exc()
        if 'pins' in receipt:
            try:
                receipt['isolation_after_error'] = isolation(receipt['pins'])
            except BaseException:
                receipt['isolation_after_error'] = traceback.format_exc()
    receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
    durable_json(job / 'receipt.json', receipt)
    return 0 if receipt['status'] == 'COMPLETE' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--job-dir', required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.config, args.job_dir))
