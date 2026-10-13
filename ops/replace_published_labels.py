"""Explicit offline metadata/cache-point replacement of first-publication snapshots.

Requires maintenance of readers and writers. Does not admit managed revisions,
change first-publication rules, rewrite existing vectors, or alter paper/coarse metadata.
Optional source-proven cached additions embed and insert only missing points.
"""
import base64
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid

from ops import repair_section_labels as labels
from pipeline import document_cache
from pipeline.payload_contract import bibliographic_payload_equal

RepairError = labels.RepairError
_jbytes = labels._json_bytes


def _decode(value):
    return base64.b64decode(value)


def _snapshot_revision(artifact, cache_sha):
    points = json.loads(json.dumps(artifact['points'], allow_nan=False))
    for p in points:
        p['payload'].pop('publication_revision', None)
    material = _jbytes({'points': points, 'metadata': artifact['metadata']})
    return hashlib.sha256(cache_sha.encode() + material).hexdigest()


def _scroll(client, collection, aid):
    points, offset, seen = [], None, set()
    while True:
        kwargs = dict(collection_name=collection,
            scroll_filter={'must': [{'key': 'arxiv_id', 'match': {'value': aid}}]},
            limit=200, with_payload=True, with_vectors=True)
        if offset is not None:
            kwargs['offset'] = offset
        page, offset = client.scroll(**kwargs)
        for p in page:
            vector = p.get('vector') if isinstance(p, dict) else p.vector
            points.append(json.loads(json.dumps({'id': labels._point_id(p),
                'payload': labels._payload(p), 'vector': vector}, allow_nan=False)))
        if len(points) > 3000:
            raise RepairError('snapshot exceeds existing 3000-point bound')
        if offset is None:
            break
        marker = _jbytes(offset)
        if marker in seen:
            raise RepairError('repeated Qdrant pagination offset')
        seen.add(marker)
    result = {p['id']: p for p in points}
    if len(result) != len(points):
        raise RepairError('duplicate Qdrant point IDs')
    return result


def _vector_equal(a, b):
    return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and bool(a)
            and all(isinstance(x, (int, float)) and isinstance(y, (int, float))
                    and math.isfinite(x) and math.isfinite(y)
                    and abs(x-y) <= 1e-6 for x, y in zip(a, b)))


def _state_check(conn, paper):
    aid = paper['arxiv_id']
    if conn.execute('SELECT status FROM papers WHERE arxiv_id=?', (aid,)).fetchone() != ('done',):
        raise RepairError('paper must remain done')
    if labels._exists(conn, 'paper_active') and conn.execute(
            'SELECT 1 FROM paper_active WHERE arxiv_id=?', (aid,)).fetchone():
        raise RepairError('managed revision is outside snapshot replacement')
    metadata = conn.execute('SELECT title,year,layers,source_url FROM papers WHERE arxiv_id=?', (aid,)).fetchone()
    if dict(zip(('title', 'year', 'layers', 'source_url'), metadata)) != paper['metadata']:
        raise RepairError('paper metadata drift')
    rows = conn.execute("SELECT revision,state FROM paper_publications WHERE arxiv_id=? AND kind='snapshot' AND state='published'", (aid,)).fetchall()
    if rows not in ([(paper['parent_revision'], 'published')], [(paper['revision'], 'published')]):
        raise RepairError('current published snapshot changed')
    # An unfinished ingestion/replacement must not be overwritten by this repair.
    others = conn.execute("SELECT revision FROM paper_publications WHERE arxiv_id=? AND kind='snapshot' AND state NOT IN ('published','superseded')", (aid,)).fetchall()
    if any(r[0] != paper['revision'] for r in others):
        raise RepairError('another snapshot operation is unfinished')


def _parent_payload_equal(live, parent):
    return bibliographic_payload_equal(live, parent)


