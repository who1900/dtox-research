"""Resumable source-proven cached-point restoration for existing documents.

The caller supplies published-reader maintenance, a verified embedding recipe
and REST/SDK client. Legacy additions keep existing readers available.
A wall-time budget stops dispatch between bounded operations, never mid-commit.
"""
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import math
from contextlib import nullcontext

from ops import restore_cached_points as restore
from ops import audit_corpus_quality as census_tools
from ops import audit_projection_coverage as projection_tools
from ops import repair_section_labels as labels
from pipeline import document_cache


class DriverError(RuntimeError):
    pass


class RecoveryRequired(DriverError):
    pass


def _ro(path):
    conn=labels._connect(path,readonly=True);conn.row_factory=sqlite3.Row;return conn


def _identity(path):
    return projection_tools._identity(path)


def _meta(conn):
    return dict(conn.execute('SELECT key,value FROM meta'))


def _ledger(path,identity):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path,timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    prior=conn.execute("SELECT value FROM meta WHERE key='inputs'").fetchone()
    if prior and prior[0]!=identity:
        conn.close();raise DriverError('run ledger belongs to different frozen inputs')
    conn.execute("INSERT OR IGNORE INTO meta VALUES('inputs',?)",(identity,))
    conn.execute('CREATE TABLE IF NOT EXISTS targets(paper_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,journal_path TEXT,detail TEXT,PRIMARY KEY(paper_id,chunk_index))')
    conn.commit();return conn


def _mark(ledger,aid,indices,status,*,path=None,detail=None,attempt=None):
    for idx in indices:
        if attempt is None:
            ledger.execute('UPDATE targets SET status=?,journal_path=?,detail=? WHERE paper_id=? AND chunk_index=?',
                (status,str(path)if path else None,detail,aid,idx))
        else:
            ledger.execute('UPDATE targets SET status=?,journal_path=?,detail=?,attempt=? WHERE paper_id=? AND chunk_index=?',
                (status,str(path)if path else None,detail,attempt,aid,idx))
    ledger.commit()


def _cache_hash(row,raw,label_run_db):
    actual=labels._sha(raw)
    if actual==row['cache_sha256']:return actual
    if not label_run_db:raise DriverError('cache changed without verified label receipt')
    conn=_ro(label_run_db)
    try:
        receipt=conn.execute("SELECT journal_path FROM papers WHERE paper_id=? AND status='verified'",(row['paper_id'],)).fetchone()
    finally:conn.close()
    if not receipt:raise DriverError('verified label receipt missing')
    def paper_images(plan):
        if plan.get('kind')=='section-label-repair':
            papers=[p for p in plan.get('papers',[])if p.get('arxiv_id')==row['paper_id']]
            if len(papers)!=1:raise DriverError('label receipt paper identity mismatch')
            paper=papers[0]
        elif plan.get('kind')=='published-label-replacement':
            paper=plan.get('paper')
        else:raise DriverError('unsupported label receipt kind')
        if not isinstance(paper,dict)or paper.get('arxiv_id')!=row['paper_id']:
            raise DriverError('label repair paper mismatch')
        try:
            before=base64.b64decode(paper['cache_before'],validate=True)
            after=base64.b64decode(paper['cache_after'],validate=True)
        except Exception as exc:raise DriverError('label receipt cache image is invalid')from exc
        return before,after

    def content(data):
        try:chunks=json.loads(data)
        except (TypeError,ValueError)as exc:raise DriverError('label receipt cache JSON is invalid')from exc
        if not isinstance(chunks,list)or any(not isinstance(c,dict)for c in chunks):
            raise DriverError('label receipt cache shape is invalid')
        return [{k:v for k,v in c.items()if k not in('section_title','section_type')}for c in chunks]

    def read_journal(path, *, referenced=False):
        path=Path(path)
        if (referenced and not path.is_absolute())or path.is_symlink()or not path.is_file():
            raise DriverError('label receipt journal path is invalid')
        try:raw_journal=path.read_bytes();plan=json.loads(raw_journal)
        except Exception as exc:raise DriverError('label receipt journal is unreadable')from exc
        if plan.get('phase')!='applied':raise DriverError('label repair is not applied')
        before,after=paper_images(plan)
        if content(before)!=content(after):raise DriverError('label receipt changed content beyond labels')
        return raw_journal,plan,before,after

    current_path=Path(receipt[0])
    _,plan,before,after=read_journal(current_path)
    if after!=raw:raise DriverError('label receipt does not bind current cache')
    if labels._sha(before)==row['cache_sha256']:
        if plan.get('cache_parent_journal'):
            raise DriverError('unexpected parent reference on frozen-cache label receipt')
        return actual

    # Repeated verified label passes form a hash-bound journal chain. Follow
    # only recorded parent paths; never scan journal directories for a guess.
    expected_child_before=before
    reference=plan.get('cache_parent_journal')
    seen={str(current_path.resolve())}
    while True:
        if not isinstance(reference,dict)or set(reference)!={'path','sha256'}:
            raise DriverError('label receipt parent reference is missing or malformed')
        parent_path=Path(reference['path'])
        normalized=str(parent_path.resolve())
        if normalized in seen:raise DriverError('label receipt parent journal cycle')
        seen.add(normalized)
        parent_raw,parent_plan,parent_before,parent_after=read_journal(parent_path,referenced=True)
        if labels._sha(parent_raw)!=reference['sha256']:
            raise DriverError('label receipt parent journal hash drift')
        if parent_after!=expected_child_before:
            raise DriverError('label receipt parent cache boundary drift')
        if labels._sha(parent_before)==row['cache_sha256']:
            if parent_plan.get('cache_parent_journal'):
                raise DriverError('frozen-cache root unexpectedly has a parent reference')
            return actual
        expected_child_before=parent_before
        reference=parent_plan.get('cache_parent_journal')


