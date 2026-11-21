"""Small adapter example, not a complete competition solution. Never reads gold."""
import argparse,base64,copy,json,os,re,sys,time,urllib.request,uuid
from pathlib import Path
from artifact_hash import path_digest
from contracts import prediction_errors,annotation_errors
FORMAT='返回固定行格式，不要JSON，不要代码围栏。第1行 LABEL: pass/reject/ambiguous 中的一个值；第2行 APPLICABILITY: applicable/not_applicable/unknown 中的一个值；第3行 REASON: 简洁具体依据，不换行；第4行 EVIDENCE:；后续每条证据单独一行，格式为 material_id<TAB>整数page_index<TAB>fact，三个字段用制表符分隔。没有图像证据时写 NONE。最后一行 END。引用只使用当前materials的ID及源页；元数据依据写REASON。明确不适用用pass/not_applicable；仅适用条件未知用ambiguous/unknown，已经适用但证据不足仍applicable。'
def rows(p):return [json.loads(x) for x in Path(p).read_text(encoding='utf-8').splitlines() if x.strip()]
def write(p,x):p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def jl(p,x):p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in x),encoding='utf-8')
def model_call(text,task,inputdir,model):
 if not model:raise RuntimeError('model_not_configured')
 content=[{'type':'text','text':text}]
 for m in task['materials']:
  path=(inputdir/m['relative_path']).resolve()
  if not path.is_relative_to(inputdir.resolve()):raise ValueError('Unsafe material path')
  content += [{'type':'text','text':f"material_id={m['material_id']} page_index={m['page_index']}"},{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()}}]
 payload={'model':model,'messages':[{'role':'user','content':content}],'temperature':0,'max_tokens':4096,'enable_thinking':False}
 if not os.environ.get('OPENAI_BASE_URL'):raise RuntimeError('gateway_not_configured')
 req=urllib.request.Request(os.environ['OPENAI_BASE_URL'].rstrip('/')+'/chat/completions',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ.get('OPENAI_API_KEY','')})
 with urllib.request.urlopen(req,timeout=300) as response:obj=json.load(response)
 return obj['choices'][0]['message']['content']
def parse_lines(raw):
 starts=list(re.finditer(r'(?m)^\s*LABEL\s*[:：]',raw))
 if len(starts)!=1:raise ValueError('Result block must be unique')
 d={};e=[];inside=False
 for line in raw[starts[0].start():].splitlines():
  line=line.strip()
  if line=='END':break
  m=re.match(r'^(LABEL|APPLICABILITY|REASON|EVIDENCE)\s*[:：]\s*(.*)$',line)
  if m:
   k,v=m.groups()
   if k in d:raise ValueError('Duplicate output field')
   d[k]=v;inside=k=='EVIDENCE' or inside;continue
  if inside and line and line!='NONE':
   mid,page,fact=re.split(r'\t+|\s+\|\s+',line,maxsplit=2);e.append({'material_id':mid,'page_index':int(page),'fact':fact})
 if set(d)!={'LABEL','APPLICABILITY','REASON','EVIDENCE'}:raise ValueError('Missing result fields')
 return {'label':d['LABEL'],'applicability':d['APPLICABILITY'],'reason':d['REASON'],'evidence':e}
def build(a):
 rules=rows(a.rules);cfg=json.loads(Path(a.config).read_text(encoding='utf-8'));out=Path(a.out)
 policy={'rules':{r['rule_id']+'@'+r['version']:r['rule_text'] for r in rules},'general':cfg.get('general',''),'supplement':cfg.get('supplement',''),'model':cfg.get('model'),'artifact_version':'minimal'}
 write(out/'engine/policy.json',policy)
 write(out/'artifact_manifest.json',{'entries':[{'rule_id':r['rule_id'],'rule_version':r['version'],'artifact_version':policy['artifact_version'],'path':'engine','sha256':path_digest(out/'engine')} for r in rules]})
def infer(a):
 policy=json.loads((Path(a.artifacts)/'engine/policy.json').read_text(encoding='utf-8'));outputs=[];traces=[]
 for t in rows(a.input):
  start=time.monotonic();p={k:t[k] for k in ['audit_unit_id','rule_id','rule_version']};p.update(artifact_version=policy['artifact_version'],trace_id=uuid.uuid4().hex,cost_units=None)
  try:
   prompt='依据公开规则和全部材料执行审核，不猜测不可读事实。先适用性，再特别冲突或例外、已证实不满足、影响结论的未知、全部满足。\n'+FORMAT+'\n'+policy['general']+'\n'+policy['supplement']+'\n'+policy['rules'][t['rule_id']+'@'+t['rule_version']]+'\n'+json.dumps(t,ensure_ascii=False)
   for attempt in range(2):
    raw=model_call(prompt,t,Path(a.input).parent,policy['model']);traces.append({'audit_unit_id':t['audit_unit_id'],'attempt':attempt+1,'response':raw})
    try:
     result=parse_lines(raw);p.update(result,execution_status='completed',reason_code='rule_not_applicable' if result['applicability']=='not_applicable' else 'model_review',latency_ms=(time.monotonic()-start)*1000)
     errors=prediction_errors(p,t)
     if errors:raise ValueError(str(errors))
     break
    except (ValueError,KeyError,TypeError) as err:
     if attempt:raise
     prompt+='\n【公开协议校验反馈，无标准答案】'+str(err)+'\n请按上述固定行格式重新完整输出。上次输出：'+raw
  except Exception as e:p.update(execution_status='error',label=None,applicability=None,reason_code='execution_error',reason=(str(e) or type(e).__name__)[:200],error_code=(str(e) or type(e).__name__)[:200],evidence=[])
  p['latency_ms']=(time.monotonic()-start)*1000;outputs.append(p)
 jl(a.out,outputs);jl(str(a.out)+'.trace.jsonl',traces)
