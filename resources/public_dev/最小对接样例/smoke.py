"""Offline public integration test; reference outputs are arithmetic fixtures only."""
import json,os,subprocess,sys,tempfile
from pathlib import Path
from artifact_hash import tree_digest
from contracts import read_rows,prediction_errors
from quality import score_annotations
from plugin_runner import run_plugin
ROOT=Path(__file__).resolve().parent;S=ROOT.parent
def main():
 with tempfile.TemporaryDirectory(prefix='audit-public-demo-') as tmp:
  tmp=Path(tmp);env={k:v for k,v in os.environ.items() if k not in ['OPENAI_BASE_URL','OPENAI_API_KEY']};env['PYTHONDONTWRITEBYTECODE']='1'
  def run(*args):subprocess.run([sys.executable,'-B',str(ROOT/'app.py'),*map(str,args)],check=True,cwd=ROOT,env=env)
  build=tmp/'baseline';sample=S/'03_公开开发集/标注样例';tasks=read_rows(sample/'inputs/tasks.jsonl')
  run('build','--rules',ROOT/'rules.jsonl','--config',ROOT/'config.json','--out',build)
  before=tree_digest(build)
  run('infer','--input',sample/'inputs/tasks.jsonl','--artifacts',build,'--out',tmp/'predictions.jsonl')
  preds=read_rows(tmp/'predictions.jsonl')
  assert len(preds)==len(tasks) and all(p['execution_status']=='error' and not prediction_errors(p,t) for p,t in zip(preds,tasks))
  run('annotate','--input',sample/'inputs/tasks.jsonl','--targets',sample/'inputs/targets.jsonl','--artifacts',build,'--out',tmp/'annotations.jsonl')
  assert len(json.loads((tmp/'annotations.jsonl.errors.json').read_text(encoding='utf-8')))==3
  packet=json.loads((ROOT/'feedback.example.json').read_text(encoding='utf-8'));packet['base_artifact_sha256']=before;(tmp/'feedback.json').write_text(json.dumps(packet),encoding='utf-8')
  run('iterate','--base',build,'--feedback',tmp/'feedback.json','--input',sample/'inputs/tasks.jsonl','--out',tmp/'candidate');assert tree_digest(build)==before
  run('evaluate','--plugin','plugins:conclusion','--inputs',S/'03_公开开发集/inputs/audit_inputs.jsonl','--outputs',S/'03_公开开发集/reference_predictions.jsonl','--references',S/'03_公开开发集/references/audit_gold.jsonl','--out',tmp/'plugin.json')
  assert json.loads((tmp/'plugin.json').read_text(encoding='utf-8'))['metrics']['quality']==1
  refs=read_rows(sample/'references/annotation_gold.jsonl')
  for f in ['reference_annotations.jsonl','reference_annotations_nfkc.jsonl']:assert score_annotations(refs,read_rows(sample/f),tasks)[0]==7
  expected=json.loads((S/'02_协议与评分/hash_fixture_expected.json').read_text(encoding='utf-8'));assert tree_digest(S/'02_协议与评分/hash_fixture')==expected['tree_sha256']
  print(json.dumps({'passed':True,'commands':['build','infer','annotate','iterate','evaluate'],'offline_model_errors_explicit':True,'reference_annotation_score':7,'nfkc_reference_annotation_score':7,'reference_audit_quality':1,'baseline_unchanged':True,'hash_vector_match':True,'official':False,'scope':'Interface/fixture arithmetic only; no model performance or product rubric claim'},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