def _inputs(row,args):
    name=document_cache.safe_id(row['paper_id'])
    cache_path=Path(args.cache_dir).resolve()/(name+'.json')
    raw=document_cache.read_bounded(cache_path)
    cache_sha=_cache_hash(row,raw,getattr(args,'label_run_db',None))
    location=row['source_location']
    if location=='fulltext_cache':source_path=Path(args.fulltext_cache_dir).resolve()/(name+'.tex')
    elif location=='latex_cache':source_path=Path(args.latex_cache_dir).resolve()/name/'source.tex'
    else:raise DriverError('source location is unavailable')
    source=document_cache.read_bounded(source_path)
    if labels._sha(source)!=row['source_sha256']:raise DriverError('source changed since frozen census')
    chunks=json.loads(raw);by_index={c['chunk_index']:c for c in chunks}
    if len(by_index)!=len(chunks):raise DriverError('duplicate cache indices')
    if census_tools._is_spec(row['paper_id']):route='spec_markdown'
    elif census_tools._is_markdown_route(row['paper_id'],row['fulltext_source'],row['parser_mode']):route='paper_markdown'
    else:route='latex'
    return cache_path,cache_sha,source_path,source,by_index,route


def _publication_route(state, aid):
    row=state.execute('SELECT status FROM papers WHERE arxiv_id=?',(aid,)).fetchone()
    if not row or tuple(row)!=('done',):
        raise DriverError('existing done paper required')
    if labels._exists(state,'paper_active') and state.execute('SELECT 1 FROM paper_active WHERE arxiv_id=?',(aid,)).fetchone():
        raise DriverError('managed revision requires its revision-aware route')
    publications=(state.execute('SELECT kind,state FROM paper_publications WHERE arxiv_id=?',(aid,)).fetchall()
                  if labels._exists(state,'paper_publications') else [])
    if not publications:
        return restore
    if sum(tuple(r)==('snapshot','published') for r in publications)==1:
        return restore.snapshots
    raise DriverError('unsupported publication state')


def _operation(plan):
    if plan.get('kind')=='restore-cached-points':
        return restore,plan['arxiv_id'],plan['points']
    if plan.get('kind')=='published-label-replacement':
        paper=plan['paper']
        return restore.snapshots,paper['arxiv_id'],paper['points_added']
    raise DriverError('unsupported restoration journal')


def _maintenance(module,args):
    if module is restore:
        return nullcontext()
    factory=getattr(args,'publication_maintenance',None)
    if factory is None:
        raise DriverError('published replacement requires a reader maintenance context')
    return factory()


