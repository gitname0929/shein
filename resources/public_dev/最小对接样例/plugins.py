from quality import metric
def conclusion(inputs,outputs,references,context):
 m=metric(references,outputs,inputs)
 return {'plugin_id':'demo.conclusion','plugin_version':'1.0','input_schema_version':'1.3.1','metrics':{'accuracy':m['accuracy'],'quality':m['quality']},'diagnostics':[],'errors':[]}
def evidence(inputs,outputs,references,context):
 from contracts import prediction_errors
 tasks={t['audit_unit_id']:t for t in inputs};diagnostics=[]
 for p in outputs:
  t=tasks.get(p.get('audit_unit_id'))
  if t:diagnostics.append({'audit_unit_id':t['audit_unit_id'],'issues':prediction_errors(p,t)})
 return {'plugin_id':'demo.evidence_structure','plugin_version':'1.0','input_schema_version':'1.3.1','metrics':{'valid_structure_count':sum(not d['issues'] for d in diagnostics)},'diagnostics':diagnostics,'errors':[],'limitation':'Checks references/structure only; does not establish visual fact truth or satisfy complete evidence-plugin rubric.'}
