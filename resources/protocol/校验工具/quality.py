from normalization import annotation_values_equal
"""Protocol 1.3: correct means legal output AND label AND applicability correct."""
from collections import defaultdict
from contracts import *

def index_inputs(tasks):
    out={}
    for t in tasks:
        errors=input_errors(t)
        if errors:raise ContractError('Invalid organizer input: '+str(errors))
        uid=t['audit_unit_id']
        if uid in out:raise ContractError('Duplicate organizer input ID')
        out[uid]=t
    return out

def metric(refs,preds,tasks,artifact_versions=None):
    ins=index_inputs(tasks) if isinstance(tasks,list) else tasks
    byid={};groups=defaultdict(list);extraneous=0;unidentified=0
    for r in refs:
        if not isinstance(r,dict) or not nonempty(r.get('audit_unit_id'),256) or r.get('label') not in LABELS or r.get('applicability') not in APPLICABILITY:raise ContractError('Invalid reference')
        uid=r['audit_unit_id']
        if uid in byid or uid not in ins:raise ContractError('Reference/input mismatch or duplicate')
        if any(r[k]!=ins[uid][k] for k in ['rule_id','rule_version']):raise ContractError('Reference rule/version mismatch')
        byid[uid]=r
    for p in preds:
        if not isinstance(p,dict) or not nonempty(p.get('audit_unit_id'),256):unidentified+=1;continue
        if p['audit_unit_id'] not in byid:extraneous+=1;continue
        groups[p['audit_unit_id']].append(p)
    c={'all':len(refs),'correct':0,'label_only_correct':0,'applicability_correct':0,'binary_correct':0,'binary_truth':sum(r['label'] in ('pass','reject') for r in refs),'pass':0,'reject':0,'ambiguous':0,'error':0,'invalid':0,'missing':0,'extraneous':extraneous,'unidentified_invalid_records':unidentified}
    outcomes={};correct=[];issues={};confusion={truth:{x:0 for x in LABELS+('error','invalid','missing')} for truth in LABELS}
    for uid,r in byid.items():
        rows=groups[uid];errors=[]
        if not rows:out='missing'
        elif len(rows)>1:out='invalid';errors=['duplicate_output']
        else:
            expected=None
            if artifact_versions is not None:
                expected=artifact_versions.get(r['rule_id']+'@'+r['rule_version'])
                if expected is None:errors=['rule_artifact_not_built']
            errors+=prediction_errors(rows[0],ins[uid],expected)
            out='invalid' if errors else 'error' if rows[0]['execution_status']=='error' else rows[0]['label']
        outcomes[uid]=out;c[out]+=1;confusion[r['label']][out]+=1
        if errors:issues[uid]=errors
        if out in LABELS:
            label_ok=out==r['label'];app_ok=rows[0]['applicability']==r['applicability']
            c['label_only_correct']+=label_ok;c['applicability_correct']+=app_ok
            if label_ok and app_ok:
                c['correct']+=1;correct.append(uid)
                if out in ['pass','reject']:c['binary_correct']+=1
    n=c['all'];count_binary=c['pass']+c['reject'];a=c['correct']/n if n else None
    u=c['binary_correct']/c['binary_truth'] if c['binary_truth'] else a
    c.update(accuracy=a,label_only_accuracy=c['label_only_correct']/n if n else None,applicability_accuracy=c['applicability_correct']/n if n else None,machine_review_rate=count_binary/n if n else None,machine_accuracy=c['binary_correct']/count_binary if count_binary else None,correct_decisive_coverage=u,quality=.7*a+.3*u if n else None,correct_ids=correct,outcomes=outcomes,issues=issues,label_confusion_matrix=confusion,protocol_version='1.3.1')
    return c