def _prepare_additions(request, aid, chunks, live, fts, fts_db, qdrant,
                       embedder, recipe, source_parser):
    from ops import restore_cached_points as restore
    from pipeline import extractor
    indices=request['chunk_indices']
    if (request['arxiv_id']!=aid or not indices or len(indices)>20
            or any(type(i)is not int for i in indices) or len(set(indices))!=len(indices)):
        raise RepairError('one paper and 1..20 distinct cached additions required')
    if not live or len(live)+len(indices)>3000:
        raise RepairError('cached additions exceed bounded snapshot point count')
    if (recipe is None or recipe.get('dimension')!=384 or recipe.get('document_prefix')!=''
            or recipe.get('pooling')!='CLS/L2' or recipe.get('max_length')!=512
            or not recipe.get('model_sha256') or not recipe.get('tokenizer_sha256')):
        raise RepairError('verified deployed document embedding recipe required')
    source=restore._read(request['source_path'],request['expected_source_sha256'])
    route=request['source_route']
    if route not in ('latex','paper_markdown','spec_markdown'):
        raise RepairError('unsupported explicit source route')
    parsed,elements=restore.parse_source_views(source,route,source_parser)
    by={c['chunk_index']:c for c in chunks};targets=[]
    for idx in indices:
        c=by.get(idx)
        if (c is None or not extractor.should_embed(c) or not restore.source_proven(c,parsed,elements)
                or c.get('section_type','other')not in document_cache.SECTION_TYPES
                or not document_cache._label(c.get('section_title',''))):
            raise RepairError('cached addition is not source-proven with valid labels')
        targets.append(c)
    ids=[str(uuid.uuid5(uuid.NAMESPACE_URL,f'{aid}#{i}'))for i in indices]
    inventory=restore._inventory(request['rowmap_db'],fts_db)
    try:
        for pid in ids:
            if (pid in live or inventory.execute('SELECT 1 FROM rows WHERE point_id=? LIMIT 1',(pid,)).fetchone()
                    or restore._fts_point(fts,pid)):
                raise RepairError('cached addition UUID already exists')
    finally:inventory.close()
    for collection in ('papers_fulltext','papers_coarse_base'):
        if restore._retrieve(qdrant,collection,ids):raise RepairError('cached addition Qdrant UUID collision')
    points=restore.build_cached_points(aid,chunks,targets,next(iter(live.values()))['payload'],embedder)
    restore._read(request['source_path'],request['expected_source_sha256'])
    return points


