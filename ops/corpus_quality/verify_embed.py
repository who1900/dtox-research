"""Compare deployed passage embeddings with two existing 384D corpus vectors."""
import importlib.util
import json
from pathlib import Path

here=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('remote',here/'remote.py')
remote=importlib.util.module_from_spec(spec);spec.loader.exec_module(remote)
source=json.loads((remote.WORKSPACE/'evidence/corpus-quality-20261013/embedding-recipe-source.json').read_text())['embed-query']
recipe={'dimension':384,'document_prefix':'','pooling':'CLS/L2','max_length':512,
        'model_sha256':source['files']['/app/model/model_int8.onnx'],
        'tokenizer_sha256':source['files']['/app/model/tokenizer.json'],
        'app_sha256':source['app_sha256'],'endpoint':'http://127.0.0.1:18006/embed_batch'}
program='RECIPE='+repr(recipe)+'\n'+r'''
import json,math,pathlib,urllib.request
def post(url,body):
 req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=120) as r:return json.load(r)
samples=[]
for aid in ('2005.11401','acl:2026.wassa-1.1'):
 points=post('http://127.0.0.1:6333/collections/papers_fulltext/points/scroll',{'filter':{'must':[{'key':'arxiv_id','match':{'value':aid}}]},'limit':1,'with_payload':True,'with_vector':True})['result']['points']
 if len(points)!=1:raise RuntimeError('existing embedding comparison point absent')
 p=points[0];old=p['vector'];new=post(RECIPE['endpoint'],{'texts':[p['payload']['text']]})['vectors'][0]
 cosine=sum(x*y for x,y in zip(old,new))/(math.sqrt(sum(x*x for x in old))*math.sqrt(sum(x*x for x in new)))
 result={'paper_id':aid,'point_id':p['id'],'dimension':len(new),'cosine':cosine,'max_abs_difference':max(abs(x-y) for x,y in zip(old,new))}
 samples.append(result)
 if len(old)!=384 or len(new)!=384 or not all(math.isfinite(x) for x in new):raise RuntimeError('invalid deployed embedding output')
RECIPE['comparison_samples']=samples
RECIPE['historical_numerical_identity_established']=False
RECIPE['recipe_evidence']='deployed container source and model/tokenizer hashes; old vectors are comparison evidence only'
pathlib.Path('/opt/dtox-corpus-quality-20261013/embedding-recipe.json').write_text(json.dumps(RECIPE,indent=2)+'\n')
print(json.dumps(RECIPE))
'''
result=remote.execute(program,timeout=150)
(remote.WORKSPACE/'evidence/corpus-quality-20261013/embedding-recipe-verified.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps(result))
