"""Run a provenance-pinned, separate curated Web3 replacement queue."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ops.corpus_quality import replace_documents as worker

DEFAULT_BASE = worker.CORPUS_BASE / 'curated-web3-fulltext-20261018'
MANIFEST_KEY = 'curated_web3_manifest'


class _RepairLifecycleBusy(Exception):
    pass


def _repair_idle():
    status_path = worker.BASE / 'bulk-controller-status.json'
    try:
        status = json.loads(status_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    if (not isinstance(status, dict) or status.get('phase') != 'bulk_complete'
            or status.get('status') != 'pass_complete'):
        return False
    gate_path = worker.BASE / 'maintenance-gate.json'
    try:
        gate = json.loads(gate_path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return (isinstance(gate, dict) and gate.get('phase') in ('closed', 'restored')
            and gate.get('safe_to_restore') is True
            and gate.get('attention_required') is False)


def _wait_for_repair_complete(sleeper=None):
    sleeper = sleeper or time.sleep
    while not _repair_idle():
        sleeper(10)


def _require_repair_complete():
    if not _repair_idle():
        raise _RepairLifecycleBusy()


def _read_manifest(path):
    raw = Path(path).read_bytes()
    try:
        value = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise worker.WorkerError('curated manifest is not UTF-8 JSON') from exc
    if not isinstance(value, dict) or set(value) != {'schema', 'sources'} or value['schema'] != 1:
        raise worker.WorkerError('curated manifest schema mismatch')
    sources = value['sources']
    if not isinstance(sources, list) or not sources:
        raise worker.WorkerError('curated manifest requires explicit source rows')
    seen = set()
    requests = {}
    for row in sources:
        if (not isinstance(row, dict)
                or set(row) != {'paper_id', 'title', 'fresh_url', 'edition'}):
            raise worker.WorkerError('curated source row fields mismatch')
        paper_id, title, url, edition = (row['paper_id'], row['title'],
                                         row['fresh_url'], row['edition'])
        if (not isinstance(paper_id, str) or not paper_id.startswith('iacr:')
                or not re.fullmatch(r'iacr:\d{4}/\d{3,6}', paper_id)
                or paper_id in seen):
            raise worker.WorkerError('curated paper IDs must be unique canonical IACR IDs')
        if not isinstance(title, str) or len(title.strip()) < 12:
            raise worker.WorkerError('curated title is invalid')
        try:
            parsed = urlsplit(url)
            parsed.port
        except (TypeError, ValueError) as exc:
            raise worker.WorkerError('curated source URL is invalid') from exc
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise worker.WorkerError('curated source URL must be credential-free HTTPS')
        if not isinstance(edition, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', edition):
            raise worker.WorkerError('curated source edition must pin the acquired raw SHA-256')
        seen.add(paper_id)
        requests[paper_id] = dict(paper_id=paper_id, title=title,
            fresh_kind='PDF', fresh_url=url, fresh_version=edition,
            source_label='source-pdf')
    return hashlib.sha256(raw).hexdigest(), requests


def _validate_targets(root, requests, *, allow_upgraded=False):
    db = Path(root) / 'state.db'
    conn = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        for paper_id, request in requests.items():
            row = conn.execute('SELECT status,title,fulltext_source FROM papers WHERE arxiv_id=?',
                               (paper_id,)).fetchone()
            allowed_sources = ('iacr-abstract', 'source-pdf') if allow_upgraded else ('iacr-abstract',)
            if row is None or row['status'] != 'done' or row['fulltext_source'] not in allowed_sources:
                raise worker.WorkerError('curated target is not a done abstract-only paper: ' + paper_id)
            if row['title'] != request['title']:
                raise worker.WorkerError('curated title differs from live paper: ' + paper_id)
    finally:
        conn.close()


def initialize(base, manifest, root=worker.ROOT):
    base, root = Path(base).resolve(), Path(root).resolve(strict=True)
    digest, requests = _read_manifest(manifest)
    _validate_targets(root, requests)
    base.mkdir(parents=True, exist_ok=True)
    conn = worker._connect(worker.db_path(base))
    try:
        worker._schema(conn)
        with conn:
            prior = dict(conn.execute('SELECT key,value FROM meta'))
            identity = json.dumps(dict(schema=1, manifest_sha256=digest,
                sources={pid: {k: v for k, v in req.items() if k != 'source_label'}
                         for pid, req in sorted(requests.items())}),
                ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            if prior:
                if prior.get(MANIFEST_KEY) != identity:
                    raise worker.WorkerError('curated manifest changed; existing runbase retained')
            else:
                conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (MANIFEST_KEY, identity))
                now = worker.utc_now()
                conn.executemany("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,created_at,updated_at) "
                    "VALUES(?,NULL,NULL,NULL,'pending',?,?)",
                    [(paper_id, now, now) for paper_id in sorted(requests)])
    finally:
        conn.close()
    return dict(base=str(base), manifest_sha256=digest, targets=len(requests),
                paper_ids=sorted(requests))


def _stored_requests(base, manifest, root):
    digest, requests = _read_manifest(manifest)
    conn = worker._connect(worker.db_path(base), readonly=True)
    try:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (MANIFEST_KEY,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise worker.WorkerError('curated runbase is not initialized')
    try:
        stored = json.loads(row['value'])
    except ValueError as exc:
        raise worker.WorkerError('curated provenance record is invalid') from exc
    if stored.get('manifest_sha256') != digest:
        raise worker.WorkerError('curated manifest hash changed after initialization')
    _validate_targets(root, requests, allow_upgraded=True)
    return requests


def run(base, manifest, *, root=worker.ROOT, wait_for_lock=False, retry_deferred=False):
    base, root = Path(base).resolve(), Path(root).resolve(strict=True)
    if wait_for_lock:
        _wait_for_repair_complete()
    requests = _stored_requests(base, manifest, root)
    while True:
        try:
            return worker.run(base, root, papers=sorted(requests), source_requests=requests,
                              gate_base=worker.BASE, wait_for_lock=wait_for_lock,
                              retry_deferred=retry_deferred,
                              lock_preflight=_require_repair_complete if wait_for_lock else None)
        except _RepairLifecycleBusy:
            _wait_for_repair_complete()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--base', type=Path, default=DEFAULT_BASE)
    init.add_argument('--root', type=Path, default=worker.ROOT)
    init.add_argument('--manifest', type=Path, required=True)
    work = sub.add_parser('run')
    work.add_argument('--base', type=Path, default=DEFAULT_BASE)
    work.add_argument('--root', type=Path, default=worker.ROOT)
    work.add_argument('--manifest', type=Path, required=True)
    work.add_argument('--wait-for-lock', action='store_true')
    work.add_argument('--retry-deferred', action='store_true')
    args = parser.parse_args(argv)
    result = (initialize(args.base, args.manifest, args.root) if args.command == 'init'
              else run(args.base, args.manifest, root=args.root,
                       wait_for_lock=args.wait_for_lock,
                       retry_deferred=args.retry_deferred))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