def prepare_plan(changes, *, state_db, fts_db, cache_dir, section_cache_dir,
                 journal_path, qdrant, collection='papers_fulltext',
                 complete_fts_rowids_by_paper=None, cache_labels_by_paper=None,
                 expected_indexed_indices_by_paper=None, source_bytes_by_paper=None,
                 expected_source_sha256_by_paper=None, source_route_by_paper=None,
                 cached_additions=None, embedder=None, embedding_recipe=None, source_parser=None):
    """Prepare one paper's label changes and/or source-proven cached additions."""
    paper_ids = {c['arxiv_id'] for c in changes}
    if cached_additions is not None:paper_ids.add(cached_additions['arxiv_id'])
    if not paper_ids and cache_labels_by_paper is not None and expected_indexed_indices_by_paper is not None:
        paper_ids = set(cache_labels_by_paper)
    if len(paper_ids) != 1:
        raise RepairError('one paper per replacement journal required')
    if collection != 'papers_fulltext' or Path(journal_path).exists():
        raise RepairError('existing journal or unsupported collection')
    aid = next(iter(paper_ids))
    state = labels._connect(state_db, readonly=True)
    fts = labels._connect(fts_db, readonly=True)
    try:
        current = state.execute("SELECT revision,artifact_path,cache_sha256 FROM paper_publications WHERE arxiv_id=? AND kind='snapshot' AND state='published'", (aid,)).fetchall()
        if len(current) != 1:
            raise RepairError('exactly one published parent snapshot required')
        parent, artifact_path, cache_sha = current[0]
        artifact_raw = document_cache.read_bounded(Path(artifact_path).resolve(), 64*1024*1024)
        artifact = json.loads(artifact_raw)
        if _snapshot_revision(artifact, cache_sha) != parent:
            raise RepairError('parent snapshot artifact hash mismatch')
        cache_path = Path(cache_dir).resolve() / (document_cache.safe_id(aid)+'.json')
        if Path(artifact['cache_path']).resolve() != cache_path:
            raise RepairError('parent cache path differs from supplied resolved cache')
        before_cache = document_cache.read_bounded(cache_path)
        if labels._sha(before_cache) != cache_sha:
            raise RepairError('parent cache hash mismatch')
        if cached_additions is not None and cached_additions['expected_cache_sha256']!=cache_sha:
            raise RepairError('frozen cached additions hash differs from parent')
        chunks = json.loads(before_cache)
        by_index = {c['chunk_index']: c for c in chunks}
        if len(by_index) != len(chunks):
            raise RepairError('duplicate cache indices')
        supplied_labels = None
        if cache_labels_by_paper is not None:
            if set(cache_labels_by_paper) != {aid}:
                raise RepairError('cache label paper scope mismatch')
            supplied_labels = {}
            for c in cache_labels_by_paper[aid]:
                idx = c.get('chunk_index')
                if (type(idx) is not int or idx in supplied_labels
                        or c.get('section_type') not in document_cache.SECTION_TYPES
                        or not document_cache._label(c.get('section_title'))):
                    raise RepairError('invalid complete cache label record')
                supplied_labels[idx] = (c['section_title'],c['section_type'])
            if set(supplied_labels) != set(by_index):
                raise RepairError('cache labels must cover every cached chunk')
        expected_indices = set(by_index)
        if expected_indexed_indices_by_paper is not None:
            if set(expected_indexed_indices_by_paper) != {aid}:
                raise RepairError('indexed paper scope mismatch')
            values = expected_indexed_indices_by_paper[aid]
            if (any(type(i) is not int for i in values) or len(set(values)) != len(values)
                    or not set(values) <= set(by_index)):
                raise RepairError('invalid expected indexed set')
            expected_indices = set(values)
        live = _scroll(qdrant, collection, aid)
        if cached_additions is not None:
            from ops import restore_cached_points as restore
            restore._catalog(fts)
            if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()!=(1,):
                raise RepairError('published additions require complete FTS ownership')
            expected_indices={p['payload']['chunk_index']for p in live.values()}
        original = {str(p['id']): p for p in artifact['points']}
        if len(original) != len(artifact['points']) or set(live) != set(original):
            raise RepairError('live point set differs from complete parent artifact')
        indices = set()
        for pid, p in live.items():
            old = original[pid]
            idx = p['payload'].get('chunk_index')
            if (not _parent_payload_equal(p['payload'], old['payload']) or not _vector_equal(p['vector'], old['vector'])
                    or p['payload'].get('publication_revision') != parent
                    or p['payload'].get('arxiv_id') != aid or idx in indices or idx not in by_index
                    or by_index[idx].get('text') != p['payload'].get('text')
                    or by_index[idx].get('section_title', '') != p['payload'].get('section_title')
                    or by_index[idx].get('section_type', 'other') != p['payload'].get('section_type')):
                raise RepairError('parent/live/cache payload or vector mismatch')
            indices.add(idx)
        if indices != expected_indices:
            raise RepairError('cache/point bijection incomplete')
        # Independently establish the entire paper FTS set, including duplicates.
        supplied_rowids = None
        if cached_additions is not None:
            supplied_rowids=[r[0]for r in fts.execute('SELECT row_id FROM pipeline_fts_points WHERE arxiv_id=?',(aid,))]
        if complete_fts_rowids_by_paper is not None:
            if set(complete_fts_rowids_by_paper) != {aid}:
                raise RepairError('complete FTS map paper scope mismatch')
            supplied_rowids = complete_fts_rowids_by_paper[aid]
            if (any(type(i) is not int or i <= 0 for i in supplied_rowids)
                    or len(set(supplied_rowids)) != len(supplied_rowids)):
                raise RepairError('invalid complete FTS row IDs')
        rows = _fts_rows(fts,aid,supplied_rowids)
        fts_by_id = {r[2]: r for r in rows}
        if len(fts_by_id) != len(rows) or set(fts_by_id) != set(live):
            raise RepairError('complete FTS identity mapping mismatch')
        for pid, r in fts_by_id.items():
            if (r[3] != aid or (r[1], r[4]) != (live[pid]['payload']['text'], live[pid]['payload']['section_type'])):
                raise RepairError('FTS text/type differs from parent')
        if supplied_labels is not None:
            for idx, (title, kind) in supplied_labels.items():
                by_index[idx]['section_title'],by_index[idx]['section_type'] = title,kind
        seen = set()
        for c in changes:
            pid = c['point_id']
            if (pid in seen or pid not in live or fts_by_id[pid][0] != c['rowid']
                    or c['section_type'] not in document_cache.SECTION_TYPES
                    or not document_cache._label(c['section_title'])):
                raise RepairError('invalid or duplicate supplied change')
            seen.add(pid)
            idx = live[pid]['payload']['chunk_index']
            if supplied_labels is not None and supplied_labels[idx] != (c['section_title'],c['section_type']):
                raise RepairError('selected point labels disagree with full cache labels')
            by_index[idx]['section_title'] = c['section_title']
            by_index[idx]['section_type'] = c['section_type']
        after_cache = before_cache if chunks==json.loads(before_cache) else _jbytes(chunks)
        candidate = json.loads(json.dumps(artifact, allow_nan=False))
        # Use exact readback vectors; the operation never writes/re-embeds them.
        candidate['points'] = [live[str(p['id'])] for p in artifact['points']]
        for p in candidate['points']:
            c = by_index[p['payload']['chunk_index']]
            p['payload']['section_title'] = c['section_title']
            p['payload']['section_type'] = c['section_type']
        points_added=[]
        if cached_additions is not None:
            points_added=_prepare_additions(cached_additions,aid,chunks,live,fts,fts_db,qdrant,
                embedder,embedding_recipe,source_parser)
            candidate['points'].extend(points_added)
        revision = _snapshot_revision(candidate, labels._sha(after_cache))
        if revision == parent:
            raise RepairError('replacement is a no-op')
        for p in candidate['points']:
            p['payload']['publication_revision'] = revision
        new_artifact_path = Path(artifact_path).parent / (hashlib.sha256(aid.encode()).hexdigest()+'-'+revision+'.json')
        manifest_path = Path(section_cache_dir).resolve() / cache_path.name
        before_manifest = after_manifest = None
        if manifest_path.exists():
            before_manifest = document_cache.read_bounded(manifest_path)
            if after_cache==before_cache:
                after_manifest=before_manifest
            elif (source_bytes_by_paper is None or aid not in source_bytes_by_paper
                    or expected_source_sha256_by_paper is None or aid not in expected_source_sha256_by_paper
                    or source_route_by_paper is None or aid not in source_route_by_paper):
                raise RepairError('existing manifest repair requires frozen source bytes, hash, and route')
            else:
                after_manifest = labels._manifest_after(before_manifest, aid, before_cache, after_cache,
                    source_bytes=source_bytes_by_paper[aid],
                    expected_source_sha256=expected_source_sha256_by_paper[aid],
                    source_route=source_route_by_paper[aid])
        paper = dict(arxiv_id=aid, parent_revision=parent, revision=revision,
            metadata=artifact['metadata'], parent_artifact_path=str(Path(artifact_path).resolve()),
            parent_artifact_sha256=labels._sha(artifact_raw), artifact_path=str(new_artifact_path),
            artifact=candidate, cache_path=str(cache_path), cache_sha256=labels._sha(after_cache),
            manifest_path=str(manifest_path) if before_manifest is not None else None,
            cache_before=base64.b64encode(before_cache).decode(), cache_after=base64.b64encode(after_cache).decode(),
            manifest_before=base64.b64encode(before_manifest).decode() if before_manifest is not None else None,
            manifest_after=base64.b64encode(after_manifest).decode() if after_manifest is not None else None,
            points_before=list(_scroll(qdrant, collection, aid).values()), fts_before=rows,
            fts_rowids_from_complete_inventory=supplied_rowids)
        if cached_additions is not None:
            proof=dict(cached_additions)
            for key in ('source_path','rowmap_db'):proof[key]=str(Path(proof[key]).resolve())
            paper.update(points_added=points_added,addition_proof=proof,
                embedding_recipe=embedding_recipe,addition_operation_id=str(uuid.uuid4()))
        _state_check(state, paper)
        plan = dict(schema=1, kind='published-label-replacement', phase='prepared',
                    state_db=str(Path(state_db).resolve()), fts_db=str(Path(fts_db).resolve()),
                    collection=collection, paper=paper)
        labels._atomic(journal_path, _jbytes(plan))
        return plan
    finally:
        state.close(); fts.close()


