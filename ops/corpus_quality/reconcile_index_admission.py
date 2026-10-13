"""Withdraw audited receipt-owned noise through the existing publication owners."""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from ops import restore_cached_points as legacy, restore_corpus_points as driver
try:from ops.corpus_quality import audit_index_admission as audit
except ImportError:import audit_index_admission as audit


class RecoveryRequired(RuntimeError):
    pass


def now():return datetime.now(timezone.utc).isoformat()


def operational_ledger(path):
    conn=sqlite3.connect(path);conn.row_factory=sqlite3.Row
    conn.execute('''CREATE TABLE IF NOT EXISTS operations(
        operation_id TEXT PRIMARY KEY,paper_id TEXT UNIQUE NOT NULL,journal_path TEXT NOT NULL,
        owner TEXT NOT NULL,state TEXT NOT NULL,before_targets TEXT NOT NULL,
        policy_sha256 TEXT NOT NULL,detail TEXT,updated_at TEXT NOT NULL)''')
    conn.commit();return conn


def checkpoint(ops,operation_id,state,detail=None):
    ops.execute('UPDATE operations SET state=?,detail=?,updated_at=? WHERE operation_id=?',
        (state,detail,now(),operation_id));ops.commit()


def status(args,ops,phase,paper_id=None):
    payload=dict(phase=phase,paper_id=paper_id,updated_at=now(),
        operations=dict(ops.execute('SELECT state,COUNT(*) FROM operations GROUP BY state')))
    legacy.labels._atomic(args.status_file,legacy.labels._json_bytes(payload))
    return payload


@contextmanager
def maintenance(args,worker,aid):
    """The worker service gate, with durable preservation of prior active state."""
    marker=args.base/'index-admission-maintenance.json'
    prior=json.loads(marker.read_text()) if marker.exists() else None
    if prior and prior['phase']=='closed':
        if prior['paper_id']!=aid:raise RecoveryRequired('another withdrawal retains reader maintenance')
        active=prior['previously_active']
    else:active=worker.systemctl('show',worker.UNIT,'--value','--property=ActiveState')=='active'
    gate=dict(phase='closed',paper_id=aid,previously_active=active,safe=False)
    legacy.labels._atomic(marker,legacy.labels._json_bytes(gate))
    if worker.systemctl('show',worker.UNIT,'--value','--property=ActiveState')=='active':worker.systemctl('stop',worker.UNIT)
    try:
        yield gate
    finally:
        if gate['safe']:
            if active:worker.systemctl('start',worker.UNIT);worker.health()
            gate['phase']='restored';legacy.labels._atomic(marker,legacy.labels._json_bytes(gate))


def owner_for(operation):
    if operation['owner'] not in ('legacy','snapshot'):raise RecoveryRequired('withdrawal owner is not recorded')
    return legacy if operation['owner']=='legacy' else legacy.snapshots


def exact_after(module,plan,client):module._verify(plan,client,'after')


def mark_global(global_ledger,operation,plan):
    before=json.loads(operation['before_targets']);module=owner_for(operation)
    removed=plan['removed'] if module is legacy else plan['paper']['removed']
    receipts={r['point']['payload']['chunk_index']:r['receipt'] for r in removed}
    global_ledger.execute('BEGIN IMMEDIATE')
    try:
        for old in before:
            current=global_ledger.execute('SELECT * FROM targets WHERE paper_id=? AND chunk_index=?',
                (old['paper_id'],old['chunk_index'])).fetchone()
            if current is None:raise RecoveryRequired('withdrawal global target disappeared')
            current=dict(current)
            detail=dict(operation='receipt-withdrawal',operation_id=operation['operation_id'],
                origin_journal=old['journal_path'],origin_receipt=receipts[old['chunk_index']],
                policy_sha256=operation['policy_sha256'])
            expected=dict(old,status='excluded',attempt=old['attempt']+1,
                journal_path=operation['journal_path'],detail=json.dumps(detail,sort_keys=True))
            if current==expected:continue  # crash after the primary ledger commit
            if current!=old or current['status']!='verified':raise RecoveryRequired('withdrawal global target changed')
            changed=global_ledger.execute("UPDATE targets SET status='excluded',attempt=?,journal_path=?,detail=? WHERE paper_id=? AND chunk_index=? AND status='verified'",
                (expected['attempt'],expected['journal_path'],expected['detail'],old['paper_id'],old['chunk_index'])).rowcount
            if changed!=1:raise RecoveryRequired('withdrawal global target update lost')
        global_ledger.commit()
    except BaseException:global_ledger.rollback();raise