def _recover(ledger,args,client,deadline):
    added=verified=0
    groups=ledger.execute("SELECT DISTINCT paper_id,journal_path FROM targets WHERE status NOT IN('verified','excluded') AND journal_path IS NOT NULL").fetchall()
    for aid,path in groups:
        if time.monotonic()>=deadline:break
        entries=ledger.execute('SELECT chunk_index,status FROM targets WHERE paper_id=? AND journal_path=?',(aid,path)).fetchall()
        indices=[r[0]for r in entries]
        if not Path(path).exists():
            if all(r[1]=='failed'for r in entries)and not getattr(args,'retry_failed',False):continue
            _mark(ledger,aid,indices,'pending');continue
        plan=json.loads(Path(path).read_text(encoding='utf-8'));phase=plan['phase']
        module,plan_aid,points=_operation(plan)
        if plan_aid!=aid or {p['payload']['chunk_index']for p in points}!=set(indices):
            raise DriverError('run ledger and recovery journal disagree')
        # Started operations are always recovered, including a previous failure.
        if phase in('applying','rolling_back'):
            with _maintenance(module,args):module.rollback_plan(path,qdrant=client)
            _mark(ledger,aid,indices,'pending');continue
        if phase=='rolled_back':
            if getattr(args,'retry_failed',False)or any(r[1]!='failed'for r in entries):_mark(ledger,aid,indices,'pending')
            continue
        if any(r[1]in('failed','deferred')for r in entries)and not getattr(args,'retry_failed',False):continue
        try:
            if phase=='prepared':
                with _maintenance(module,args):module.apply_plan(path,qdrant=client)
                added+=len(indices)
            elif phase!='applied':raise DriverError('unknown recovery journal phase')
            module._verify(module._load(path),client,'after')
            _mark(ledger,aid,indices,'verified',path=path);verified+=len(indices)
        except Exception as exc:
            _mark(ledger,aid,indices,'failed',path=path,detail=type(exc).__name__+': '+str(exc)[:300])
            if module._load(path)['phase']in('applying','rolling_back'):
                raise RecoveryRequired('started restore could not be recovered; keep maintenance')from exc
    return added,verified


