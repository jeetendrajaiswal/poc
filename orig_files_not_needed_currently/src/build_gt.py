"""Build an INDEPENDENT 'careful read' reference for scoring.

Deliberately different from the production pipeline so disagreements are meaningful
(not correlated errors): higher DPI (400), ONE focused single-item question per call,
medium reasoning. This is a *second opinion*, NOT gospel — ground truth can be wrong
too (we already caught a paid-up-vs-outstanding error). The scorer flags disagreements
for investigation; it does not assume this reference is correct.

Usage:  .venv/bin/python -m src.build_gt itc
Output: data/gt_<company>.csv  (company,key,scope,expected_value)
"""
from __future__ import annotations

import base64
import csv
import os
import sys

import fitz
import yaml

from src.config import config
from src.llm import client
from src.locate import locate
from src.structure_map import build_structure_map

PDF_DIR = os.path.expanduser("~/Downloads/")
TAX_DIR = os.path.join(os.path.dirname(__file__), "..", "taxonomy")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
_GT_DPI = 400  # higher than the pipeline's 300 — sharper digits for the reference


def _careful_read(pdf, pages, *, key, definition, column_hint, scope) -> str:
    doc = fitz.open(pdf)
    try:
        imgs = [base64.b64encode(doc[p - 1].get_pixmap(dpi=_GT_DPI).tobytes("png")).decode()
                for p in pages[:3]]
    finally:
        doc.close()
    q = (
        f"Read this {scope} financial-statement page carefully and extract ONE value.\n"
        f"DATA POINT: {key}\nMEANING: {definition.strip()}\nCOLUMN: {column_hint or '-'}\n"
        f"Return ONLY the exact figure as printed for the MOST RECENT year (current-year column, "
        f"not prior-year), parentheses = negative. Double-check every digit. "
        f"If not on this page, reply exactly 'NOT_FOUND'."
    )
    r = client().responses.create(
        model=config.model_default, store=False, max_output_tokens=2500,
        reasoning={"effort": "medium"},
        input=[{"role": "user", "content": (
            [{"type": "input_text", "text": q}]
            + [{"type": "input_image", "image_url": f"data:image/png;base64,{b}"} for b in imgs]
        )}],
    )
    return (getattr(r, "output_text", "") or "").strip()


def run(company: str, defs_name: str = "definitions_thin.yaml", out_name: str | None = None):
    pdf = os.path.join(PDF_DIR, f"{company}.pdf")
    defs = yaml.safe_load(open(os.path.join(TAX_DIR, defs_name)))["items"]
    profile = yaml.safe_load(open(os.path.join(TAX_DIR, "company_profiles.yaml"))).get("companies", {}).get(company, {})
    na_items = set(profile.get("na_items", []))
    sm = build_structure_map(pdf)
    rows = []
    for item in defs:
        if item["key"] in na_items:
            continue
        scopes = ["standalone", "consolidated"] if item["scope"] == "both" else [item["scope"]]
        for scope in scopes:
            cands = locate(sm, item["aliases"], scope, item.get("column_hint"))
            val = "NOT_FOUND"
            if cands:
                val = _careful_read(pdf, [c.page for c in cands], key=item["key"],
                                    definition=item["concept"], column_hint=item.get("column_hint"),
                                    scope=scope)
            lines = [l for l in val.splitlines() if l.strip()]
            clean = "" if (not lines or "NOT_FOUND" in val.upper()) else lines[0].strip()[:40]
            rows.append({"company": company, "key": item["key"], "scope": scope, "expected_value": clean})
            print(f"  {item['key'][:40]:40} {scope:12} -> {clean[:30]}")
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, out_name or f"gt_{company}.csv")
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "key", "scope", "expected_value"])
        w.writeheader(); w.writerows(rows)
    print(f"\nsaved -> {out}")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "itc")
