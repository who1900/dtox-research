"""Explicit offline snapshot transitions and exact source-bound orphan adoption.

Requires maintenance of readers and writers. Does not admit managed revisions
or change first-publication rules. The label/addition route preserves existing
fulltext vectors; whole-document replacement stages a fresh complete source,
prepared vectors and exact before-images before its bounded maintenance writes.
Orphan adoption creates missing projections; cached additions insert missing points.
"""
import base64
import gzip
import hashlib
import inspect
import json
import math
import re
from datetime import datetime
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


def _document_rows(conn, table, aid, fts_table=None):
    if table in ('pipeline_fts_rows','pipeline_fts_papers'):
        if fts_table not in ('chunks','papers_fts'): raise RepairError('explicit FTS family required')
        return [list(r) for r in conn.execute('SELECT * FROM '+table+
            ' WHERE table_name=? AND arxiv_id=? ORDER BY rowid',(fts_table,aid))]
    return [list(r) for r in conn.execute('SELECT * FROM '+table+' WHERE arxiv_id=? ORDER BY rowid',(aid,))]


def _document_tables(conn, aid, paper_fts=False):
    tables = ('pipeline_fts_rows', 'pipeline_fts_papers') if paper_fts else ('pipeline_fts_points', 'pipeline_fts_rows')
    return {t: _document_rows(conn, t, aid, 'papers_fts' if paper_fts else 'chunks') for t in tables}


def _document_fts_rows(conn, aid):
    # Complete indexed ownership replaces the unindexed FTS arxiv_id scan.
    # _owned_rows validates each raw rowid and UUID against the existing catalog;
    # unlike ensure_catalog/_scope_complete, this helper performs no DDL.
    from pipeline.fts_catalog import _owned_rows, CatalogBlocked
    try: rowids=_owned_rows(conn,aid)
    except CatalogBlocked as exc: raise RepairError(str(exc)) from exc
    return [list(r) for r in _fts_rows(conn,aid,rowids)]


def _document_paper_fts_rows(conn,aid):
    rowids=conn.execute("SELECT row_id FROM pipeline_fts_rows WHERE table_name='papers_fts' AND arxiv_id=?",(aid,)).fetchall()
    rows=[]
    for (rid,) in rowids:
        row=conn.execute('SELECT rowid,arxiv_id,title,abstract,layers,year FROM papers_fts WHERE rowid=?',(rid,)).fetchone()
        if row is None or row[1]!=aid: raise RepairError('paper FTS row ownership drift')
        rows.append(list(row))
    return rows


def _document_last_rowid(conn,table):
    if table not in ('chunks','papers_fts'): raise RepairError('unsupported FTS family')
    # FTS5 consumes rowid descending order; max(rowid) can scan the full table.
    row=conn.execute('SELECT rowid FROM '+table+' ORDER BY rowid DESC LIMIT 1').fetchone()
    return row[0] if row else 0


def _document_policy_binding():
    from pipeline import extractor
    function=extractor.is_boilerplate_chunk
    if function is None: raise RepairError('current admission policy unavailable')
    return dict(filter_enabled=extractor.BOILERPLATE_FILTER,
        extractor_sha256=labels._sha(Path(inspect.getsourcefile(extractor)).read_bytes().replace(b'\r\n',b'\n')),
        policy_sha256=labels._sha(Path(inspect.getsourcefile(function)).read_bytes().replace(b'\r\n',b'\n')))


