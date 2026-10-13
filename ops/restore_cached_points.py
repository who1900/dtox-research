"""Maintenance restoration of source-proven missing LEGACY cached points.

Only existing cache text/indices are restored. The caller freezes ingestion and
supplies a verified 384D document embedding function and recipe.
"""
import json
import math
import inspect
from pathlib import Path
import sqlite3
import uuid

from ops import repair_section_labels as labels
from ops import replace_published_labels as snapshots
from pipeline import document_cache

RepairError = labels.RepairError


def parse_source(source, route, *, section_sink=None, element_sink=None):
    from pipeline import extractor
    text = source.decode('utf-8')
    if route == 'latex':
        chunks, mode = extractor.build_chunks(text, section_sink=section_sink, element_sink=element_sink)
    elif route in ('paper_markdown','spec_markdown'):
        chunks, mode = extractor.build_chunks_markdown(text,section_sink=section_sink,element_sink=element_sink,document_kind='paper' if route=='paper_markdown' else 'spec')
    else:
        raise RepairError('unsupported explicit source route')
    if mode in ('legacy','abstract') or not chunks:
        raise RepairError('source parser did not produce a supported body')
    return chunks


def parse_source_views(source, route, source_parser=None):
    """Parse once, returning split chunks plus canonical unsplit typed elements."""
    elements = []
    if source_parser is None:
        chunks = parse_source(source, route, element_sink=elements)
        return chunks, elements
    result = source_parser(source, route)
    if isinstance(result, tuple) and len(result) == 2:
        chunks, elements = result
        return chunks, elements
    return result, None


def _occurrences(text, target):
    count = 0
    start = text.find(target)
    while start >= 0:
        count += 1
        start = text.find(target, start + 1)
    return count


def source_proven(cached_chunk, chunks, source_elements=None):
    """Prove one exact interval in canonical source elements or a parsed chunk.

    Canonical elements are unsplit and typed, so overlap does not double-count
    an interval and code cannot prove prose. No normalization or guessed
    cross-part reconstruction is used.
    """
    from pipeline import extractor

    if not isinstance(cached_chunk, dict):
        return False
    target = cached_chunk.get("text")
    element_type = cached_chunk.get("element_type", "prose")
    if (not isinstance(target, str) or not target
            or not extractor.should_embed(cached_chunk)):
        return False
    if source_elements is not None:
        element_matches = 0
        for element in source_elements:
            if (not isinstance(element, dict) or element.get("element_type", "prose") != element_type
                    or not isinstance(element.get("text"), str)):
                continue
            scope = dict(text=element["text"], section_type=element.get("section_type"),
                         section_title=element.get("section_title"), element_type=element_type)
            if extractor.should_embed(scope):
                element_matches += _occurrences(element["text"], target)
                if element_matches > 1:
                    return False
        if element_matches:
            return element_matches == 1

    matches = 0
    for chunk in chunks:
        if (not isinstance(chunk, dict) or not isinstance(chunk.get("text"), str)
                or chunk.get("element_type", "prose") != element_type
                or not extractor.should_embed(chunk)):
            continue
        matches += _occurrences(chunk["text"], target)
        if matches > 1:
            return False
    return matches == 1


def _read(path, expected):
    data = document_cache.read_bounded(Path(path).resolve())
    if labels._sha(data) != expected:
        raise RepairError('source/cache snapshot changed')
    return data


def _legacy(conn, aid):
    conn.row_factory=sqlite3.Row
    row = conn.execute('SELECT * FROM papers WHERE arxiv_id=?',(aid,)).fetchone()
    if not row or row['status'] != 'done':
        raise RepairError('existing done paper required')
    for table in ('paper_active','paper_publications'):
        if labels._exists(conn,table) and conn.execute('SELECT 1 FROM '+table+' WHERE arxiv_id=?',(aid,)).fetchone():
            raise RepairError('journaled/managed documents are outside legacy restoration')
    return dict(row)


def _inventory(path,fts_path):
    conn=labels._connect(path,readonly=True)
    try:
        meta=dict(conn.execute('SELECT key,value FROM meta'))
        if meta.get('complete') != '1':
            raise RepairError('complete frozen global FTS map required')
        identity=json.loads(meta['source_identity'])
        stat=Path(fts_path).resolve().stat()
        if Path(identity[0]).resolve()!=Path(fts_path).resolve() or tuple(identity[1:])!=(stat.st_dev,stat.st_ino):
            raise RepairError('FTS inventory file identity mismatch')
        indexes=conn.execute('PRAGMA index_list(rows)').fetchall()
        if not any([r[2] for r in conn.execute('PRAGMA index_info("'+i[1].replace('"','""')+'")')][:1]==['point_id'] for i in indexes):
            raise RepairError('global FTS map needs a point_id-leading index')
        return conn
    except BaseException:
        conn.close();raise


