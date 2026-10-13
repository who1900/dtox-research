"""Compact read-only primary repair receipt."""
import importlib.util,json
from pathlib import Path
here=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('remote',here/'remote.py')
remote=importlib.util.module_from_spec(spec);spec.loader.exec_module(remote)
result=remote.execute(r'''
import ast,hashlib,json,pathlib,shutil,sqlite3,subprocess,time,urllib.request
root=pathlib.Path('/opt/dtox-research');base=pathlib.Path('/opt/dtox-corpus-quality-20261013')
p=pathlib.Path('/opt/dtox-research-api/document_cache.py');source=p.read_text();tree=ast.parse(source)
answer={'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'free_bytes':shutil.disk_usage(root).free,
 'document_cache_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'document_cache_safe_id':next(ast.get_source_segment(source,n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='safe_id')}
try:
 with urllib.request.urlopen('http://127.0.0.1:8010/v1/health',timeout=3) as r:answer['api_health']=json.load(r)
except Exception as exc:answer['api_health_error']=type(exc).__name__
if (base/'label-run.sqlite').exists():
 c=sqlite3.connect((base/'label-run.sqlite').as_uri()+'?mode=ro',uri=True)
 answer['label_outcomes']=dict(c.execute('SELECT status,COUNT(*) FROM papers GROUP BY status'))
 answer['label_failures']=[list(r) for r in c.execute("SELECT paper_id,detail FROM papers WHERE status!='verified' LIMIT 20")]
 c.close()
for name in ('projection','headings','labels','coverage'):
 p=base/(name+'.pid')
 if p.exists():answer[name+'_pid']=int(p.read_text())
print(json.dumps(answer))
''')
(remote.WORKSPACE/'evidence/corpus-quality-20261013/primary-current.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k!='document_cache_safe_id'},ensure_ascii=False))
