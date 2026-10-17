"""Prepare and publish complete source replacements for a frozen target set.

The queue and status live in one operational SQLite database. Source acquisition,
conversion, canonical parsing and embedding run before the publication maintenance
gate. The existing snapshot owner writes every corpus surface and its journal.
"""
from __future__ import annotations

import argparse
import errno
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import hashlib
import gzip
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
from email.utils import parsedate_to_datetime
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import sys



CORPUS_BASE = Path('/opt/dtox-corpus-quality-20261013')
BASE = CORPUS_BASE / 'whole-document-replacement-20261014'
ROOT = Path('/opt/dtox-research')
NATIVE_BUILD = Path('/opt/dtox-extraction-eval-20261012/baseline/native_build')
ORIGINAL_STORE = Path('/opt/dtox-source-recovery-20261012/originals-store-v1')
EMBED_BATCH_SIZE = 64
PREFETCH_WORKERS = int(os.getenv('DTOX_REPAIR_ACQUIRE_WORKERS', '6'))
CANDIDATE_WORKERS = int(os.getenv('DTOX_REPAIR_CANDIDATE_WORKERS', '2'))
MAX_IN_FLIGHT = int(os.getenv('DTOX_REPAIR_MAX_IN_FLIGHT', '12'))
API_UNIT = 'dtox-research-api-oracle-20261012.service'
WRITER_UNITS = ('dtox-research.service', 'dtox-pipeline.service')
SCHEMA = 1
PHASE_KEYS = ('acquisition_queue_wait_seconds', 'source_resolution_seconds',
              'fetch_seconds', 'convert_seconds', 'acquire_parse_seconds',
              'embedding_seconds', 'candidate_build_seconds', 'prepare_seconds', 'apply_seconds')


class WorkerError(RuntimeError):
    """A shared dependency or unsafe worker state prevents this run."""


class RetryableWorkerError(WorkerError):
    """A known temporary shared service outage; a managed run may restart."""


class DeferredPaper(RuntimeError):
    """A source or document-specific preflight does not support replacement."""


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def db_path(base=BASE):
    return Path(base) / 'whole-document-replacement.sqlite'


def _connect(path, readonly=False):
    path = Path(path).resolve(strict=True) if readonly else Path(path).resolve()
    if readonly:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


