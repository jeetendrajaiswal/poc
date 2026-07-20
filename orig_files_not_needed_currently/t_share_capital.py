"""Targeted eval: share_capital section ONLY, all 5 companies x 2 scopes, diffed vs GT.

⚠️ RUNS THE OPENAI API — needs explicit owner approval before running.
Much cheaper than build_error_report.py (only the share_capital read per scope, plus the
statement-line reads the engine path makes anyway): expect roughly 60-70 small calls total.

Usage:
    .venv/bin/python t_share_capital.py           # one pass
    .venv/bin/python t_share_capital.py --n 3     # repeat 3x to expose run-to-run variance

Judge by the DETERMINISTIC signal first: "collapsed scopes" must be 0 (a collapse = the
engine returned absent for EVERY GT-present item of a scope, the failure mode fixed on
2026-07-03). The raw error count still carries single-run LLM noise.
"""
import csv, os, re, sys

import src.llm as llm

_cli = llm.client(); _real = _cli.responses.create; ACC = {"calls": 0, "in": 0, "out": 0}
def _w(*a, **k):
    r = _real(*a, **k); u = getattr(r, "usage", None); ACC["calls"] += 1
    if u:
        ACC["in"] += getattr(u, "input_tokens", 0) or 0
        ACC["out"] += getattr(u, "output_tokens", 0) or 0
    return r
_cli.responses.create = _w

from src.engine.index import PageIndex
from src.engine import datapoints as dp


def num(s):
    if s is None: return None
    s = str(s).replace("−", "-").replace("–", "-")
    neg = "(" in s and ")" in s
    m = re.search(r"-?\d[\d,]*\.?\d*", s.replace(" ", ""))
    if not m: return None
    v = float(m.group(0).replace(",", "")); return -abs(v) if neg else v


def vmatch(gt, ev):
    x, y = num(gt), num(ev)
    if x is None or y is None: return False
    return abs(x) < 0.5 if abs(y) < 1e-9 else abs(x - y) / abs(y) < 0.01


def gt_is_absent(v):
    if num(v) is not None: return False
    return (v or "").strip().lower() in ("", "-", "na", "n/a", "not disclosed",
                                         "not applicable", "absent", "none", "nil")


GT = {}
for r in csv.DictReader(open("data/gt_master_corrected.csv")):
    GT[(r["company"], r["scope"], r["key"])] = r["corrected_value"]

COMPANIES = ["reliance", "hindalco", "itc", "infosys", "adani"]
SC = [c for c in dp.load_concepts() if c.section == "share_capital"]
SC_KEYS = [c.key for c in SC]

n_runs = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 1
totals = []
for run in range(1, n_runs + 1):
    errs, collapses, ok_n = [], [], 0
    for comp in COMPANIES:
        idx = PageIndex(os.path.expanduser(f"~/Downloads/{comp}.pdf"))
        for scope in ("standalone", "consolidated"):
            res = dp.extract_datapoints(idx, scope, SC)
            gt_present = [k for k in SC_KEYS if not gt_is_absent(GT.get((comp, scope, k)))]
            found = [k for k in gt_present if res.get(k) and res[k].present]
            if gt_present and not found:
                collapses.append(f"{comp}/{scope}")
            for k in SC_KEYS:
                gv = GT.get((comp, scope, k))
                if gv is None:            # no GT row for this key/scope
                    continue
                d = res.get(k)
                present = d is not None and d.present
                if gt_is_absent(gv):
                    if present:
                        errs.append((comp, scope, k, "false_positive", d.value, gv, d.pages))
                    else:
                        ok_n += 1
                elif not present:
                    errs.append((comp, scope, k, "miss", None, gv,
                                 d.pages if d is not None else []))
                elif vmatch(gv, d.value):
                    ok_n += 1
                else:
                    errs.append((comp, scope, k, "mismatch", d.value, gv, d.pages))
    print(f"\n===== RUN {run}/{n_runs}: {len(errs)} errors, {ok_n} ok, "
          f"collapsed scopes: {collapses or 'NONE'} =====")
    for comp, scope, k, et, mv, gv, pages in errs:
        print(f"  {et:14s} {comp:9s} {scope:12s} {k[:52]:52s} model={str(mv)[:16]:16s} "
              f"gt={str(gv)[:16]:16s} pages={','.join(map(str, pages))}")
    totals.append((len(errs), len(collapses)))

print(f"\nRuns: {[t[0] for t in totals]} errors, {[t[1] for t in totals]} collapses")
print(f"API: calls={ACC['calls']} cost~${ACC['in']/1e6*0.75 + ACC['out']/1e6*4.5:.2f}")
