"""Independently re-verify every disputed item against the PDF (4-way adjudication).

For each (key, scope) where pipeline or GT was wrong per the external adjudication, do a
focused, high-fidelity read (400 DPI, top-3 pages, HIGH reasoning) from the report itself
and record OUR independent truth — then compare all four sources:
    pipeline · our_GT · chatgpt · my_read
so no single source (including ChatGPT) is trusted blindly.

  .venv/bin/python -m src.verify_disputes hindalco
"""
from __future__ import annotations

import base64
import csv
import os
import sys

import fitz
import openpyxl
import yaml

from src.config import config
from src.llm import client
from src.locate import locate
from src.structure_map import build_structure_map

PDF_DIR = os.path.expanduser("~/Downloads/")
TAX = os.path.join(os.path.dirname(__file__), "..", "taxonomy")
DL = os.path.expanduser("~/Downloads/")
GOOD = {"MATCH_BOTH", "BOTH_NOT_DISCLOSED"}


def _careful(doc, pages, *, key, concept, col, scope) -> str:
    imgs = [base64.b64encode(doc[p - 1].get_pixmap(dpi=400).tobytes("png")).decode() for p in pages[:3]]
    q = (f"Read carefully and extract ONE value from this {scope} statement/note, current year only.\n"
         f"DATA POINT: {key}\nMEANING: {concept}\nCOLUMN/RULE: {col or '-'}\n"
         f"Rules: parentheses=negative; if the exact concept is not disclosed at this granularity say "
         f"'Not disclosed'; do not substitute a different line. Reply with the number (or 'Not disclosed') "
         f"then a brief 'where:' note.")
    content = [{"type": "input_text", "text": q}] + [
        {"type": "input_image", "image_url": f"data:image/png;base64,{b}"} for b in imgs]
    r = client().responses.create(model=config.model_default, store=False, max_output_tokens=2500,
                                  reasoning={"effort": "high"},
                                  input=[{"role": "user", "content": content}])
    return (getattr(r, "output_text", "") or "").strip().replace("\n", " ")[:160]


def run(company: str):
    # adjudication (chatgpt)
    wb = openpyxl.load_workbook(os.path.join(DL, f"{company}_independent_adjudication.xlsx"))
    ws = wb["Adjudication"]; arows = list(ws.iter_rows(values_only=True)); ah = {h: i for i, h in enumerate(arows[0])}
    adj = {(r[ah["key"]], r[ah["scope"]]): (r[ah["verdict"]], r[ah["true_value"]]) for r in arows[1:]}
    # our comparison (pipeline + gt)
    comp = {}
    with open(f"output/{company}_comparison.csv") as fh:
        for r in csv.DictReader(fh):
            comp[(r["key"], r["scope"])] = (r["extracted_value"], r["gt_value"])
    defs = {i["key"]: i for i in yaml.safe_load(open(os.path.join(TAX, "definitions.yaml")))["items"]}

    pdf = os.path.join(PDF_DIR, f"{company}.pdf"); sm = build_structure_map(pdf); doc = fitz.open(pdf)
    out_rows = []
    disputed = [(k, v) for (k, v) in adj.items() if v[0] not in GOOD]
    print(f"{company}: {len(disputed)} disputed items to re-verify\n")
    for (key, scope), (verdict, ct) in disputed:
        d = defs.get(key, {})
        cands = locate(sm, d.get("aliases", [key]), scope, d.get("column_hint"))
        mine = _careful(doc, [c.page for c in cands], key=key, concept=d.get("concept", ""),
                        col=d.get("column_hint"), scope=scope) if cands else "Not disclosed (no candidate page)"
        pipe, gt = comp.get((key, scope), ("?", "?"))
        out_rows.append({"key": key, "scope": scope, "verdict": verdict,
                         "pipeline": pipe, "our_gt": gt, "chatgpt": ct, "my_read": mine})
        print(f"[{verdict[:16]:16}] {key[:40]:40} {scope:12}\n    pipe={pipe!s:14} gt={gt!s:14} cgpt={ct!s:14}\n    MINE: {mine}\n")
    doc.close()
    with open(f"output/{company}_disputes_verified.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["key", "scope", "verdict", "pipeline", "our_gt", "chatgpt", "my_read"])
        w.writeheader(); w.writerows(out_rows)
    print(f"saved -> output/{company}_disputes_verified.csv")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "hindalco")