def _catalog(conn):
    schemas={'pipeline_fts_points':['point_id','arxiv_id','row_id'],
             'pipeline_fts_rows':['table_name','arxiv_id','row_id'],
             'pipeline_fts_scope':['singleton','complete']}
    for table,columns in schemas.items():
        if [r[1] for r in conn.execute('PRAGMA table_info('+table+')')]!=columns:
            raise RepairError('existing exact partial FTS catalog required')


def _retrieve(client,collection,ids):
    result={}
    for start in range(0,len(ids),200):
        for p in client.retrieve(collection_name=collection,ids=ids[start:start+200],with_payload=True,with_vectors=True):
            vector=p.get('vector') if isinstance(p,dict) else p.vector
            result[labels._point_id(p)]=json.loads(json.dumps(dict(id=labels._point_id(p),payload=labels._payload(p),vector=vector),allow_nan=False))
    return result


def _fts_point(conn,pid):
    row=conn.execute('SELECT arxiv_id,row_id FROM pipeline_fts_points WHERE point_id=?',(pid,)).fetchone()
    if row is None:return None
    actual=labels._fts_tuple(conn,row[1])
    owned=conn.execute("SELECT 1 FROM pipeline_fts_rows WHERE table_name='chunks' AND arxiv_id=? AND row_id=?",tuple(row)).fetchone()
    if not actual or not owned or actual[1:3]!=(pid,row[0]):
        raise RepairError('existing point ownership catalog is inconsistent')
    return row[1],actual


def _expected(point):
    p=point['payload']
    # Same projection policy as pipeline.fts_catalog.append_points.
    return (p['text'],point['id'],p['arxiv_id'],p['section_type'],p.get('element_type','prose'),str(p.get('layers') or ''),p.get('year'))


def build_cached_points(paper_id,chunks,targets,template,embedder):
    """Build only new candidates; preserve paper provenance and target fragment fields."""
    ids=[str(uuid.uuid5(uuid.NAMESPACE_URL,f'{paper_id}#{c["chunk_index"]}')) for c in targets]
    chunk_fields={key for chunk in chunks for key in chunk}
    chunk_fields.update({'text','section_title','section_type','chunk_index','element_type',
        'has_math','has_algorithm','part_index','part_total','page','page_number',
        'source_locator','source_order','chunk_sha256','text_sha256','representation_id',
        'evidence_ref','fragment_id','offset','token_count'})
    points=[]
    vectors=embedder([c['text']for c in targets])
    if len(vectors)!=len(targets):raise RepairError('embedding count mismatch')
    for chunk,pid,vector in zip(targets,ids,vectors):
        if not isinstance(vector,list) or len(vector)!=384 or any(type(x)not in(int,float)or not math.isfinite(x)for x in vector):
            raise RepairError('finite 384D embedding required')
        norm=math.sqrt(sum(x*x for x in vector))
        if abs(norm-1)>1e-3:raise RepairError('embedding must be L2 normalized')
        # Retain paper/source provenance, licensing and extraction fields;
        # never inherit another fragment's locator or content identity.
        payload={k:v for k,v in template.items()if k not in chunk_fields}
        payload.update(chunk)
        payload.update(arxiv_id=paper_id,text=chunk['text'],chunk_index=chunk['chunk_index'],
            section_title=chunk.get('section_title',''),section_type=chunk.get('section_type','other'),
            element_type=chunk.get('element_type','prose'),has_math=chunk.get('has_math',False),
            has_algorithm=chunk.get('has_algorithm',False),part_index=chunk.get('part_index',0),part_total=chunk.get('part_total',1))
        points.append(dict(id=pid,payload=payload,vector=vector))
    return points


