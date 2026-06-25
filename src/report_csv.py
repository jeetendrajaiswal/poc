"""Build analysis CSVs from saved artifacts (no API calls).

For each company it joins the pipeline output (output/<company>_full.json) with the
independent ground truth (data/gt_<company>_full.csv) by (key, scope), classifies each
row, and writes:
  output/<company>_comparison.csv   per-item: extracted vs gt vs verdict + evidence
  output/summary.csv                per-company accuracy + verdict counts

Verdicts come from scoring.classify (methodology-aware). GT is fallible — SPURIOUS /
TRUE_ERROR rows are the ones to eyeball (the pipeline may be right and GT wrong).

  .venv/bin/python -m src.report_csv               # all companies with artifacts
  .venv/bin/python -m src.report_csv reddy itc
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter

import yaml

from src import scoring

OUT = "output"
TAX = os.path.join(os.path.dirname(__file__), "..", "taxonomy", "definitions.yaml")
ALL = ["reddy", "adani", "reliance", "itc", "infosys", "hindalco"]

_FIELDS = ["company", "key", "scope", "value_type", "verdict", "extracted_value",
           "extracted_prior", "gt_value", "confidence", "candidate_pages",
           "observed_scope", "reported_label", "evidence_quote"]


def _vtype_map():
    items = yaml.safe_load(open(TAX))["items"]
    return {i["key"]: i.get("value_type", "") for i in items}


def comparison(company: str, vtypes: dict) -> tuple[list[dict], Counter]:
    res_path = os.path.join(OUT, f"{company}_full.json")
    gt_path = os.path.join("data", f"gt_{company}_full.csv")
    if not (os.path.exists(res_path) and os.path.exists(gt_path)):
        return [], Counter()
    rows = json.load(open(res_path))
    gt = {(r["key"].strip(), r["scope"].strip().lower()): r["expected_value"]
          for r in csv.DictReader(open(gt_path)) if r["company"].strip().lower() == company.lower()}

    out_rows, counts = [], Counter()
    for r in rows:
        key, scope = r.get("key"), r.get("scope")
        if r.get("status"):  # N/A (sector / no consolidated)
            verdict = "N/A"
        else:
            exp = gt.get((key, scope), "")
            verdict = scoring.classify(r, exp)
        counts[verdict] += 1
        out_rows.append({
            "company": company, "key": key, "scope": scope,
            "value_type": vtypes.get(key, ""), "verdict": verdict,
            "extracted_value": r.get("value", ""), "extracted_prior": r.get("value_prior", ""),
            "gt_value": gt.get((key, scope), ""), "confidence": r.get("confidence", ""),
            "candidate_pages": r.get("candidate_pages", ""), "observed_scope": r.get("observed_scope", ""),
            "reported_label": r.get("reported_label", ""), "evidence_quote": r.get("evidence_quote", ""),
        })
    return out_rows, counts


def main(companies):
    vtypes = _vtype_map()
    summary = []
    all_rows = []
    for c in companies:
        rows, counts = comparison(c, vtypes)
        if not rows:
            print(f"  {c}: no artifacts (skipped)")
            continue
        path = os.path.join(OUT, f"{c}_comparison.csv")
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_FIELDS); w.writeheader(); w.writerows(rows)
        all_rows.extend(rows)
        applicable = sum(v for k, v in counts.items() if k != "N/A")
        correct = counts["MATCH"] + counts["CORRECTLY_ABSENT"]
        acc = round(correct / applicable * 100, 1) if applicable else 0.0
        summary.append({"company": c, "accuracy_pct": acc, "applicable": applicable, **counts})
        print(f"  {c:10} {acc:5.1f}%  -> {path}")

    # combined per-item + summary
    if all_rows:
        with open(os.path.join(OUT, "all_comparison.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=_FIELDS); w.writeheader(); w.writerows(all_rows)
    if summary:
        cols = ["company", "accuracy_pct", "applicable", "MATCH", "CORRECTLY_ABSENT",
                "TRUE_ERROR", "SCALE_MISMATCH", "MISS", "SPURIOUS", "N/A"]
        with open(os.path.join(OUT, "summary.csv"), "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore"); w.writeheader()
            for s in summary:
                w.writerow({k: s.get(k, 0) for k in cols})
        print(f"\nwrote output/summary.csv and output/all_comparison.csv")


if __name__ == "__main__":
    main([a for a in sys.argv[1:] if not a.startswith("--")] or ALL)
