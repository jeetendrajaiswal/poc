"""Cross-year consistency check — a second, automated source of truth.

The prior-year COMPARATIVE column in THIS year's report must equal the current-year
value in LAST year's report (same fiscal year, two independent disclosures). Agreement
strongly confirms both extractions; a mismatch flags either an extraction error OR a
genuine restatement (regrouped prior year) — reported separately, not as a silent pass.

Broad (applies to every numeric item), label- and math-independent — it catches the
wrong-cell / wrong-column / wrong-year errors that evidence-grounding cannot.

  .venv/bin/python -m src.cross_year hindalco            # thin slice
  .venv/bin/python -m src.cross_year hindalco --full     # full taxonomy

Reads the CURRENT report (gives value + value_prior) and the PRIOR report
(~/Downloads/prev/<company>.pdf, gives its current value = prior fiscal year), then:
    current.value_prior   vs   prev.value     (both = prior fiscal year)
"""
from __future__ import annotations

import os
import sys

from src import phase0
from src.scoring import _rel_close, parse_number

PREV_DIR = os.path.join(phase0.PDF_DIR, "prev")


def _key(r):
    return (r.get("key"), r.get("scope"))


def cross_year(company: str, full: bool = False):
    defs_path = os.path.join(phase0.TAX_DIR, "definitions.yaml" if full else "definitions_thin.yaml")
    suffix = "_full" if full else "_thin"

    print(f"\n=== CURRENT report: {company} ===")
    cur, _ = phase0.run(company, defs_path=defs_path, out_suffix=suffix)
    print(f"\n=== PRIOR-year report: prev/{company} ===")
    prev, _ = phase0.run(company, defs_path=defs_path, out_suffix=suffix + "_prev", pdf_dir=PREV_DIR)

    prev_by = {_key(r): r for r in prev}
    verified = restated = insufficient = 0
    flags = []
    for r in cur:
        k = _key(r)
        cur_prior = parse_number(r.get("value_prior"))      # this report's prior-year column
        p = prev_by.get(k, {})
        prev_cur = parse_number(p.get("value"))             # last report's current-year value
        if cur_prior is None or prev_cur is None:
            insufficient += 1
            continue
        if _rel_close(cur_prior, prev_cur, tol=0.01):
            verified += 1
        else:
            restated += 1
            flags.append((r.get("key"), r.get("scope"), r.get("value_prior"), p.get("value")))

    checkable = verified + restated
    rate = (verified / checkable * 100) if checkable else 0.0
    print(f"\n=== {company} cross-year consistency ===")
    print(f"  VERIFIED (two sources agree):       {verified}")
    print(f"  RESTATEMENT_OR_ERROR (disagree):    {restated}")
    print(f"  INSUFFICIENT (a side missing):      {insufficient}")
    print(f"  cross-year agreement: {rate:.1f}% of {checkable} checkable items")
    if flags:
        print("\n  Flagged (this-yr prior-col vs last-yr current):")
        for key, scope, a, b in flags:
            print(f"    {key[:40]:40} {scope:12} {str(a)[:16]:16} vs {str(b)[:16]}")
    return verified, restated, insufficient


if __name__ == "__main__":
    args = sys.argv[1:]
    full = "--full" in args
    company = next((a for a in args if not a.startswith("--")), "hindalco")
    cross_year(company, full=full)
