"""Stage and run the authorized corpus repair on its existing Oracle primary."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO.parent.parent
REMOTE = '/opt/dtox-corpus-quality-20261013'
SSH = ['ssh', '-T', '-i', 'C:/Users/User/.ssh/id_ed25519_claude_dtox',
       '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ConnectTimeout=15', 'ubuntu@92.5.127.67', 'sudo -n python3 -B -']


def execute(program, timeout=60):
    proc = subprocess.run(SSH, input=program.encode(), capture_output=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode(errors='replace')[-3000:])
    return json.loads(proc.stdout)


def stage():
    names = ['ops/audit_corpus_quality.py', 'ops/repair_section_labels.py',
             'pipeline/document_cache.py', 'pipeline/extractor.py', 'pipeline/pdf_memory.py',
             'api/boilerplate.py']
    names += [name for name in ['ops/repair_corpus_labels.py', 'ops/replace_published_labels.py', 'ops/audit_projection_coverage.py', 'ops/restore_cached_points.py', 'ops/source_heading_labels.py', 'ops/extend_heading_candidates.py', 'ops/restore_corpus_points.py', 'ops/build_partial_heading_candidates.py',
                                'ops/corpus_quality/prepare_label_boundaries.py',
                                'ops/corpus_quality/audit_index_admission.py',
                                'ops/corpus_quality/adopt_orphan_documents.py',
                                'ops/corpus_quality/reconcile_index_admission.py',
                                'ops/corpus_quality/run_label_worker.py',
                                'ops/corpus_quality/fresh_sources.py',
                                'ops/corpus_quality/replace_documents.py',
                                'pipeline/structured_html.py', 'pipeline/pdf_unicode.py',
                                'pipeline/pdfbox_additional_glyphlist.txt', 'pipeline/PDFBOX_GLYPHLIST_LICENSE.txt',
                                'pipeline/service.py', 'pipeline/originals.py',
                                'ops/restore_cache_tails.py', 'pipeline/publication.py',
                                'pipeline/payload_contract.py', 'pipeline/fts_catalog.py',
                                'ops/reconcile_fts_catalog.py'] if (REPO / name).is_file()]
    files = {name: base64.b64encode((REPO / name).read_bytes().replace(b'\r\n', b'\n')).decode() for name in names}
    files.update({'build_fts_rowmap.py': base64.b64encode((Path(__file__).parent / 'build_fts_rowmap.py').read_bytes().replace(b'\r\n', b'\n')).decode(),
                  'run_label_worker.py': base64.b64encode((Path(__file__).parent / 'run_label_worker.py').read_bytes().replace(b'\r\n', b'\n')).decode(),
                  'audit_projection_ids.py': base64.b64encode((Path(__file__).parent / 'audit_projection_ids.py').read_bytes().replace(b'\r\n', b'\n')).decode(),
                  'run_sequence.py': base64.b64encode((Path(__file__).parent / 'run_sequence.py').read_bytes().replace(b'\r\n', b'\n')).decode(),
                  'ops/__init__.py': '', 'pipeline/__init__.py': '', 'api/__init__.py': ''})
    program = 'FILES = ' + repr(files) + '\nBASE = ' + repr(REMOTE) + '\n' + r'''
import base64, hashlib, json, pathlib
root=pathlib.Path(BASE); root.mkdir(exist_ok=True,mode=0o700)
result={}
for name,data in FILES.items():
    path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
    raw=base64.b64decode(data);path.write_bytes(raw);result[name]=hashlib.sha256(raw).hexdigest()
print(json.dumps(result))
'''
    return execute(program)


def start(job):
    program = 'BASE = ' + repr(REMOTE) + '\nJOB = ' + repr(job) + '\n' + r'''
import json, os, pathlib, subprocess, sys
root=pathlib.Path(BASE);data=pathlib.Path('/opt/dtox-research')
pidfile=root/(JOB+'.pid')
if pidfile.exists():
    pid=int(pidfile.read_text())
    try:os.kill(pid,0)
    except ProcessLookupError:pass
    else:raise RuntimeError('job already running; inspect status instead')
if JOB=='census':
    cmd=[sys.executable,'-B','-m','ops.audit_corpus_quality','--data-dir',str(data),'--chunk-dir',str((data/'chunk_cache').resolve()),'--source-dir',str((data/'fulltext_cache').resolve()),'--report',str(root/'census.sqlite')]
elif JOB=='fts-rowmap':
    cmd=[sys.executable,'-B',str(root/'build_fts_rowmap.py'),'--source',str((data/'fts.db').resolve()),'--output',str(root/'fts-rowmap.sqlite')]
elif JOB=='projection':
    cmd=[sys.executable,'-B','-m','ops.audit_projection_coverage','--data-dir',str(data),'--census',str(root/'census.sqlite'),'--rowmap',str(root/'fts-rowmap.sqlite'),'--report',str(root/'projection.sqlite'),'--chunk-dir',str((data/'chunk_cache').resolve())]
elif JOB=='headings':
    cmd=[sys.executable,'-B','-m','ops.extend_heading_candidates','--census',str(root/'census.sqlite'),'--chunk-dir',str((data/'chunk_cache').resolve()),'--fulltext-cache-dir',str((data/'fulltext_cache').resolve()),'--latex-cache-dir',str((data/'latex_cache').resolve())]
elif JOB=='labels':
    cmd=[sys.executable,'-B',str(root/'run_label_worker.py'),'--base',str(root)]
elif JOB=='points':
    cmd=['/opt/dtox-extraction-eval-20261012/venv/bin/python','-B',str(root/'run_label_worker.py'),'--base',str(root),'--mode','points']
elif JOB=='coverage':
    cmd=[sys.executable,'-B',str(root/'audit_projection_ids.py')]
elif JOB=='partial-headings':
    cmd=[sys.executable,'-B','-m','ops.build_partial_heading_candidates','--census',str(root/'census.sqlite'),'--report',str(root/'partial-label-candidates.sqlite'),'--chunk-dir',str((data/'chunk_cache').resolve()),'--fulltext-cache-dir',str((data/'fulltext_cache').resolve()),'--latex-cache-dir',str((data/'latex_cache').resolve())]
elif JOB=='partial-labels':
    cmd=[sys.executable,'-B',str(root/'run_label_worker.py'),'--base',str(root),'--candidate-census',str(root/'partial-label-candidates.sqlite')]
elif JOB=='sequence':
    cmd=[sys.executable,'-B',str(root/'run_sequence.py')]
elif JOB=='remainder':
    cmd=[sys.executable,'-B',str(root/'run_sequence.py'),'--remainder']
elif JOB=='points-latex':
    cmd=['/opt/dtox-extraction-eval-20261012/venv/bin/python','-B',str(root/'run_label_worker.py'),'--base',str(root),'--mode','points','--sources','latex']
else:raise RuntimeError('invalid job')
with (root/(JOB+'.log')).open('ab') as log:
    proc=subprocess.Popen(cmd,cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
pidfile.write_text(str(proc.pid))
print(json.dumps({'job':JOB,'pid':proc.pid,'command':cmd,'corpus_writes':JOB in ('labels','points','points-latex','partial-labels','sequence','remainder')}))
'''
    return execute(program)


def status():
    program = 'BASE = ' + repr(REMOTE) + '\n' + r'''
import json, os, pathlib, sqlite3
root=pathlib.Path(BASE);result={}
for job in ('census','fts-rowmap','headings','projection','labels','points','points-latex','coverage','partial-headings','partial-labels','catalog','sequence','remainder'):
    p=root/(job+'.pid');item={}
    if p.exists():
        pid=int(p.read_text());item['pid']=pid
        try:os.kill(pid,0);item['process_exists']=True
        except ProcessLookupError:item['process_exists']=False
    log=root/(job+'.log')
    if log.exists():
        with log.open('rb') as f:f.seek(max(0,log.stat().st_size-1200));item['log_tail']=f.read().decode(errors='replace')
    if job in ('labels','points','points-latex','partial-labels'):
        prefix={'labels':'label','points':'point','partial-labels':'partial-label','points-latex':'point-latex'}[job]
        status=root/(prefix+'-worker-status.json')
        if status.exists():item['status']=json.loads(status.read_text())
        fatal=root/(prefix+'-worker-fatal.json')
        if fatal.exists():item['fatal']=json.loads(fatal.read_text())
        result[job]=item
        continue
    if job=='coverage':
        status=root/'identity-coverage.json'
        if status.exists():item['status']=json.loads(status.read_text())
        result[job]=item
        continue
    if job in ('sequence','remainder','catalog'):
        status=root/('catalog-worker-status.json' if job=='catalog' else 'sequence-status.json')
        if status.exists():item['status']=json.loads(status.read_text())
        result[job]=item
        continue
    db=root/('partial-label-candidates.sqlite' if job=='partial-headings' else 'census.sqlite' if job=='headings' else job+'.sqlite')
    if db.exists():
        c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=2)
        try:
            item['meta']=dict(c.execute('SELECT key,value FROM meta'))
            if job=='census':
                item['papers']=c.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
                item['candidates']=dict(c.execute('SELECT candidate_status,COUNT(*) FROM papers GROUP BY candidate_status'))
            elif job=='projection':
                item['papers']=c.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
                item['projection_statuses']=dict(c.execute('SELECT projection_status,COUNT(*) FROM papers GROUP BY projection_status'))
        except sqlite3.Error as exc:item['db_initializing']=str(exc)
        c.close()
    result[job]=item
for job in ('index-admission-audit','index-admission-sequence','verified-unresolved-label'):
    item={};pidfile=root/(job+'.pid')
    if pidfile.exists():
        pid=int(pidfile.read_text());item['pid']=pid
        try:os.kill(pid,0);item['process_exists']=True
        except ProcessLookupError:item['process_exists']=False
    statusfile=root/(job+('-worker-status.json' if job=='verified-unresolved-label' else '-status.json'))
    if statusfile.exists():item['status']=json.loads(statusfile.read_text())
    log=root/(job+'.log')
    if log.exists():
        with log.open('rb') as f:f.seek(max(0,log.stat().st_size-1200));item['log_tail']=f.read().decode(errors='replace')
    if job=='index-admission-audit':
        db=root/(job+'.sqlite')
        if db.exists():
            c=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True,timeout=2)
            try:
                item['meta']=dict(c.execute('SELECT key,value FROM meta'))
                item['papers']=c.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
                item['candidates']=[list(row) for row in c.execute('SELECT ledger_status,outcome,receipt_state,COUNT(*) FROM candidates GROUP BY 1,2,3')]
            finally:c.close()
    result[job]=item
statusfile=root/'index-admission-withdrawal-status.json'
if statusfile.exists():result['index-admission-withdrawals']={'status':json.loads(statusfile.read_text())}
replacement=root/'whole-document-replacement-20261014'
if (replacement/'whole-document-replacement.sqlite').exists():
    import sys
    sys.path.insert(0,str(root))
    from ops.corpus_quality import replace_documents
    item={'status':replace_documents.status_snapshot(replacement)}
    controller=replacement/'bulk-controller-status.json'
    if controller.exists():item['controller']=json.loads(controller.read_text())
    pidfile=replacement/'bulk-controller.pid'
    if pidfile.exists():
        pid=int(pidfile.read_text());item['pid']=pid
        proc=pathlib.Path('/proc')/str(pid)/'stat'
        item['process_exists']=proc.exists() and proc.read_text().split()[2]!='Z'
    result['whole-document-replacement']=item
print(json.dumps(result))
'''
    return execute(program)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['stage', 'start-census', 'start-rowmap', 'start-headings', 'start-projection', 'start-labels', 'start-points', 'start-latex-points', 'start-coverage', 'start-partial-headings', 'start-partial-labels', 'start-sequence', 'start-remainder', 'status'])
    args = p.parse_args()
    if args.mode == 'stage': result = stage()
    elif args.mode == 'status': result = status()
    else: result = start({'start-census':'census','start-rowmap':'fts-rowmap','start-headings':'headings','start-projection':'projection','start-labels':'labels','start-points':'points','start-latex-points':'points-latex','start-coverage':'coverage','start-partial-headings':'partial-headings','start-partial-labels':'partial-labels','start-sequence':'sequence','start-remainder':'remainder'}[args.mode])
    out=WORKSPACE/'evidence/corpus-quality-20261013';out.mkdir(exist_ok=True)
    (out/(args.mode+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    if args.mode=='status':
        compact={key:{name:value for name,value in item.items() if name not in ('meta','log_tail')} for key,item in result.items()}
        print(json.dumps(compact,ensure_ascii=False))
    else:print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__': main()
