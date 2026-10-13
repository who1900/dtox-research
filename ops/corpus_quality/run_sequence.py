"""One-time continuation of the already authorized corpus repair classes."""
import fcntl,json,os,sqlite3
from pathlib import Path
import subprocess,sys,time

BASE=Path('/opt/dtox-corpus-quality-20261013')
def status(phase,**extra):
    value={'phase':phase,'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'whole_corpus_quality_complete':False,**extra}
    tmp=BASE/'sequence-status.tmp';tmp.write_text(json.dumps(value,indent=2)+'\n');tmp.replace(BASE/'sequence-status.json')
    print(json.dumps(value),flush=True)

def alive(pid):
    try:
        state=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()[0]
        return state!='Z'
    except FileNotFoundError:return False

def wait_existing(job,receipt,completion):
    pid=int((BASE/(job+'.pid')).read_text())
    while alive(pid):time.sleep(10)
    value=json.loads((BASE/receipt).read_text())
    if value.get('phase')!=completion:raise RuntimeError(job+' stopped without completion: '+str(value.get('phase')))
    return value

def launch(job,args,receipt,completion):
    python='/opt/dtox-extraction-eval-20261012/venv/bin/python' if '--mode' in args and 'points' in args else sys.executable
    with (BASE/(job+'.log')).open('ab') as log:
        proc=subprocess.Popen([python,'-B',str(BASE/'run_label_worker.py'),'--base',str(BASE),*args],cwd=BASE,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
    (BASE/(job+'.pid')).write_text(str(proc.pid))
    code=proc.wait()
    if code:raise RuntimeError(job+' failed with exit '+str(code))
    value=json.loads((BASE/receipt).read_text())
    if value.get('phase')!=completion:raise RuntimeError(job+' lacks completed receipt')
    return value


def retry_class(database, table, predicate, name, pending):
    """Requeue a diagnosed class once, retaining its previous ledger records."""
    conn=sqlite3.connect(BASE/database,timeout=60)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS repair_retries(name TEXT PRIMARY KEY,before_path TEXT NOT NULL,count INTEGER NOT NULL)')
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        prior=conn.execute('SELECT count FROM repair_retries WHERE name=?',(name,)).fetchone()
        if prior:
            conn.commit()
            return prior[0]
        rows=[dict(row)for row in conn.execute('SELECT * FROM '+table+' WHERE '+predicate)]
        path=BASE/(name+'-before.json')
        raw=(json.dumps(rows,ensure_ascii=False,indent=2)+'\n').encode()
        if path.exists():
            if path.read_bytes()!=raw:
                raise RuntimeError('retry before-image differs from current ledger')
        else:
            with path.open('xb')as out:
                out.write(raw);out.flush();os.fsync(out.fileno())
        conn.execute('UPDATE '+table+' SET status=? WHERE '+predicate,(pending,))
        conn.execute('INSERT INTO repair_retries VALUES(?,?,?)',(name,str(path),len(rows)))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def remainder():
    status('waiting_for_fts_ownership_reconciliation')
    catalog=wait_existing('catalog','catalog-worker-status.json','complete')
    selected=retry_class('label-run.sqlite','papers',
        "status='failed' AND detail IN ('RepairError: manifest/cache section mapping is ambiguous','RepairError: parent/live/cache payload or vector mismatch')",
        'remainder-v2-label-retry','retry_pending')
    retry_class('label-run.sqlite','papers',
        "status='failed' AND detail='DriverError: Qdrant points differ from complete frozen FTS point map'",
        'remainder-v2-label-catalog-retry','retry_pending')
    status('repairing_source_bound_labels',selected_papers=selected,catalog=catalog)
    labels=launch('labels',[],'label-worker-status.json','labels_complete')
    selected=retry_class('point-run.sqlite','targets',
        "status='deferred' AND detail IN ('cached target has no unique currently eligible exact source reproduction','published/managed/non-done paper requires another repair route')",
        'remainder-v2-point-retry','pending')
    retry_class('point-run.sqlite','targets',
        "status='failed' AND detail='RepairError: parent/live/cache payload or vector mismatch'",
        'remainder-v2-null-url-retry','pending')
    status('restoring_exact_source_intervals',selected_points=selected,label_outcomes=labels['paper_outcomes'])
    points=launch('points',['--mode','points'],'point-worker-status.json','points_complete')
    status('all_dispatched_repair_classes_processed',label_outcomes=labels['paper_outcomes'],
           point_outcomes=points['batch_result'],catalog=catalog,
           unresolved_source_quality='Unknown source text, editions, and parser drift remain explicit; not semantic quality acceptance')

def main():
    with (BASE/'sequence.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            if '--remainder' in sys.argv:
                remainder()
                return
            status('waiting_for_full_label_pass')
            labels=wait_existing('labels','label-worker-status.json','labels_complete')
            status('applying_source_anchored_partial_labels',full_label_outcomes=labels['paper_outcomes'])
            partial=launch('partial-labels',['--candidate-census',str(BASE/'partial-label-candidates.sqlite')],'partial-label-worker-status.json','labels_complete')
            status('waiting_for_full_identity_reconciliation')
            while alive(int((BASE/'coverage.pid').read_text())):time.sleep(10)
            if not json.loads((BASE/'identity-coverage.json').read_text()).get('complete'):raise RuntimeError('identity reconciliation incomplete')
            if (BASE/'points-latex.pid').exists():
                status('waiting_for_parallel_latex_point_pass')
                wait_existing('points-latex','point-latex-worker-status.json','points_complete')
            status('restoring_source_proven_missing_cached_points')
            points=launch('points',['--mode','points'],'point-worker-status.json','points_complete')
            status('all_dispatched_repair_classes_processed',label_outcomes=partial['paper_outcomes'],point_outcomes=points['batch_result'],
                   unresolved_source_quality='Unicode damage and source-unproven cases remain explicit; no semantic quality acceptance')
        except BaseException as exc:
            status('needs_attention',error=type(exc).__name__+': '+str(exc)[:300])
            raise

if __name__=='__main__':main()