def prepare_plan(paper_id,chunk_indices,*,source_path,cache_path,expected_source_sha256,
                 expected_cache_sha256,state_db,fts_db,rowmap_db,journal_path,qdrant,
                 embedder,embedding_recipe,source_route,source_parser=None):
    if Path(journal_path).exists() or not chunk_indices or len(set(chunk_indices))!=len(chunk_indices) or len(chunk_indices)>20:
        raise RepairError('fresh journal and 1..20 unique cached indices required')
    if source_route not in ('latex','paper_markdown','spec_markdown'):
        raise RepairError('unsupported source route')
    if (embedding_recipe.get('dimension')!=384 or embedding_recipe.get('document_prefix')!=''
            or embedding_recipe.get('pooling')!='CLS/L2' or not embedding_recipe.get('model_sha256')
            or not embedding_recipe.get('tokenizer_sha256') or embedding_recipe.get('max_length')!=512):
        raise RepairError('verified deployed bge-small document recipe required')
    source=_read(source_path,expected_source_sha256);cache=_read(cache_path,expected_cache_sha256)
    chunks=json.loads(cache);by_index={c['chunk_index']:c for c in chunks}
    if len(by_index)!=len(chunks) or any(type(i)is not int for i in by_index):
        raise RepairError('cache indices are invalid')
    state=labels._connect(state_db,readonly=True);fts=labels._connect(fts_db,readonly=True)
    inventory=_inventory(rowmap_db,fts_db)
    try:
        row=_legacy(state,paper_id);_catalog(fts)
        if (paper_id.startswith(('acl:','openalex:','pmlr:','hal:')) or row.get('fulltext_source')=='oa-mirror-pdf') and source_route!='paper_markdown':
            raise RepairError('PDF-derived scientific source requires paper markdown route')
        reproduced,source_sections=parse_source_views(source,source_route,source_parser)
        from pipeline import extractor
        targets=[]
        for idx in chunk_indices:
            chunk=by_index.get(idx)
            if chunk is None or not extractor.should_embed(chunk):
                raise RepairError('target cache chunk is absent or currently excluded')
            if not source_proven(chunk, reproduced, source_sections):
                raise RepairError('target text/element has no unique exact current-source interval')
            targets.append(chunk)
        ids=[str(uuid.uuid5(uuid.NAMESPACE_URL,f'{paper_id}#{c["chunk_index"]}')) for c in targets]
        for pid in ids:
            if inventory.execute('SELECT 1 FROM rows WHERE point_id=? LIMIT 1',(pid,)).fetchone() or _fts_point(fts,pid):
                raise RepairError('target UUID already exists in global FTS map/catalog')
        for collection in ('papers_fulltext','papers_coarse_base'):
            if _retrieve(qdrant,collection,ids):
                raise RepairError('target UUID exists in Qdrant collection '+collection)
        baseline=snapshots._scroll(qdrant,'papers_fulltext',paper_id)
        if any('publication_revision'in p['payload'] for p in baseline.values()):
            raise RepairError('legacy baseline has a publication marker')
        for p in baseline.values():
            idx=p['payload'].get('chunk_index')
            if idx not in by_index or p['payload'].get('text')!=by_index[idx].get('text'):
                raise RepairError('baseline point does not match cached text')
        if not baseline:
            raise RepairError('existing legacy payload baseline required')
        baseline_rows=[]
        for rid,pid in inventory.execute('SELECT rowid,point_id FROM rows WHERE arxiv_id=?',(paper_id,)):
            actual=labels._fts_tuple(fts,rid)
            if actual is None or actual[1:3]!=(pid,paper_id):
                raise RepairError('frozen baseline FTS identity mismatch')
            baseline_rows.append([rid,list(actual)])
        # Earlier accepted restores are absent from the frozen census. Include
        # only their exact, receipt-owned current rows, without rescanning FTS.
        known_rowids={r[0]for r in baseline_rows}
        for pid,rid in fts.execute('SELECT point_id,row_id FROM pipeline_fts_points WHERE arxiv_id=?',(paper_id,)):
            if rid in known_rowids:continue
            receipt=None
            if labels._exists(fts,'dtox_cached_point_receipts'):
                receipt=fts.execute('SELECT 1 FROM dtox_cached_point_receipts WHERE point_id=? AND row_id=?',(pid,rid)).fetchone()
            owned=_fts_point(fts,pid)
            if not receipt or owned is None or owned[0]!=rid:
                raise RepairError('unrecorded post-census FTS addition')
            baseline_rows.append([rid,list(owned[1])]);known_rowids.add(rid)
        if (len({r[1][1] for r in baseline_rows})!=len(baseline_rows)
                or {r[1][1] for r in baseline_rows}!=set(baseline)):
            raise RepairError('existing complete FTS and Qdrant baseline differ')
        for rid,actual in baseline_rows:
            p=baseline[actual[1]]['payload']
            if (actual[0],actual[3])!=(p.get('text'),p.get('section_type')):
                raise RepairError('existing FTS and Qdrant baseline text/type differ')
        template=next(iter(baseline.values()))['payload']
        points=build_cached_points(paper_id,chunks,targets,template,embedder)
        plan=dict(schema=1,kind='restore-cached-points',operation_id=str(uuid.uuid4()),phase='prepared',
            arxiv_id=paper_id,state_db=str(Path(state_db).resolve()),fts_db=str(Path(fts_db).resolve()),
            rowmap_db=str(Path(rowmap_db).resolve()),source_path=str(Path(source_path).resolve()),
            cache_path=str(Path(cache_path).resolve()),source_sha256=expected_source_sha256,
            cache_sha256=expected_cache_sha256,state_before=row,embedding_recipe=embedding_recipe,
            source_route=source_route,baseline=list(baseline.values()),baseline_fts=baseline_rows,points=points)
        _read(source_path,expected_source_sha256);_read(cache_path,expected_cache_sha256)
        labels._atomic(journal_path,labels._json_bytes(plan));return plan
    finally:
        state.close();fts.close();inventory.close()