def prepare_document_replacement(paper_id, *, candidate, state_db, fts_db, paper_fts_db,
        cache_dir, section_cache_dir, fulltext_cache_dir, publication_cache_dir,
        journal_path, qdrant, source_parser=None, rowid_floors=None):
    """Stage one complete fresh-source replacement; no writes to corpus surfaces.

    Candidate supplies source_path/sha256/binding/route, complete chunks/manifest,
    prepared points and deployed embedding_recipe. source_parser is injectable
    for isolated tests; production replays the canonical extractor. Existing IDs
    can be reused only by apply_plan(maintenance=True), with exact before-images.
    """
    from ops import restore_cached_points as restore
    from pipeline import extractor
    floors={} if rowid_floors is None else rowid_floors
    if (not isinstance(floors,dict) or not set(floors)<={'chunks','papers_fts'}
            or any(type(v) is not int or v<0 for v in floors.values())):
        raise RepairError('rowid floors must be nonnegative integers for existing FTS families')
    if Path(journal_path).exists(): raise RepairError('replacement journal already exists')
    c = json.loads(json.dumps(candidate, allow_nan=False))
    source = restore._read(c['source_path'], c['source_sha256'])
    binding = c['source_binding']
    if (not isinstance(binding.get('url'), str) or not binding['url'].startswith('https://')
            or not binding.get('version') or not binding.get('retrieved_at')):
        raise RepairError('fresh source URL, version and retrieval receipt required')
    try: datetime.fromisoformat(binding['retrieved_at'].replace('Z', '+00:00'))
    except (ValueError, TypeError): raise RepairError('invalid source retrieval timestamp')
    sections = []
    if source_parser is None:
        parsed = restore.parse_source(source, c['source_route'], section_sink=sections)
    else: parsed, sections = source_parser(source, c['source_route'])
    if (not parsed or parsed != c['chunks'] or not sections
            or any(not isinstance(x.get('text'),str) or '\ufffd' in x['text'] for x in parsed)):
        raise RepairError('candidate must equal complete canonical source replay')
    cache = _jbytes(parsed)
    manifest = c['manifest']
    document_cache.validate_section_manifest(manifest, paper_id, cache, source)
    expected_manifest = document_cache.make_section_manifest(paper_id, sections, cache,
        source, manifest['parser_mode'], manifest['parser_revision'])
    if manifest != expected_manifest: raise RepairError('candidate canonical manifest differs from source replay')
    recipe = c['embedding_recipe']
    if (recipe.get('dimension') != 384 or recipe.get('document_prefix') != ''
            or recipe.get('pooling') != 'CLS/L2' or recipe.get('max_length') != 512
            or not recipe.get('model_sha256') or not recipe.get('tokenizer_sha256')):
        raise RepairError('verified deployed document embedding recipe required')
    indices = [x.get('chunk_index') for x in parsed]
    if any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != len(indices):
        raise RepairError('invalid complete source chunk indices')
    admitted = {str(uuid.uuid5(uuid.NAMESPACE_URL, f'{paper_id}#{x["chunk_index"]}')): x
                for x in parsed if extractor.should_embed(x)}
    points = {str(p['id']): p for p in c['points']}
    if not admitted or len(points) != len(c['points']) or set(points) != set(admitted) or len(points) > 3000:
        raise RepairError('candidate must cover exact current admitted source set')
    for pid, point in points.items():
        chunk = admitted[pid]; payload = point['payload']; vector = point['vector']
        if (payload.get('arxiv_id') != paper_id or any(payload.get(k) != v for k,v in chunk.items())
                or not _vector_equal(vector, vector) or len(vector) != 384
                or abs(math.sqrt(sum(x*x for x in vector))-1) > 1e-3
                or chunk.get('section_type') not in document_cache.SECTION_TYPES
                or not document_cache._label(chunk.get('section_title'))):
            raise RepairError('invalid complete candidate point/content/vector')
    state = labels._connect(state_db, readonly=True); fts = labels._connect(fts_db, readonly=True)
    pf = labels._connect(paper_fts_db, readonly=True)
    try:
        before_state = _paper_row(state, paper_id)
        if not before_state or before_state['status'] != 'done': raise RepairError('existing done paper required')
        if labels._exists(state,'paper_active') and state.execute('SELECT 1 FROM paper_active WHERE arxiv_id=?',(paper_id,)).fetchone():
            raise RepairError('managed paper_active document is outside replacement')
        publications = _document_rows(state,'paper_publications',paper_id)
        if publications and (sum(r[5]=='published' for r in publications)!=1
                or any(r[2]!='snapshot' or r[5] not in ('published','superseded') for r in publications)):
            raise RepairError('unfinished or unsupported publication owner')
        updates = c.get('state_updates', {})
        if not set(updates) <= {'source_url','abstract','fulltext_chars','chunk_count','parser_mode','parser_revision',
                'extractor_version','fulltext_source','parse_status','parse_error','parse_retry_after'} & set(before_state):
            raise RepairError('state updates must be existing source-derived fields')
        after_state = dict(before_state, **updates)
        if (('source_url' in updates and updates['source_url']!=binding['url'])
                or ('parser_mode' in updates and updates['parser_mode']!=manifest['parser_mode'])
                or ('parser_revision' in updates and str(updates['parser_revision'])!=manifest['parser_revision'])):
            raise RepairError('source-derived state differs from candidate binding')
        for point in points.values():
            if (any(point['payload'].get(k)!=after_state.get(k) for k in ('title','year'))
                    or point['payload'].get('layers') != ((after_state.get('layers') or '').split(',') if after_state.get('layers') else [])):
                raise RepairError('candidate fulltext/state metadata mismatch')
        live = _scroll(qdrant,'papers_fulltext',paper_id)
        reuse=c.get('embedding_reuse',{})
        if (not isinstance(reuse,dict) or not set(reuse)<=set(points)
                or any(not isinstance(pid,str) for pid in reuse.values())):
            raise RepairError('invalid same-paper embedding reuse mapping')
        if reuse and recipe.get('scope')!='newly_embedded_points':
            raise RepairError('embedding recipe must explicitly scope only newly embedded points')
        reuse_proof=[]
        for new_id,old_id in reuse.items():
            old=live.get(old_id);new=points[new_id]
            if (old is None or old['payload'].get('arxiv_id')!=paper_id
                    or old['payload'].get('text')!=new['payload']['text']
                    or old['vector']!=new['vector'] or len(old['vector'])!=384
                    or not _vector_equal(old['vector'],old['vector'])
                    or abs(math.sqrt(sum(x*x for x in old['vector']))-1)>1e-3):
                raise RepairError('reused embedding must equal same-paper exact-text baseline vector')
            reuse_proof.append(dict(point_id=new_id,origin_point_id=old_id,text_sha256=labels._sha(new['payload']['text'].encode()),
                vector_sha256=labels._sha(_jbytes(old['vector'])),reused_vector_recipe_unverified=True))
        restore._catalog(fts)
        if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone() != (1,):
            raise RepairError('complete chunk FTS ownership required')
        rows = _document_fts_rows(fts,paper_id)
        tables = _document_tables(fts,paper_id)
        if (len({r[2] for r in rows}) != len(rows)
                or {tuple(r) for r in tables['pipeline_fts_points']} != {(r[2],paper_id,r[0]) for r in rows}
                or {tuple(r) for r in tables['pipeline_fts_rows']} != {('chunks',paper_id,r[0]) for r in rows}):
            raise RepairError('complete old FTS/catalog set mismatch')
        for collection in ('papers_fulltext','papers_coarse_base'):
            for pid,p in restore._retrieve(qdrant,collection,list(points)).items():
                if collection != 'papers_fulltext' or p['payload'].get('arxiv_id') != paper_id:
                    raise RepairError('candidate UUID foreign or cross-family collision')
        for pid in points:
            owner = fts.execute('SELECT arxiv_id FROM pipeline_fts_points WHERE point_id=?',(pid,)).fetchone()
            if owner and owner != (paper_id,): raise RepairError('candidate UUID foreign FTS owner')
        coarse_before = list(_scroll(qdrant,'papers_coarse_base',paper_id).values())
        coarse_after = c.get('coarse_point')
        if coarse_after is None:
            if len(coarse_before)!=1 or after_state.get('abstract') != before_state.get('abstract'):
                raise RepairError('changed/missing coarse embedding text requires prepared coarse candidate')
            coarse_after = coarse_before[0]
        cp = coarse_after['payload']; cv = coarse_after['vector']
        coarse_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'coarse:'+paper_id))
        if (coarse_after['id'] != coarse_id or cp.get('arxiv_id') != paper_id
                or not _vector_equal(cv,cv) or len(cv)!=768
                or abs(math.sqrt(sum(x*x for x in cv))-1)>1e-3
                or any(cp.get(k)!=after_state.get(k) for k in ('title','year'))
                or cp.get('layers') != ((after_state.get('layers') or '').split(',') if after_state.get('layers') else [])):
            raise RepairError('invalid coarse candidate metadata/vector')
        for p in restore._retrieve(qdrant,'papers_coarse_base',[coarse_id]).values():
            if p['payload'].get('arxiv_id')!=paper_id: raise RepairError('foreign coarse UUID owner')
        if restore._retrieve(qdrant,'papers_fulltext',[coarse_id]): raise RepairError('cross-family coarse collision')
        metadata = {k:after_state.get(k) for k in ('title','year','layers','source_url')}
        metadata['source_binding'] = dict(binding,sha256=c['source_sha256'],route=c['source_route'])
        metadata['embedding_provenance']=dict(current_recipe=recipe,current_recipe_scope='newly_embedded_points',
            newly_embedded_point_ids=sorted(set(points)-set(reuse)),reused_points=reuse_proof,
            reused_vector_recipe_unverified=bool(reuse))
        cache_path = Path(cache_dir).resolve()/(document_cache.safe_id(paper_id)+'.json')
        artifact = dict(points=list(points.values()),metadata=metadata,cache_path=str(cache_path))
        revision = _snapshot_revision(artifact,labels._sha(cache))
        for point in artifact['points']: point['payload']['publication_revision']=revision
        artifact_path = document_cache._cache_directory(publication_cache_dir)/(hashlib.sha256(paper_id.encode()).hexdigest()+'-'+revision+'.json')
        if artifact_path.exists(): raise RepairError('candidate artifact already exists')
        old_by = {r[2]:r[0] for r in rows}; next_id = max(_document_last_rowid(fts,'chunks'),floors.get('chunks',0))
        after_rows = []
        for point in artifact['points']:
            rid = old_by.get(point['id'])
            if rid is None: next_id += 1; rid = next_id
            after_rows.append([rid,*restore._expected(point)])
        pf_before = _document_paper_fts_rows(pf,paper_id)
        pf_tables = _document_tables(pf,paper_id,True)
        if len(pf_before)>1: raise RepairError('duplicate paper FTS rows')
        pf_id = pf_before[0][0] if pf_before else max(_document_last_rowid(pf,'papers_fts'),floors.get('papers_fts',0))+1
        pf_after = [[pf_id,paper_id,after_state['title'],after_state.get('abstract') or '',after_state.get('layers'),after_state.get('year')]]
        files = []
        for path,data in ((cache_path,cache),(Path(section_cache_dir).resolve()/cache_path.name,document_cache.manifest_bytes(manifest)),
                (Path(fulltext_cache_dir).resolve()/(document_cache.safe_id(paper_id)+'.tex'),source)):
            path=document_cache._plain_path(path)
            raw=document_cache.read_bounded(path) if path.exists() else None
            files.append(dict(path=str(path),before=base64.b64encode(raw).decode() if raw is not None else None,
                after=base64.b64encode(data).decode()))
        after_tables = {'pipeline_fts_points':[[r[2],paper_id,r[0]] for r in after_rows],
            'pipeline_fts_rows':[['chunks',paper_id,r[0]] for r in after_rows]}
        retained_artifacts=[]
        for row in publications:
            path=_retained_publication_path(row[3],publication_cache_dir)
            raw=document_cache.read_bounded(path,64*1024*1024) if path.exists() else None
            retained_artifacts.append(dict(path=str(path),sha256=labels._sha(raw) if raw is not None else None))
        paper = dict(arxiv_id=paper_id,revision=revision,artifact=artifact,artifact_path=str(artifact_path),cache_sha256=labels._sha(cache),
            points_before=list(live.values()),coarse_before=coarse_before,coarse_after=[coarse_after],
            state_before=before_state,state_after=after_state,publications_before=publications,
            fts_before=rows,fts_after=after_rows,catalog_before=tables,catalog_after=after_tables,
            paper_fts_before=pf_before,paper_fts_after=pf_after,paper_catalog_before=pf_tables,
            paper_catalog_after={'pipeline_fts_rows':[['papers_fts',paper_id,pf_id]],'pipeline_fts_papers':[['papers_fts',paper_id]]},
            files=files,source_path=str(Path(c['source_path']).resolve()),source_sha256=c['source_sha256'],embedding_recipe=recipe,
            admission_policy=_document_policy_binding(),retained_artifacts=retained_artifacts)
        paper['embedding_reuse']=reuse
        plan=dict(schema=1,kind='published-label-replacement',operation='whole-document',phase='prepared',paper=paper,
            state_db=str(Path(state_db).resolve()),fts_db=str(Path(fts_db).resolve()),paper_fts_db=str(Path(paper_fts_db).resolve()),collection='papers_fulltext')
        plan['binding_sha256'] = _document_binding(plan)
        _verify_document(plan,qdrant,'before')
        labels._atomic(journal_path,_jbytes(plan))
        return plan
    finally: state.close();fts.close();pf.close()