def _load(path):
    plan = json.loads(Path(path).read_text(encoding='utf-8'))
    if plan.get('schema') != 1 or plan.get('kind') != 'published-label-replacement':
        raise RepairError('invalid replacement journal')
    return plan


def _fts_rows(conn, aid, rowids=None):
    columns = 'rowid,text,point_id,arxiv_id,section_type,element_type,layers,year'
    if rowids is None:
        return conn.execute('SELECT '+columns+' FROM chunks WHERE arxiv_id=?',(aid,)).fetchall()
    result = []
    for rowid in rowids:
        row = conn.execute('SELECT '+columns+' FROM chunks WHERE rowid=?',(rowid,)).fetchone()
        if row is None:
            raise RepairError('complete inventory FTS row is missing')
        result.append(row)
    return result


def _verify(plan, qdrant, mode):
    paper = plan['paper']
    state = labels._connect(plan['state_db'], readonly=True)
    fts = labels._connect(plan['fts_db'], readonly=True)
    try:
        _state_check(state, paper)
        if labels._sha(Path(paper['parent_artifact_path']).read_bytes()) != paper['parent_artifact_sha256']:
            raise RepairError('parent artifact changed')
        for key in ('cache', 'manifest'):
            path = paper.get(key+'_path')
            if path:
                choices = [_decode(paper[key+'_'+side]) for side in ('before', 'after')]
                allowed = choices if mode == 'partial' else [choices[mode == 'after']]
                if Path(path).read_bytes() not in allowed:
                    raise RepairError(key+' drift')
        live = _scroll(qdrant, plan['collection'], paper['arxiv_id'])
        before = {p['id']: p for p in paper['points_before']}
        after = {p['id']: p for p in paper['artifact']['points']}
        added={p['id']:p for p in paper.get('points_added',[])}
        required=set(after)if mode=='after'else set(before)
        permitted=set(after)if mode in('after','partial')else set(before)
        if not required<=set(live) or not set(live)<=permitted:
            raise RepairError('point set drift')
        for pid, p in live.items():
            if pid in added:continue
            choices = [before[pid], after[pid]]
            allowed = choices if mode == 'partial' else [choices[mode == 'after']]
            if p not in allowed:
                raise RepairError('point payload/vector drift')
        rows = _fts_rows(fts,paper['arxiv_id'],paper.get('fts_rowids_from_complete_inventory'))
        by_id = {r[2]: list(r) for r in rows}
        if len(by_id) != len(rows) or set(by_id) != set(before):
            raise RepairError('FTS set drift')
        for row in paper['fts_before']:
            old = list(row); new = list(row)
            new[4] = after[row[2]]['payload']['section_type']
            allowed = [old,new] if mode == 'partial' else [new if mode == 'after' else old]
            if by_id[row[2]] not in allowed:
                raise RepairError('FTS row drift')
        if added:
            from ops import restore_cached_points as restore
            proof=paper['addition_proof']
            restore._read(proof['source_path'],proof['expected_source_sha256'])
            restore._catalog(fts)
            if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()!=(1,):
                raise RepairError('complete FTS ownership changed')
            inventory=restore._inventory(proof['rowmap_db'],plan['fts_db'])
            try:
                global_added=restore._retrieve(qdrant,plan['collection'],list(added))
                if set(global_added)!=(set(live)&set(added)):
                    raise RepairError('added UUID acquired another paper identity')
                if restore._retrieve(qdrant,'papers_coarse_base',list(added)):
                    raise RepairError('addition coarse UUID collision')
                catalog_ids={r[0]for r in fts.execute('SELECT point_id FROM pipeline_fts_points WHERE arxiv_id=?',(paper['arxiv_id'],))}
                if not set(before)<=catalog_ids or not catalog_ids<=set(after):
                    raise RepairError('addition catalog point set drift')
                for pid,p in added.items():
                    current=live.get(pid)
                    if current is not None and not restore._candidate_equal(current,p):
                        raise RepairError('added point payload/vector drift')
                    owned=restore._fts_point(fts,pid)
                    receipt=None
                    if labels._exists(fts,'dtox_cached_point_receipts'):
                        receipt=fts.execute('SELECT row_id FROM dtox_cached_point_receipts WHERE operation_id=? AND point_id=?',
                            (paper['addition_operation_id'],pid)).fetchone()
                    if inventory.execute('SELECT 1 FROM rows WHERE point_id=?LIMIT 1',(pid,)).fetchone():
                        raise RepairError('added UUID entered frozen map')
                    if owned is not None and (owned[1]!=restore._expected(p)or receipt is None or receipt[0]!=owned[0]):
                        raise RepairError('added FTS ownership drift')
                    if receipt is not None and (owned is None or receipt[0]!=owned[0]):
                        raise RepairError('added receipt drift')
                    if ((mode=='before'and(current is not None or owned is not None))
                            or(mode=='after'and(current is None or owned is None))):
                        raise RepairError('added point/FTS visibility mismatch')
            finally:inventory.close()
    finally:
        state.close(); fts.close()