def _load(path):
    plan=json.loads(Path(path).read_text(encoding='utf-8'))
    if plan.get('schema')!=1 or plan.get('kind')!='restore-cached-points':raise RepairError('invalid restore journal')
    return plan


def _candidate_equal(actual, expected):
    """Allow only float32 storage/normalization noise on newly written vectors.

    Qdrant's Cosine storage can round/renormalize float64 JSON components.
    Identity and payload remain exact; existing baseline vectors remain exact.
    """
    if actual.get('id')!=expected.get('id') or actual.get('payload')!=expected.get('payload'):
        return False
    a,b=actual.get('vector'),expected.get('vector')
    if not isinstance(a,list)or not isinstance(b,list)or len(a)!=384 or len(b)!=384:
        return False
    if any(type(x)not in(int,float)or not math.isfinite(x)for x in a+b):return False
    if any(abs(math.sqrt(sum(x*x for x in v))-1)>1e-3 for v in(a,b)):return False
    differences=[abs(x-y)for x,y in zip(a,b)]
    return max(differences)<=1e-7 and math.sqrt(sum(d*d for d in differences))<=1e-6


def _verify(plan,client,mode):
    if plan.get('operation')=='receipt-withdrawal':
        return _verify_legacy_withdrawal(plan,client,mode)
    _read(plan['source_path'],plan['source_sha256']);_read(plan['cache_path'],plan['cache_sha256'])
    state=labels._connect(plan['state_db'],readonly=True);fts=labels._connect(plan['fts_db'],readonly=True)
    inventory=_inventory(plan['rowmap_db'],plan['fts_db'])
    try:
        if _legacy(state,plan['arxiv_id'])!=plan['state_before']:raise RepairError('paper state drift')
        _catalog(fts)
        live=snapshots._scroll(client,'papers_fulltext',plan['arxiv_id'])
        baseline={p['id']:p for p in plan['baseline']};new={p['id']:p for p in plan['points']}
        if not set(baseline)<=set(live) or not set(live)<=set(baseline)|set(new):raise RepairError('baseline point set drift')
        if any(live[pid]!=p for pid,p in baseline.items()):raise RepairError('baseline payload/vector drift')
        for rid,actual in plan['baseline_fts']:
            if labels._fts_tuple(fts,rid)!=tuple(actual):raise RepairError('baseline FTS row drift')
        if _retrieve(client,'papers_coarse_base',list(new)):raise RepairError('coarse UUID collision')
        for pid,p in new.items():
            current=live.get(pid)
            if current is not None and not _candidate_equal(current,p):raise RepairError('candidate point drift')
            if mode=='before'and current is not None or mode=='after'and current is None:raise RepairError('candidate point visibility mismatch')
            if inventory.execute('SELECT 1 FROM rows WHERE point_id=? LIMIT 1',(pid,)).fetchone():raise RepairError('target UUID entered frozen map')
            owned=_fts_point(fts,pid)
            receipt=None
            if labels._exists(fts,'dtox_cached_point_receipts'):
                receipt=fts.execute('SELECT row_id FROM dtox_cached_point_receipts WHERE operation_id=? AND point_id=?',(plan['operation_id'],pid)).fetchone()
            if owned is not None and (owned[1]!=_expected(p)or receipt is None or receipt[0]!=owned[0]):raise RepairError('candidate FTS ownership drift')
            if receipt is not None and (owned is None or receipt[0]!=owned[0]):raise RepairError('receipt row drift')
            if mode=='before'and owned is not None or mode=='after'and owned is None:raise RepairError('candidate FTS visibility mismatch')
    finally:
        state.close();fts.close();inventory.close()


def _phase(path,plan,phase):
    plan['phase']=phase;labels._atomic(path,labels._json_bytes(plan))