def _identity(path, *, mutable=False):
    p = Path(path).resolve(strict=True)
    st = p.stat()
    identity = dict(path=str(p), device=st.st_dev, inode=st.st_ino)
    if not mutable:
        identity.update(size=st.st_size, mtime_ns=st.st_mtime_ns)
    return identity


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode()
    tmp = path.with_name('.' + path.name + '.' + str(os.getpid()) + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if os.name != 'nt':
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    finally:
        if tmp.exists():
            tmp.unlink()


def _schema(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS targets(
        paper_id TEXT PRIMARY KEY,
        cache_ufffd INTEGER NOT NULL,
        source_ufffd INTEGER NOT NULL,
        empty_fulltext INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN
          ('pending','staging','prepared','applying','applied','deferred','failed')),
        reason TEXT,
        journal_path TEXT,
        metrics_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS targets_status ON targets(status,paper_id);
    ''')


def _target_rows(census_db, state_db):
    """Select only frozen root facts and records still done in live state."""
    census = _connect(census_db, readonly=True)
    state = _connect(state_db, readonly=True)
    try:
        meta = dict(census.execute('SELECT key,value FROM meta'))
        if meta.get('run_complete') != '1':
            raise WorkerError('complete frozen corpus census required')
        columns = {r['name'] for r in census.execute('PRAGMA table_info(papers)')}
        required = {'paper_id','cache_replacement_count','source_replacement_count',
                    'source_class','chunk_count'}
        if not required <= columns:
            raise WorkerError('frozen census schema missing replacement facts')
        state_columns = {r['name'] for r in state.execute('PRAGMA table_info(papers)')}
        if not {'arxiv_id','status'} <= state_columns:
            raise WorkerError('live state papers schema unavailable')
        rows = census.execute('''SELECT paper_id,cache_replacement_count,
                    source_replacement_count,source_class,chunk_count
                  FROM papers
                 WHERE COALESCE(cache_replacement_count,0)>0
                    OR COALESCE(source_replacement_count,0)>0
                    OR (source_class='fulltext' AND COALESCE(chunk_count,0)=0)
                 ORDER BY paper_id''')
        result = []
        for row in rows:
            if not state.execute("SELECT 1 FROM papers WHERE arxiv_id=? AND status='done'",
                                 (row['paper_id'],)).fetchone():
                continue
            result.append((row['paper_id'], int(row['cache_replacement_count'] or 0),
                           int(row['source_replacement_count'] or 0),
                           int(row['source_class'] == 'fulltext' and row['chunk_count'] == 0)))
        return result, meta
    finally:
        census.close()
        state.close()


def initialize(base=BASE, census_db=None, state_db=None):
    base = Path(base).resolve()
    census_db = Path(census_db or (base.parent / 'census.sqlite')).resolve(strict=True)
    state_db = Path(state_db or (ROOT / 'state.db')).resolve(strict=True)
    base.mkdir(parents=True, exist_ok=True)
    census_identity, state_identity = _identity(census_db), _identity(state_db, mutable=True)
    targets, census_meta = _target_rows(census_db, state_db)
    conn = _connect(db_path(base))
    try:
        _schema(conn)
        existing = dict(conn.execute('SELECT key,value FROM meta'))
        identity = dict(census=census_identity, state=state_identity,
                        run_id=census_meta.get('run_id'), schema=SCHEMA)
        if existing:
            for key, expected in (('identity', identity),):
                if key in existing and json.loads(existing[key]) != expected:
                    raise WorkerError('frozen census or state identity changed; queue retained')
        now = utc_now()
        with conn:
            conn.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',
                         ('identity', json.dumps(identity, sort_keys=True)))
            conn.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',
                         ('candidate_count', str(len(targets))))
            for paper_id, cache_bad, source_bad, empty in targets:
                reasons = (cache_bad > 0, source_bad > 0, bool(empty))
                conn.execute('''INSERT OR IGNORE INTO targets
                    (paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,created_at,updated_at)
                    VALUES(?,?,?,?, 'pending',?,?)''',
                    (paper_id, int(reasons[0]), int(reasons[1]), int(reasons[2]), now, now))
            actual = conn.execute('SELECT COUNT(*) FROM targets').fetchone()[0]
            expected = int(dict(conn.execute('SELECT key,value FROM meta'))['candidate_count'])
            if actual != expected or expected != len(targets):
                raise WorkerError('frozen target set differs from existing queue')
    finally:
        conn.close()
    return status_snapshot(base)


def _update(base, paper_id, status, *, reason=None, journal_path=None, metrics=None):
    conn = _connect(db_path(base))
    try:
        with conn:
            prior = conn.execute('SELECT metrics_json FROM targets WHERE paper_id=?',
                                 (paper_id,)).fetchone()
            old_metrics = json.loads(prior[0] or '{}') if prior else {}
            if metrics is not None:
                metrics = dict(metrics)
                if 'source_transport_retry_passes' in old_metrics:
                    metrics.setdefault('source_transport_retry_passes', old_metrics['source_transport_retry_passes'])
                new_metrics = dict(metrics)
                active = conn.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
                if active:
                    run = json.loads(active[0])
                    totals = run.setdefault('phase_seconds', {})
                    for key in PHASE_KEYS:
                        before, after = old_metrics.get(key, 0), new_metrics.get(key, 0)
                        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                            totals[key] = max(0.0, totals.get(key, 0.0) - before + after)
                    run['updated_epoch'] = time.time()
                    conn.execute("UPDATE meta SET value=? WHERE key='active_run'",
                                 (json.dumps(run, separators=(',', ':')),))
            cur = conn.execute('''UPDATE targets SET status=?,reason=?,
                journal_path=COALESCE(?,journal_path),metrics_json=COALESCE(?,metrics_json),updated_at=?
                WHERE paper_id=?''',
                (status, reason, str(journal_path) if journal_path else None,
                 json.dumps(metrics, ensure_ascii=False, allow_nan=False) if metrics is not None else None,
                 utc_now(), paper_id))
            if cur.rowcount != 1:
                raise WorkerError('paper is not in the frozen replacement queue')
    finally:
        conn.close()
    write_status(base)


def _update_metrics(base, paper_id, metrics):
    conn = _connect(db_path(base))
    try:
        with conn:
            prior = conn.execute('SELECT metrics_json FROM targets WHERE paper_id=?',
                                 (paper_id,)).fetchone()
            old_metrics = json.loads(prior[0] or '{}') if prior else {}
            metrics = dict(metrics)
            if 'source_transport_retry_passes' in old_metrics:
                metrics.setdefault('source_transport_retry_passes', old_metrics['source_transport_retry_passes'])
            active = conn.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
            if active:
                run = json.loads(active[0])
                if isinstance(metrics.get('journal_size_bytes'), (int, float)):
                    run.setdefault('journal_size_bytes', {})[paper_id] = metrics['journal_size_bytes']
                run['updated_epoch'] = time.time()
                conn.execute("UPDATE meta SET value=? WHERE key='active_run'",
                             (json.dumps(run, separators=(',', ':')),))
            cur = conn.execute('UPDATE targets SET metrics_json=?,updated_at=? WHERE paper_id=?',
                               (json.dumps(metrics, ensure_ascii=False, allow_nan=False), utc_now(), paper_id))
            if cur.rowcount != 1:
                raise WorkerError('paper is not in the frozen replacement queue')
    finally:
        conn.close()
    write_status(base)


def _get_targets(base, statuses, papers=None, limit=None):
    if not statuses:
        return []
    conn = _connect(db_path(base), readonly=True)
    try:
        sql = 'SELECT * FROM targets WHERE status IN (' + ','.join('?' for _ in statuses) + ')'
        params = list(statuses)
        if papers:
            sql += ' AND paper_id IN (' + ','.join('?' for _ in papers) + ')'
            params.extend(papers)
        sql += ' ORDER BY paper_id'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(limit)
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _counts(base):
    conn = _connect(db_path(base), readonly=True)
    try:
        return {r[0]: r[1] for r in conn.execute(
            'SELECT status,COUNT(*) FROM targets GROUP BY status ORDER BY status')}
    finally:
        conn.close()


def _selected_counts(base, paper_ids):
    conn = _connect(db_path(base), readonly=True)
    try:
        result = {}
        for batch in batched(paper_ids, 500):
            if not batch:
                continue
            marks = ','.join('?' for _ in batch)
            for status, count in conn.execute(
                    f'SELECT status,COUNT(*) FROM targets WHERE paper_id IN ({marks}) GROUP BY status', batch):
                result[status] = result.get(status, 0) + count
        return result
    finally:
        conn.close()


def _order_preparation(selected, cache_dir, cache):
    """Start costly sources early using existing cache-file sizes as estimates."""
    sizes = {}
    for row in selected:
        path = Path(cache_dir) / (cache.safe_id(row['paper_id']) + '.json')
        try:
            sizes[row['paper_id']] = path.lstat().st_size
        except FileNotFoundError:
            sizes[row['paper_id']] = 0
    return sorted(selected, key=lambda row: (row['status'] != 'prepared',
                  -sizes[row['paper_id']], row['paper_id']))


def _rowid_floors(runtime):
    floors = {}
    owner = runtime['owner']
    labels = runtime['restore'].labels
    for key, path, table in (
            ('chunks', runtime['paths']['fts_db'], 'chunks'),
            ('papers_fts', runtime['paths']['paper_fts_db'], 'papers_fts')):
        conn = labels._connect(path, readonly=True)
        try:
            floors[key] = int(owner._document_last_rowid(conn, table))
        finally:
            conn.close()
    return floors


def _advance_rowid_floors(floors, plan):
    paper = plan.get('paper') or {}
    for key, name in (('chunks', 'fts_after'), ('papers_fts', 'paper_fts_after')):
        values = paper.get(name) or []
        if values:
            floors[key] = max(floors[key], max(int(row[0]) for row in values))


def status_snapshot(base=BASE, run=None):
    base = Path(base)
    counts = _counts(base) if db_path(base).is_file() else {}
    total = sum(counts.values())
    if run is None and db_path(base).is_file():
        conn = _connect(db_path(base), readonly=True)
        try:
            value = conn.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
            if value is None:
                value = conn.execute("SELECT value FROM meta WHERE key='last_run'").fetchone()
            run = json.loads(value[0]) if value else None
        finally:
            conn.close()
    progress = None
    if run is not None and db_path(base).is_file():
        conn = _connect(db_path(base), readonly=True)
        try:
            ids = run['target_ids']
            selected = {status: count for status, count in conn.execute(
                'SELECT status,COUNT(*) FROM targets WHERE paper_id IN '
                '(SELECT value FROM json_each(?)) GROUP BY status',
                (json.dumps(ids, separators=(',', ':')),))}
            phase_seconds = run.get('phase_seconds', {})
        finally:
            conn.close()
        done = selected.get('applied', 0) + selected.get('deferred', 0) + selected.get('failed', 0)
        started = float(run['started_epoch'])
        now = time.time()
        stale = False
        if not run.get('finished'):
            try:
                os.kill(int(run['pid']), 0)
            except (KeyError, TypeError, ValueError, OSError):
                stale = True
        stop_time = float(run['finished_epoch']) if run.get('finished_epoch') is not None else None
        if stale:
            # A dead worker's last run timestamp is meaningful only when the
            # controller records a terminal event for that exact worker PID.
            try:
                controller = json.loads((base / 'bulk-controller-status.json').read_text(encoding='utf-8'))
                controller_time = float(controller.get('time', controller.get('epoch')))
                if (controller.get('worker_pid') == int(run['pid'])
                        and controller.get('phase') in ('bulk_interrupted', 'bulk_complete')
                        and math.isfinite(controller_time)
                        and started <= controller_time <= now):
                    stop_time = controller_time
                else:
                    stop_time = None
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                stop_time = None
        elif stop_time is None:
            stop_time = now
        elapsed = max(0.001, stop_time - started) if stop_time is not None and started <= stop_time else None
        rate = selected.get('applied', 0) / elapsed if elapsed is not None else None
        considered_rate = done / elapsed if elapsed is not None else None
        remaining = max(0, len(ids) - done)
        eta = round(remaining / considered_rate, 1) if considered_rate and considered_rate > 0 and done >= 10 and not stale else None
        progress = dict(run_id=run.get('run_id'), phase=('complete' if run.get('finished') else 'interrupted' if stale else 'running'),
                        scope='selected run subset only', selected_documents=len(ids),
                        completed_documents=done, applied_documents=selected.get('applied', 0),
                        deferred_documents=selected.get('deferred', 0),
                        failed_documents=selected.get('failed', 0),
                        remaining_documents=remaining, phase_counts=selected,
                        phase_seconds={key: round(value, 3) for key, value in phase_seconds.items()},
                        journal_size_bytes=run.get('journal_size_bytes', {}),
                        elapsed_seconds=round(elapsed, 1) if elapsed is not None else None,
                        throughput_docs_per_minute=round(rate * 60, 3) if rate is not None else None,
                        considered_docs_per_minute=round(considered_rate * 60, 3) if considered_rate is not None else None,
                        eta_seconds=eta, eta_scope='selected run subset only; never whole corpus')
    return dict(schema=SCHEMA, phase=(progress or {}).get('phase', 'status'), scope='frozen replacement candidates only',
                candidate_count=total, statuses=counts, whole_corpus_quality_complete=False,
                run=progress or run, disk_free_bytes=shutil.disk_usage(base if base.exists() else base.parent).free)


def write_status(base=BASE, run=None):
    if run and run.get('finished'):
        conn = _connect(db_path(base))
        try:
            with conn:
                conn.execute("DELETE FROM meta WHERE key='active_run'")
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('last_run',?)",
                             (json.dumps(run, ensure_ascii=False, separators=(',', ':')),))
        finally:
            conn.close()
    elif run is None:
        conn = _connect(db_path(base))
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
            if row:
                active = json.loads(row[0])
                now = time.time()
                status_path = Path(base) / 'status.json'
                if now - float(active.get('status_epoch', 0)) < 10 and status_path.is_file():
                    return json.loads(status_path.read_text(encoding='utf-8'))
                active['status_epoch'] = now
                with conn:
                    conn.execute("UPDATE meta SET value=? WHERE key='active_run'",
                                 (json.dumps(active, separators=(',', ':')),))
        finally:
            conn.close()
    value = status_snapshot(base, run)
    _atomic_json(Path(base) / 'status.json', value)
    return value


def batched(values, size=EMBED_BATCH_SIZE):
    if type(size) is not int or size < 1:
        raise ValueError('positive batch size required')
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _load_runtime(base, root=ROOT):
    import inspect
    from ops import replace_published_labels as owner
    from ops import restore_cached_points as restore
    from pipeline import document_cache, extractor
    from ops.corpus_quality import fresh_sources, run_label_worker

    signature = inspect.signature(fresh_sources.acquire_and_parse)
    if 'original_store_root' not in signature.parameters:
        raise WorkerError('fresh source helper lacks verified OriginalStore registration')
    if not callable(getattr(fresh_sources, 'resolve_arxiv_version', None)):
        raise WorkerError('fresh source helper lacks explicit arXiv version resolver')
    paths = {
        'state_db': (root / 'state.db').resolve(strict=True),
        'fts_db': (root / 'fts.db').resolve(strict=True),
        'paper_fts_db': (root / 'papers_fts.db').resolve(strict=True),
        'cache_dir': (root / 'chunk_cache').resolve(strict=True),
        'section_cache_dir': (root / 'section_cache').resolve(strict=True),
        'fulltext_cache_dir': (root / 'fulltext_cache').resolve(strict=True),
        'publication_cache_dir': (root / 'publication_cache').resolve(strict=True),
    }
    for key in ('cache_dir', 'section_cache_dir', 'fulltext_cache_dir', 'publication_cache_dir'):
        document_cache._cache_directory(paths[key])
    if not Path(NATIVE_BUILD).is_dir():
        raise WorkerError('verified PDF native build is unavailable')
    code_root = Path(__file__).resolve().parents[2]
    verify_code = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); "
        "sys.path.insert(0,sys.argv[2]); from pipeline import pdf_memory; "
        "print(json.dumps(pdf_memory.verify_native_build(sys.argv[2])))")
    verify = subprocess.run([sys.executable, '-B', '-c', verify_code,
                             str(code_root), str(NATIVE_BUILD)], cwd=str(code_root),
                            capture_output=True, text=True, timeout=30)
    if verify.returncode != 0:
        raise WorkerError('verified PDF native build preflight failed: ' + verify.stderr[-500:])
    try:
        native_receipt = json.loads(verify.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise WorkerError('PDF native build preflight returned invalid receipt') from exc
    recipe_path = Path(base).parent / 'embedding-recipe.json'
    recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
    if (recipe.get('dimension') != 384 or recipe.get('document_prefix') != ''
            or recipe.get('pooling') != 'CLS/L2' or recipe.get('max_length') != 512
            or not recipe.get('model_sha256') or not recipe.get('tokenizer_sha256')
            or not isinstance(recipe.get('endpoint'), str)):
        raise WorkerError('deployed 384D embedding recipe is incomplete')
    endpoint = recipe['endpoint']
    if not endpoint.startswith('http://127.0.0.1:') or not endpoint.endswith('/embed_batch'):
        raise WorkerError('unexpected configured document embedding endpoint')
    health_url = endpoint[:-len('/embed_batch')] + '/healthz'
    _check_health(health_url, 'document embedding endpoint')
    client = run_label_worker.Qdrant()
    for collection in ('papers_fulltext', 'papers_coarse_base'):
        try:
            client.request('/collections/' + collection, {}, method='GET')
        except Exception as exc:
            raise _shared_failure('Qdrant preflight failed', exc) from exc
    fts = restore.labels._connect(paths['fts_db'], readonly=True)
    try:
        restore._catalog(fts)
        if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone() != (1,):
            raise WorkerError('complete FTS ownership map is required')
    finally:
        fts.close()
    return dict(owner=owner, restore=restore, document_cache=document_cache,
                extractor=extractor, fresh=fresh_sources, worker=run_label_worker,
                client=client, paths=paths, recipe=recipe, native_receipt=native_receipt)


def is_arxiv_id(paper_id):
    # The API ExecCondition runs this file directly outside the repository.
    # Keep its startup-check path free of project imports.
    from ops.corpus_quality.fresh_sources import is_arxiv_id as check
    return check(paper_id)


def _fresh_request(state, fresh):
    paper_id = state['arxiv_id']
    if paper_id.startswith('acl:'):
        return dict(paper_id=paper_id, fresh_kind='PDF', fresh_version=paper_id)
    if is_arxiv_id(paper_id):
        resolved = fresh.resolve_arxiv_version(paper_id, timeout=30)
        version = resolved.get('fresh_version')
        if not version:
            raise DeferredPaper('fresh_arxiv_version_unresolved')
        return dict(paper_id=paper_id, fresh_kind='HTML', fresh_version=version,
                    title=resolved.get('title'), resolver_receipt=resolved)
    source_url = state.get('source_url')
    if paper_id.startswith('pmlr:') and isinstance(source_url, str):
        from ops.corpus_quality.fresh_sources import canonical_source_url
        return dict(paper_id=paper_id, fresh_kind='PDF',
                    fresh_url=canonical_source_url(paper_id, source_url),
                    declared_source_url=source_url, title=state.get('title'))
    if isinstance(source_url, str) and source_url.startswith('http://') and state.get('title'):
        # Request TLS at the declared host/path. The existing acquisition helper
        # still verifies credentials, redirect host, PDF body and expected title.
        return dict(paper_id=paper_id, fresh_kind='PDF',
                    fresh_url='https://' + source_url[len('http://'):],
                    declared_source_url=source_url, title=state['title'])
    if isinstance(source_url, str) and source_url.startswith('https://') and state.get('title'):
        return dict(paper_id=paper_id, fresh_kind='PDF', fresh_url=source_url,
                    title=state['title'])
    raise DeferredPaper('unsupported_canonical_source_id')


def _source_route(result, paper_id):
    route = result.get('source_route')
    binding = result.get('source_binding') or {}
    kind = binding.get('original_kind')
    if route in ('rawtexarchive', 'latex') or kind == 'rawTexarchive':
        return 'latex', 'latex'
    if kind in ('PDF', 'HTML') or route in ('pdf', 'html'):
        return 'paper_markdown', ('acl-pdf' if paper_id.startswith('acl:') else
                                  'arxiv-html' if kind == 'HTML' and is_arxiv_id(paper_id)
                                  else 'arxiv-pdf' if is_arxiv_id(paper_id)
                                  else 'source-pdf')
    raise DeferredPaper('unsupported_fresh_source_route')


def _source_binding(result):
    binding = dict(result.get('source_binding') or {})
    url = binding.get('retrieval_url') or binding.get('source_url')
    if not isinstance(url, str) or not url.startswith('https://'):
        raise DeferredPaper('fresh_source_binding_missing_https_url')
    version = binding.get('edition') or binding.get('version')
    if not version:
        raise DeferredPaper('fresh_source_binding_missing_version')
    retrieved = binding.get('retrieved_at')
    if not retrieved:
        raise DeferredPaper('fresh_source_binding_missing_timestamp')
    binding.update(url=url, version=str(version))
    return binding


def _state_row(root, paper_id):
    conn = _connect(Path(root) / 'state.db', readonly=True)
    try:
        row = conn.execute('SELECT * FROM papers WHERE arxiv_id=?', (paper_id,)).fetchone()
        if row is None or row['status'] != 'done':
            raise DeferredPaper('live_state_not_done')
        return dict(row)
    finally:
        conn.close()


def _layers(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in value.split(',') if part.strip()]
    return []


def _embedder(runtime, metrics):
    endpoint = runtime['recipe']['endpoint']

    def embed(texts):
        # Similar lengths reduce transformer padding; restore canonical order.
        order = sorted(range(len(texts)), key=lambda index: len(texts[index]))
        vectors = [None] * len(texts)
        for indices in batched(order):
            batch = [texts[index] for index in indices]
            request = urllib.request.Request(endpoint,
                data=json.dumps({'texts': batch}, ensure_ascii=False).encode('utf-8'),
                headers={'Content-Type': 'application/json'}, method='POST')
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = json.load(response)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                if _transport_failure(exc):
                    raise RetryableWorkerError('document embedding batch failed') from exc
                raise DeferredPaper('document embedding response unavailable or invalid') from exc
            metrics['embedding_seconds'] = metrics.get('embedding_seconds', 0.0) + time.monotonic() - started
            metrics['embedding_batch_sizes'] = metrics.get('embedding_batch_sizes', []) + [len(batch)]
            part = result.get('vectors') if isinstance(result, dict) else None
            if not isinstance(part, list) or len(part) != len(batch):
                raise DeferredPaper('document embedding count mismatch')
            for index, vector in zip(indices, part):
                if (not isinstance(vector, list) or len(vector) != 384
                        or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                        or abs(math.sqrt(sum(value * value for value in vector)) - 1) > 1e-3):
                    raise DeferredPaper('document embedding vector is invalid')
                vectors[index] = vector
        return vectors
    return embed


def _preserving_embedder(runtime, metrics, paper_id, targets, existing):
    """Retain proven unchanged baseline vectors; encode changed/new text only."""
    fresh = _embedder(runtime, metrics)
    reuse = {}
    by_text = {}
    for point in existing.values():
        vector = point.get('vector')
        if (point.get('payload', {}).get('arxiv_id') != paper_id
                or not isinstance(vector, list) or len(vector) != 384
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                or abs(math.sqrt(sum(value * value for value in vector)) - 1) > 1e-3):
            continue
        by_text.setdefault(point['payload'].get('text'), []).append(point)

    def embed(texts):
        if texts != [chunk['text'] for chunk in targets]:
            raise DeferredPaper('embedding inputs differ from canonical targets')
        vectors = [None] * len(texts)
        missing = []
        for index, (text, chunk) in enumerate(zip(texts, targets)):
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f'{paper_id}#{chunk["chunk_index"]}'))
            matches = by_text.get(text, [])
            old = next((point for point in matches if point['id'] == point_id), None)
            if old is None and len(matches) == 1:
                old = matches[0]
            if old is None:
                missing.append(index)
            else:
                vectors[index] = list(old['vector'])
                reuse[point_id] = old['id']
        new_vectors = fresh([texts[index] for index in missing]) if missing else []
        for index, vector in zip(missing, new_vectors):
            vectors[index] = vector
        metrics.update(reused_vectors=len(reuse), newly_embedded_vectors=len(missing))
        return vectors
    return embed, reuse


def _candidate_metadata(state, fulltext, coarse_payload):
    """Retain ancillary payload fields; bibliographic fields follow live state.

    Coarse/state disagreement is still rejected by the caller. An old fulltext
    payload is a reuse source, not an authority for the replacement's metadata.
    """
    template = dict(next(iter(fulltext.values()))['payload'] if fulltext else coarse_payload)
    for key in ('title', 'year', 'abstract'):
        if key in state:
            template[key] = state[key]
    template['layers'] = list(_layers(state.get('layers')))
    return template


def _coarse_metadata_candidate(state, coarse_point):
    """Reconcile layer projection without changing coarse embedding inputs."""
    payload = coarse_point['payload']
    if payload.get('title') != state.get('title') or payload.get('year') != state.get('year'):
        raise DeferredPaper('coarse_metadata_differs_from_current_state')
    layers = list(_layers(state.get('layers')))
    if payload.get('layers') == layers:
        return None
    # The publication owner snapshots before/after points and verifies rollback.
    # Title, abstract and the existing vector remain untouched.
    return dict(coarse_point, payload=dict(payload, layers=layers))


def _prepare_candidate(paper_id, result, state, runtime, base, metrics):
    owner, restore = runtime['owner'], runtime['restore']
    extractor, cache = runtime['extractor'], runtime['document_cache']
    source_path = Path(result['source_path']).resolve(strict=True)
    source_bytes = source_path.read_bytes()
    source_text = source_bytes.decode('utf-8')
    diagnostics = result.get('diagnostics') or {}
    if result.get('body_quality_candidate') is not True:
        raise DeferredPaper(str(diagnostics.get('body_quality_reason') or 'source_quality_gate'))
    if source_text.count('\ufffd') or diagnostics.get('replacement_characters', 0):
        raise DeferredPaper('fresh_source_contains_replacement_characters')
    if diagnostics.get('parse_status') not in (None, 'ok'):
        raise DeferredPaper('fresh_source_parse_warning')
    if diagnostics.get('unsupported_structures'):
        raise DeferredPaper('fresh_source_has_unsupported_structures')
    if diagnostics.get('unresolved_include_warnings'):
        raise DeferredPaper('fresh_source_has_unresolved_includes')
    chunks, sections = result.get('chunks'), result.get('sections')
    if not isinstance(chunks, list) or not chunks or not isinstance(sections, list) or not sections:
        raise DeferredPaper('fresh_source_has_no_complete_chunks_or_sections')
    if any(not isinstance(item, dict) or not isinstance(item.get('text'), str)
           or '\ufffd' in item['text'] for item in chunks):
        raise DeferredPaper('fresh_source_chunks_invalid')
    targets = [item for item in chunks if extractor.should_embed(item)]
    if not targets or len(targets) > 3000:
        raise DeferredPaper('admitted_chunk_count_outside_owner_bound')
    mode, source_label = _source_route(result, paper_id)
    if (mode == 'paper_markdown' and not paper_id.startswith('acl:')
            and not is_arxiv_id(paper_id)):
        source_label = state.get('fulltext_source') or 'source-pdf'
    binding = _source_binding(result)
    binding['extraction'] = diagnostics
    binding['source_helper_sha256'] = hashlib.sha256(
        Path(runtime['fresh'].__file__).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    parser_mode = result.get('parser_mode')
    if parser_mode not in cache.PARSER_MODES:
        raise DeferredPaper('fresh_parser_mode_unsupported')
    cache_bytes = owner._jbytes(chunks)
    manifest = cache.make_section_manifest(paper_id, sections, cache_bytes, source_bytes,
                                           parser_mode, 5)
    fulltext = owner._scroll(runtime['client'], 'papers_fulltext', paper_id)
    coarse = owner._scroll(runtime['client'], 'papers_coarse_base', paper_id)
    if len(coarse) != 1:
        raise DeferredPaper('coarse_baseline_not_unique')
    coarse_point = next(iter(coarse.values()))
    coarse_payload = coarse_point['payload']
    coarse_candidate = _coarse_metadata_candidate(state, coarse_point)
    template = _candidate_metadata(state, fulltext, coarse_payload)
    template.update(source_url=binding['url'], fulltext_source=source_label,
                    parser_mode=parser_mode, parser_revision=5, extractor_version=5)
    embed, reuse = _preserving_embedder(runtime, metrics, paper_id, targets, fulltext)
    points = restore.build_cached_points(paper_id, chunks, targets, template, embed)
    updates = {}
    schema_conn = restore.labels._connect(runtime['paths']['state_db'], readonly=True)
    try:
        columns = {row[1] for row in schema_conn.execute('PRAGMA table_info(papers)')}
    finally:
        schema_conn.close()
    values = dict(source_url=binding['url'], fulltext_chars=len(source_text),
                  parser_mode=parser_mode, parser_revision=5, extractor_version=5,
                  fulltext_source=source_label, parse_status='ok')
    updates.update({key: value for key, value in values.items() if key in columns})
    metrics.update(source_chars=len(source_text), chunks=len(chunks), admitted_chunks=len(targets),
                   source_sha256=result['source_sha256'], raw_sha256=binding.get('raw_sha256'),
                   source_url=binding['url'], parser_mode=parser_mode)
    candidate = dict(source_path=str(source_path), source_sha256=result['source_sha256'],
                     source_binding=binding, source_route=mode, chunks=chunks,
                     manifest=manifest, points=points,
                     embedding_recipe=dict(runtime['recipe'], scope='newly_embedded_points'),
                     embedding_reuse=reuse,
                     state_updates=updates)
    if coarse_candidate is not None:
        candidate['coarse_point'] = coarse_candidate
    return candidate


def _journal_path(base, paper_id, cache):
    directory = Path(base) / 'journals'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / (cache.safe_id(paper_id) + '.json')


def _compress_applied_journal(base, paper_id, journal, runtime, metrics):
    """Keep one exact rollback receipt, compressed after verified publication."""
    plan = None
    try:
        path = Path(journal)
        raw = path.read_bytes()
        if raw.startswith(b'\x1f\x8b'):
            return path
        plan = runtime['owner']._load(path)
        if plan.get('phase') != 'applied':
            return path
        compressed = gzip.compress(raw, compresslevel=1, mtime=0)
        target = path.with_suffix(path.suffix + '.gz')
        runtime['owner'].labels._atomic(target, compressed)
        if gzip.decompress(target.read_bytes()) != raw or runtime['owner']._load(target) != plan:
            raise WorkerError('compressed rollback receipt differs from accepted journal')
        metrics = dict(metrics, journal_size_bytes=len(compressed), journal_uncompressed_bytes=len(raw))
        # The durable queue selects the verified new file before retiring its old
        # encoding. A crash between these steps leaves a harmless owned duplicate.
        _update(base, paper_id, 'applied', journal_path=target, metrics=metrics)
        path.unlink()
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return target
    except Exception as exc:
        if (isinstance(exc, (sqlite3.DatabaseError, MemoryError))
                or isinstance(exc, OSError) and exc.errno in
                (errno.ENOSPC, errno.EIO, errno.EROFS, errno.EMFILE, errno.ENFILE)):
            raise
        # Compression is optional. Only the durable queue chooses the accepted
        # receipt; it may already select the verified compressed encoding.
        rows = _get_targets(base, ('applied',), [paper_id])
        if len(rows) != 1 or not rows[0].get('journal_path'):
            raise WorkerError('compression failure left no accepted journal') from exc
        selected = Path(rows[0]['journal_path'])
        try:
            accepted = runtime['owner']._load(selected)
        except Exception as journal_exc:
            raise WorkerError('accepted journal unavailable after compression failure') from journal_exc
        if accepted.get('phase') != 'applied' or (plan is not None and accepted != plan):
            raise WorkerError('accepted journal changed after compression failure') from exc
        metrics = json.loads(rows[0]['metrics_json'] or '{}')
        metrics['compression_deferred'] = _error_reason(exc)
        _update(base, paper_id, 'applied', reason='compression_deferred:' + _error_reason(exc),
                journal_path=selected, metrics=metrics)
        return selected


def _read_meta_state(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def _service_states(worker):
    return {unit: worker.systemctl('show', unit, '--value', '--property=ActiveState')
            for unit in WRITER_UNITS}


@contextmanager
def maintenance_gate(base, worker, paper_ids, *, cleanup=None):
    """Persist the prior API state before closing readers for owner writes."""
    base = Path(base)
    marker_path = base / 'maintenance-gate.json'
    previous = _read_meta_state(marker_path)
    if previous and previous.get('phase') == 'closed' and previous.get('attention_required'):
        raise WorkerError('maintenance gate requires operator recovery; API remains closed')
    writer_states = _service_states(worker)
    if any(value not in ('inactive', 'failed') for value in writer_states.values()):
        raise WorkerError('ingestion writers must remain inactive during replacement')
    if previous and previous.get('phase') == 'closed':
        was_active = bool(previous.get('previously_active'))
        gate = dict(previous, operation_papers=sorted(set(previous.get('operation_papers', []) + list(paper_ids))),
                    safe_to_restore=False, attention_required=False, updated_at=utc_now())
    else:
        was_active = worker.systemctl('show', API_UNIT, '--value', '--property=ActiveState') == 'active'
        gate = dict(schema=1, phase='closed', previously_active=was_active,
                    operation_papers=sorted(set(paper_ids)), safe_to_restore=False,
                    attention_required=False, started_at=utc_now(), updated_at=utc_now())
    _atomic_json(marker_path, gate)
    try:
        if worker.systemctl('show', API_UNIT, '--value', '--property=ActiveState') == 'active':
            worker.systemctl('stop', API_UNIT)
        if worker.systemctl('show', API_UNIT, '--value', '--property=ActiveState') == 'active':
            raise WorkerError('API did not stop for maintenance')
        yield gate
    except BaseException:
        if cleanup is not None:
            try:
                cleanup()
                gate['safe_to_restore'] = True
                gate['attention_required'] = False
            except BaseException:
                gate['safe_to_restore'] = False
                gate['attention_required'] = True
        else:
            gate['safe_to_restore'] = False
            gate['attention_required'] = True
        gate['updated_at'] = utc_now()
        _atomic_json(marker_path, gate)
        raise
    finally:
        if gate.get('safe_to_restore'):
            checking_health = False
            try:
                gate.update(phase='closed', attention_required=False, updated_at=utc_now())
                _atomic_json(marker_path, gate)
                if was_active:
                    worker.systemctl('start', API_UNIT)
                    checking_health = True
                    _wait_for_api()
                gate.update(phase='restored', attention_required=False,
                            restored_at=utc_now(), updated_at=utc_now())
                _atomic_json(marker_path, gate)
            except BaseException as exc:
                transient = checking_health and (isinstance(exc, RetryableWorkerError)
                                                 or (isinstance(exc, Exception) and _transport_failure(exc)))
                gate.update(phase='closed', attention_required=not transient, updated_at=utc_now())
                _atomic_json(marker_path, gate)
                if transient:
                    raise RetryableWorkerError('API restore health temporarily unavailable') from exc
                raise
        else:
            gate.update(phase='closed', attention_required=True, updated_at=utc_now())
            _atomic_json(marker_path, gate)


def recover_journals(records, *, owner, qdrant, gate_factory, update, trusted_terminal=False):
    """Resolve owner journals before selecting new work; never guess after writes."""
    loaded = []
    uncertain = []
    for record in records:
        paper_id, path = record['paper_id'], record.get('journal_path')
        if not path or not Path(path).exists():
            if record['status'] == 'staging':
                update(paper_id, 'pending', reason='recovered_without_owner_journal')
                continue
            if record['status'] in ('prepared', 'applying', 'applied'):
                raise WorkerError('queue references a missing owner journal: ' + paper_id)
            continue
        plan = owner._load(path)
        phase = plan.get('phase')
        if trusted_terminal and record['status'] == 'applied' and phase == 'applied':
            # A safe persisted gate and matching terminal ledger/journal already
            # attest completed publication. Restart recovery handles uncertainty.
            continue
        loaded.append((record, plan))
        if phase in ('applying', 'rolling_back'):
            uncertain.append((record, plan))
    if uncertain:
        with gate_factory([r['paper_id'] for r, _ in uncertain]) as gate:
            for record, plan in uncertain:
                path, paper_id = record['journal_path'], record['paper_id']
                owner.rollback_plan(path, qdrant=qdrant, maintenance=True)
                owner._verify_document(owner._load(path), qdrant, 'before')
                update(paper_id, 'failed', reason='recovered_interrupted_apply_verified_rollback')
            gate['safe_to_restore'] = True
    for record, plan in loaded:
        paper_id, path = record['paper_id'], record['journal_path']
        phase = owner._load(path).get('phase')
        if phase == 'prepared':
            if record['status'] != 'deferred':
                update(paper_id, 'prepared', journal_path=path)
        elif phase == 'applied':
            owner._verify_document(owner._load(path), qdrant, 'published')
            update(paper_id, 'applied', reason='recovered_published_readback', journal_path=path)
        elif phase == 'rolled_back':
            owner._verify_document(owner._load(path), qdrant, 'before')
            update(paper_id, 'failed', reason='recovered_rolled_back', journal_path=path)
        elif phase in ('applying', 'rolling_back'):
            raise WorkerError('interrupted owner journal did not reach verified rollback')
        else:
            raise WorkerError('unknown whole-document owner journal phase: ' + str(phase))
    marker = Path(records[0]['journal_path']).parents[1] / 'maintenance-gate.json' if records and records[0].get('journal_path') else None
    # If the previous process died after closing the API, restore only after every
    # recoverable journal has passed its before/published verification above.
    if marker and marker.exists():
        gate_state = _read_meta_state(marker)
        if gate_state and gate_state.get('phase') == 'closed' and not gate_state.get('attention_required'):
            with gate_factory([]) as gate:
                gate['safe_to_restore'] = True


def _error_reason(exc):
    causes, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        causes.append(type(exc).__name__ + ':' + str(exc))
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return ' <- '.join(causes)


def _record_error(base, paper_id, exc, metrics, *, defer=True):
    status = 'deferred' if defer else 'failed'
    reason = _error_reason(exc)
    metrics = dict(metrics)
    metrics.update(getattr(exc, 'source_metrics', {}))
    _update(base, paper_id, status, reason=reason, metrics=metrics)


def _transport_failure(exc, runtime=None):
    import requests
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                        TimeoutError, ConnectionError, socket.gaierror)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        return exc.response is not None and (exc.response.status_code == 429
                                             or 500 <= exc.response.status_code <= 599)
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, urllib.error.URLError):
        return _transport_failure(exc.reason)
    if isinstance(exc, OSError) and exc.errno in (errno.ECONNREFUSED, errno.ECONNRESET,
                                                errno.ETIMEDOUT, errno.ENETUNREACH, errno.EHOSTUNREACH):
        return True
    if runtime is None:
        return False
    dependency = getattr(runtime['fresh'], 'FreshSourceDependencyError', None)
    if dependency is not None and isinstance(exc, dependency):
        return False
    status = getattr(exc, 'status_code', None)
    if isinstance(exc, runtime['fresh'].FreshSourceError) and isinstance(status, int):
        return status == 429 or 500 <= status <= 599
    return isinstance(exc, runtime['fresh'].FreshSourceError) and bool(
        re.search(r'\bHTTP\s+(?:429|5\d\d)\b', str(exc), re.I))


def _retry_delay(exc, attempt):
    headers = getattr(exc, 'headers', None)
    response = getattr(exc, 'response', None)
    if headers is None and response is not None:
        headers = response.headers
    value = headers.get('Retry-After') if headers else None
    if value is not None:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                delay = None
        if delay is not None and math.isfinite(delay):
            return max(0.0, min(60.0, delay))
    return (5.0, 15.0)[attempt]


def _source_attempt(call, runtime, sleeper=None):
    sleeper = sleeper or time.sleep
    for attempt in range(3):
        try:
            return call()
        except Exception as exc:
            if not _transport_failure(exc, runtime):
                raise
            if attempt == 2:
                raise DeferredPaper('source_transport_retries_exhausted:' + type(exc).__name__
                                    + ':' + str(exc)[:240]) from exc
            sleeper(_retry_delay(exc, attempt))


def _shared_failure(message, exc):
    error = RetryableWorkerError if _transport_failure(exc) else WorkerError
    return error(message)


def _check_health(url, name):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            value = json.load(response)
    except Exception as exc:
        raise _shared_failure(name + ' preflight failed', exc) from exc
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        raise WorkerError(name + ' returned invalid health receipt')
    if value['status'] != 'ok':
        raise RetryableWorkerError(name + ' is temporarily unhealthy')


def _wait_for_api(sleeper=None):
    sleeper = sleeper or time.sleep
    for attempt in range(30):
        try:
            _check_health('http://127.0.0.1:8010/v1/health', 'API restore')
            return
        except RetryableWorkerError:
            if attempt == 29:
                raise
            sleeper(1)


def _acquire_one(paper_id, state, runtime, base, queued_at=None):
    task_started = time.monotonic()
    queue_wait = max(0.0, task_started - queued_at) if queued_at is not None else 0.0
    resolver_started = time.monotonic()
    request = _source_attempt(lambda: _fresh_request(state, runtime['fresh']), runtime)
    resolver_seconds = time.monotonic() - resolver_started
    stage = Path(base) / 'source-stage' / runtime['document_cache'].safe_id(paper_id)
    kwargs = dict(timeout=120, native_build=str(NATIVE_BUILD), original_store_root=str(ORIGINAL_STORE))
    started = time.monotonic()
    is_arxiv = is_arxiv_id(paper_id)
    routes = [request]
    if is_arxiv and request.get('fresh_kind') == 'HTML':
        routes = [dict(request, fresh_kind=kind) for kind in ('HTML', 'rawTexarchive', 'PDF')]
    route_attempts, route_rejections, source_diagnostics = [], [], []
    result = None
    for route in routes:
        name = route['fresh_kind']
        route_attempts.append(name)
        try:
            candidate_result = _source_attempt(
                lambda: runtime['fresh'].acquire_and_parse(route, stage, **kwargs), runtime)
        except getattr(runtime['fresh'], 'FreshSourceDependencyError', WorkerError):
            raise
        except runtime['fresh'].FreshSourceError as exc:
            # Transport retries are exhausted by _source_attempt before route fallback.
            route_rejections.append({'route': name, 'reason': type(exc).__name__ + ':' + str(exc)[:240]})
            continue
        diagnostics = candidate_result.get('diagnostics') or {}
        source_diagnostics.append(dict(route=name,
            raw_sha256=(candidate_result.get('source_binding') or {}).get('raw_sha256'),
            source_sha256=candidate_result.get('source_sha256'), diagnostics=diagnostics))
        if candidate_result.get('body_quality_candidate') is True:
            result = candidate_result
            break
        reason = diagnostics.get('body_quality_reason') or 'body_quality_candidate_not_verified'
        route_rejections.append({'route': name, 'reason': str(reason)[:240]})
    if result is None:
        if not is_arxiv or len(routes) == 1:
            error = DeferredPaper('fresh_source_not_quality_approved:' + json.dumps(route_rejections, ensure_ascii=False))
        else:
            error = DeferredPaper('arxiv_source_routes_exhausted:' + json.dumps(route_rejections, ensure_ascii=False))
        error.source_metrics = dict(source_route_attempts=route_attempts,
            source_route_rejections=route_rejections, source_diagnostics=source_diagnostics,
            acquire_parse_seconds=time.monotonic() - started,
            acquisition_queue_wait_seconds=queue_wait, source_resolution_seconds=resolver_seconds)
        raise error
    metrics = {'acquire_parse_seconds': time.monotonic() - started,
               'acquisition_queue_wait_seconds': queue_wait,
               'source_resolution_seconds': resolver_seconds,
               'source_route_attempts': route_attempts,
               'source_route_rejections': route_rejections}
    diagnostics = result.get('diagnostics') or {}
    metrics['fetch_seconds'] = diagnostics.get('fetch_seconds')
    metrics['convert_seconds'] = sum(
        value for value in (diagnostics.get('parse_seconds'), diagnostics.get('chunk_build_seconds'))
        if isinstance(value, (int, float)))
    return result, metrics


def _recover_batch_plans(plans, runtime, base):
    """On an escaping exception, verify every plan while the API is still closed."""
    for paper_id, journal, metrics in plans:
        plan = runtime['owner']._load(journal)
        phase = plan.get('phase')
        if phase in ('applying', 'rolling_back'):
            runtime['owner'].rollback_plan(journal, qdrant=runtime['client'], maintenance=True)
            plan = runtime['owner']._load(journal)
            runtime['owner']._verify_document(plan, runtime['client'], 'before')
            _record_error(base, paper_id, WorkerError('interrupted_apply_verified_rollback'),
                          metrics, defer=False)
        elif phase == 'applied':
            runtime['owner']._verify_document(plan, runtime['client'], 'published')
            _update(base, paper_id, 'applied', reason='recovered_after_batch_exception',
                    journal_path=journal, metrics=metrics)
        elif phase == 'prepared':
            runtime['owner']._verify_document(plan, runtime['client'], 'before')
            _update(base, paper_id, 'prepared', reason='preserved_after_batch_exception',
                    journal_path=journal, metrics=metrics)
        elif phase == 'rolled_back':
            runtime['owner']._verify_document(plan, runtime['client'], 'before')
            _record_error(base, paper_id, WorkerError('interrupted_apply_rolled_back'),
                          metrics, defer=False)
        else:
            raise WorkerError('batch exception left an unknown owner journal phase: ' + str(phase))


def _apply_batch(plans, runtime, base, worker):
    if not plans:
        return
    gate_started = time.monotonic()
    try:
        with maintenance_gate(base, worker, [paper_id for paper_id, _, _ in plans],
                              cleanup=lambda: _recover_batch_plans(plans, runtime, base)) as gate:
            for paper_id, journal, metrics in plans:
                _update(base, paper_id, 'applying', reason=None, journal_path=journal, metrics=metrics)
                started = time.monotonic()
                try:
                    runtime['owner'].apply_plan(journal, qdrant=runtime['client'], maintenance=True)
                    metrics['apply_seconds'] = time.monotonic() - started
                    metrics['elapsed_seconds'] = (metrics.get('acquire_parse_seconds', 0)
                        + metrics.get('candidate_build_seconds', 0)
                        + metrics.get('embedding_seconds', 0)
                        + metrics.get('prepare_seconds', 0)
                        + metrics['apply_seconds'])
                    _update(base, paper_id, 'applied', reason=None, journal_path=journal, metrics=metrics)
                except Exception as exc:
                    phase = runtime['owner']._load(journal).get('phase')
                    try:
                        if phase in ('applying', 'applied', 'rolling_back'):
                            runtime['owner'].rollback_plan(journal, qdrant=runtime['client'], maintenance=True)
                        if phase != 'prepared':
                            runtime['owner']._verify_document(runtime['owner']._load(journal),
                                                              runtime['client'], 'before')
                    except BaseException as rollback_exc:
                        gate['attention_required'] = True
                        gate['safe_to_restore'] = False
                        _update(base, paper_id, 'failed', reason='rollback_unverified:' + type(rollback_exc).__name__)
                        raise WorkerError('rollback could not be verified; API left closed') from rollback_exc
                    metrics['apply_seconds'] = time.monotonic() - started
                    _record_error(base, paper_id, exc, metrics, defer=(phase == 'prepared'))
                gate['safe_to_restore'] = True
                gate['updated_at'] = utc_now()
                _atomic_json(Path(base) / 'maintenance-gate.json', gate)
    finally:
        gate_seconds = time.monotonic() - gate_started
        for paper_id, journal, metrics in plans:
            metrics['api_gate_seconds'] = gate_seconds
            try:
                metrics['journal_size_bytes'] = Path(journal).stat().st_size
            except OSError:
                metrics['journal_size_bytes'] = None
            _update_metrics(base, paper_id, metrics)


@contextmanager
def _locks(base):
    import fcntl
    paths = (Path(base).parent / 'labels-worker.lock',
             Path(base).parent / 'points-worker.lock',
             Path(base) / 'whole-document-replacement.lock')
    with ExitStack() as stack:
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = stack.enter_context(path.open('a'))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkerError('another corpus worker holds ' + path.name) from exc
        yield


def _check_storage(base):
    if shutil.disk_usage(base).free < 2 * 1024 ** 3:
        raise WorkerError('storage_headroom: less than 2 GiB free; remaining targets retained')


def _runtime_preflight(runtime, worker):
    if worker.systemctl('show', API_UNIT, '--value', '--property=ActiveState') == 'active':
        _check_health('http://127.0.0.1:8010/v1/health', 'API')
    states = _service_states(worker)
    if any(value not in ('inactive', 'failed') for value in states.values()):
        raise WorkerError('ingestion writer is active: ' + json.dumps(states, sort_keys=True))



def _unsafe_preparation_error(exc):
    """Only storage or explicitly signalled integrity faults stop preparation."""
    return (isinstance(exc, (WorkerError, sqlite3.DatabaseError, MemoryError))
            or isinstance(exc, OSError) and exc.errno in
            (errno.ENOSPC, errno.EIO, errno.EROFS, errno.EMFILE, errno.ENFILE))


def _candidate_one(paper_id, source_result, state, runtime, base, metrics):
    started = time.monotonic()
    for attempt in range(3):
        try:
            candidate = _prepare_candidate(paper_id, source_result, state, runtime, base, metrics)
            metrics['candidate_build_seconds'] = max(
                0.0, time.monotonic() - started - metrics.get('embedding_seconds', 0.0))
            return candidate, metrics
        except Exception as exc:
            if not isinstance(exc, RetryableWorkerError) and not _transport_failure(exc):
                raise
            if attempt == 2:
                raise RetryableWorkerError('candidate dependency retries exhausted') from exc
            time.sleep((5, 15)[attempt])


def _submit_preparation(sources, candidates, paper_id, state, runtime, base, metrics):
    """Forward source completion into embedding without waiting for the writer."""
    completed = Future()
    def candidate_ready(future):
        try:
            completed.set_result(future.result())
        except BaseException as exc:
            completed.set_exception(exc)
    def source_ready(future):
        try:
            result, source_metrics = future.result()
            metrics.update(source_metrics)
            candidate = candidates.submit(_candidate_one, paper_id, result, state, runtime, base, metrics)
            candidate.add_done_callback(candidate_ready)
        except BaseException as exc:
            completed.set_exception(exc)
    source = sources.submit(_acquire_one, paper_id, state, runtime, base, time.monotonic())
    source.add_done_callback(source_ready)
    return completed


def _run_stages(selected, runtime, base, root, rowid_floors):
    """Bounded independent source/embedding stages feed one publication owner.

    Source acquisition uses the standard fresh-source adapters, extractor and
    OriginalStore; candidate construction uses canonical cache/embedding tools.
    The existing queue and owner journals are the only durable progress state.
    """
    if min(PREFETCH_WORKERS, CANDIDATE_WORKERS, MAX_IN_FLIGHT) < 1:
        raise WorkerError('stage worker counts and in-flight bound must be positive')
    records = iter(selected)
    exhausted = False
    # A future owns exactly one document until it reaches the serialized writer.
    pending = {}
    with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as sources, \
            ThreadPoolExecutor(max_workers=CANDIDATE_WORKERS) as candidates:
        while pending or not exhausted:
            ready = []
            while not exhausted and len(pending) + len(ready) < MAX_IN_FLIGHT:
                try:
                    record = next(records)
                except StopIteration:
                    exhausted = True
                    break
                paper_id = record['paper_id']
                _check_storage(base)
                path = record.get('journal_path')
                if path and Path(path).exists() and runtime['owner']._load(path).get('phase') == 'prepared':
                    ready.append((paper_id, Path(path), json.loads(record['metrics_json'] or '{}')))
                    continue
                journal = _journal_path(base, paper_id, runtime['document_cache'])
                _update(base, paper_id, 'staging', reason=None, journal_path=journal)
                try:
                    state = _state_row(root, paper_id)
                except Exception as exc:
                    if _unsafe_preparation_error(exc):
                        raise
                    _record_error(base, paper_id, exc, {})
                    continue
                metrics = {}
                future = _submit_preparation(sources, candidates, paper_id, state, runtime, base, metrics)
                pending[future] = (paper_id, metrics)
            if pending:
                done = {future for future in pending if future.done()}
                if not done and not ready:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                # Callbacks feed candidate work even while this sole owner is
                # publishing. Only complete documents reach this writer.
                for future in done:
                    paper_id, metrics = pending.pop(future)
                    journal = _journal_path(base, paper_id, runtime['document_cache'])
                    preparing = False
                    try:
                        value, metrics = future.result()
                        _update(base, paper_id, 'staging', reason=None, journal_path=journal, metrics=metrics)
                        _check_storage(base)
                        started = time.monotonic()
                        preparing = True
                        plan = runtime['owner'].prepare_document_replacement(
                            paper_id, candidate=value, **runtime['paths'], journal_path=journal,
                            qdrant=runtime['client'], rowid_floors=rowid_floors)
                        _advance_rowid_floors(rowid_floors, plan)
                        metrics['prepare_seconds'] = time.monotonic() - started
                        metrics['journal_size_bytes'] = Path(journal).stat().st_size if Path(journal).exists() else None
                        _update(base, paper_id, 'prepared', reason=None, journal_path=journal, metrics=metrics)
                        ready.append((paper_id, journal, metrics))
                    except Exception as exc:
                        if _unsafe_preparation_error(exc):
                            raise
                        # prepare writes only its recovery journal. Preserve a
                        # completed journal for the next run instead of losing it
                        # when an unrelated document-local step raises afterward.
                        if preparing and Path(journal).exists():
                            plan = runtime['owner']._load(journal)
                            if plan.get('phase') == 'prepared':
                                _advance_rowid_floors(rowid_floors, plan)
                                _update(base, paper_id, 'prepared', reason=_error_reason(exc),
                                        journal_path=journal, metrics=metrics)
                                continue
                            if plan.get('phase') not in ('rolled_back',):
                                raise WorkerError('preparation left an unsafe owner journal') from exc
                        _record_error(base, paper_id, exc, metrics)
            if ready:
                _apply_batch(ready, runtime, base, runtime['worker'])
                for paper_id, journal, metrics in ready:
                    current = _get_targets(base, ('applied',), [paper_id])
                    if current:
                        _compress_applied_journal(base, paper_id, journal, runtime,
                                                  json.loads(current[0]['metrics_json']))


def run(base=BASE, root=ROOT, *, papers=None, limit=None, retry_deferred=False):
    base, root = Path(base).resolve(), Path(root).resolve(strict=True)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('limit must be a positive integer')
    papers = list(dict.fromkeys(papers or []))
    started = time.monotonic()
    run_id = hashlib.sha256((utc_now() + str(os.getpid())).encode()).hexdigest()[:16]
    with _locks(base):
        runtime = _load_runtime(base, root)
        worker = runtime['worker']
        _runtime_preflight(runtime, worker)
        recovery_records = _get_targets(base, ('staging','prepared','applying','applied','failed'))
        gate_state = _read_meta_state(base / 'maintenance-gate.json') or {}
        trusted_terminal = (gate_state.get('phase') in ('closed', 'restored')
                            and gate_state.get('safe_to_restore') is True
                            and gate_state.get('attention_required') is False)
        recover_journals(recovery_records, owner=runtime['owner'], qdrant=runtime['client'],
                         gate_factory=lambda ids: maintenance_gate(base, worker, ids),
                         update=lambda pid, status, **kw: _update(base, pid, status, **kw),
                         trusted_terminal=trusted_terminal)
        rowid_floors = _rowid_floors(runtime)
        for record in _get_targets(base, ('prepared',)):
            if record.get('journal_path') and Path(record['journal_path']).exists():
                _advance_rowid_floors(rowid_floors, runtime['owner']._load(record['journal_path']))
        statuses = ('pending','prepared','staging') + (('deferred',) if retry_deferred else ())
        selected = _get_targets(base, statuses, papers=papers or None, limit=limit)
        if papers and not selected:
            unknown = [p for p in papers if not _get_targets(base, ('pending','prepared','staging','deferred','applied','failed'), [p])]
            if unknown:
                raise WorkerError('requested paper is outside the frozen target set: ' + ','.join(unknown))
        selected = _order_preparation(selected, runtime['paths']['cache_dir'], runtime['document_cache'])
        selection_count = len(selected)
        active_run = dict(run_id=run_id, pid=os.getpid(), target_ids=[row['paper_id'] for row in selected],
                          started_epoch=time.time(), updated_epoch=time.time())
        # Seed from selected resumable records so subsequent metric deltas remain
        # correct when a previous preparation stopped before publication.
        totals = active_run['phase_seconds'] = {key: 0.0 for key in PHASE_KEYS}
        for row in selected:
            prior_metrics = json.loads(row['metrics_json'] or '{}')
            for key in PHASE_KEYS:
                value = prior_metrics.get(key, 0)
                if isinstance(value, (int, float)):
                    totals[key] += value
        conn = _connect(db_path(base))
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('active_run',?)",
                             (json.dumps(active_run, separators=(',', ':')),))
        finally:
            conn.close()
        write_status(base)
        _run_stages(selected, runtime, base, root, rowid_floors)
        elapsed = time.monotonic() - started
        selected_statuses = _selected_counts(base, active_run['target_ids'])
        successful = selected_statuses.get('applied', 0)
        terminal = successful + selected_statuses.get('deferred', 0) + selected_statuses.get('failed', 0)
        current_eta = None
        if successful >= 2 and elapsed > 0 and selection_count > terminal:
            current_eta = round((selection_count - terminal) * elapsed / successful, 1)
        conn = _connect(db_path(base), readonly=True)
        try:
            active_row = conn.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
            final_phase_seconds = json.loads(active_row[0]).get('phase_seconds', {}) if active_row else {}
        finally:
            conn.close()
        run_status = dict(run_id=run_id, elapsed_seconds=round(elapsed, 3),
            selected_documents=selection_count, applied_documents=successful,
            deferred_or_failed=selected_statuses.get('deferred', 0) + selected_statuses.get('failed', 0),
            throughput_docs_per_minute=round(successful * 60 / elapsed, 3) if elapsed and successful else None,
            selected_scope_eta_seconds=current_eta,
            eta_scope='selected run subset only; never whole corpus',
            prefetch_workers=PREFETCH_WORKERS, embed_batch_size=EMBED_BATCH_SIZE,
            embed_concurrency=CANDIDATE_WORKERS, max_in_flight=MAX_IN_FLIGHT, target_ids=active_run['target_ids'],
            phase_seconds=final_phase_seconds,
            started_epoch=active_run['started_epoch'], finished_epoch=time.time(), finished=True)
        return write_status(base, run_status)


def _transport_retry_targets(base):
    # One extra managed pass after ordinary work drains; retain this allowance
    # in the existing durable ledger across restarts and metric replacement.
    if _get_targets(base, ('pending', 'staging', 'prepared')):
        return []
    selected = []
    for row in _get_targets(base, ('deferred',)):
        metrics = json.loads(row['metrics_json'] or '{}')
        reason = row.get('reason') or ''
        transport = ('source_transport_retries_exhausted:' in reason
                     or bool(re.match(r'(?:ReadTimeout|ConnectTimeout|Timeout|ConnectionError|URLError):', reason))
                     or bool(re.search(r'\bHTTP\s+(?:Error\s*)?(?:429|5\d\d)\b', reason)))
        if transport and metrics.get('source_transport_retry_passes', 0) < 1:
            selected.append(row['paper_id'])
            metrics['source_transport_retry_passes'] = 1
            _update(base, row['paper_id'], 'pending',
                    reason='source_transport_delayed_retry_claimed', metrics=metrics)
    return selected


def api_startup_check(base):
    try:
        gate = json.loads((Path(base) / 'maintenance-gate.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return 1
    return 0 if (isinstance(gate, dict) and gate.get('phase') in ('closed', 'restored')
                 and gate.get('safe_to_restore') is True
                 and gate.get('attention_required') is False) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init','status','api-startup-check'):
        item = sub.add_parser(name)
        item.add_argument('--base', type=Path, default=BASE)
    work = sub.add_parser('run')
    work.add_argument('--base', type=Path, default=BASE)
    work.add_argument('--root', type=Path, default=ROOT)
    work.add_argument('--paper', action='append', default=[])
    work.add_argument('--limit', type=int)
    work.add_argument('--retry-deferred', action='store_true')
    work.add_argument('--managed', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'api-startup-check':
        return api_startup_check(args.base)
    if args.command == 'init':
        result = initialize(args.base)
    elif args.command == 'status':
        result = status_snapshot(args.base)
    else:
        if args.managed:
            path = args.base / 'bulk-controller-status.json'
            def lifecycle(phase, status, cause=None, **details):
                _atomic_json(path, dict(worker_pid=os.getpid(), phase=phase, time=time.time(),
                                        status=status, cause=cause, **details))
            try:
                _atomic_json(args.base / 'bulk-controller.pid', os.getpid())
                lifecycle('bulk_running', 'running')
                result = run(args.base, args.root, papers=args.paper, limit=args.limit,
                             retry_deferred=args.retry_deferred)
                # Explicit bounded subsets and manual retry selections keep their scope.
                if not args.paper and args.limit is None and not args.retry_deferred:
                    retry_ids = _transport_retry_targets(args.base)
                    if retry_ids:
                        lifecycle('bulk_running', 'retry_wait', 'source_transport_delayed_pass',
                                  retry_documents=len(retry_ids))
                        time.sleep(60)
                        lifecycle('bulk_running', 'running')
                        result = run(args.base, args.root, papers=retry_ids)
                counts = result.get('statuses', {})
                lifecycle('bulk_complete', 'pass_complete',
                          deferred_documents=counts.get('deferred', 0),
                          failed_documents=counts.get('failed', 0),
                          whole_corpus_quality_complete=False)
            except Exception as exc:
                retryable = (isinstance(exc, (RetryableWorkerError, MemoryError))
                             or isinstance(exc, OSError) and exc.errno in (errno.EMFILE, errno.ENFILE))
                try:
                    lifecycle('bulk_interrupted', 'retry_wait' if retryable else 'needs_attention',
                              type(exc).__name__ + ':' + str(exc)[:300])
                except OSError:
                    retryable = False  # A durable lifecycle write failure needs operator attention.
                print(json.dumps(dict(error=type(exc).__name__, retryable=retryable)), file=sys.stderr)
                return 75 if retryable else 78
        else:
            result = run(args.base, args.root, papers=args.paper, limit=args.limit,
                         retry_deferred=args.retry_deferred)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
