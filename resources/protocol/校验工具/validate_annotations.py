"""Public annotation validation and optional development scoring."""
import argparse,json
from contracts import read_rows,annotation_errors
from quality import score_annotations
def main():
 p=argparse.ArgumentParser();p.add_argument('--inputs',required=True);p.add_argument('--targets',required=True);p.add_argument('--predictions',required=True);p.add_argument('--references');a=p.parse_args()
 tasks=read_rows(a.inputs,False);byid={x['audit_unit_id']:x for x in tasks};targets=read_rows(a.targets,False);bt={x['annotation_id']:x for x in targets};preds=read_rows(a.predictions,False);seen=set();issues=[]
 for row in preds:
  aid=row.get('annotation_id') if isinstance(row,dict) else None
  if not isinstance(aid,str) or aid not in bt:issues.append({'id':aid,'errors':['unknown_target']});continue
  e=annotation_errors(row,bt[aid],byid[bt[aid]['audit_unit_id']])
  if aid in seen:e.append('duplicate_target')
  if e:issues.append({'id':aid,'errors':e})
  seen.add(aid)
 result={'official':False,'issues':issues,'missing':sorted(set(bt)-seen)}
 if a.references:result['score7'],result['details']=score_annotations(read_rows(a.references,False),preds,tasks)
 print(json.dumps(result,ensure_ascii=False,indent=2));return 1 if issues or result['missing'] else 0
if __name__=='__main__':raise SystemExit(main())