def verify_terminal(operation,global_ledger,fts_path,client):
    """Later accepted label repairs may supersede the captured after snapshot."""
    plan=owner_for(operation)._load(operation['journal_path'])
    removed=plan['removed'] if operation['owner']=='legacy' else plan['paper']['removed']
    ids=[r['point']['id'] for r in removed]
    expected={str(uuid.uuid5(uuid.NAMESPACE_URL,f'{r["paper_id"]}#{r["chunk_index"]}')) for r in json.loads(operation['before_targets'])}
    if plan.get('operation')!='receipt-withdrawal' or plan.get('phase')!='applied' or set(ids)!=expected:
        raise RecoveryRequired('terminal withdrawal journal identity changed')
    if legacy._retrieve(client,'papers_fulltext',ids):raise RecoveryRequired('excluded points became visible again')
    fts=legacy.labels._connect(fts_path,readonly=True)
    try:
        for r in removed:
            pid=r['point']['id']
            if legacy._fts_point(fts,pid) is not None or fts.execute('SELECT 1 FROM dtox_cached_point_receipts WHERE point_id=?',(pid,)).fetchone():
                raise RecoveryRequired('excluded point FTS/receipt reappeared')
    finally:fts.close()
    for old in json.loads(operation['before_targets']):
        row=global_ledger.execute('SELECT status,journal_path,detail FROM targets WHERE paper_id=? AND chunk_index=?',
            (old['paper_id'],old['chunk_index'])).fetchone()
        if row is None or row['status']!='excluded' or row['journal_path']!=operation['journal_path']:
            raise RecoveryRequired('excluded global target identity changed')
        if json.loads(row['detail']).get('operation_id')!=operation['operation_id']:
            raise RecoveryRequired('excluded global operation identity changed')


def finish_operation(args,ops,global_ledger,operation,client,worker,fault):
    module=owner_for(operation);path=operation['journal_path'];aid=operation['paper_id']
    if operation['state']=='excluded':
        verify_terminal(operation,global_ledger,args.fts_db,client)
        marker=args.base/'index-admission-maintenance.json'
        previous=json.loads(marker.read_text()) if marker.exists() else None
        if previous and previous['phase']=='closed' and previous['paper_id']==aid:
            with maintenance(args,worker,aid) as gate:gate['safe']=True
        return
    marker=args.base/'index-admission-maintenance.json'
    previous=json.loads(marker.read_text()) if marker.exists() else None
    retained=previous and previous['phase']=='closed' and previous['paper_id']==aid
    context=(nullcontext({'safe':False}) if module is legacy and not retained
        else maintenance(args,worker,aid))
    with context as gate:
        try:
            plan=module._load(path)
            if plan['phase'] in ('applying','rolling_back'):
                module.rollback_plan(path,qdrant=client,**({'maintenance':True} if module is not legacy else {}))
                checkpoint(ops,operation['operation_id'],'failed','interrupted primary operation compensated')
                gate['safe']=True;return
            if plan['phase']=='rolled_back':
                module._verify(plan,client,'before')
                checkpoint(ops,operation['operation_id'],'failed','primary operation already compensated')
                gate['safe']=True;return
            if plan['phase']=='prepared':
                for old in json.loads(operation['before_targets']):
                    row=global_ledger.execute('SELECT * FROM targets WHERE paper_id=? AND chunk_index=?',
                        (old['paper_id'],old['chunk_index'])).fetchone()
                    if row is None or dict(row)!=old or row['status']!='verified':
                        raise RecoveryRequired('global target changed before primary withdrawal')
                checkpoint(ops,operation['operation_id'],'applying')
                module.apply_plan(path,qdrant=client,**({'maintenance':True} if module is not legacy else {}))
                fault('after_owner_apply')
                plan=module._load(path)
            if plan['phase']!='applied':raise RecoveryRequired('unexpected primary withdrawal journal phase')
            exact_after(module,plan,client)
            checkpoint(ops,operation['operation_id'],'applied')
            mark_global(global_ledger,operation,plan)
            checkpoint(ops,operation['operation_id'],'excluded')
            gate['safe']=True
        except Exception as exc:
            # Once primary application is complete, preserve it and recover the
            # global ledger mark on the next run instead of undoing accepted work.
            current=module._load(path)
            if current['phase']=='applied':raise RecoveryRequired('applied withdrawal needs ledger reconciliation') from exc
            try:
                if current['phase'] in ('applying','rolling_back'):
                    module.rollback_plan(path,qdrant=client,**({'maintenance':True} if module is not legacy else {}))
                module._verify(module._load(path),client,'before')
            except BaseException as recovery:raise RecoveryRequired('withdrawal compensation unverified; keep readers closed') from recovery
            checkpoint(ops,operation['operation_id'],'failed',type(exc).__name__+': '+str(exc)[:300])
            gate['safe']=True