def apply_plan(journal_path,*,qdrant):
    plan=_load(journal_path)
    if plan['phase']!='prepared':raise RepairError('restore must be prepared')
    _verify(plan,qdrant,'before');_phase(journal_path,plan,'applying')
    try:
        if plan.get('operation')=='receipt-withdrawal':
            _write_removed_points(qdrant,'papers_fulltext',plan['removed'],True)
            _write_removed_fts_transaction(plan['fts_db'],plan['removed'],True)
            _verify(plan,qdrant,'after');_phase(journal_path,plan,'applied');return plan
        # Plain records also support a small REST adapter on hosts without SDK.
        qdrant.upsert(collection_name='papers_fulltext',points=plan['points'],wait=True)
        fts=labels._connect(plan['fts_db'])
        try:
            fts.execute('BEGIN IMMEDIATE');_catalog(fts)
            fts.execute('CREATE TABLE IF NOT EXISTS dtox_cached_point_receipts(operation_id TEXT NOT NULL,point_id TEXT NOT NULL,row_id INTEGER NOT NULL,PRIMARY KEY(operation_id,point_id))')
            fts.execute('CREATE INDEX IF NOT EXISTS dtox_cached_point_receipts_point ON dtox_cached_point_receipts(point_id,row_id)')
            for p in plan['points']:
                if _fts_point(fts,p['id']):raise RepairError('candidate acquired FTS owner')
                cursor=fts.execute('INSERT INTO chunks(text,point_id,arxiv_id,section_type,element_type,layers,year)VALUES(?,?,?,?,?,?,?)',_expected(p))
                rid=cursor.lastrowid
                fts.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(p['id'],plan['arxiv_id'],rid))
                fts.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(plan['arxiv_id'],rid))
                fts.execute('INSERT INTO dtox_cached_point_receipts VALUES(?,?,?)',(plan['operation_id'],p['id'],rid))
            fts.commit()
        finally:fts.close()
        _verify(plan,qdrant,'after');_phase(journal_path,plan,'applied')
    except Exception:
        rollback_plan(journal_path,qdrant=qdrant);raise
    return plan


def rollback_plan(journal_path,*,qdrant):
    plan=_load(journal_path)
    if plan['phase']not in('applying','applied','rolling_back'):raise RepairError('restore has no started operation')
    _verify(plan,qdrant,'partial');_phase(journal_path,plan,'rolling_back')
    if plan.get('operation')=='receipt-withdrawal':
        _write_removed_points(qdrant,'papers_fulltext',plan['removed'],False)
        _write_removed_fts_transaction(plan['fts_db'],plan['removed'],False)
        _verify(plan,qdrant,'before');_phase(journal_path,plan,'rolled_back');return plan
    ids=[p['id']for p in plan['points']]
    # Exact IDs were independently verified; never delete using a paper filter.
    qdrant.delete(collection_name='papers_fulltext',points_selector=ids,wait=True)
    fts=labels._connect(plan['fts_db'])
    try:
        fts.execute('BEGIN IMMEDIATE')
        for pid in ids:
            owned=_fts_point(fts,pid)
            if owned:
                rid=owned[0]
                fts.execute('DELETE FROM chunks WHERE rowid=?',(rid,))
                fts.execute('DELETE FROM pipeline_fts_points WHERE point_id=? AND arxiv_id=? AND row_id=?',(pid,plan['arxiv_id'],rid))
                fts.execute("DELETE FROM pipeline_fts_rows WHERE table_name='chunks'AND arxiv_id=?AND row_id=?",(plan['arxiv_id'],rid))
        if labels._exists(fts,'dtox_cached_point_receipts'):
            fts.execute('DELETE FROM dtox_cached_point_receipts WHERE operation_id=?',(plan['operation_id'],))
        fts.commit()
    finally:fts.close()
    _verify(plan,qdrant,'before');_phase(journal_path,plan,'rolled_back');return plan


WITHDRAWAL_POLICY_SHA256='b827f11ceee3404af6c2ec3e4dccd50e89cb6eef961bc929857245ff4d191893'


def _withdrawal_policy(expected):
    from pipeline import extractor
    function=extractor.is_boilerplate_chunk
    if (expected!=WITHDRAWAL_POLICY_SHA256 or extractor.BOILERPLATE_FILTER is not True or function is None
            or labels._sha(Path(inspect.getsourcefile(function)).read_bytes().replace(b'\r\n',b'\n'))!=expected):
        raise RepairError('withdrawal requires the exact active boilerplate policy')
    return extractor