def _retained_publication_path(stored_path, publication_cache_dir):
    # Historical rows may name a directory alias, but only the configured
    # physical root may supply the retained file. Never resolve the leaf.
    path = Path(stored_path).absolute()
    root = document_cache._cache_directory(publication_cache_dir)
    if ".." in path.parts or path.parent.resolve(strict=True) != root:
        raise RepairError('retained publication artifact outside configured root')
    return document_cache._plain_path(root / path.name)


def _document_binding(plan):
    return labels._sha(_jbytes({k:v for k,v in plan.items() if k not in ('phase','binding_sha256')}))


def _document_publications(p, published=False):
    rows=json.loads(json.dumps(p['publications_before']))
    if published:
        for row in rows:
            if row[5]=='published': row[5]='superseded'
    rows.append([p['arxiv_id'],p['revision'],'snapshot',p['artifact_path'],p['cache_sha256'],
        'published' if published else 'replacement_staged',
        _jbytes(['preflight','qdrant','fts','complete']).decode() if published else '[]',0,None])
    return rows


def _document_equal_points(actual, expected):
    return (set(actual)==set(expected) and all(actual[k]['payload']==expected[k]['payload']
        and _vector_equal(actual[k]['vector'],expected[k]['vector']) for k in actual))


def _verify_document(plan, client, mode):
    from ops import restore_cached_points as restore
    p=plan['paper']; aid=p['arxiv_id']
    if _document_binding(plan)!=plan['binding_sha256']: raise RepairError('whole-document journal binding drift')
    if _document_policy_binding()!=p['admission_policy']: raise RepairError('prepared admission policy changed')
    restore._read(p['source_path'],p['source_sha256'])
    if _snapshot_revision(p['artifact'],p['cache_sha256'])!=p['revision']:
        raise RepairError('whole-document candidate artifact binding drift')
    old_points={x['id']:x for x in p['points_before']};candidate_points={x['id']:x for x in p['artifact']['points']}
    for new_id,old_id in p.get('embedding_reuse',{}).items():
        old=old_points[old_id];new=candidate_points[new_id]
        if old['payload'].get('text')!=new['payload'].get('text') or old['vector']!=new['vector']:
            raise RepairError('whole-document baseline embedding proof drift')
    def allowed(actual,before,after):
        choices=[before,after] if mode=='partial' else [before if mode=='before' else after]
        if actual not in choices: raise RepairError('whole-document surface drift')
    for item in p['files']:
        path=Path(item['path']); raw=document_cache.read_bounded(path) if path.exists() else None
        allowed(raw,_decode(item['before']) if item['before'] is not None else None,_decode(item['after']))
    for item in p['retained_artifacts']:
        path=document_cache._plain_path(item['path'])
        raw=document_cache.read_bounded(path,64*1024*1024) if path.exists() else None
        if (labels._sha(raw) if raw is not None else None)!=item['sha256']:
            raise RepairError('retained parent artifact drift')
    path=Path(p['artifact_path'])
    if path.exists() and path.read_bytes()!=_jbytes(p['artifact']): raise RepairError('candidate artifact collision')
    for collection,before_list,after_list in (
            ('papers_fulltext',p['points_before'],p['artifact']['points']),
            ('papers_coarse_base',p['coarse_before'],p['coarse_after'])):
        before={x['id']:x for x in before_list}; after={x['id']:x for x in after_list}
        live=_scroll(client,collection,aid)
        global_points=restore._retrieve(client,collection,list(set(before)|set(after)))
        if set(global_points)!=set(live): raise RepairError('replacement UUID acquired a foreign owner')
        if mode=='partial':
            if not set(live)<=set(before)|set(after): raise RepairError('unexpected partial replacement UUID')
            for pid,x in live.items():
                if not any(_document_equal_points({pid:x},{pid:choices[pid]}) for choices in (before,after) if pid in choices):
                    raise RepairError('partial replacement point drift')
        elif not _document_equal_points(live,before if mode=='before' else after):
            raise RepairError('whole-document Qdrant set/content drift')
        other='papers_coarse_base' if collection=='papers_fulltext' else 'papers_fulltext'
        if restore._retrieve(client,other,list(set(before)|set(after))): raise RepairError('cross-family replacement UUID collision')
    state=labels._connect(plan['state_db'],readonly=True); fts=labels._connect(plan['fts_db'],readonly=True)
    pf=labels._connect(plan['paper_fts_db'],readonly=True)
    try:
        if labels._exists(state,'paper_active') and state.execute('SELECT 1 FROM paper_active WHERE arxiv_id=?',(aid,)).fetchone():
            raise RepairError('managed document acquired ownership')
        allowed(_paper_row(state,aid),p['state_before'],p['state_after'])
        pubs=_document_rows(state,'paper_publications',aid)
        choices=[p['publications_before']] if mode in ('before','partial') else []
        if mode!='before': choices += [_document_publications(p),_document_publications(p,True)]
        if sorted(pubs) not in [sorted(x) for x in choices]: raise RepairError('publication journal drift')
        if mode=='published' and sorted(pubs)!=sorted(_document_publications(p,True)):
            raise RepairError('replacement publication not switched')
        allowed(sorted(_document_fts_rows(fts,aid)),sorted(p['fts_before']),sorted(p['fts_after']))
        for table,old in p['catalog_before'].items():
            allowed(sorted(_document_rows(fts,table,aid,'chunks')),sorted(old),sorted(p['catalog_after'][table]))
        if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()!=(1,):
            raise RepairError('FTS catalog completeness drift')
        for row in p['fts_after']:
            actual=fts.execute('SELECT arxiv_id FROM chunks WHERE rowid=?',(row[0],)).fetchone()
            owner=fts.execute('SELECT arxiv_id FROM pipeline_fts_points WHERE point_id=?',(row[2],)).fetchone()
            if (actual and actual!=(aid,)) or (owner and owner!=(aid,)):
                raise RepairError('candidate FTS row/UUID foreign collision')
        allowed(sorted(_document_paper_fts_rows(pf,aid)),sorted(p['paper_fts_before']),sorted(p['paper_fts_after']))
        for table,old in p['paper_catalog_before'].items():
            allowed(sorted(_document_rows(pf,table,aid,'papers_fts')),sorted(old),sorted(p['paper_catalog_after'][table]))
        for row in p['paper_fts_after']:
            actual=pf.execute('SELECT arxiv_id FROM papers_fts WHERE rowid=?',(row[0],)).fetchone()
            if actual and actual!=(aid,): raise RepairError('candidate paper FTS row foreign collision')
    finally: state.close();fts.close();pf.close()


