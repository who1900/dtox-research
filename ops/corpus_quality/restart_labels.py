"""Reload the worker at a verified batch boundary, preserving its journal ledger."""
import importlib.util
import json
from pathlib import Path
here=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('remote',here/'remote.py')
remote=importlib.util.module_from_spec(spec);spec.loader.exec_module(remote)
result=remote.execute(r'''
import json,os,pathlib,signal,subprocess,sys,time
base=pathlib.Path('/opt/dtox-corpus-quality-20261013');pid=int((base/'labels.pid').read_text())
if b'run_label_worker.py' not in pathlib.Path('/proc/'+str(pid)+'/cmdline').read_bytes():raise RuntimeError('worker PID identity mismatch')
deadline=time.monotonic()+90
while time.monotonic()<deadline:
 if subprocess.check_output(['systemctl','show','dtox-research-api-oracle-20261012.service','--value','--property=ActiveState'],text=True).strip()=='active':
  os.kill(pid,signal.SIGSTOP)
  state=subprocess.check_output(['systemctl','show','dtox-research-api-oracle-20261012.service','--value','--property=ActiveState'],text=True).strip()
  if state=='active':
   os.kill(pid,signal.SIGTERM);os.kill(pid,signal.SIGCONT);break
  os.kill(pid,signal.SIGCONT)
 time.sleep(.15)
else:raise RuntimeError('no verified online batch boundary; old worker retained')
for _ in range(50):
 try:os.kill(pid,0)
 except ProcessLookupError:break
 time.sleep(.1)
else:raise RuntimeError('worker did not terminate')
with (base/'labels.log').open('ab') as log:
 proc=subprocess.Popen([sys.executable,'-B',str(base/'run_label_worker.py'),'--base',str(base)],cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
(base/'labels.pid').write_text(str(proc.pid))
print(json.dumps({'old_pid':pid,'new_pid':proc.pid,'restart_at_verified_online_boundary':True}))
''',timeout=110)
(remote.WORKSPACE/'evidence/corpus-quality-20261013/worker-online-restart.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