def _withdrawal_request(paper_id,point_ids,source_path,cache_path,expected_source_sha256,
        expected_cache_sha256,rowmap_db,expected_policy_sha256,origin_journals,source_route):
    if source_route not in ('latex','paper_markdown','spec_markdown'):raise RepairError('unsupported withdrawal source route')
    return dict(arxiv_id=paper_id,point_ids=list(point_ids),source_path=str(Path(source_path).resolve()),
        cache_path=str(Path(cache_path).resolve()),expected_source_sha256=expected_source_sha256,
        expected_cache_sha256=expected_cache_sha256,rowmap_db=str(Path(rowmap_db).resolve()),
        expected_policy_sha256=expected_policy_sha256,source_route=source_route,
        origin_journals={pid:str(Path(path).resolve()) for pid,path in origin_journals.items()})


def _prepare_withdrawals(request,live,fts,fts_db,client,source_parser=None):
    aid=request['arxiv_id'];ids=request['point_ids']
    if (not ids or len(ids)>3000 or len(set(ids))!=len(ids) or not set(ids)<=set(live)
            or set(request['origin_journals'])!=set(ids)):
        raise RepairError('unique current receipt-owned withdrawal IDs required')
    policy=_withdrawal_policy(request['expected_policy_sha256'])
    source=_read(request['source_path'],request['expected_source_sha256'])
    chunks=json.loads(_read(request['cache_path'],request['expected_cache_sha256']))
    by_index={c['chunk_index']:c for c in chunks}
    if len(by_index)!=len(chunks):raise RepairError('duplicate withdrawal cache indices')
    try:parsed,elements=parse_source_views(source,request['source_route'],source_parser)
    except RepairError:parsed,elements=[],[]
    _catalog(fts)
    if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()!=(1,):
        raise RepairError('withdrawal requires complete ownership')
    if not labels._exists(fts,'dtox_cached_point_receipts'):raise RepairError('no accepted addition receipts')
    inventory=_inventory(request['rowmap_db'],fts_db)
    removed=[]
    try:
        for pid in ids:
            p=live[pid];payload=p['payload'];c=by_index.get(payload.get('chunk_index'))
            if (payload.get('arxiv_id')!=aid or c is None or any(payload.get(k)!=c.get(k) for k in
                    ('text','chunk_index','section_title','section_type'))
                    or payload.get('element_type','prose')!=c.get('element_type','prose')
                    or policy.should_embed(c)):
                raise RepairError('withdrawal point is not an exact currently excluded cache chunk')
            if inventory.execute('SELECT 1 FROM rows WHERE point_id=? LIMIT 1',(pid,)).fetchone():
                raise RepairError('original frozen baseline point cannot be withdrawn')
            owned=_fts_point(fts,pid)
            receipts=[list(r) for r in fts.execute('SELECT operation_id,point_id,row_id FROM dtox_cached_point_receipts WHERE point_id=?',(pid,))]
            if owned is None or owned[1]!=_expected(p) or len(receipts)!=1 or receipts[0][2]!=owned[0]:
                raise RepairError('withdrawal requires exact unique receipt/FTS ownership')
            origin_path=Path(request['origin_journals'][pid])
            origin_raw=document_cache.read_bounded(origin_path,64*1024*1024);origin=json.loads(origin_raw)
            if origin.get('schema')!=1 or origin.get('phase')!='applied':raise RepairError('origin addition journal was not accepted')
            if origin.get('kind')=='restore-cached-points' and not origin.get('operation'):
                candidates=origin['points'];operation=origin['operation_id']
                source_hash=origin['source_sha256'];route=origin['source_route']
            elif origin.get('kind')=='published-label-replacement' and 'addition_proof' in origin.get('paper',{}):
                candidates=origin['paper']['points_added'];operation=origin['paper']['addition_operation_id']
                source_hash=origin['paper']['addition_proof']['expected_source_sha256'];route=origin['paper']['addition_proof']['source_route']
            else:raise RepairError('origin journal does not prove a newly added point')
            matches=[x for x in candidates if x['id']==pid]
            if (len(matches)!=1 or receipts[0][0]!=operation or source_hash!=request['expected_source_sha256']
                    or route!=request['source_route']):raise RepairError('origin receipt/source binding mismatch')
            old=json.loads(json.dumps(matches[0]))
            # Later accepted label repairs and snapshot markers do not change
            # the original text/vector identity of an addition.
            for key in ('section_title','section_type','publication_revision'):
                if key in payload:old['payload'][key]=payload[key]
                else:old['payload'].pop(key,None)
            if (not snapshots._parent_payload_equal(payload,old['payload'])
                    or not snapshots._vector_equal(p['vector'],old['vector'])):
                raise RepairError('origin addition payload/vector identity changed')
            reproduced=source_proven(c,parsed,elements)
            removed.append(dict(point=p,fts_row=[owned[0],*owned[1]],receipt=receipts[0],
                origin_path=str(origin_path),origin_sha256=labels._sha(origin_raw),
                source_proof='current-typed-replay' if reproduced else 'accepted-source-bound-origin'))
        if _retrieve(client,'papers_coarse_base',ids):raise RepairError('withdrawal coarse UUID collision')
        if _retrieve(client,'papers_fulltext',ids)!={pid:live[pid] for pid in ids}:
            raise RepairError('withdrawal global UUID ownership mismatch')
        return removed
    finally:inventory.close()