def _write_document(plan, client, forward):
    p=plan['paper'];aid=p['arxiv_id'];side='after' if forward else 'before'
    # Upload the entire valid candidate before retiring old-only IDs. Reused IDs
    # are safe solely under the explicit gate and complete before-image journal.
    for collection,before,after in (('papers_fulltext',p['points_before'],p['artifact']['points']),
            ('papers_coarse_base',p['coarse_before'],p['coarse_after'])):
        points=after if forward else before
        for start in range(0,len(points),200): client.upsert(collection_name=collection,points=points[start:start+200],wait=True)
        remove=list({x['id'] for x in (before if forward else after)}-{x['id'] for x in points})
        for start in range(0,len(remove),200): client.delete(collection_name=collection,points_selector=remove[start:start+200],wait=True)
    for db,table,rows,tables in ((plan['fts_db'],'chunks',p['fts_'+side],p['catalog_'+side]),
            (plan['paper_fts_db'],'papers_fts',p['paper_fts_'+side],p['paper_catalog_'+side])):
        conn=labels._connect(db)
        try:
            conn.execute('BEGIN IMMEDIATE')
            owned=_document_fts_rows(conn,aid) if table=='chunks' else _document_paper_fts_rows(conn,aid)
            conn.executemany('DELETE FROM '+table+' WHERE rowid=?',[(row[0],) for row in owned])
            columns='rowid,text,point_id,arxiv_id,section_type,element_type,layers,year' if table=='chunks' else 'rowid,arxiv_id,title,abstract,layers,year'
            conn.executemany('INSERT INTO '+table+'('+columns+') VALUES('+','.join('?' for _ in columns.split(','))+')',rows)
            for name,values in tables.items():
                if name in ('pipeline_fts_rows','pipeline_fts_papers'):
                    conn.execute('DELETE FROM '+name+' WHERE table_name=? AND arxiv_id=?',(table,aid))
                else: conn.execute('DELETE FROM '+name+' WHERE arxiv_id=?',(aid,))
                if values: conn.executemany('INSERT INTO '+name+' VALUES('+','.join('?' for _ in values[0])+')',values)
            conn.commit()
        finally: conn.close()
    conn=labels._connect(plan['state_db'])
    try:
        conn.execute('BEGIN IMMEDIATE')
        row=p['state_'+side];cols=[k for k in row if k!='arxiv_id']
        if not all(k.isidentifier() for k in cols): raise RepairError('invalid state projection columns')
        conn.execute('UPDATE papers SET '+','.join('"'+k+'"=?' for k in cols)+' WHERE arxiv_id=?',[row[k] for k in cols]+[aid])
        conn.commit()
    finally: conn.close()
    for item in p['files']:
        raw=item[side];path=Path(item['path'])
        if raw is None: path.unlink(missing_ok=True)
        else: labels._atomic(path,_decode(raw))


def _write_document_publications(plan, rows):
    conn=labels._connect(plan['state_db'])
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM paper_publications WHERE arxiv_id=?',(plan['paper']['arxiv_id'],))
        if rows: conn.executemany('INSERT INTO paper_publications(arxiv_id,revision,kind,artifact_path,cache_sha256,state,completed,retry_after,error_code) VALUES(?,?,?,?,?,?,?,?,?)',rows)
        conn.commit()
    finally: conn.close()


def _apply_document(path,plan,client,maintenance):
    if maintenance is not True: raise RepairError('whole-document replacement requires reader and writer maintenance')
    if plan['phase']!='prepared': raise RepairError('whole-document replacement must be prepared')
    _verify_document(plan,client,'before')
    _checkpoint(path,plan,'applying')
    try:
        labels._atomic(plan['paper']['artifact_path'],_jbytes(plan['paper']['artifact']))
        _write_document_publications(plan,_document_publications(plan['paper']))
        _write_document(plan,client,True)
        _verify_document(plan,client,'after')
        _write_document_publications(plan,_document_publications(plan['paper'],True))
        _verify_document(plan,client,'published')
        _checkpoint(path,plan,'applied')
    except Exception:
        _rollback_document(path,plan,client,maintenance)
        raise
    return plan


