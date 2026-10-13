"""Restore the independently confirmed absent coarse point with the deployed recipe."""
import ast,importlib.util,json
from pathlib import Path

here=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('remote',here/'remote.py')
remote=importlib.util.module_from_spec(spec);spec.loader.exec_module(remote)
record=json.loads((remote.WORKSPACE/'evidence/corpus-quality-20261013/coarse-contabo-source-readonly.json').read_text())['/opt/dtox-research/coarse_index.py']
tree=ast.parse(record['source']);names={'coarse_point_id','source_for','sentence_truncate','build_embed_text','build_payload'}
helpers='\n\n'.join(ast.get_source_segment(record['source'],n) for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names)
program='HELPERS='+repr(helpers)+'\nRECIPE_SOURCE_SHA='+repr(record['sha256'])+'\n'+r'''
import hashlib,json,math,pathlib,sqlite3,types,urllib.request,uuid
base=pathlib.Path('/opt/dtox-corpus-quality-20261013');aid='1803.02084';collection='papers_coarse_base'
def request(path,body=None,method=None,origin='http://127.0.0.1:6333'):
 req=urllib.request.Request(origin+path,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'},method=method)
 with urllib.request.urlopen(req,timeout=120)as r:return json.load(r)
def result(path,body=None,method=None):
 value=request(path,body,method)
 if value.get('status')!='ok':raise RuntimeError('Qdrant non-ok response')
 return value['result']
namespace=dict(service=types.SimpleNamespace(uuid=uuid,NAMESPACE_URL=uuid.NAMESPACE_URL),COARSE_ID_PREFIX='coarse:',_ID_PREFIX_SOURCE=(),ABSTRACT_CHAR_LIMIT=800,MIN_ABSTRACT_LEN=80)
exec(compile(HELPERS,'verified-deployed-coarse-helpers','exec'),namespace)
state=sqlite3.connect(pathlib.Path('/opt/dtox-research/state.db').resolve().as_uri()+'?mode=ro',uri=True);state.row_factory=sqlite3.Row
row=state.execute("SELECT arxiv_id,title,abstract,layers,year,niche_score,citation_count,influential FROM papers WHERE arxiv_id=? AND status='done'",(aid,)).fetchone()
if row is None or len(row['abstract']or'')<80:raise RuntimeError('expected done paper with substantive abstract missing')
metadata=dict(row);text=namespace['build_embed_text'](row['title'],row['abstract'],aid)
pid=namespace['coarse_point_id'](aid);count=state.execute('SELECT COUNT(*)FROM citations WHERE dst=?',(aid,)).fetchone()[0]
payload=namespace['build_payload'](row,count)
spec=result('/collections/'+collection)
if spec['config']['params']['vectors']['size']!=768:raise RuntimeError('coarse collection dimension differs')
filter={'must':[{'key':'arxiv_id','match':{'value':aid}}]}
existing=result('/collections/'+collection+'/points/scroll',{'filter':filter,'limit':2,'with_payload':True,'with_vector':True})['points']
journal=base/'coarse-1803.02084.json'
if journal.exists():
 plan=json.loads(journal.read_text());point=plan['point']
 if point['payload']!=payload:raise RuntimeError('paper metadata changed since prepared journal')
else:
 if existing or result('/collections/'+collection+'/points',{'ids':[pid],'with_payload':False,'with_vector':False}):raise RuntimeError('coarse point unexpectedly exists')
 vector=request('/embed_batch',{'texts':[text]},origin='http://127.0.0.1:8090')['vectors'][0]
 if len(vector)!=768 or not all(math.isfinite(x)for x in vector)or abs(sum(x*x for x in vector)-1)>.0001:raise RuntimeError('invalid coarse vector')
 point={'id':pid,'vector':vector,'payload':payload}
 plan={'phase':'prepared','arxiv_id':aid,'point':point,'state_row_sha256':hashlib.sha256(json.dumps(metadata,sort_keys=True).encode()).hexdigest(),
       'embedding_text_sha256':hashlib.sha256(text.encode()).hexdigest(),'helper_source_sha256':RECIPE_SOURCE_SHA,
       'model_sha256':'c9729cc84cbd0e9fecc759505d2be65916c9fe05222d7ea26c65fcb3382af38d','document_recipe':'bge-base int8 768D CLS/L2 max384 no prefix; title+sentence-truncated abstract800'}
 journal.write_text(json.dumps(plan,indent=2)+'\n')
if existing:
 if len(existing)!=1 or str(existing[0]['id'])!=pid or existing[0]['payload']!=point['payload']:raise RuntimeError('coarse collision after prepare')
else:result('/collections/'+collection+'/points?wait=true',{'points':[point]},'PUT')
after=result('/collections/'+collection+'/points',{'ids':[pid],'with_payload':True,'with_vector':True})
if len(after)!=1 or after[0]['payload']!=payload or max(abs(x-y)for x,y in zip(after[0]['vector'],point['vector']))>1e-6:raise RuntimeError('coarse readback mismatch')
plan['phase']='verified';journal.write_text(json.dumps(plan,indent=2)+'\n')
print(json.dumps({'status':'verified','paper_id':aid,'point_id':pid,'collection':collection,'dimension':768,'collection_points':result('/collections/'+collection)['points_count'],'fts_and_state_writes':False}))
'''
result=remote.execute(program,timeout=150)
(remote.WORKSPACE/'evidence/corpus-quality-20261013/coarse-restore.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
