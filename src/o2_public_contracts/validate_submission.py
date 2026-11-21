"""Local validation only; this tool cannot produce an official competition total."""
import argparse,json
from pathlib import Path
from contracts import read_rows,prediction_errors,annotation_errors
from quality import index_inputs,metric
p=argparse.ArgumentParser();p.add_argument('--inputs',required=True);p.add_argument('--predictions',required=True);p.add_argument('--references');p.add_argument('--out');a=p.parse_args()
tasks=read_rows(a.inputs,False);ins=index_inputs(tasks);preds=read_rows(a.predictions,False);details=[];seen={}
for row in preds:
    uid=row.get('audit_unit_id') if isinstance(row,dict) else None
    if not isinstance(uid,str) or uid not in ins:details.append({'audit_unit_id':uid,'errors':['unknown_or_invalid_id']});continue
    seen[uid]=seen.get(uid,0)+1;errors=prediction_errors(row,ins[uid])
    if errors:details.append({'audit_unit_id':uid,'errors':errors})
for uid,count in seen.items():
    if count>1:details.append({'audit_unit_id':uid,'errors':['duplicate_output']})
result={'protocol_version':'1.3.1','official':False,'total':None,'issues':details,'missing_ids':sorted(set(ins)-set(seen))}
if a.references:result['quality_diagnostic']=metric(read_rows(a.references,False),preds,tasks)
text=json.dumps(result,ensure_ascii=False,indent=2)
if a.out:Path(a.out).write_text(text+'\n',encoding='utf-8')
print(text)
