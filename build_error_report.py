"""Build a per-company error report (one Excel sheet per company).
Runs the full engine on each company/scope, diffs against gt_master_corrected.csv,
and lists every error with page, item, model value, GT value, confidence, reason.

Detailed run logging (for post-run forensics, all under logs/run_<ts>/):
  llm_calls.jsonl              one record per API call: company/scope, schema, pages sent,
                               tokens in/out, cost, duration, status
  datapoints_<comp>_<scope>.json  EVERY datapoint (correct ones too): value, confidence,
                               pages, grounded, evidence — lets you trace any error to the
                               exact read without re-running
  errors.json                  the diff rows in machine-readable form
  summary.json                 totals: per-company/per-section error counts, tokens, cost
"""
import csv, json, os, re, threading, time
from datetime import datetime

import src.llm as llm

# --- run log ------------------------------------------------------------------
RUN_DIR = os.path.join("logs", f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
os.makedirs(RUN_DIR, exist_ok=True)
_LOG_LOCK = threading.Lock()
CURRENT = {"company": "", "scope": ""}          # set by the outer loop, read by the wrapper


def _log(rec: dict):
    with _LOG_LOCK:
        with open(os.path.join(RUN_DIR, "llm_calls.jsonl"), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --- cost meter + call logger ---------------------------------------------------
_cli = llm.client(); _real = _cli.responses.create
ACC = {"calls": 0, "in": 0, "out": 0}
PRICE_IN, PRICE_OUT = 0.75 / 1e6, 4.5 / 1e6     # $/token (keep in sync with the model)


def _w(*a, **k):
    t0 = time.time()
    r = _real(*a, **k)
    dt = time.time() - t0
    u = getattr(r, "usage", None)
    ti = (getattr(u, "input_tokens", 0) or 0) if u else 0
    to = (getattr(u, "output_tokens", 0) or 0) if u else 0
    with _LOG_LOCK:
        ACC["calls"] += 1; ACC["in"] += ti; ACC["out"] += to
    inp = k.get("input", "")
    txt = inp if isinstance(inp, str) else " ".join(
        c.get("text", "") for m in inp for c in m.get("content", []) if isinstance(c, dict))
    n_imgs = 0 if isinstance(inp, str) else sum(
        1 for m in inp for c in m.get("content", []) if c.get("type") == "input_image")
    sec = re.search(r"SECTION: ([^\n]*)", txt)
    _log({
        "ts": round(time.time(), 2), "company": CURRENT["company"], "scope": CURRENT["scope"],
        "schema": (k.get("text") or {}).get("format", {}).get("name", "?"),
        "section": sec.group(1).strip() if sec else "",
        "pages": [int(p) for p in re.findall(r"=== PAGE (\d+) ===", txt)],
        "images": n_imgs, "model": k.get("model", ""),
        "input_tokens": ti, "output_tokens": to,
        "cost": round(ti * PRICE_IN + to * PRICE_OUT, 5),
        "duration_s": round(dt, 2), "status": getattr(r, "status", ""),
        "instr": (k.get("instructions") or "")[:90],
    })
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

# --- load GT ----------------------------------------------------------------
GT = {}   # (company, scope, key) -> {value, evidence}
for r in csv.DictReader(open("data/gt_master_corrected.csv")):
    GT[(r["company"], r["scope"], r["key"])] = {"value": r["corrected_value"], "evidence": r.get("evidence", "")}

COMPANIES = ["reliance", "hindalco", "itc", "infosys", "adani"]
SCOPES = [("standalone", "standalone"), ("consolidated", "consolidated")]
concepts = dp.load_concepts()
sec_of = {c.key: c.section for c in concepts}

def gt_is_absent(v):
    """GT cell means 'not present in the report' (no numeric truth)."""
    if num(v) is not None: return False
    t = (v or "").strip().lower()
    return t in ("", "-", "na", "n/a", "not disclosed", "not applicable", "absent", "none", "nil")

def classify(gt_val, dp_obj):
    present = dp_obj is not None and dp_obj.present
    model_v = dp_obj.value if present else None
    if gt_is_absent(gt_val):
        if present:
            return "false_positive", model_v, f"Model returned a value but GT says '{gt_val or 'absent'}' — not disclosed in the report"
        return "ok", None, ""
    # GT has a numeric truth
    if not present:
        return "miss", None, "Model returned ABSENT — datapoint not located/extracted (locate or extraction gap)"
    if vmatch(gt_val, model_v):
        return "ok", model_v, ""
    return "mismatch", model_v, "Value mismatch — model extracted a different figure than the report"

# --- run --------------------------------------------------------------------
errors = {c: [] for c in COMPANIES}
per_company_cost = {}
for comp in COMPANIES:
    calls0, in0, out0 = ACC["calls"], ACC["in"], ACC["out"]
    idx = PageIndex(os.path.expanduser(f"~/Downloads/{comp}.pdf"))
    got = {}   # (scope, key) -> Datapoint
    for eng_scope, gt_scope in SCOPES:
        CURRENT["company"], CURRENT["scope"] = comp, eng_scope
        t0 = time.time()
        res = dp.extract_datapoints(idx, eng_scope, concepts)
        for k, d in res.items():
            got[(gt_scope, k)] = d
        # full per-datapoint dump — correct values included (forensics without re-running)
        with open(os.path.join(RUN_DIR, f"datapoints_{comp}_{eng_scope}.json"), "w") as f:
            json.dump({k: {"section": d.section, "present": d.present, "value": d.value,
                           "confidence": d.confidence, "grounded": d.grounded,
                           "pages": d.pages, "evidence": d.evidence}
                       for k, d in res.items()}, f, indent=1, ensure_ascii=False)
        print(f"  {comp}/{eng_scope}: {len(res)} datapoints in {time.time()-t0:.0f}s "
              f"(running cost ~${ACC['in']*PRICE_IN + ACC['out']*PRICE_OUT:.2f})", flush=True)
    # diff every GT row for this company
    for (cc, scope, key), info in GT.items():
        if cc != comp: continue
        lookups = [scope] if scope != "both" else ["standalone", "consolidated"]
        # for 'both', report once; pick the worse outcome across scopes
        chosen = None
        for sc in lookups:
            d = got.get((sc, key))
            etype, mv, reason = classify(info["value"], d)
            cand = (etype, mv, reason, d, sc)
            order = {"mismatch": 0, "miss": 1, "false_positive": 2, "ok": 3}
            if chosen is None or order[etype] < order[chosen[0]]:
                chosen = cand
        etype, mv, reason, d, sc = chosen
        if etype == "ok": continue
        pages = ",".join(str(p) for p in (d.pages or [])) if d is not None else ""
        conf = d.confidence if d is not None else "absent"
        errors[comp].append({
            "section": sec_of.get(key, ""), "scope": sc, "item": key,
            "model_value": mv, "gt_value": info["value"], "pages": pages,
            "confidence": conf, "error_type": etype, "reason": reason,
            "gt_evidence": info["evidence"],
        })
    dc, din, dout = ACC["calls"] - calls0, ACC["in"] - in0, ACC["out"] - out0
    per_company_cost[comp] = {"calls": dc, "input_tokens": din, "output_tokens": dout,
                              "cost": round(din * PRICE_IN + dout * PRICE_OUT, 3)}
    print(f"{comp:9}: {len(errors[comp])} errors | {dc} calls, {din:,} in / {dout:,} out tok, "
          f"~${per_company_cost[comp]['cost']:.2f}", flush=True)

# --- machine-readable outputs -------------------------------------------------
with open(os.path.join(RUN_DIR, "errors.json"), "w") as f:
    json.dump(errors, f, indent=1, ensure_ascii=False)
sec_counts = {}
for comp, es in errors.items():
    for e in es:
        sec_counts[e["section"]] = sec_counts.get(e["section"], 0) + 1
summary = {
    "total_errors": sum(len(v) for v in errors.values()),
    "per_company": {c: len(errors[c]) for c in COMPANIES},
    "per_section": dict(sorted(sec_counts.items(), key=lambda x: -x[1])),
    "api": {"calls": ACC["calls"], "input_tokens": ACC["in"], "output_tokens": ACC["out"],
            "cost": round(ACC["in"] * PRICE_IN + ACC["out"] * PRICE_OUT, 2)},
    "per_company_cost": per_company_cost,
}
with open(os.path.join(RUN_DIR, "summary.json"), "w") as f:
    json.dump(summary, f, indent=1)

# --- write Excel ------------------------------------------------------------
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

wb = Workbook()
HEAD = ["section", "scope", "item", "model_value", "gt_value", "pages", "confidence", "error_type", "reason", "gt_evidence"]
hfill = PatternFill("solid", fgColor="1F4E78"); hfont = Font(color="FFFFFF", bold=True)
type_fill = {"mismatch": "FCE4D6", "miss": "FFF2CC", "false_positive": "E2EFDA"}

# summary sheet
ws = wb.active; ws.title = "Summary"
ws.append(["Company", "Total errors", "Mismatch", "Miss", "False positive"])
for c in range(1, 6):
    ws.cell(1, c).fill = hfill; ws.cell(1, c).font = hfont
for comp in COMPANIES:
    es = errors[comp]
    ws.append([comp,
               len(es),
               sum(e["error_type"] == "mismatch" for e in es),
               sum(e["error_type"] == "miss" for e in es),
               sum(e["error_type"] == "false_positive" for e in es)])
for i, w in enumerate([16, 13, 11, 8, 14], 1):
    ws.column_dimensions[get_column_letter(i)].width = w

# per-company sheets
for comp in COMPANIES:
    ws = wb.create_sheet(comp.capitalize())
    ws.append(HEAD)
    for c in range(1, len(HEAD) + 1):
        ws.cell(1, c).fill = hfill; ws.cell(1, c).font = hfont
    rows = sorted(errors[comp], key=lambda e: (e["section"], e["scope"], e["item"]))
    for e in rows:
        ws.append([e[h] for h in HEAD])
        r = ws.max_row
        fill = type_fill.get(e["error_type"])
        if fill:
            ws.cell(r, HEAD.index("error_type") + 1).fill = PatternFill("solid", fgColor=fill)
    widths = [16, 12, 30, 14, 14, 10, 12, 14, 50, 50]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

out = "error_report.xlsx"
wb.save(out)
print(f"\nWrote {out}  ({summary['total_errors']} total errors)")
print(f"Per-section: {summary['per_section']}")
print(f"API: calls={ACC['calls']} in={ACC['in']:,} out={ACC['out']:,} "
      f"cost~${summary['api']['cost']:.2f}")
print(f"Logs: {RUN_DIR}/")
