"""Prepare rejected canonical papers, then publish through the stock first-publication lifecycle.

Preparation writes only the separate queue and ORIGINAL store. Run waits for the
existing repair to finish; it never resets a published paper or adopts old points.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib
import json
import os
import re
import unicodedata
from pathlib import Path
import sqlite3
import sys
import time

from ops.corpus_quality import curated_web3 as barrier
from ops.corpus_quality import replace_documents as worker


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write_once(path, value):
    path = Path(path)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise worker.WorkerError('admission artifact changed: ' + path.name)
    else:
        with path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    return hashlib.sha256(raw).hexdigest()


def _rejected(conn, paper_id):
    row = conn.execute('SELECT * FROM papers WHERE arxiv_id=?', (paper_id,)).fetchone()
    if (row is None or row['status'] != 'rejected' or row['twin_of'] is not None
            or row['fulltext_source'] is not None or row['parse_status'] is not None):
        raise worker.WorkerError('admission requires an untouched rejected canonical paper: ' + paper_id)
    return dict(row)


def _no_owned_state(conn, paper_id):
    for table in ('completion_history', 'paper_publications', 'paper_index_sync'):
        if conn.execute('SELECT 1 FROM ' + table + ' WHERE arxiv_id=? LIMIT 1', (paper_id,)).fetchone():
            raise worker.WorkerError('admission already has state ownership: ' + paper_id)


def _no_owned_indexes(runtime, root, paper_id):
    for collection in ('papers_fulltext', 'papers_coarse_base'):
        if runtime['owner']._scroll(runtime['client'], collection, paper_id):
            raise worker.WorkerError('admission already has vector ownership: ' + paper_id)
    for filename in ('fts.db', 'papers_fts.db'):
        conn = sqlite3.connect((root / filename).as_uri() + '?mode=ro', uri=True)
        try:
            if filename == 'fts.db':
                if conn.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone() != (1,):
                    raise worker.WorkerError('complete FTS ownership map required')
                owned = conn.execute('SELECT 1 FROM pipeline_fts_points WHERE arxiv_id=? LIMIT 1', (paper_id,)).fetchone()
            else:
                owned = conn.execute('SELECT 1 FROM papers_fts WHERE arxiv_id=? LIMIT 1', (paper_id,)).fetchone()
            if owned:
                raise worker.WorkerError('admission already has lexical ownership: ' + paper_id)
        finally:
            conn.close()
    sid = runtime['document_cache'].safe_id(paper_id)
    for folder, suffix in (('fulltext_cache', '.tex'), ('chunk_cache', '.json'), ('section_cache', '.json')):
        if (root / folder / (sid + suffix)).exists():
            raise worker.WorkerError('admission already has cache ownership: ' + paper_id)


def _duplicate_aliases(conn, paper_id, title, root):
    def key(value):
        return re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', value or '').casefold()).strip()
    root = Path(root)
    fts = sqlite3.connect((root / 'papers_fts.db').as_uri() + '?mode=ro', uri=True)
    fts.row_factory = sqlite3.Row
    try:
        terms = re.findall(r'\w+', title, re.UNICODE)
        query = ' AND '.join('title:"' + term + '"' for term in terms)
        candidates = fts.execute('SELECT arxiv_id,title FROM papers_fts WHERE papers_fts MATCH ?', (query,)).fetchall()
        return [dict(row) for row in candidates if row['arxiv_id'] != paper_id and key(row['title']) == key(title)]
    finally:
        fts.close()


def _new_papers(path):
    values = _read(path) if path else []
    result = {}
    for row in values:
        if (set(row) != {'paper_id','title','year','authors','abstract','fresh_url','edition','publisher_url'}
                or not re.fullmatch(r'wp:[a-z0-9-]+',row['paper_id'])
                or not re.fullmatch(r'sha256:[0-9a-f]{64}',row['edition'])
                or row['paper_id'] in result or not row['authors'] or not row['publisher_url'].startswith('https://')
                or not row['fresh_url'].startswith('https://')):
            raise worker.WorkerError('new paper metadata/provenance is invalid')
        result[row['paper_id']] = row
    return result


def prepare(base, root, paper_ids, new_papers=None):
    base, root = Path(base).resolve(), Path(root).resolve(strict=True)
    new = _new_papers(new_papers)
    ids = sorted(set(paper_ids) | set(new))
    if not ids or any(not worker.is_arxiv_id(pid) for pid in paper_ids):
        raise worker.WorkerError('explicit canonical arXiv IDs required')
    if (base / 'queue.json').exists():
        raise worker.WorkerError('queue already prepared; retain its pinned artifacts')
    base.mkdir(parents=True, exist_ok=True)
    runtime = worker._load_runtime(base, root)
    conn = worker._connect(root / 'state.db', readonly=True)
    rows, deferred = [], []
    try:
        for paper_id in ids:
            if paper_id in new:
                if conn.execute('SELECT 1 FROM papers WHERE arxiv_id=?',(paper_id,)).fetchone():
                    raise worker.WorkerError('new canonical paper already exists')
                metadata = new[paper_id]
                state = dict(arxiv_id=paper_id,title=metadata['title'],year=metadata['year'],
                             abstract=metadata['abstract'],source_url=metadata['fresh_url'],layers='web3',status='new')
            else:
                state = _rejected(conn, paper_id)
            _no_owned_state(conn, paper_id)
            _no_owned_indexes(runtime, root, paper_id)
            try:
                aliases = _duplicate_aliases(conn, paper_id, state['title'], root)
                if aliases:
                    raise worker.DeferredPaper('canonical title aliases require resolution: ' + ','.join(row['arxiv_id'] for row in aliases))
                request = (dict(paper_id=paper_id,title=state['title'],fresh_kind='PDF',fresh_url=new[paper_id]['fresh_url'],fresh_version=new[paper_id]['edition']) if paper_id in new else None)
                result, metrics = worker._acquire_one(paper_id, state, runtime, base, source_request=request)
                worker._source_binding(result)
                if result.get('body_quality_candidate') is not True:
                    raise worker.DeferredPaper('admission_source_quality_not_verified')
            except runtime['fresh'].FreshSourceDependencyError:
                raise
            except (worker.DeferredPaper, runtime['fresh'].FreshSourceError) as exc:
                deferred.append(dict(paper_id=paper_id, reason=str(exc)[:500]))
                continue
            receipt = dict(paper_id=paper_id, state=state, result=result, metrics=metrics, declared_metadata=new.get(paper_id), admission_basis='explicit_user_curated_web3_source')
            filename = paper_id + '.json'
            digest = _write_once(base / filename, receipt)
            rows.append(dict(paper_id=paper_id, receipt=filename, sha256=digest))
    finally:
        conn.close()
    _write_once(base / 'queue.json', dict(schema=1, root=str(root), rows=rows, deferred=deferred))
    return dict(prepared=[row['paper_id'] for row in rows], deferred=deferred, corpus_writes=False, publication='waiting_for_original_repair')


def _service(root, runtime, coarse_endpoint):
    from urllib.parse import urlsplit
    if not isinstance(coarse_endpoint, str):
        raise worker.WorkerError('production coarse endpoint is required')
    parsed = urlsplit(coarse_endpoint)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or parsed.path != '/embed_batch' or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise worker.WorkerError('explicit loopback production coarse embed endpoint required')
    os.environ.update(DTOX_DATA_DIR=str(root), QDRANT_COLLECTION='papers_fulltext',
        EMBED_BATCH_URL=runtime['recipe']['endpoint'], EMBED_BATCH_URLS=runtime['recipe']['endpoint'],
        COARSE_INDEX_ENABLED='1', PAPER_FTS_ENABLED='1', COARSE_COLLECTION='papers_coarse_base',
        COARSE_DIM='768', COARSE_EMBED_BATCH_URL=coarse_endpoint)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'pipeline'))
    if 'service' in sys.modules:
        raise worker.WorkerError('pipeline service must be imported after production configuration')
    service = importlib.import_module('service')
    return service


def _run_once(base, root, coarse_endpoint, *, wait=False):
    base, root = Path(base).resolve(strict=True), Path(root).resolve(strict=True)
    queue = _read(base / 'queue.json')
    if queue.get('schema') != 1 or queue.get('root') != str(root):
        raise worker.WorkerError('admission queue root/schema differs')
    if not queue.get('rows'):
        return dict(no_work=True, deferred=queue.get('deferred',[]))
    if wait:
        barrier._wait_for_repair_complete()
    with worker._locks(base, wait=wait, preflight=barrier._require_repair_complete):
        runtime = worker._load_runtime(base, root)
        worker._runtime_preflight(runtime, runtime['worker'])
        service = _service(root, runtime, coarse_endpoint)
        session = service.requests.Session()
        # Probe both deployed dimensions before any state/cache publication.
        if len(service.embed_texts_batch(session, ['DTOX embedding dimension probe'])[0]) != 384:
            raise worker.WorkerError('document embedding dimension differs')
        import coarse_index
        if len(coarse_index.embed_coarse_batch(session, ['DTOX embedding dimension probe'])[0]) != 768:
            raise worker.WorkerError('coarse embedding dimension differs')
        conn = service.get_conn()
        try:
            for entry in queue['rows']:
                path = base / entry['receipt']
                if path.parent != base or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
                    raise worker.WorkerError('admission source receipt changed')
                receipt = _read(path)
                pid, result = entry['paper_id'], receipt['result']
                if pid != receipt['paper_id'] or pid != result['canonical_id']:
                    raise worker.WorkerError('admission source identity differs')
                raw = Path(result['source_path']).read_bytes()
                if hashlib.sha256(raw).hexdigest() != result['source_sha256']:
                    raise worker.WorkerError('admission parsed source changed')
                # Stock cache publication permits a degraded reader projection;
                # curated admission requires the complete independent manifest invariant.
                expected_manifest = runtime['document_cache'].make_section_manifest(pid, result['sections'],
                    json.dumps(result['chunks']).encode('utf-8'), raw,
                    result['parser_mode'], service.EXTRACTOR_VERSION)
                if _duplicate_aliases(conn, pid, receipt['state']['title'], root):
                    raise worker.WorkerError('admission title aliases changed after preparation')
                marker = base / (pid + '.admitted.json')
                if not marker.exists():
                    if receipt['state']['status'] == 'new':
                        if conn.execute('SELECT 1 FROM papers WHERE arxiv_id=?',(pid,)).fetchone():
                            raise worker.WorkerError('new canonical paper appeared after preparation')
                    elif _rejected(conn, pid) != receipt['state']:
                        raise worker.WorkerError('admission metadata changed after preparation')
                    _no_owned_state(conn, pid)
                    _no_owned_indexes(runtime, root, pid)
                    # Marker precedes the transaction so interrupted admission can resume.
                    _write_once(marker, dict(paper_id=pid, receipt_sha256=entry['sha256']))
                if _read(marker) != dict(paper_id=pid, receipt_sha256=entry['sha256']):
                    raise worker.WorkerError('admission ownership marker differs')
                history = conn.execute('SELECT first_known,completed FROM completion_history WHERE arxiv_id=?',(pid,)).fetchone()
                state = conn.execute('SELECT status FROM papers WHERE arxiv_id=?',(pid,)).fetchone()
                if history is not None:
                    sid = service.safe_id(pid)
                    expected_files = ((root/'fulltext_cache'/(sid+'.tex'), raw),
                        (root/'chunk_cache'/(sid+'.json'),json.dumps(result['chunks']).encode('utf-8')))
                    require_all = state is not None and state[0] in ('chunked','done')
                    for cache_path, expected_bytes in expected_files:
                        if ((require_all and not cache_path.exists())
                                or cache_path.exists() and cache_path.read_bytes() != expected_bytes):
                            raise worker.WorkerError('admission resumed cache differs from pinned source')
                    section_path = root/'section_cache'/(sid+'.json')
                    if ((require_all and not section_path.exists())
                            or section_path.exists() and _read(section_path) != expected_manifest):
                        raise worker.WorkerError('admission resumed section manifest differs')
                if history is None:
                    is_new = receipt['state']['status'] == 'new'
                    if (state is not None if is_new else state is None or state[0] != 'rejected'):
                        raise worker.WorkerError('admission state changed before first ownership')
                    conn.execute('BEGIN IMMEDIATE')
                    if is_new:
                        metadata = receipt['state']
                        conn.execute("INSERT INTO papers(arxiv_id,title,year,layers,abstract,source_url,status,passed,updated_at) VALUES(?,?,?,?,?,?,'fulltext_fetched',1,?)",(pid,metadata['title'],metadata['year'],'web3',metadata['abstract'],metadata['source_url'],service.now_iso()))
                    conn.execute('INSERT INTO completion_history(arxiv_id,first_known,completed) VALUES (?,1,0)',(pid,))
                    layers = list(dict.fromkeys([part for part in (receipt['state'].get('layers') or '').split(',') if part] + ['web3']))
                    conn.execute("UPDATE papers SET status='fulltext_fetched',passed=1,layers=?,updated_at=? WHERE arxiv_id=? AND status='rejected'",(','.join(layers),service.now_iso(),pid))
                    conn.commit()
                elif not history[0]:
                    raise worker.WorkerError('admission no longer has first-publication ownership')
                if state is None or state[0] in ('rejected','fulltext_fetched'):
                    target = service.FULLTEXT_DIR / (service.safe_id(pid) + '.tex')
                    target.parent.mkdir(exist_ok=True)
                    if target.exists() and target.read_bytes() != raw:
                        raise worker.WorkerError('admission source cache differs')
                    from pipeline.publication import _atomic
                    _atomic(target, raw)
                    service.CHUNK_DIR.mkdir(exist_ok=True)
                    service._write_parse_cache(pid,result['chunks'],raw,result['parser_mode'],canonical_sections=result['sections'])
                    label = worker._source_route(result,pid)[1]
                    conn.execute("UPDATE papers SET status='chunked',fulltext_source=?,parse_status='ok',parser_mode=?,parser_revision=?,extractor_version=?,updated_at=? WHERE arxiv_id=? AND status='fulltext_fetched'",(label,result['parser_mode'],service.EXTRACTOR_VERSION,service.EXTRACTOR_VERSION,service.now_iso(),pid))
                    conn.commit()
            ids = [entry['paper_id'] for entry in queue['rows']]
            while True:
                service.embed_step(conn, session, limit=len(ids), paper_ids=ids)
                service._reconcile_index_sync(conn, session, limit=len(ids), paper_ids=ids)
                status = {pid:dict(conn.execute('SELECT status,parse_status,fulltext_source FROM papers WHERE arxiv_id=?',(pid,)).fetchone()) for pid in ids}
                pending = [pid for pid in ids if status[pid]['status'] != 'done' or conn.execute('SELECT 1 FROM paper_index_sync WHERE arxiv_id=?',(pid,)).fetchone()]
                blocked = [pid for pid in ids if conn.execute("SELECT 1 FROM paper_publications WHERE arxiv_id=? AND state='blocked'",(pid,)).fetchone()]
                report = dict(papers=status, pending=pending, deferred=queue.get('deferred',[]))
                temporary = base / 'run-status.json.tmp'
                temporary.write_text(json.dumps(report,sort_keys=True),encoding='utf-8')
                os.replace(temporary, base / 'run-status.json')
                if blocked:
                    raise worker.WorkerError('first publication blocked; retained for inspection: ' + ','.join(blocked))
                if not pending:
                    return report
                time.sleep(30)
        finally:
            conn.close()
            session.close()


def run(base, root, coarse_endpoint, *, wait=False):
    while True:
        try:
            return _run_once(base, root, coarse_endpoint, wait=wait)
        except barrier._RepairLifecycleBusy:
            if not wait:
                raise worker.WorkerError('original repair is not complete; queue retained')
            barrier._wait_for_repair_complete()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('prepare','run'))
    parser.add_argument('--base',type=Path,required=True)
    parser.add_argument('--root',type=Path,default=worker.ROOT)
    parser.add_argument('--paper-id',action='append',default=[])
    parser.add_argument('--new-papers',type=Path)
    parser.add_argument('--coarse-endpoint')
    parser.add_argument('--wait-for-lock',action='store_true')
    args=parser.parse_args()
    result=(prepare(args.base,args.root,args.paper_id,args.new_papers) if args.command=='prepare' else
            run(args.base,args.root,args.coarse_endpoint,wait=args.wait_for_lock))
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':
    main()