def run(args,qdrant,embedder,embedding_recipe,*,source_parser=None):
    """Process all frozen candidates, bounded by args.max_seconds and batch_size."""
    deadline=time.monotonic()+float(args.max_seconds)
    batch_size=int(getattr(args,'batch_size',20))
    allowed=getattr(args,'allowed_sources',None)
    if isinstance(allowed,str):raise DriverError('allowed_sources must be an iterable of source names, not a string')
    allowed=None if allowed is None else frozenset(allowed)
    if not math.isfinite(float(args.max_seconds))or args.max_seconds<=0 or not 1<=batch_size<=20:raise DriverError('positive budget and batch size 1..20 required')
    census=_ro(args.census_db);projection=_ro(args.projection_db);rowmap=_ro(args.rowmap_db)
    ledger=None
    try:
        cm,pm,rm=map(_meta,(census,projection,rowmap))
        if cm.get('run_complete')!='1'or pm.get('run_complete')!='1'or rm.get('complete')!='1':
            raise DriverError('completed census, projection and FTS map required')
        if pm.get('census_identity')!=_identity(args.census_db)or pm.get('rowmap_identity')!=_identity(args.rowmap_db):
            raise DriverError('projection input identities changed')
        identity=labels._json_bytes(dict(census=_identity(args.census_db),projection=_identity(args.projection_db),rowmap=_identity(args.rowmap_db),
            state=str(Path(args.state_db).resolve()),fts=str(Path(args.fts_db).resolve()),embedding_recipe=embedding_recipe)).decode()
        ledger=_ledger(args.run_db,identity)
        Path(args.journal_dir).mkdir(parents=True,exist_ok=True)
        candidates=[]
        for p in projection.execute('SELECT *FROM papers WHERE cache_backed_missing_current_count>0 ORDER BY paper_id'):
            row=census.execute('SELECT *FROM papers WHERE paper_id=?',(p['paper_id'],)).fetchone()
            if row is None:raise DriverError('projection paper missing from census')
            if allowed is not None and row['fulltext_source']not in allowed:continue
            indices=json.loads(p['cache_backed_missing_current_indices_json'])
            if (len(indices)!=p['cache_backed_missing_current_count']or len(set(indices))!=len(indices)
                    or any(type(i)is not int or i<0 for i in indices)):
                raise DriverError('projection missing index set is invalid')
            if p['census_fingerprint']!=row['input_fingerprint']or p['cache_sha256']!=row['cache_sha256']:
                raise DriverError('projection does not bind census cache fingerprint')
            ledger.executemany("INSERT OR IGNORE INTO targets(paper_id,chunk_index,status)VALUES(?,?,'pending')",[(row['paper_id'],i)for i in indices])
            candidates.append((dict(row),dict(p),indices))
        ledger.commit()
        # Exercise the newly supported publication route before the much larger
        # legacy cohort; the selected universe and journal identities stay fixed.
        state=labels._connect(args.state_db,readonly=True)
        try:
            published=({r[0]for r in state.execute("SELECT arxiv_id FROM paper_publications WHERE kind='snapshot' AND state='published'")}
                       if labels._exists(state,'paper_publications')else set())
        finally:state.close()
        candidates.sort(key=lambda entry:(entry[0]['paper_id']not in published,entry[0]['paper_id']))
        added,verified=_recover(ledger,args,qdrant,deadline)
        if getattr(args,'retry_failed',False):
            ledger.execute("UPDATE targets SET status='pending',journal_path=NULL WHERE status IN('failed','deferred')")
            ledger.commit()
        for row,p,all_indices in candidates:
            if time.monotonic()>=deadline:break
            aid=row['paper_id']
            pending=[r[0]for r in ledger.execute("SELECT chunk_index FROM targets WHERE paper_id=?AND status='pending' ORDER BY chunk_index",(aid,))]
            if not pending:continue
            if p['projection_status']!='cache_backed_current_projection_missing':
                _mark(ledger,aid,pending,'deferred',detail='projection has duplicate/unmapped or unsupported identities');continue
            try:
                state=labels._connect(args.state_db,readonly=True)
                try:module=_publication_route(state,aid)
                finally:state.close()
            except (restore.RepairError,DriverError) as exc:
                _mark(ledger,aid,pending,'deferred',detail=str(exc));continue
            try:
                cache_path,cache_sha,source_path,source,by_index,route=_inputs(row,args)
                parsed,elements=restore.parse_source_views(source,route,source_parser)
                proven=[]
                for idx in pending:
                    c=by_index.get(idx)
                    if c is not None and restore.source_proven(c,parsed,elements):
                        proven.append(idx)
                    else:_mark(ledger,aid,[idx],'deferred',detail='cached target has no unique currently eligible exact source interval')
                for start in range(0,len(proven),batch_size):
                    if time.monotonic()>=deadline:break
                    indices=proven[start:start+batch_size]
                    attempt=1+max(ledger.execute('SELECT attempt FROM targets WHERE paper_id=?AND chunk_index=?',(aid,i)).fetchone()[0]for i in indices)
                    token=hashlib.sha256(labels._json_bytes([aid,indices,attempt])).hexdigest()
                    path=Path(args.journal_dir)/token[:2]/(token+'.json')
                    path.parent.mkdir(parents=True,exist_ok=True)
                    _mark(ledger,aid,indices,'preparing',path=path,attempt=attempt)
                    try:
                        if module is restore:
                            module.prepare_plan(aid,indices,source_path=source_path,cache_path=cache_path,
                                expected_source_sha256=row['source_sha256'],expected_cache_sha256=cache_sha,
                                state_db=args.state_db,fts_db=args.fts_db,rowmap_db=args.rowmap_db,journal_path=path,
                                qdrant=qdrant,embedder=embedder,embedding_recipe=embedding_recipe,source_route=route,
                                source_parser=lambda raw,requested_route:(parsed,elements))
                        else:
                            module.prepare_plan([],state_db=args.state_db,fts_db=args.fts_db,
                                cache_dir=args.cache_dir,section_cache_dir=args.section_cache_dir,
                                journal_path=path,qdrant=qdrant,
                                cached_additions=dict(chunk_indices=indices,source_path=source_path,
                                    expected_source_sha256=row['source_sha256'],expected_cache_sha256=cache_sha,
                                    source_route=route,rowmap_db=args.rowmap_db,arxiv_id=aid),
                                embedder=embedder,embedding_recipe=embedding_recipe,
                                source_parser=lambda raw,requested_route:(parsed,elements))
                        _mark(ledger,aid,indices,'applying',path=path,attempt=attempt)
                        with _maintenance(module,args):module.apply_plan(path,qdrant=qdrant)
                        module._verify(module._load(path),qdrant,'after')
                        _mark(ledger,aid,indices,'verified',path=path,attempt=attempt)
                        added+=len(indices);verified+=len(indices)
                    except Exception as exc:
                        _mark(ledger,aid,indices,'failed',path=path,detail=type(exc).__name__+': '+str(exc)[:300],attempt=attempt)
                        if path.exists()and module._load(path)['phase']in('applying','rolling_back'):
                            raise RecoveryRequired('started restore could not be compensated; keep maintenance')from exc
            except RecoveryRequired:
                raise
            except Exception as exc:
                remaining=[r[0]for r in ledger.execute("SELECT chunk_index FROM targets WHERE paper_id=?AND status='pending'",(aid,))]
                _mark(ledger,aid,remaining,'deferred',detail=type(exc).__name__+': '+str(exc)[:300])
        counts=dict(ledger.execute('SELECT status,COUNT(*)FROM targets GROUP BY status'))
        return dict(candidate_papers=len(candidates),candidate_points=sum(len(x[2])for x in candidates),
            added_this_run=added,verified_this_run=verified,verified_points=counts.get('verified',0),
            pending_points=sum(counts.get(k,0)for k in('pending','preparing','applying')),
            unresolved_points=sum(counts.get(k,0)for k in('failed','deferred')),statuses=counts,
            allowed_sources=None if allowed is None else sorted(allowed),
            scope='frozen currently eligible missing cached points; not whole corpus quality acceptance')
    finally:
        if ledger is not None:ledger.close()
        census.close();projection.close();rowmap.close()
