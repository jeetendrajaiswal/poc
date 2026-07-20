import csv, re, os
from src.engine.index import PageIndex
from src.engine import datapoints as dp
def num(s):
    if s is None: return None
    s=str(s).replace("−","-").replace("–","-"); neg="(" in s and ")" in s
    m=re.search(r"-?\d[\d,]*\.?\d*", s.replace(" ","")); 
    if not m: return None
    v=float(m.group(0).replace(",","")); return -abs(v) if neg else v
def vm(g,e):
    x,y=num(g),num(e)
    if x is None or y is None: return False
    return abs(x)<0.5 if abs(y)<1e-9 else abs(x-y)/abs(y)<0.01
GT={}
for r in csv.DictReader(open("data/gt_master_corrected.csv")): GT[(r["company"],r["scope"],r["key"])]=r["corrected_value"]
concepts=[c for c in dp.load_concepts() if c.section=="ppe"]
def score(comp, runs=1):
    idx=PageIndex(os.path.expanduser(f"~/Downloads/{comp}.pdf"))
    for run in range(1,runs+1):
        line=[]
        for scope in ("standalone","consolidated"):
            r=dp.extract_datapoints(idx, scope, concepts); ok=tot=0; bad=[]
            for k,d in r.items():
                if "Office Equip" not in k and "Furnitur" not in k: continue
                gt=GT.get((comp,scope,k)); got=d.value if d.present else None
                if vm(got,gt): ok+=1
                else: bad.append(f"{k.split('_')[-1][:8]}={got}")
                tot+=1
            line.append(f"{scope[:4]} {ok}/{tot}"+(("["+",".join(bad)+"]") if bad else ""))
        print(f"{comp:9} run{run}: "+"  ".join(line))
score("infosys", runs=3)   # must be identical & 8/8 (deterministic)
score("reliance")          # per-row regression
score("adani")             # per-row + shredded cons
score("itc")
