"""Read the deployed document embedding recipe without exposing environment secrets."""
import json
from pathlib import Path
import subprocess

program = r'''
import json,subprocess
code="""
import hashlib,json,pathlib
p=pathlib.Path('/app/app.py');text=p.read_text()
print(json.dumps({'app':text,'app_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (pathlib.Path('/app/model/model_int8.onnx'),pathlib.Path('/app/model/tokenizer.json'))}}))
"""
result={}
for name in ('embed-query','embed-small'):
 p=subprocess.run(['docker','exec',name,'python','-B','-c',code],capture_output=True,text=True,check=True)
 result[name]=json.loads(p.stdout)
print(json.dumps(result))
'''
ssh=['ssh','-T','-i','C:/Users/User/.ssh/id_ed25519_claude_dtox','-o','BatchMode=yes','-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15','root@161.97.126.166','python3 -B -']
p=subprocess.run(ssh,input=program.encode(),capture_output=True,timeout=90,check=True)
answer=json.loads(p.stdout)
out=Path(__file__).resolve().parents[4]/'evidence/corpus-quality-20261013/embedding-recipe-source.json'
out.write_text(json.dumps(answer,indent=2)+'\n',encoding='utf-8')
print(json.dumps(answer))