def _checkpoint(path, plan, phase):
    plan['phase'] = phase
    labels._atomic(path, _jbytes(plan))


def _switch(plan, forward):
    paper = plan['paper']
    state = labels._connect(plan['state_db'])
    try:
        state.execute('BEGIN IMMEDIATE')
        _state_check(state, paper)
        source, target = (paper['parent_revision'],paper['revision']) if forward else (paper['revision'],paper['parent_revision'])
        row = state.execute('SELECT state FROM paper_publications WHERE arxiv_id=? AND revision=?', (paper['arxiv_id'], target)).fetchone()
        if row is None:
            raise RepairError('target snapshot row missing')
        state.execute("UPDATE paper_publications SET state='superseded' WHERE arxiv_id=? AND revision=?", (paper['arxiv_id'],source))
        state.execute("UPDATE paper_publications SET state='published',completed=?,error_code=NULL WHERE arxiv_id=? AND revision=?", (_jbytes(['preflight','qdrant','fts','complete']).decode(),paper['arxiv_id'],target))
        state.commit()
    finally:
        state.close()


def apply_plan(journal_path, *, qdrant):
    plan = _load(journal_path); paper = plan['paper']
    if plan['phase'] != 'prepared':
        raise RepairError('replacement must be prepared')
    _verify(plan,qdrant,'before')
    _checkpoint(journal_path,plan,'applying')
    raw = _jbytes(paper['artifact']); path = Path(paper['artifact_path'])
    if path.exists() and path.read_bytes() != raw:
        raise RepairError('candidate artifact collision')
    labels._atomic(path,raw)
    state = labels._connect(plan['state_db'])
    try:
        state.execute('BEGIN IMMEDIATE'); _state_check(state,paper)
        existing = state.execute('SELECT kind,artifact_path,cache_sha256,state FROM paper_publications WHERE arxiv_id=? AND revision=?',
            (paper['arxiv_id'],paper['revision'])).fetchone()
        if existing is None:
            state.execute("INSERT INTO paper_publications(arxiv_id,revision,kind,artifact_path,cache_sha256,state) VALUES (?,?,'snapshot',?,?,'replacement_staged')", (paper['arxiv_id'],paper['revision'],str(path),paper['cache_sha256']))
        elif existing == ('snapshot',str(path),paper['cache_sha256'],'superseded'):
            state.execute("UPDATE paper_publications SET state='replacement_staged' WHERE arxiv_id=? AND revision=?",
                (paper['arxiv_id'],paper['revision']))
        else:
            raise RepairError('candidate journal identity conflict')
        state.commit()
    finally:
        state.close()
    # BaseException interruptions deliberately retain durable journal for rollback.
    try:
        _write_surfaces(plan,qdrant,True)
        _verify(plan,qdrant,'after')
        _switch(plan,True)
        _checkpoint(journal_path,plan,'applied')
    except Exception:
        rollback_plan(journal_path,qdrant=qdrant)
        raise
    return plan