def _rollback_document(path,plan,client,maintenance):
    if maintenance is not True: raise RepairError('whole-document rollback requires reader and writer maintenance')
    if plan['phase'] not in ('applying','applied','rolling_back'): raise RepairError('no whole-document replacement to roll back')
    _verify_document(plan,client,'partial')
    _checkpoint(path,plan,'rolling_back')
    _write_document(plan,client,False)
    _write_document_publications(plan,plan['paper']['publications_before'])
    _verify_document(plan,client,'before')
    _checkpoint(path,plan,'rolled_back')
    return plan


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
                 cached_additions=None, embedder=None, embedding_recipe=None, source_parser=None,
                 withdrawn_points=None):
    """Prepare one paper's label changes and/or source-proven cached additions."""
    paper_ids = {c['arxiv_id'] for c in changes}
    if withdrawn_points is not None:
        if changes or cached_additions is not None or cache_labels_by_paper is not None:
            raise RepairError('withdrawal cannot also change labels or add points')
        paper_ids.add(withdrawn_points['arxiv_id'])
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
        if cached_additions is not None or withdrawn_points is not None:
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
        if cached_additions is not None or withdrawn_points is not None:
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
        removed=[]
        if withdrawn_points is not None:
            from ops import restore_cached_points as restore
            if (str(cache_path)!=withdrawn_points['cache_path']
                    or cache_sha!=withdrawn_points['expected_cache_sha256']):
                raise RepairError('withdrawal cache differs from published parent')
            removed=restore._prepare_withdrawals(withdrawn_points,live,fts,fts_db,qdrant,source_parser)
            removed_ids={r['point']['id'] for r in removed}
            candidate['points']=[p for p in candidate['points'] if p['id'] not in removed_ids]
            if not candidate['points']:raise RepairError('withdrawal must retain a nonempty published baseline')
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
        if withdrawn_points is not None:paper.update(removed=removed,withdrawal_proof=withdrawn_points)
        _state_check(state, paper)
        plan = dict(schema=1, kind='published-label-replacement', phase='prepared',
                    state_db=str(Path(state_db).resolve()), fts_db=str(Path(fts_db).resolve()),
                    collection=collection, paper=paper)
        if withdrawn_points is not None:plan['operation']='receipt-withdrawal'
        labels._atomic(journal_path, _jbytes(plan))
        return plan
    finally:
        state.close(); fts.close()


def _load(path):
    raw = Path(path).read_bytes()
    if raw.startswith(b'\x1f\x8b'):
        raw = gzip.decompress(raw)
    plan = json.loads(raw)
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
    if plan.get('operation')=='receipt-withdrawal':
        return _verify_snapshot_withdrawal(plan,qdrant,mode)
    if plan.get('operation') == 'orphan-adoption':
        return _verify_adoption(plan, qdrant, mode)
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
    with Path(path).open('rb') as journal:
        compressed = journal.read(2) == b'\x1f\x8b'
    raw = _jbytes(plan)
    labels._atomic(path, gzip.compress(raw, compresslevel=1, mtime=0) if compressed else raw)


def _switch(plan, forward):
    paper = plan['paper']
    state = labels._connect(plan['state_db'])
    try:
        state.execute('BEGIN IMMEDIATE')
        if plan.get('operation') == 'orphan-adoption':
            if forward:
                changed=state.execute("UPDATE paper_publications SET state='published',completed=? WHERE arxiv_id=? AND revision=? AND kind='snapshot' AND artifact_path=? AND cache_sha256=? AND state='replacement_staged'",
                    (_jbytes(['preflight','qdrant','fts','complete']).decode(),paper['arxiv_id'],paper['revision'],paper['artifact_path'],paper['cache_sha256'])).rowcount
                if changed!=1:raise RepairError('adoption candidate journal missing or changed')
            else:
                changed=state.execute("DELETE FROM paper_publications WHERE arxiv_id=? AND revision=? AND kind='snapshot' AND artifact_path=? AND cache_sha256=?",
                    (paper['arxiv_id'],paper['revision'],paper['artifact_path'],paper['cache_sha256'])).rowcount
                if changed!=1:raise RepairError('adoption candidate journal missing or changed')
            state.commit()
            return
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


def apply_plan(journal_path, *, qdrant, maintenance=False):
    plan = _load(journal_path); paper = plan['paper']
    if plan.get('operation') == 'whole-document':
        return _apply_document(journal_path, plan, qdrant, maintenance)
    if plan.get('operation') in ('orphan-adoption','receipt-withdrawal') and maintenance is not True:
        raise RepairError('snapshot transition requires reader and writer maintenance')
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
        state.execute('BEGIN IMMEDIATE')
        if plan.get('operation') != 'orphan-adoption':_state_check(state,paper)
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
        if plan.get('operation') == 'orphan-adoption':_verify_adoption(plan,qdrant,'published')
        _checkpoint(journal_path,plan,'applied')
    except Exception:
        rollback_plan(journal_path,qdrant=qdrant,maintenance=maintenance)
        raise
    return plan


def _write_surfaces(plan,client,forward):
    if plan.get('operation')=='receipt-withdrawal':
        from ops import restore_cached_points as restore
        paper=plan['paper']
        restore._write_removed_points(client,plan['collection'],paper['removed'],forward)
        retained=paper['artifact']['points']
        revision=paper['revision'] if forward else paper['parent_revision']
        for start in range(0,len(retained),200):
            client.set_payload(collection_name=plan['collection'],points=[p['id'] for p in retained[start:start+200]],
                payload={'publication_revision':revision},wait=True)
        restore._write_removed_fts_transaction(plan['fts_db'],paper['removed'],forward)
        return
    if plan.get('operation') == 'orphan-adoption':
        return _write_adoption_surfaces(plan,client,forward)
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


def rollback_plan(journal_path, *, qdrant, maintenance=False):
    plan = _load(journal_path)
    if plan.get('operation') == 'whole-document':
        return _rollback_document(journal_path, plan, qdrant, maintenance)
    if plan.get('operation') in ('orphan-adoption','receipt-withdrawal') and maintenance is not True:
        raise RepairError('snapshot transition requires reader and writer maintenance')
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


def _paper_row(conn, aid):
    columns = [r[1] for r in conn.execute('PRAGMA table_info(papers)')]
    row = conn.execute('SELECT * FROM papers WHERE arxiv_id=?', (aid,)).fetchone()
    return dict(zip(columns,row)) if row is not None else None


def _paper_fts_rows(conn, aid):
    return [list(r) for r in conn.execute(
        'SELECT rowid,arxiv_id,title,abstract,layers,year FROM papers_fts WHERE arxiv_id=?', (aid,))]


