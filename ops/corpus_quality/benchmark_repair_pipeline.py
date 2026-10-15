"""Bounded Linux repair benchmark: freeze once, run each code root in isolation.

Only freeze reads the primary corpus. Runs write newly created fixture paths and
two explicitly namespaced Qdrant collections. Service restart overhead is excluded.
Invoke this file directly, not with -m, so --code-root selects all pipeline code.
"""
import argparse
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
from unittest.mock import patch


def encoded(raw):
    return base64.b64encode(raw).decode()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def readonly(path):
    return sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)


def module_provenance(code_root):
    names = ('pipeline', 'pipeline.extractor', 'pipeline.pdf_memory', 'pipeline.pdf_unicode',
        'ops.replace_published_labels', 'ops.corpus_quality.fresh_sources',
        'ops.corpus_quality.replace_documents', 'ops.corpus_quality.run_label_worker')
    result = {}
    for name in names:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve(strict=True)
        selected = code_root / (name.replace('.', '/') + '.py')
        if name == 'pipeline':
            selected = code_root / 'pipeline/__init__.py'
        if selected.exists() and path != selected.resolve(strict=True):
            raise RuntimeError('benchmark mixed code roots: ' + name + ' loaded ' + str(path))
        result[name] = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest())
    return result


def freeze(args, w, worker):
    import requests
    from ops import replace_published_labels as owner
    from ops.corpus_quality import fresh_sources
    from pipeline.document_cache import safe_id
    ids = args.papers
    if not 1 <= len(ids) <= 12 or len(set(ids)) != len(ids):
        raise ValueError('freeze requires 1..12 unique explicit paper IDs')
    if any(not re.fullmatch(r'acl:[A-Za-z0-9._/-]+', aid) or '..' in aid for aid in ids):
        raise ValueError('this bounded benchmark accepts ACL IDs only')
    if args.output.exists():
        raise ValueError('freeze output already exists')
    q = worker.Qdrant()
    snap = dict(schema=1, ids=ids, created_at=w.utc_now(), databases={}, files={}, points={}, sources={})
    placeholders = ','.join('?' for _ in ids)
    families = {'state.db': ('papers', 'paper_publications', 'paper_active'),
        'fts.db': ('chunks', 'pipeline_fts_points', 'pipeline_fts_rows', 'pipeline_fts_papers', 'pipeline_fts_scope'),
        'papers_fts.db': ('papers_fts', 'pipeline_fts_rows', 'pipeline_fts_papers')}
    for filename, tables in families.items():
        with readonly(args.primary_root / filename) as conn:
            db = snap['databases'][filename] = {}
            for table in tables:
                ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if ddl is None:
                    continue
                if table == 'pipeline_fts_scope':
                    rows = conn.execute('SELECT rowid,* FROM ' + table).fetchall()
                elif table in ('chunks', 'papers_fts'):
                    rows = conn.execute('SELECT rowid,* FROM ' + table +
                        ' WHERE rowid IN (SELECT row_id FROM pipeline_fts_rows WHERE table_name=? '
                        'AND arxiv_id IN (' + placeholders + '))', (table, *ids)).fetchall()
                else:
                    rows = conn.execute('SELECT rowid,* FROM ' + table + ' WHERE arxiv_id IN (' + placeholders + ')', ids).fetchall()
                columns = [r[1] for r in conn.execute('PRAGMA table_info(' + table + ')')]
                db[table] = dict(ddl=ddl[0], columns=columns, rows=rows)
                if table == 'paper_publications':
                    path_index = columns.index('artifact_path') + 1
                    for row in rows:
                        path = Path(row[path_index])
                        target = 'publication_cache/' + path.name
                        raw = path.read_bytes()
                        prior = snap['files'].get(target)
                        if prior is not None and prior != encoded(raw):
                            raise ValueError('retained artifact filename collision')
                        snap['files'][target] = encoded(raw)
    with readonly(args.primary_root / 'state.db') as conn:
        conn.row_factory = sqlite3.Row
        for aid in ids:
            state = conn.execute('SELECT * FROM papers WHERE arxiv_id=?', (aid,)).fetchone()
            if state is None or state['status'] != 'done':
                raise ValueError('cohort must contain existing done papers: ' + aid)
            for folder, suffix in (('chunk_cache', '.json'), ('section_cache', '.json'), ('fulltext_cache', '.tex')):
                relative = folder + '/' + safe_id(aid) + suffix
                path = args.primary_root / relative
                if path.exists():
                    snap['files'][relative] = encoded(path.read_bytes())
            request = w._fresh_request(dict(state), fresh_sources)
            artifact = fresh_sources.fetch_fresh_artifact(request['paper_id'], original_kind=request['fresh_kind'],
                edition=request['fresh_version'], session=requests.Session())
            snap['sources'][artifact.source_url] = dict(raw=encoded(artifact.raw_bytes), sha256=artifact.raw_sha256,
                retrieval_url=artifact.retrieval_url)
    for collection in ('papers_fulltext', 'papers_coarse_base'):
        snap['points'][collection] = []
        for aid in ids:
            snap['points'][collection].extend(owner._scroll(q, collection, aid).values())
    snap['binding_sha256'] = digest(snap)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snap, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(dict(frozen=str(args.output), binding_sha256=snap['binding_sha256'], papers=ids)))


