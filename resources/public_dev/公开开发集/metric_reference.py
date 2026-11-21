"""Development-set quality only, not an official score."""
import sys,argparse,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'02_协议与评分/校验工具'))
from quality import metric
from contracts import read_rows
p=argparse.ArgumentParser();p.add_argument('--inputs',required=True);p.add_argument('--references',required=True);p.add_argument('--predictions',required=True);a=p.parse_args()
print(json.dumps(metric(read_rows(a.references,False),read_rows(a.predictions,False),read_rows(a.inputs,False)),ensure_ascii=False,indent=2))