def score_annotations(refs,preds,tasks):
    ins=index_inputs(tasks) if isinstance(tasks,list) else tasks;g=defaultdict(list)
    for p in preds:
        if isinstance(p,dict) and nonempty(p.get('annotation_id'),256):g[p['annotation_id']].append(p)
    details=[];seen=set()
    for r in refs:
        aid=r['annotation_id']
        if aid in seen or r['audit_unit_id'] not in ins:raise ContractError('Bad annotation references')
        seen.add(aid);rows=g[aid];ok=False;issues=[]
        if len(rows)==1:
            p=rows[0];issues=annotation_errors(p,r,ins[r['audit_unit_id']])
            ok=not issues and p['page_index']==r['page_index'] and p['label']==r['expected_label'] and p['status']==r['expected_status'] and annotation_values_equal(p['value'],r['expected_value']) and iou(p['bbox'],r['expected_bbox'])>=r['bbox_iou_min']
        else:issues=['missing_or_duplicate_annotation']
        details.append({'annotation_id':aid,'correct':bool(ok),'issues':issues})
    if not refs:raise ContractError('Empty annotation reference group')
    return 7*sum(d['correct'] for d in details)/len(refs),details

def contrasts(refs,preds,tasks,groups,artifact_versions=None):
    m=metric(refs,preds,tasks,artifact_versions);correct=set(m['correct_ids']);byid={r['audit_unit_id']:r for r in refs};buckets=defaultdict(list);details=[];seen=set()
    for group in groups:
        gid=group['group_id'];members=group['members']
        if gid in seen or not isinstance(members,list) or len(members)<2 or any(not isinstance(u,str) for u in members) or len(set(members))!=len(members):raise ContractError('Invalid contrast group')
        seen.add(gid)
        if any(uid not in byid or byid[uid]['rule_id']!=group['rule_id'] for uid in members):raise ContractError('Contrast member mismatch')
        ok=all(uid in correct for uid in members);buckets[group['rule_id']].append(ok);details.append({'group_id':gid,'correct':ok})
    return {k:sum(v)/len(v) for k,v in buckets.items()},details

def quality_parts(refs,preds,tasks,groups,artifact_versions=None):
    base={v:metric([r for r in refs if r['rule_id']=='B001' and r['rule_version']==v],preds,tasks,artifact_versions) for v in ['3.0','3.1']}
    adv={f'R{i:03}':metric([r for r in refs if r['rule_id']==f'R{i:03}'],preds,tasks,artifact_versions) for i in range(1,61)}
    if not all(v['all'] for v in list(base.values())+list(adv.values())):raise ContractError('Empty frozen quality group')
    cs,cd=contrasts(refs,preds,tasks,groups,artifact_versions)
    if set(cs)!={'B001'}|set(adv):raise ContractError('Missing rule contrasts')
    b=16*sum(x['quality'] for x in base.values())/2+4*cs['B001'];a=16*sum(x['quality'] for x in adv.values())/60+4*sum(cs[r] for r in adv)/60
    return {'base_quality':b,'advanced_quality':a,'quality40':b+a,'base':base,'advanced':adv,'contrasts':cs,'contrast_details':cd,'micro':metric(refs,preds,tasks,artifact_versions)}

def gate(repair_refs,repair_tasks,protected_refs,protected_tasks,outputs,versions=None):
    detail={};new_failures=[];complete=True
    for name,refs,tasks in [('repair',repair_refs,repair_tasks),('protected',protected_refs,protected_tasks)]:
        if not refs:raise ContractError('Missing protected/repair references')
        old=metric(refs,outputs.get('baseline_'+name,[]),tasks,(versions or {}).get('baseline'));new=metric(refs,outputs.get('candidate_'+name,[]),tasks,(versions or {}).get('candidate'))
        errors=sorted(set(old['correct_ids'])-set(new['correct_ids']));complete &= all(m['invalid']==0 and m['missing']==0 for m in [old,new])
        new_failures += [uid for uid,o in new['outcomes'].items() if o in ['invalid','missing','error'] and old['outcomes'][uid] not in ['invalid','missing','error']]
        detail[name]={'baseline':old,'candidate':new,'new_error_ids':errors}
    promotion=bool(complete and not detail['protected']['new_error_ids'] and not new_failures and detail['repair']['candidate']['accuracy']>=detail['repair']['baseline']['accuracy']-1e-12)
    return {'comparison_inputs_valid':bool(complete),'promotion_eligible':promotion,'new_execution_failure_ids':new_failures,'sets':detail}