def _verify_withdrawal_data(proof,removed,before,after,fts_before,fts,fts_db,client,mode):
    policy=_withdrawal_policy(proof['expected_policy_sha256'])
    _read(proof['source_path'],proof['expected_source_sha256'])
    cache=json.loads(_read(proof['cache_path'],proof['expected_cache_sha256']))
    by_index={c['chunk_index']:c for c in cache}
    live=snapshots._scroll(client,'papers_fulltext',proof['arxiv_id'])
    required=set(after) if mode=='after' else set(before) if mode=='before' else set(before)&set(after)
    permitted=set(before) if mode in ('before','partial') else set(after)
    if not required<=set(live)<=permitted:raise RepairError('withdrawal point set drift')
    removed_ids={r['point']['id'] for r in removed}
    for pid,point in live.items():
        choices=[before[pid],after[pid]] if pid in after else [before[pid]]
        allowed=choices if mode=='partial' else [after[pid] if mode=='after' else before[pid]]
        if not any(point==p or pid in removed_ids and _candidate_equal(point,p) for p in allowed):
            raise RepairError('withdrawal payload/vector drift')
    actual_ids={r[0] for r in fts.execute('SELECT point_id FROM pipeline_fts_points WHERE arxiv_id=?',(proof['arxiv_id'],))}
    if not set(after)<=actual_ids<=set(before):raise RepairError('withdrawal catalog point set drift')
    for row in fts_before:
        if row[2] not in removed_ids and _fts_point(fts,row[2])!=(row[0],tuple(row[1:])):
            raise RepairError('retained withdrawal FTS row drift')
    inventory=_inventory(proof['rowmap_db'],fts_db)
    try:
        _catalog(fts)
        if fts.execute('SELECT complete FROM pipeline_fts_scope WHERE singleton=1').fetchone()!=(1,):raise RepairError('withdrawal ownership completeness changed')
        global_points=_retrieve(client,'papers_fulltext',list(removed_ids))
        if set(global_points)!=(set(live)&removed_ids):raise RepairError('withdrawn UUID acquired another owner')
        if _retrieve(client,'papers_coarse_base',list(removed_ids)):raise RepairError('withdrawal coarse UUID collision')
        for r in removed:
            point=r['point'];pid=point['id'];row=r['fts_row'];receipt=r['receipt']
            c=by_index.get(point['payload']['chunk_index'])
            if c is None or policy.should_embed(c):raise RepairError('withdrawal eligibility changed')
            if labels._sha(document_cache.read_bounded(Path(r['origin_path']),64*1024*1024))!=r['origin_sha256']:
                raise RepairError('withdrawal origin journal changed')
            if inventory.execute('SELECT 1 FROM rows WHERE point_id=? LIMIT 1',(pid,)).fetchone():raise RepairError('withdrawal entered frozen baseline')
            owned=_fts_point(fts,pid);actual=labels._fts_tuple(fts,row[0])
            receipts=[list(x) for x in fts.execute('SELECT operation_id,point_id,row_id FROM dtox_cached_point_receipts WHERE point_id=?',(pid,))]
            if owned is None:
                if actual is not None or receipts:raise RepairError('withdrawn FTS/receipt/rowid collision')
                if fts.execute("SELECT 1 FROM pipeline_fts_rows WHERE table_name='chunks' AND row_id=?",(row[0],)).fetchone():raise RepairError('withdrawn row ownership collision')
            elif owned!=(row[0],tuple(row[1:])) or receipts!=[receipt]:raise RepairError('withdrawal receipt/FTS drift')
            if mode=='before' and owned is None or mode=='after' and owned is not None:
                raise RepairError('withdrawal FTS visibility mismatch')
    finally:inventory.close()


def _write_removed_points(client,collection,removed,forward):
    if forward:client.delete(collection_name=collection,points_selector=[r['point']['id'] for r in removed],wait=True)
    else:client.upsert(collection_name=collection,points=[r['point'] for r in removed],wait=True)


