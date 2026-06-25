"""Full-taxonomy validation across companies (current report only = production model).

For each company: run the full ~59-item extraction, build an INDEPENDENT careful-read
ground truth (different method → meaningful disagreements), score, and adjudicate.
Ground truth is treated as fallible: disagreements are reported for review, not assumed
to be pipeline errors.

  .venv/bin/python -m src.validate                 # all 6
  .venv/bin/python -m src.validate reddy adani     # subset
"""
from __future__ import annotations

import os
import sys

from src import build_gt, phase0, scoring

TAX = "definitions.yaml"
ALL = ["reddy", "adani", "reliance", "itc", "infosys", "hindalco"]


def validate(company: str):
    print(f"\n{'='*70}\n{company.upper()} — full taxonomy\n{'='*70}", flush=True)
    print(f"[1/3] extracting (current report) ...", flush=True)
    _, out = phase0.run(company, defs_path=os.path.join(phase0.TAX_DIR, TAX), out_suffix="_full")
    print(f"\n[2/3] building independent ground truth ...", flush=True)
    gt = f"gt_{company}_full.csv"
    build_gt.run(company, defs_name=TAX, out_name=gt)
    print(f"\n[3/3] scoring ...", flush=True)
    acc, counts, _ = scoring.score(company, os.path.join("data", gt), results_path=out)
    return company, acc, counts


if __name__ == "__main__":
    companies = [a for a in sys.argv[1:] if not a.startswith("--")] or ALL
    summary = []
    for c in companies:
        try:
            summary.append(validate(c))
        except Exception as e:
            print(f"!! {c} failed: {e}", flush=True)
            summary.append((c, 0.0, {}))
    print(f"\n{'='*70}\nSUMMARY (full taxonomy, current report, vs independent GT)\n{'='*70}")
    for c, acc, counts in summary:
        print(f"  {c:10} {acc:5.1f}%   {dict(counts)}")
    print("\nNote: GT is fallible — investigate SPURIOUS/TRUE_ERROR before trusting the gap.")
