"""Maintenance restoration of source-proven missing LEGACY cached points.

Only existing cache text/indices are restored. The caller freezes ingestion and
supplies a verified 384D document embedding function and recipe.
"""
import json
import math
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