def prepare_orphan_adoption(paper_id, *, state_db, fts_db, paper_fts_db,
        cache_dir, section_cache_dir, fulltext_cache_dir, data_dir, journal_path, qdrant,
        source_path, expected_source_sha256, source_route, source_binding,
        state_after, coarse_point, parser_revision, source_parser=None, publication_cache_dir=None):
    """Prepare exact orphan replay; apply/rollback require readers and writers closed.

    No vectors are generated here. The caller prepares the source-bound coarse
    point with the deployed coarse recipe before entering maintenance. Existing
    fulltext vectors and UUIDs are preserved. Normal ingestion admission is untouched.
    """
    from ops import restore_cached_points as restore
    from pipeline import publication
    if Path(journal_path).exists():raise RepairError('journal already exists')
    if source_route != 'spec_markdown':raise RepairError('orphan adoption requires explicit spec source')
    if not isinstance(source_binding,dict) or not all(source_binding.get(k) for k in ('url','commit','retrieved_at')):
        raise RepairError('commit-pinned source provenance required')
    if (not isinstance(source_binding['commit'],str) or not re.fullmatch('[a-fA-F0-9]{40}',source_binding['commit'])
            or not isinstance(source_binding['url'],str) or not source_binding['url'].startswith('https://')
            or '/'+source_binding['commit']+'/' not in source_binding['url']):
        raise RepairError('source URL must pin the recorded Git commit')
    try:retrieved=datetime.fromisoformat(source_binding['retrieved_at'].replace('Z','+00:00'))
    except (ValueError,TypeError,AttributeError) as exc:raise RepairError('invalid retrieval timestamp') from exc
    if retrieved.tzinfo is None and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',source_binding['retrieved_at']):
        raise RepairError('retrieval timestamp requires timezone; date-only receipts retain day precision')
    source=restore._read(source_path,expected_source_sha256)
    sections=[]
    if source_parser is None:
        chunks=restore.parse_source(source,source_route,section_sink=sections)
    else:
        chunks,sections=source_parser(source,source_route)
    if not chunks or not sections:raise RepairError('source replay requires chunks and canonical sections')
    cache=_jbytes(chunks)
    manifest=document_cache.manifest_bytes(document_cache.make_section_manifest(
        paper_id,sections,cache,source,'markdown',parser_revision))
    cache_path,manifest_path=document_cache.document_paths(data_dir,paper_id,
        cache_dir=cache_dir,section_cache_dir=section_cache_dir)
    source_cache_root=document_cache._cache_directory(fulltext_cache_dir)
    canonical_source_path=document_cache._plain_path(source_cache_root/(document_cache.safe_id(paper_id)+'.tex'))
    source_before=document_cache.read_bounded(canonical_source_path) if canonical_source_path.exists() else None
    if source_before is not None and source_before!=source:raise RepairError('orphan canonical source already differs')
    if cache_path.exists() or manifest_path.exists():raise RepairError('orphan cache already exists')
    state=labels._connect(state_db,readonly=True);fts=labels._connect(fts_db,readonly=True)
    paper_fts=labels._connect(paper_fts_db,readonly=True)
    try:
        if _paper_row(state,paper_id) is not None:raise RepairError('orphan state already exists')
        if state.execute('SELECT 1 FROM paper_publications WHERE arxiv_id=?',(paper_id,)).fetchone():
            raise RepairError('orphan publication already exists')
        if labels._exists(state,'paper_active') and state.execute('SELECT 1 FROM paper_active WHERE arxiv_id=?',(paper_id,)).fetchone():
            raise RepairError('managed paper cannot be adopted')
        columns={r[1] for r in state.execute('PRAGMA table_info(papers)')}
        if (not isinstance(state_after,dict) or not set(state_after)<=columns
                or state_after.get('arxiv_id')!=paper_id or state_after.get('status')!='done'
                or state_after.get('source_url')!=source_binding['url']
                or not state_after.get('title')):raise RepairError('invalid orphan state projection')
        # Bind omitted defaults too, so exact after-readback covers the complete row.
        state_after=dict(state_after)
        for col in state.execute('PRAGMA table_info(papers)'):
            if col[1] not in state_after:
                if col[4] is not None:raise RepairError('state projection must include defaulted columns')
                state_after[col[1]]=None
        live=_scroll(qdrant,'papers_fulltext',paper_id)
        # Adoption creates no fulltext points. Replay every existing ID against
        # all source chunks, independently of today's new-ingestion filters.
        expected={str(uuid.uuid5(uuid.NAMESPACE_URL,f'{paper_id}#{c["chunk_index"]}')):c for c in chunks}
        if not live or not set(live)<=set(expected):raise RepairError('source replay point set mismatch')
        if len(expected)!=len(chunks):raise RepairError('duplicate source indices')
        for pid,p in live.items():
            c=expected[pid];payload=p['payload']
            if ('publication_revision' in payload or any(payload.get(k)!=c.get(k) for k in
                    ('text','chunk_index','element_type'))
                    or not _vector_equal(p['vector'],p['vector']) or len(p['vector'])!=384
                    or abs(math.sqrt(sum(x*x for x in p['vector']))-1)>1e-3):
                raise RepairError('orphan source replay differs from existing point')
            if (not document_cache._label(c.get('section_title'))
                    or c.get('section_type') not in document_cache.SECTION_TYPES):
                raise RepairError('orphan source replay labels invalid')
        restore._catalog(fts)
        if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()!=(1,):
            raise RepairError('complete FTS ownership required')
        rows=[list(r) for r in _fts_rows(fts,paper_id)]
        catalog=[list(r) for r in fts.execute('SELECT point_id,arxiv_id,row_id FROM pipeline_fts_points WHERE arxiv_id=? ORDER BY point_id',(paper_id,))]
        owned=[list(r) for r in fts.execute("SELECT table_name,arxiv_id,row_id FROM pipeline_fts_rows WHERE table_name='chunks' AND arxiv_id=? ORDER BY row_id",(paper_id,))]
        if len(rows)!=len(live) or {r[2] for r in rows}!=set(live):raise RepairError('orphan FTS point set mismatch')
        for row in rows:
            if tuple(row[1:])!=restore._expected(live[row[2]]) or [row[2],paper_id,row[0]] not in catalog:
                raise RepairError('orphan FTS/catalog mismatch')
        if {r[2] for r in owned}!={r[0] for r in rows} or len(catalog)!=len(rows):raise RepairError('orphan row ownership mismatch')
        if _paper_fts_rows(paper_fts,paper_id) or paper_fts.execute(
                "SELECT 1 FROM pipeline_fts_papers WHERE table_name='papers_fts' AND arxiv_id=?",(paper_id,)).fetchone() or paper_fts.execute(
                "SELECT 1 FROM pipeline_fts_rows WHERE table_name='papers_fts' AND arxiv_id=?",(paper_id,)).fetchone():
            raise RepairError('orphan paper FTS already exists')
        coarse=json.loads(json.dumps(coarse_point,allow_nan=False))
        coarse_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'coarse:'+paper_id))
        if (str(coarse.get('id'))!=coarse_id or coarse.get('payload',{}).get('arxiv_id')!=paper_id
                or not _vector_equal(coarse.get('vector'),coarse.get('vector'))
                or len(coarse['vector'])!=768
                or abs(math.sqrt(sum(x*x for x in coarse['vector']))-1)>1e-3):raise RepairError('invalid normalized 768D coarse candidate')
        coarse_layers=state_after['layers'].split(',') if state_after.get('layers') else []
        if (any(coarse['payload'].get(k)!=state_after.get(k) for k in ('title','year'))
                or coarse['payload'].get('layers')!=coarse_layers):
            raise RepairError('coarse/state metadata mismatch')
        old_coarse=_scroll(qdrant,'papers_coarse_base',paper_id)
        if old_coarse and old_coarse!={coarse_id:coarse}:raise RepairError('existing coarse differs from candidate')
        if restore._retrieve(qdrant,'papers_coarse_base',list(live)) or restore._retrieve(qdrant,'papers_fulltext',[coarse_id]):
            raise RepairError('cross-family UUID collision')
        global_coarse=restore._retrieve(qdrant,'papers_coarse_base',[coarse_id])
        if global_coarse!=old_coarse:raise RepairError('coarse UUID belongs to another paper')
        metadata={k:state_after.get(k) for k in ('title','year','layers','source_url')}
        metadata['source_binding']=dict(source_binding,sha256=expected_source_sha256,route=source_route,parser_revision=str(parser_revision))
        candidate_points=json.loads(json.dumps(list(live.values())))
        for p in candidate_points:
            c=expected[p['id']]
            p['payload'].update(section_title=c['section_title'],section_type=c['section_type'])
        fts_after=[list(r) for r in rows]
        for row in fts_after:row[4]=expected[row[2]]['section_type']
        artifact=dict(points=candidate_points,metadata=metadata,cache_path=str(cache_path))
        revision=_snapshot_revision(artifact,labels._sha(cache))
        artifact=json.loads(json.dumps(artifact))
        for p in artifact['points']:p['payload']['publication_revision']=revision
        publication_root=(publication._safe_dir(data_dir) if publication_cache_dir is None
            else document_cache._cache_directory(publication_cache_dir))
        artifact_path=publication_root/(hashlib.sha256(paper_id.encode()).hexdigest()+'-'+revision+'.json')
        paper=dict(arxiv_id=paper_id,revision=revision,artifact=artifact,artifact_path=str(artifact_path),
            cache_sha256=labels._sha(cache),points_before=list(live.values()),fts_before=rows,fts_after=fts_after,
            catalog_before=catalog,owned_before=owned,cache_path=str(cache_path),manifest_path=str(manifest_path),
            cache_after=base64.b64encode(cache).decode(),manifest_after=base64.b64encode(manifest).decode(),
            canonical_source_path=str(canonical_source_path),
            canonical_source_before=base64.b64encode(source_before).decode() if source_before is not None else None,
            canonical_source_after=base64.b64encode(source).decode(),
            state_after=state_after,coarse_before=list(old_coarse.values()),coarse_after=coarse,
            paper_fts_after=[paper_id,state_after['title'],state_after.get('abstract') or '',state_after.get('layers'),state_after.get('year')],
            source_path=str(Path(source_path).resolve()),source_sha256=expected_source_sha256)
        plan=dict(schema=1,kind='published-label-replacement',operation='orphan-adoption',phase='prepared',
            state_db=str(Path(state_db).resolve()),fts_db=str(Path(fts_db).resolve()),paper_fts_db=str(Path(paper_fts_db).resolve()),
            collection='papers_fulltext',paper=paper)
        _verify_adoption(plan,qdrant,'before')
        labels._atomic(journal_path,_jbytes(plan))
        return plan
    finally:state.close();fts.close();paper_fts.close()