def parse_annotation(raw,target):
 values={}
 for line in raw.splitlines():
  m=re.match(r'^\s*(VALUE_JSON|STATUS|LABEL|BBOX)\s*[:：]\s*(.*?)\s*$',line)
  if m:
   k,v=m.groups()
   if k in values:raise ValueError('Duplicate annotation field')
   values[k]=v
 if set(values)!={'VALUE_JSON','STATUS','LABEL','BBOX'}:raise ValueError('Missing annotation field')
 return {k:target[k] for k in ['annotation_id','audit_unit_id','material_id','page_index']}|{'value':json.loads(values['VALUE_JSON']),'status':values['STATUS'],'label':values['LABEL'],'bbox':json.loads(values['BBOX'])}
def annotate(a):
 policy=json.loads((Path(a.artifacts)/'engine/policy.json').read_text(encoding='utf-8'));tasks={x['audit_unit_id']:x for x in rows(a.input)};outputs=[];failures=[];traces=[]
 for target in rows(a.targets):
  t=tasks[target['audit_unit_id']]
  prompt=policy['general']+'\n'+policy['supplement']+'\n'+policy['rules'][t['rule_id']+'@'+t['rule_version']]+'\n'+json.dumps({'task':t,'annotation_target':target},ensure_ascii=False)+'\n只输出四行，不要代码围栏：第1行VALUE_JSON: JSON字符串或null；第2行STATUS: value/unreadable/invalid_value；第3行LABEL: pass/reject/ambiguous；第4行BBOX: [x1,y1,x2,y2]。label是整条指定规则的审核结论，不是仅该文本是否合格。value提取target要求的完整行，包括字段名，NFKC归一化可接受；不可读必须null和unreadable；文字可读但该字段值明确非法时用invalid_value。BBOX框选指定行的文字或对应遮挡区域，为0至1归一化坐标。标识符由宿主原样回传，无需输出。'
  try:
   for attempt in range(2):
    raw=model_call(prompt,t,Path(a.input).parent,policy['model']);traces.append({'annotation_id':target['annotation_id'],'attempt':attempt+1,'response':raw})
    try:
     p=parse_annotation(raw,target);errors=annotation_errors(p,target,t)
     if errors:raise ValueError(str(errors))
     break
    except (ValueError,KeyError,TypeError) as err:
     if attempt:raise
     prompt+='\n【公开格式校验反馈，无标准答案】'+str(err)+'\n请重新输出指定四行。上次输出：'+raw
   outputs.append(p)
  except Exception as e:failures.append({'annotation_id':target['annotation_id'],'error':(str(e) or type(e).__name__)[:200]})
 jl(a.out,outputs);write(str(a.out)+'.errors.json',failures);jl(str(a.out)+'.trace.jsonl',traces)
def iterate(a):
 # Demonstrates an explicit no_change path. This is not an answer to teacher feedback.
 import shutil
 dst=Path(a.out)
 if dst.exists():raise ValueError('Candidate directory must not exist')
 shutil.copytree(a.base,dst)
 packet=json.loads(Path(a.feedback).read_text(encoding='utf-8'))
 if packet['base_artifact_sha256']!=path_digest(a.base):raise ValueError('Feedback baseline hash mismatch')
 write(dst/'iteration_report.json',{'schema_version':'1.3.1','action':'no_change','base_artifact_sha256':path_digest(a.base),'decisions':[{'packet_id':x['packet_id'],'decision':'defer','reason':'Minimal adapter does not implement rule revision; baseline retained.','rule_citations':[]} for x in packet['items']]})
def evaluate(a):
 from plugin_runner import run_plugin
 report=run_plugin(a.plugin,rows(a.inputs),rows(a.outputs),rows(a.references),{'schema_version':'1.3.1','purpose':'public_development'},timeout=float(os.environ['PLUGIN_TIMEOUT_SECONDS']) if os.environ.get('PLUGIN_TIMEOUT_SECONDS') else None)
 write(a.out,report)
def main():
 p=argparse.ArgumentParser();sp=p.add_subparsers(dest='command',required=True)
 for name,params in {'build':['rules','config','out'],'infer':['input','artifacts','out'],'annotate':['input','targets','artifacts','out'],'iterate':['base','feedback','input','out'],'evaluate':['plugin','inputs','outputs','references','out']}.items():
  sub=sp.add_parser(name)
  for k in params:sub.add_argument('--'+k,required=True)
 a=p.parse_args();globals()[a.command](a)
if __name__=='__main__':main()
