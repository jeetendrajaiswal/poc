"""Score extracted values against a ground-truth file.

ground_truth.csv columns:  company,key,scope,expected_value
  - expected_value: a number (any Indian format) or "Not disclosed" / "" / "NA".
  - scope: standalone | consolidated.

Classifies each item (per PLAN.md, methodology-aware):
  - MATCH               : extracted ≈ expected within tolerance
  - TRUE_ERROR          : both present but differ beyond tolerance
  - MISS                : expected present, we found nothing
  - SPURIOUS            : expected absent, we returned a value
  - CORRECTLY_ABSENT    : both "not disclosed"
  - NA_SCOPE            : scope not filed by the company

Unit-scale aware: if values differ by a clean 10x/100x/1000x factor, flags a likely
SCALE mismatch (crore vs lakh vs million) rather than a true error.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter

_NUM = re.compile(r"-?\d[\d,]*\.?\d*")

# Page-verified ground-truth overlay: values we confirmed by reading the actual PDF pages.
# These OVERRIDE the (often under-reporting) external ground truth wherever they exist, so scores
# reflect what the report actually discloses. See data/verified_truth.csv.
_VERIFIED_DEFAULT = os.path.join(os.path.dirname(__file__), "..", "data", "verified_truth.csv")


def _load_verified(path: str) -> dict:
    """Return {(company, key, scope): verified_value} from the page-verified overlay CSV."""
    out: dict = {}
    if not path or not os.path.exists(path):
        return out
    for r in csv.DictReader(open(path)):
        co = (r.get("company") or "").strip().lower()
        key = (r.get("key") or "").strip()
        scope = (r.get("scope") or "").strip().lower()
        val = (r.get("verified_value") or "").strip()
        if co and key and val:
            out[(co, key, scope)] = val
    return out


def parse_number(raw) -> float | None:
    """Parse an Indian-format figure to float. Parentheses = negative. None if non-numeric."""
    if raw is None:
        return None
    s = str(raw).strip()
    # nil markers: hyphen, en-dash (–), em-dash (—), and words
    if not s or s.lower() in {"not disclosed", "na", "n/a", "nil", "none", "-", "–", "—", "--"}:
        return None
    neg = "(" in s and ")" in s
    s = s.replace("₹", "").replace("`", "").replace("Rs", "").replace("rs", "")
    m = _NUM.search(s.replace(" ", ""))
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def _rel_close(a: float, b: float, tol: float = 0.01) -> bool:
    if a == b:
        return True
    denom = max(abs(a), abs(b), 1.0)
    return abs(a - b) / denom <= tol


def _scale_factor(a: float, b: float) -> int | None:
    """If a/b is ~a power of ten (10/100/1000), return that factor; else None."""
    if a == 0 or b == 0:
        return None
    r = abs(a) / abs(b)
    for f in (10, 100, 1000, 0.1, 0.01, 0.001):
        if _rel_close(r, f, 0.02):
            return int(f) if f >= 1 else None or f
    return None


def classify(extracted: dict, expected_raw: str) -> str:
    exp = parse_number(expected_raw)
    exp_absent = exp is None
    got = parse_number(extracted.get("value")) if extracted else None
    # An extraction whose value is a nil/dash ('-', 'Nil', 'Not disclosed' -> parses to None) is NOT
    # a real found value: it means the line is disclosed-nil, which equals "absent". So a dash vs an
    # absent/dash expectation is CORRECTLY_ABSENT, not SPURIOUS.
    got_found = bool(extracted) and extracted.get("found") and got is not None

    if exp_absent and not got_found:
        return "CORRECTLY_ABSENT"
    if exp_absent and got_found:
        return "SPURIOUS"
    if not got_found:
        return "MISS"
    if got is None:
        return "TRUE_ERROR"
    if _rel_close(got, exp):
        return "MATCH"
    if _scale_factor(got, exp):
        return "SCALE_MISMATCH"
    return "TRUE_ERROR"


def score(company: str, gt_path: str, results_path: str | None = None, out_dir: str = "output",
          verified_path: str | None = _VERIFIED_DEFAULT):
    res_path = results_path or os.path.join(out_dir, f"{company}_thin.json")
    rows = json.load(open(res_path))
    index = {(r["key"], r["scope"]): r for r in rows}

    verified = _load_verified(verified_path)
    gt = [r for r in csv.DictReader(open(gt_path)) if r["company"].strip().lower() == company.lower()]
    counts = Counter()
    detail = []
    overridden = 0
    for g in gt:
        key, scope = g["key"].strip(), g["scope"].strip().lower()
        # Page-verified value (if any) OVERRIDES the external ground truth for this item.
        vkey = (company.lower(), key, scope)
        if vkey in verified:
            expected, src = verified[vkey], "verified"
            overridden += 1
        else:
            expected, src = g["expected_value"], "gt"
        r = index.get((key, scope), {})
        verdict = classify(r, expected)
        counts[verdict] += 1
        detail.append({"key": key, "scope": scope, "verdict": verdict, "expected": expected,
                       "truth_source": src,
                       "extracted": r.get("value", ""), "evidence": r.get("evidence_quote", "")})

    applicable = sum(v for k, v in counts.items() if k != "NA_SCOPE")
    correct = counts["MATCH"] + counts["CORRECTLY_ABSENT"]
    acc = (correct / applicable * 100) if applicable else 0.0
    print(f"\n=== {company} accuracy: {acc:.1f}% on {applicable} applicable items ===")
    if overridden:
        print(f"  (using {overridden} page-verified truth value(s) from {os.path.basename(verified_path)})")
    for k, v in counts.most_common():
        print(f"  {k:18} {v}")
    print("\nMismatches:")
    for d in detail:
        if d["verdict"] in {"TRUE_ERROR", "MISS", "SPURIOUS", "SCALE_MISMATCH"}:
            print(f"  [{d['verdict']:14}] {d['key'][:40]:40} {d['scope']:12} "
                  f"got={d['extracted'][:18]:18} exp={d['expected'][:18]}")
    return acc, counts, detail


if __name__ == "__main__":
    company = sys.argv[1] if len(sys.argv) > 1 else "hindalco"
    gt = sys.argv[2] if len(sys.argv) > 2 else "data/ground_truth.csv"
    if not os.path.exists(gt):
        sys.exit(f"ground truth not found: {gt} (columns: company,key,scope,expected_value)")
    score(company, gt)
