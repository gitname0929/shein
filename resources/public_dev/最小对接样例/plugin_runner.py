"""Process isolation for public plugin adapters. No network/kernel isolation claim."""
import importlib,json,subprocess,sys
from pathlib import Path
def run_plugin(entry,inputs,outputs,references,context,timeout=None):
 payload={'entry':entry,'inputs':inputs,'outputs':outputs,'references':references,'context':context}
 try:
  r=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve()),'--worker'],input=json.dumps(payload,ensure_ascii=False),text=True,encoding='utf-8',capture_output=True,timeout=timeout)
  if r.returncode:return {'metrics':{},'diagnostics':[],'errors':[{'code':'plugin_failed','detail':r.stderr[-1000:]}]}
  obj=json.loads(r.stdout)
  if not isinstance(obj,dict) or not isinstance(obj.get('metrics'),dict):raise ValueError('Invalid plugin report')
  return obj
 except subprocess.TimeoutExpired:return {'metrics':{},'diagnostics':[],'errors':[{'code':'plugin_timeout'}]}
 except (ValueError,TypeError):return {'metrics':{},'diagnostics':[],'errors':[{'code':'plugin_invalid_output'}]}
def worker():
 sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
 p=json.load(sys.stdin);module,name=p['entry'].rsplit(':',1);fn=getattr(importlib.import_module(module),name)
 args=[p[k] for k in ['inputs','outputs','references','context']];before=json.dumps(args,sort_keys=True)
 result=fn(*args)
 if json.dumps(args,sort_keys=True)!=before:raise ValueError('Plugin mutated read-only input objects')
 print(json.dumps(result,ensure_ascii=False,allow_nan=False))
if __name__=='__main__':worker()