def run(args, w, worker):
    provenance = module_provenance(args.code_root.resolve(strict=True))
    child_probe = ('import sys,json,importlib.util; sys.path.insert(0,sys.argv[1]); '
        'sys.path.insert(0,sys.argv[2]); print(json.dumps({n:importlib.util.find_spec(n).origin '
        'for n in ("pdf_memory","pdf_unicode")}))')
    child = json.loads(subprocess.check_output([sys.executable, '-B', '-c', child_probe,
        str(Path(provenance['pipeline.extractor']['path']).parent), str(w.NATIVE_BUILD)], text=True))
    for name, path in child.items():
        if Path(path).resolve() != Path(provenance['pipeline.' + name]['path']):
            raise RuntimeError('converter child would load different code: ' + name)
    snap = json.loads(args.frozen.read_text(encoding='utf-8'))
    binding = snap.pop('binding_sha256')
    if digest(snap) != binding:
        raise ValueError('frozen fixture binding mismatch')
    if not re.fullmatch(r'dtox_bench_[a-z0-9_]{1,50}', args.prefix):
        raise ValueError('sandbox collection prefix must begin dtox_bench_')
    if args.output.exists():
        raise ValueError('run output must be a new directory')
    root = args.output.resolve() / 'corpus'
    root.mkdir(parents=True)
    base = args.output.resolve() / 'work' / 'replacement'
    base.mkdir(parents=True)
    for folder in ('chunk_cache', 'section_cache', 'fulltext_cache', 'publication_cache'):
        (root / folder).mkdir()
    for relative, raw in snap['files'].items():
        path = root / relative
        if path.parent.parent != root:
            raise ValueError('invalid frozen relative file path')
        path.write_bytes(base64.b64decode(raw))
    for filename, tables in snap['databases'].items():
        with sqlite3.connect(root / filename) as conn:
            for table, spec in tables.items():
                conn.execute(spec['ddl'])
                rows = spec['rows']
                if table == 'paper_publications':
                    index = spec['columns'].index('artifact_path') + 1
                    for row in rows:
                        row[index] = str(root / 'publication_cache' / Path(row[index]).name)
                if rows:
                    conn.executemany('INSERT INTO ' + table + '(rowid,' + ','.join(spec['columns']) +
                        ') VALUES (' + ','.join('?' for _ in rows[0]) + ')', rows)
    (base.parent / 'embedding-recipe.json').write_bytes(args.recipe.read_bytes())
    from pipeline.originals import OriginalStore
    original_store = args.output.resolve() / 'original-store'
    OriginalStore.create(original_store)
    with w._connect(w.db_path(base)) as conn:
        w._schema(conn)
        for aid in snap['ids']:
            conn.execute("INSERT INTO targets(paper_id,cache_ufffd,source_ufffd,empty_fulltext,status,created_at,updated_at) VALUES (?,1,1,0,'pending',?,?)",
                (aid, w.utc_now(), w.utc_now()))
        conn.execute("INSERT INTO meta VALUES ('candidate_count',?)", (str(len(snap['ids'])),))

    original_qdrant = worker.Qdrant
    class Sandbox(original_qdrant):
        def request(self, path, body, method='POST'):
            for family, suffix in (('papers_fulltext', 'fulltext'), ('papers_coarse_base', 'coarse')):
                old = '/collections/' + family
                if path == old or path.startswith(old + '/') or path.startswith(old + '?'):
                    path = '/collections/' + args.prefix + '_' + suffix + path[len(old):]
                    break
            else:
                raise ValueError('request escaped benchmark namespace: ' + path)
            return super().request(path, body, method)
    client = Sandbox()
    for collection, dimension in (('papers_fulltext', 384), ('papers_coarse_base', 768)):
        try:
            client.request('/collections/' + collection, {}, 'GET')
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
        else:
            raise ValueError('sandbox collection already exists: ' + collection)
        client.request('/collections/' + collection, {'vectors': {'size': dimension, 'distance': 'Cosine'}}, 'PUT')
        points = snap['points'][collection]
        for start in range(0, len(points), 200):
            client.upsert(collection_name=collection, points=points[start:start + 200], wait=True)

    class FrozenSession:
        def get(self, url, **kwargs):
            source = snap['sources'].get(url)
            if source is None:
                raise ValueError('source URL outside frozen transport: ' + url)
            raw = base64.b64decode(source['raw'])
            if hashlib.sha256(raw).hexdigest() != source['sha256']:
                raise ValueError('frozen source SHA mismatch')
            return SimpleNamespace(status_code=200, url=source['retrieval_url'], headers={}, content=raw,
                iter_content=lambda chunk_size: (raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)), close=lambda: None)

    from ops.corpus_quality import fresh_sources
    acquire = fresh_sources.acquire_and_parse
    def frozen_acquire(row, stage_dir, *, timeout=120, session=None, fetched_at=None,
                       native_build=None, original_store_root=None):
        return acquire(row, stage_dir, timeout=timeout, native_build=native_build,
            session=FrozenSession(), fetched_at=snap['created_at'],
            original_store_root=str(original_store))
    def host_cpu():
        values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
        return sum(values), values[3] + values[4]
    cpu_before = host_cpu()
    started = time.monotonic()
    failure = None
    with patch.object(worker, 'Qdrant', Sandbox), patch.object(worker, 'systemctl', lambda *args: 'inactive'), \
            patch.object(worker, 'health', lambda: None), patch.object(fresh_sources, 'acquire_and_parse', frozen_acquire):
        try:
            w.run(base, root, papers=snap['ids'])
        except Exception as exc:
            failure = dict(type=type(exc).__name__, message=str(exc))
    elapsed = time.monotonic() - started
    cpu_after = host_cpu()
    average_busy_cores = os.cpu_count() * (1 - (cpu_after[1] - cpu_before[1]) /
                                           (cpu_after[0] - cpu_before[0]))
    with w._connect(w.db_path(base), readonly=True) as conn:
        records = [dict(row) for row in conn.execute('SELECT * FROM targets ORDER BY paper_id')]
        active = conn.execute("SELECT value FROM meta WHERE key='active_run'").fetchone()
        phase_seconds = json.loads(active[0]).get('phase_seconds', {}) if active else {}
    from ops import replace_published_labels as owner
    outputs = {}
    verification_errors = {}
    for row in records:
        aid = row['paper_id']
        if row['status'] != 'applied':
            continue
        plan = owner._load(row['journal_path'])
        try:
            owner._verify_document(plan, client, 'published')
        except Exception as exc:
            verification_errors[aid] = dict(type=type(exc).__name__, message=str(exc))
            continue
        fulltext = owner._scroll(client, 'papers_fulltext', aid)
        # Revision includes fixture-specific paths; compare semantic payloads instead.
        semantic = []
        for point in sorted(fulltext.values(), key=lambda p: p['id']):
            payload = dict(point['payload'])
            payload.pop('publication_revision', None)
            semantic.append(dict(id=point['id'], payload=payload, vector=point['vector']))
        outputs[aid] = dict(points_sha256=digest(semantic), point_count=len(semantic),
            coarse_sha256=digest(sorted(owner._scroll(client, 'papers_coarse_base', aid).values(), key=lambda p: p['id'])),
            chunk_sha256=plan['paper']['cache_sha256'], source_sha256=plan['paper']['source_sha256'],
            published_readback_verified=True)
    report = dict(schema=1, fixture_binding_sha256=binding, code_root=str(args.code_root.resolve()),
        module_provenance=provenance, converter_child_modules=child, native_build=str(w.NATIVE_BUILD),
        prefix=args.prefix, elapsed_seconds=elapsed, interrupted=failure, targets=records, outputs=outputs,
        verification_errors=verification_errors, accepted_outputs_valid=not verification_errors,
        phase_seconds=phase_seconds,
        service_restart_overhead_included=False, frozen_transport=True,
        host_average_busy_cores=average_busy_cores, host_logical_cpus=os.cpu_count(),
        applied_documents=len(outputs), applied_per_minute=len(outputs) * 60 / elapsed)
    path = args.output / 'report.json'
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(dict(report=str(path), applied=len(outputs), elapsed_seconds=elapsed, interrupted=failure)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('freeze', 'run'))
    p.add_argument('--code-root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--primary-root', type=Path)
    p.add_argument('--papers', nargs='+')
    p.add_argument('--frozen', type=Path)
    p.add_argument('--recipe', type=Path)
    p.add_argument('--prefix')
    args = p.parse_args()
    sys.path.insert(0, str(args.code_root.resolve(strict=True)))
    os.environ['DTOX_RESEARCH_ROOT'] = str(args.code_root.resolve(strict=True))
    # Legacy ops imports prepend their own deployed root to sys.path. Pin this
    # package before those imports so its selected overlay __path__ stays first.
    for name in ('pipeline', 'pipeline.pdf_memory', 'pipeline.pdf_unicode', 'pipeline.extractor'):
        importlib.import_module(name)
    from ops.corpus_quality import replace_documents as w, run_label_worker as worker
    module_provenance(args.code_root.resolve(strict=True))
    if args.mode == 'freeze':
        if not args.primary_root or not args.papers:
            p.error('freeze requires --primary-root and --papers')
        freeze(args, w, worker)
    else:
        if not args.frozen or not args.recipe or not args.prefix:
            p.error('run requires --frozen --recipe --prefix')
        run(args, w, worker)


if __name__ == '__main__':
    main()