def prepare_operation(args,ops,audited,census,global_ledger,aid,client,policy):
    candidates=list(audited.execute("SELECT * FROM candidates WHERE paper_id=? AND ledger_status='verified' AND outcome='EXCLUDED' AND receipt_state='valid' AND source_occurrences=1 ORDER BY chunk_index",(aid,)))
    before=[]
    for candidate in candidates:
        row=global_ledger.execute('SELECT * FROM targets WHERE paper_id=? AND chunk_index=?',(aid,candidate['chunk_index'])).fetchone()
        if row is None or row['status']!='verified':raise RuntimeError('audited target is no longer verified')
        before.append(dict(row))
    if not before:raise RuntimeError('no uniquely source-proven excluded additions')
    opid=str(uuid.uuid4());journal=args.base/'index-admission-withdrawals'/(hashlib.sha256(aid.encode()).hexdigest()+'-'+opid+'.json')
    journal.parent.mkdir(exist_ok=True)
    ops.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',
        (opid,aid,str(journal),'unknown','preparing',json.dumps(before,sort_keys=True),policy,None,now()))
    ops.commit()
    try:return _prepare_recorded_operation(args,ops,audited,census,aid,client,policy,candidates,before,opid,journal)
    except Exception as exc:
        checkpoint(ops,opid,'failed',type(exc).__name__+': '+str(exc)[:300]);return None


def _prepare_recorded_operation(args,ops,audited,census,aid,client,policy,candidates,before,opid,journal):
    original=census.execute('SELECT * FROM papers WHERE paper_id=?',(aid,)).fetchone()
    if original is None:raise RuntimeError('original census document missing')
    cache_path,cache_sha,source_path,source,chunks,route=driver._inputs(dict(original),args)
    paper=audited.execute('SELECT * FROM papers WHERE paper_id=?',(aid,)).fetchone()
    if (paper is None or paper['cache_sha256']!=cache_sha or paper['source_sha256']!=legacy.labels._sha(source)
            or paper['policy_sha256']!=policy):raise RuntimeError('audit/current source/cache/policy binding changed')
    for candidate in candidates:
        chunk=chunks[candidate['chunk_index']]
        if (legacy.labels._sha(chunk['text'].encode())!=candidate['text_sha256']
                or chunk.get('element_type','prose')!=candidate['element_type']):raise RuntimeError('audited chunk identity changed')
    state=legacy.labels._connect(args.state_db,readonly=True)
    try:module=driver._publication_route(state,aid)
    finally:state.close()
    ids=[str(uuid.uuid5(uuid.NAMESPACE_URL,f'{aid}#{r["chunk_index"]}')) for r in before]
    ops.execute('UPDATE operations SET owner=? WHERE operation_id=?',('legacy' if module is legacy else 'snapshot',opid))
    ops.commit()
    try:
        module.prepare_withdrawal_plan(aid,ids,source_path=source_path,cache_path=cache_path,
            expected_source_sha256=paper['source_sha256'],expected_cache_sha256=cache_sha,
            state_db=args.state_db,fts_db=args.fts_db,rowmap_db=args.rowmap_db,journal_path=journal,qdrant=client,
            expected_policy_sha256=policy,origin_journals={pid:r['journal_path'] for pid,r in zip(ids,before)},
            source_route=route,section_cache_dir=args.section_cache_dir)
        checkpoint(ops,opid,'prepared')
    except Exception as exc:
        checkpoint(ops,opid,'failed',type(exc).__name__+': '+str(exc)[:300])
        return None
    return ops.execute('SELECT * FROM operations WHERE operation_id=?',(opid,)).fetchone()