def _write_surfaces(plan,client,forward):
    paper = plan['paper']
    points = paper['artifact']['points'] if forward else paper['points_before']
    added=paper.get('points_added',[])
    added_ids={p['id']for p in added}
    if added:
        if forward:client.upsert(collection_name=plan['collection'],points=added,wait=True)
        else:client.delete(collection_name=plan['collection'],points_selector=list(added_ids),wait=True)
    groups = {}
    for p in points:
        if p['id']in added_ids:continue
        key = tuple(p['payload'][k] for k in ('section_title','section_type','publication_revision'))
        groups.setdefault(key,[]).append(p['id'])
    for key, ids in groups.items():
        for start in range(0,len(ids),200):
            client.set_payload(collection_name=plan['collection'],points=ids[start:start+200],wait=True,
                payload=dict(zip(('section_title','section_type','publication_revision'),key)))
    types_by_id = {p['id']:p['payload']['section_type'] for p in points}
    fts = labels._connect(plan['fts_db'])
    try:
        fts.execute('BEGIN IMMEDIATE')
        for row in paper['fts_before']:
            kind = types_by_id[row[2]]
            cur = fts.execute('UPDATE chunks SET section_type=? WHERE rowid=? AND point_id=? AND arxiv_id=?', (kind,row[0],row[2],paper['arxiv_id']))
            if cur.rowcount != 1:
                raise RepairError('FTS exact update failed')
        if added:
            from ops import restore_cached_points as restore
            if forward:
                fts.execute('CREATE TABLE IF NOT EXISTS dtox_cached_point_receipts(operation_id TEXT NOT NULL,point_id TEXT NOT NULL,row_id INTEGER NOT NULL,PRIMARY KEY(operation_id,point_id))')
                fts.execute('CREATE INDEX IF NOT EXISTS dtox_cached_point_receipts_point ON dtox_cached_point_receipts(point_id,row_id)')
                for p in added:
                    if restore._fts_point(fts,p['id']):raise RepairError('addition acquired FTS owner')
                    rid=fts.execute('INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year)VALUES(?,?,?,?,?,?,?)',restore._expected(p)).lastrowid
                    fts.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(p['id'],paper['arxiv_id'],rid))
                    fts.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(paper['arxiv_id'],rid))
                    fts.execute('INSERT INTO dtox_cached_point_receipts VALUES(?,?,?)',(paper['addition_operation_id'],p['id'],rid))
            elif labels._exists(fts,'dtox_cached_point_receipts'):
                for p in added:
                    row=fts.execute('SELECT row_id FROM dtox_cached_point_receipts WHERE operation_id=?AND point_id=?',(paper['addition_operation_id'],p['id'])).fetchone()
                    if row:
                        fts.execute('DELETE FROM chunks WHERE rowid=?',(row[0],))
                        fts.execute('DELETE FROM pipeline_fts_points WHERE point_id=?AND row_id=?',(p['id'],row[0]))
                        fts.execute("DELETE FROM pipeline_fts_rows WHERE table_name='chunks'AND arxiv_id=?AND row_id=?",(paper['arxiv_id'],row[0]))
                        fts.execute('DELETE FROM dtox_cached_point_receipts WHERE operation_id=?AND point_id=?',(paper['addition_operation_id'],p['id']))
        fts.commit()
    finally:
        fts.close()
    for key in ('cache','manifest'):
        if paper.get(key+'_path'):
            labels._atomic(paper[key+'_path'], _decode(paper[key+('_after' if forward else '_before')]))


def rollback_plan(journal_path, *, qdrant):
    plan = _load(journal_path)
    if plan['phase'] not in ('applying','applied','rolling_back'):
        raise RepairError('no replacement operation to roll back')
    _verify(plan,qdrant,'partial')
    _checkpoint(journal_path,plan,'rolling_back')
    _write_surfaces(plan,qdrant,False)
    paper = plan['paper']
    state = labels._connect(plan['state_db'],readonly=True)
    try:
        candidate = state.execute('SELECT state FROM paper_publications WHERE arxiv_id=? AND revision=?',(paper['arxiv_id'],paper['revision'])).fetchone()
    finally:
        state.close()
    if candidate:
        _switch(plan,False)
    _verify(plan,qdrant,'before')
    _checkpoint(journal_path,plan,'rolled_back')
    return plan