def _verify_adoption(plan,client,mode):
    from ops import restore_cached_points as restore
    published=mode=='published'
    if published:mode='after'
    p=plan['paper'];aid=p['arxiv_id']
    restore._read(p['source_path'],p['source_sha256'])
    if (_snapshot_revision(p['artifact'],p['cache_sha256'])!=p['revision']
            or labels._sha(_decode(p['cache_after']))!=p['cache_sha256']
            or p['artifact']['metadata']['source_binding']['sha256']!=p['source_sha256']):
        raise RepairError('orphan artifact binding drift')
    artifact_path=Path(p['artifact_path'])
    if artifact_path.exists() and artifact_path.read_bytes()!=_jbytes(p['artifact']):raise RepairError('orphan immutable artifact drift')
    def allowed(actual,before,after):
        if actual not in ([before,after] if mode=='partial' else [after if mode=='after' else before]):
            raise RepairError('orphan adoption surface drift')
    for key in ('cache','manifest'):
        path=Path(p[key+'_path'])
        allowed(path.read_bytes() if path.exists() else None,None,_decode(p[key+'_after']))
    canonical=Path(p['canonical_source_path'])
    before_source=p['canonical_source_before']
    source_after=_decode(p['canonical_source_after'])
    if labels._sha(source_after)!=p['source_sha256']:raise RepairError('canonical source hash drift')
    allowed(document_cache.read_bounded(canonical) if canonical.exists() else None,
        _decode(before_source) if before_source is not None else None,source_after)
    before={x['id']:x for x in p['points_before']};after={x['id']:x for x in p['artifact']['points']}
    live=_scroll(client,plan['collection'],aid)
    if set(live)!=set(before):raise RepairError('orphan point set drift')
    for pid,point in live.items():allowed(point,before[pid],after[pid])
    coarse=list(_scroll(client,'papers_coarse_base',aid).values())
    if not p['coarse_before'] and coarse and mode in ('partial','after'):
        expected=p['coarse_after']
        if (len(coarse)!=1 or coarse[0]['id']!=expected['id'] or coarse[0]['payload']!=expected['payload']
                or not _vector_equal(coarse[0]['vector'],expected['vector'])):raise RepairError('orphan coarse drift')
    else:allowed(coarse,p['coarse_before'],[p['coarse_after']])
    state=labels._connect(plan['state_db'],readonly=True);fts=labels._connect(plan['fts_db'],readonly=True)
    pf=labels._connect(plan['paper_fts_db'],readonly=True)
    try:
        allowed(_paper_row(state,aid),None,p['state_after'])
        publications=state.execute('SELECT revision,kind,artifact_path,cache_sha256,state FROM paper_publications WHERE arxiv_id=?',(aid,)).fetchall()
        if mode=='before' and publications:raise RepairError('orphan publication must be absent before adoption')
        for row in publications:
            if tuple(row[:4])!=(p['revision'],'snapshot',p['artifact_path'],p['cache_sha256']) or row[4] not in ('replacement_staged','published'):
                raise RepairError('orphan publication drift')
        if mode=='after' and not publications:raise RepairError('orphan publication missing')
        if published and (len(publications)!=1 or publications[0][4]!='published'):raise RepairError('orphan publication not published')
        fts_actual={r[0]:list(r) for r in _fts_rows(fts,aid)}
        if set(fts_actual)!={r[0] for r in p['fts_before']}:raise RepairError('orphan FTS set drift')
        for before_row,after_row in zip(p['fts_before'],p['fts_after']):
            allowed(fts_actual[before_row[0]],before_row,after_row)
        catalog=[list(r) for r in fts.execute('SELECT point_id,arxiv_id,row_id FROM pipeline_fts_points WHERE arxiv_id=? ORDER BY point_id',(aid,))]
        owned=[list(r) for r in fts.execute("SELECT table_name,arxiv_id,row_id FROM pipeline_fts_rows WHERE table_name='chunks' AND arxiv_id=? ORDER BY row_id",(aid,))]
        if catalog!=p['catalog_before'] or owned!=p['owned_before'] or fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()!=(1,):
            raise RepairError('orphan ownership drift')
        rows=_paper_fts_rows(pf,aid)
        if len(rows)>1:raise RepairError('duplicate paper FTS')
        allowed([r[1:] for r in rows],[],[p['paper_fts_after']])
        recorded=[r[0] for r in pf.execute("SELECT row_id FROM pipeline_fts_rows WHERE table_name='papers_fts' AND arxiv_id=?",(aid,))]
        marked=pf.execute("SELECT 1 FROM pipeline_fts_papers WHERE table_name='papers_fts' AND arxiv_id=?",(aid,)).fetchone()
        if recorded!=[r[0] for r in rows] or bool(marked)!=bool(rows):raise RepairError('paper FTS ownership drift')
    finally:state.close();fts.close();pf.close()