def _write_removed_fts(fts,removed,forward):
    for r in removed:
        p=r['point'];row=r['fts_row'];receipt=r['receipt'];pid=p['id'];aid=p['payload']['arxiv_id']
        if forward:
            if _fts_point(fts,pid)!=(row[0],tuple(row[1:])):raise RepairError('withdrawal FTS changed before deletion')
            fts.execute('DELETE FROM chunks WHERE rowid=?',(row[0],))
            fts.execute('DELETE FROM pipeline_fts_points WHERE point_id=? AND arxiv_id=? AND row_id=?',(pid,aid,row[0]))
            fts.execute("DELETE FROM pipeline_fts_rows WHERE table_name='chunks' AND arxiv_id=? AND row_id=?",(aid,row[0]))
            changed=fts.execute('DELETE FROM dtox_cached_point_receipts WHERE operation_id=? AND point_id=? AND row_id=?',receipt).rowcount
            if changed!=1:raise RepairError('withdrawal receipt changed before deletion')
        elif _fts_point(fts,pid) is None:
            if labels._fts_tuple(fts,row[0]) is not None:raise RepairError('withdrawal rollback rowid collision')
            fts.execute('INSERT INTO chunks(rowid,text,point_id,arxiv_id,section_type,element_type,layers,year) VALUES(?,?,?,?,?,?,?,?)',row)
            fts.execute('INSERT INTO pipeline_fts_points VALUES(?,?,?)',(pid,aid,row[0]))
            fts.execute("INSERT INTO pipeline_fts_rows VALUES('chunks',?,?)",(aid,row[0]))
            fts.execute('INSERT INTO dtox_cached_point_receipts VALUES(?,?,?)',receipt)


def _write_removed_fts_transaction(path,removed,forward):
    fts=labels._connect(path)
    try:
        fts.execute('BEGIN IMMEDIATE');_write_removed_fts(fts,removed,forward);fts.commit()
    finally:fts.close()


def prepare_withdrawal_plan(paper_id,point_ids,*,source_path,cache_path,expected_source_sha256,
        expected_cache_sha256,state_db,fts_db,rowmap_db,journal_path,qdrant,expected_policy_sha256,
        origin_journals,source_route,source_parser=None,section_cache_dir=None):
    if Path(journal_path).exists():raise RepairError('withdrawal journal already exists')
    proof=_withdrawal_request(paper_id,point_ids,source_path,cache_path,expected_source_sha256,
        expected_cache_sha256,rowmap_db,expected_policy_sha256,origin_journals,source_route)
    state=labels._connect(state_db,readonly=True);fts=labels._connect(fts_db,readonly=True)
    try:
        row=_legacy(state,paper_id);live=snapshots._scroll(qdrant,'papers_fulltext',paper_id)
        removed=_prepare_withdrawals(proof,live,fts,fts_db,qdrant,source_parser)
        if len(removed)==len(live):raise RepairError('withdrawal must retain a nonempty legacy baseline')
        rowids=[r[0] for r in fts.execute('SELECT row_id FROM pipeline_fts_points WHERE arxiv_id=?',(paper_id,))]
        rows=snapshots._fts_rows(fts,paper_id,rowids)
        if len(rows)!=len(live) or {r[2] for r in rows}!=set(live):raise RepairError('withdrawal full FTS set mismatch')
        for r in rows:
            if tuple(r[1:])!=_expected(live[r[2]]):raise RepairError('withdrawal baseline FTS differs')
        plan=dict(schema=1,kind='restore-cached-points',operation='receipt-withdrawal',phase='prepared',
            arxiv_id=paper_id,state_db=str(Path(state_db).resolve()),fts_db=str(Path(fts_db).resolve()),
            state_before=row,withdrawal_proof=proof,removed=removed,points_before=list(live.values()),
            fts_before=[list(r) for r in rows],points=[r['point'] for r in removed])
        _verify_legacy_withdrawal(plan,qdrant,'before')
        labels._atomic(journal_path,labels._json_bytes(plan));return plan
    finally:state.close();fts.close()


def _verify_legacy_withdrawal(plan,client,mode):
    state=labels._connect(plan['state_db'],readonly=True);fts=labels._connect(plan['fts_db'],readonly=True)
    try:
        if _legacy(state,plan['arxiv_id'])!=plan['state_before']:raise RepairError('withdrawal paper state drift')
        before={p['id']:p for p in plan['points_before']};ids={r['point']['id'] for r in plan['removed']}
        after={pid:p for pid,p in before.items() if pid not in ids}
        _verify_withdrawal_data(plan['withdrawal_proof'],plan['removed'],before,after,plan['fts_before'],fts,plan['fts_db'],client,mode)
    finally:state.close();fts.close()