def run(args,client,worker,*,fault=lambda phase:None):
    policy=audit.policy_identity()
    for unit in ('dtox-research.service','dtox-pipeline.service'):
        if worker.systemctl('show',unit,'--value','--property=ActiveState') not in ('inactive','failed'):
            raise RuntimeError('pipeline writer is active')
    if worker.systemctl('show',worker.UNIT,'--value','--property=ActiveState')=='active':worker.health()
    audited=census=None
    global_ledger=sqlite3.connect(Path(args.point_run_db).resolve().as_uri()+'?mode=rw',uri=True)
    global_ledger.row_factory=sqlite3.Row;ops=operational_ledger(args.operations_db)
    current_aid=None
    try:
        status(args,ops,'recovering')
        # Always recover this controller's own operations before selecting new work.
        for operation in list(ops.execute("SELECT * FROM operations WHERE state!='failed' ORDER BY updated_at")):
            current_aid=operation['paper_id']
            if operation['state']=='preparing' and not Path(operation['journal_path']).exists():
                checkpoint(ops,operation['operation_id'],'failed','preparation interrupted before primary journal');continue
            finish_operation(args,ops,global_ledger,operation,client,worker,fault)
        audited=audit.connect_ro(args.audit_db);census=audit.connect_ro(args.census_db)
        meta=dict(audited.execute('SELECT key,value FROM meta'))
        scoped=bool(args.paper or args.canary)
        if not (meta.get('full_run_complete')=='1' or scoped and meta.get('scoped_run_complete')=='1'):
            raise RuntimeError('completed admission audit required')
        if not scoped and audited.execute('SELECT COUNT(*) FROM candidates').fetchone()[0]!=93307:
            raise RuntimeError('full frozen 93307-candidate admission audit required')
        papers=[r[0] for r in audited.execute("SELECT DISTINCT paper_id FROM candidates WHERE ledger_status='verified' AND outcome='EXCLUDED' AND receipt_state='valid' AND source_occurrences=1 ORDER BY paper_id")]
        if args.paper:papers=[aid for aid in papers if aid in set(args.paper)]
        dispatched=0
        for aid in papers:
            if ops.execute('SELECT 1 FROM operations WHERE paper_id=?',(aid,)).fetchone():continue
            if args.max_papers is not None and dispatched>=args.max_papers:break
            current_aid=aid
            for unit in ('dtox-research.service','dtox-pipeline.service'):
                if worker.systemctl('show',unit,'--value','--property=ActiveState') not in ('inactive','failed'):
                    raise RecoveryRequired('pipeline writer started during withdrawal')
            if worker.systemctl('show',worker.UNIT,'--value','--property=ActiveState')=='active':worker.health()
            status(args,ops,'preparing',aid)
            try:operation=prepare_operation(args,ops,audited,census,global_ledger,aid,client,policy)
            except Exception as exc:
                opid=str(uuid.uuid4())
                ops.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)',
                    (opid,aid,'','unknown','failed','[]',policy,type(exc).__name__+': '+str(exc)[:300],now()));ops.commit()
                dispatched+=1;continue
            if operation is not None:
                status(args,ops,'applying',aid)
                finish_operation(args,ops,global_ledger,operation,client,worker,fault)
            dispatched+=1;status(args,ops,'running',aid)
        return status(args,ops,'finished')
    except BaseException as exc:
        if isinstance(exc,RecoveryRequired):
            marker=args.base/'index-admission-maintenance.json'
            previous=json.loads(marker.read_text()) if marker.exists() else None
            active=worker.systemctl('show',worker.UNIT,'--value','--property=ActiveState')=='active'
            if not previous or previous['phase']!='closed':
                legacy.labels._atomic(marker,legacy.labels._json_bytes(dict(phase='closed',paper_id=current_aid,
                    previously_active=active,safe=False)))
            if active:worker.systemctl('stop',worker.UNIT)
        status(args,ops,'attention_required');raise
    finally:
        if audited is not None:audited.close()
        if census is not None:census.close()
        global_ledger.close();ops.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=Path('/opt/dtox-corpus-quality-20261013'))
    parser.add_argument('--root',type=Path,default=Path('/opt/dtox-research'))
    parser.add_argument('--paper',action='append')
    parser.add_argument('--max-papers',type=int)
    parser.add_argument('--canary',action='store_true')
    for option in ('audit-db','census-db','point-run-db','rowmap-db','label-run-db','operations-db','status-file'):
        parser.add_argument('--'+option,type=Path)
    args=parser.parse_args();args.base=args.base.resolve();args.root=args.root.resolve()
    defaults=dict(audit_db='index-admission-audit.sqlite',census_db='census.sqlite',point_run_db='point-run.sqlite',
        rowmap_db='fts-rowmap.sqlite',label_run_db='label-run.sqlite',operations_db='index-admission-withdrawals.sqlite',status_file='index-admission-withdrawal-status.json')
    for key,name in defaults.items():setattr(args,key,(getattr(args,key) or args.base/name).resolve())
    for key,name in dict(state_db='state.db',fts_db='fts.db',cache_dir='chunk_cache',section_cache_dir='section_cache',
            fulltext_cache_dir='fulltext_cache',latex_cache_dir='latex_cache').items():setattr(args,key,(args.root/name).resolve())
    if args.max_papers is not None and args.max_papers<1:parser.error('--max-papers must be positive')
    if args.canary and not (args.paper or args.max_papers):parser.error('--canary requires --paper or --max-papers')
    try:from ops.corpus_quality import run_label_worker as worker
    except ImportError:import run_label_worker as worker
    import fcntl
    from contextlib import ExitStack
    with ExitStack() as locks:
        for name in ('points-worker.lock','labels-worker.lock','index-admission-withdrawals.lock'):
            handle=locks.enter_context((args.base/name).open('a'));fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        print(json.dumps(run(args,worker.Qdrant(),worker)))


if __name__=='__main__':main()