def _write_adoption_surfaces(plan,client,forward):
    p=plan['paper'];aid=p['arxiv_id']
    # overwrite_payload restores absent markers exactly without rewriting vectors.
    for point in (p['artifact']['points'] if forward else p['points_before']):
        client.overwrite_payload(collection_name=plan['collection'],points=[point['id']],payload=point['payload'],wait=True)
    if not p['coarse_before']:
        if forward:client.upsert(collection_name='papers_coarse_base',points=[p['coarse_after']],wait=True)
        else:client.delete(collection_name='papers_coarse_base',points_selector=[p['coarse_after']['id']],wait=True)
    fts=labels._connect(plan['fts_db'])
    try:
        fts.execute('BEGIN IMMEDIATE')
        for row in p['fts_after'] if forward else p['fts_before']:
            changed=fts.execute('UPDATE chunks SET section_type=? WHERE rowid=? AND point_id=? AND arxiv_id=?',
                (row[4],row[0],row[2],aid)).rowcount
            if changed!=1:raise RepairError('orphan exact FTS label update failed')
        fts.commit()
    finally:fts.close()
    pf=labels._connect(plan['paper_fts_db'])
    try:
        pf.execute('BEGIN IMMEDIATE')
        for row in _paper_fts_rows(pf,aid):pf.execute('DELETE FROM papers_fts WHERE rowid=?',(row[0],))
        pf.execute("DELETE FROM pipeline_fts_rows WHERE table_name='papers_fts' AND arxiv_id=?",(aid,))
        pf.execute("DELETE FROM pipeline_fts_papers WHERE table_name='papers_fts' AND arxiv_id=?",(aid,))
        if forward:
            rid=pf.execute('INSERT INTO papers_fts(arxiv_id,title,abstract,layers,year) VALUES(?,?,?,?,?)',p['paper_fts_after']).lastrowid
            pf.execute("INSERT INTO pipeline_fts_rows VALUES('papers_fts',?,?)",(aid,rid))
            pf.execute("INSERT INTO pipeline_fts_papers VALUES('papers_fts',?)",(aid,))
        pf.commit()
    finally:pf.close()
    state=labels._connect(plan['state_db'])
    try:
        state.execute('BEGIN IMMEDIATE')
        state.execute('DELETE FROM papers WHERE arxiv_id=?',(aid,))
        if forward:
            cols=list(p['state_after'])
            if not all(k.isidentifier() for k in cols):raise RepairError('invalid state columns')
            state.execute('INSERT INTO papers('+','.join('"'+k+'"' for k in cols)+') VALUES('+','.join('?' for _ in cols)+')',[p['state_after'][k] for k in cols])
        state.commit()
    finally:state.close()
    for key in ('cache','manifest'):
        path=Path(p[key+'_path'])
        if forward:labels._atomic(path,_decode(p[key+'_after']))
        else:path.unlink(missing_ok=True)
    canonical=Path(p['canonical_source_path'])
    content=p['canonical_source_after'] if forward else p['canonical_source_before']
    if content is None:canonical.unlink(missing_ok=True)
    else:labels._atomic(canonical,_decode(content))


def prepare_withdrawal_plan(paper_id,point_ids,*,source_path,cache_path,expected_source_sha256,
        expected_cache_sha256,state_db,fts_db,rowmap_db,journal_path,qdrant,expected_policy_sha256,
        origin_journals,source_route,source_parser=None,section_cache_dir):
    """Selectively withdraw receipt-owned noise from the current snapshot only."""
    from ops import restore_cached_points as restore
    request=restore._withdrawal_request(paper_id,point_ids,source_path,cache_path,expected_source_sha256,
        expected_cache_sha256,rowmap_db,expected_policy_sha256,origin_journals,source_route)
    return prepare_plan([],state_db=state_db,fts_db=fts_db,cache_dir=Path(cache_path).resolve().parent,
        section_cache_dir=section_cache_dir,journal_path=journal_path,qdrant=qdrant,
        withdrawn_points=request,source_parser=source_parser)


def _verify_snapshot_withdrawal(plan,client,mode):
    from ops import restore_cached_points as restore
    p=plan['paper'];state=labels._connect(plan['state_db'],readonly=True);fts=labels._connect(plan['fts_db'],readonly=True)
    try:
        _state_check(state,p)
        if labels._sha(Path(p['parent_artifact_path']).read_bytes())!=p['parent_artifact_sha256']:
            raise RepairError('withdrawal parent artifact drift')
        if _snapshot_revision(p['artifact'],p['cache_sha256'])!=p['revision']:
            raise RepairError('withdrawal candidate artifact binding drift')
        for key in ('cache','manifest'):
            if p.get(key+'_path') and Path(p[key+'_path']).read_bytes()!=_decode(p[key+'_before']):
                raise RepairError('withdrawal immutable cache/manifest drift')
        before={x['id']:x for x in p['points_before']};after={x['id']:x for x in p['artifact']['points']}
        restore._verify_withdrawal_data(p['withdrawal_proof'],p['removed'],before,after,p['fts_before'],
            fts,plan['fts_db'],client,mode)
    finally:state.close();fts.close()
