"""LLM gap-fill pass for raw table extraction — the verification/repair layer.

The deterministic extractor (tables.py) transcribes tables with proven 100%
cell faithfulness; its residual gaps are a handful of table rows on layouts
its geometry heuristics fumble. This pass:

  1. Detects, for FREE, exactly which pages still have table-resident money
     values that no extracted table captured (prose narration is excluded).
  2. Sends ONLY those pages to the model, asking it to transcribe the table
     rows containing the missing values.
  3. GROUNDS every returned cell: a digit-bearing cell whose digit string is
     not printed on the page is discarded (hallucination guard — same
     principle as the datapoint engine's grounding check).
  4. Appends the surviving rows as extra "(recovered)" sheets. It never
     touches a deterministically-extracted table.

    from src.engine.tables_llm import gap_fill
    extra = gap_fill(pdf_path, tables)      # paid: ~1 mini call per gap page
"""
from __future__ import annotations

import re
from collections import defaultdict

import pymupdf

from src.engine.tables import (RawTable, _cluster_rows, _is_numericish,
                               _page_words, _prefilter_rows, _row_text)

_MONEY = re.compile(r"^\(?-?\d{1,3}(,\d{2,3})+(\.\d+)?\)?$|^\(?-?\d+\.\d+\)?$")


def _norm(t: str) -> str:
    return re.sub(r"[^\d.]", "", t or "").strip(".")


def _digits(t: str) -> str:
    return re.sub(r"\D", "", t or "")


def find_gap_pages(pdf_path: str, tables: list[RawTable]) -> dict[int, list[str]]:
    """page -> table-resident money values not captured by any table (free)."""
    doc = pymupdf.open(pdf_path)
    by_page: dict[int, list[RawTable]] = defaultdict(list)
    for t in tables:
        by_page[t.page].append(t)
    gaps: dict[int, list[str]] = {}
    for pno, tabs in by_page.items():
        cap = set()
        for t in tabs:
            for row in t.grid:
                for cell in row:
                    for tok in cell.split():
                        cap.add(_norm(tok))
        rows, _ = _prefilter_rows(_cluster_rows(_page_words(doc[pno - 1])))
        missing: list[str] = []
        for r in rows:
            toks = [w[4] for w in r["words"]]
            miss = [t for t in toks
                    if _MONEY.match(t) and sum(c.isdigit() for c in t) >= 4
                    and _norm(t) not in cap]
            if not miss:
                continue
            # prose rows (mid-sentence values) are not table content
            wordy = sum(1 for t in toks if t.isalpha())
            if wordy >= 0.55 * len(toks):
                continue
            missing.extend(miss)
        if missing:
            gaps[pno] = missing
    doc.close()
    return gaps


_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "title": {"type": "string"},
        "rows": {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
        },
    },
    "required": ["found", "title", "rows"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """You are given the text of one annual-report page and a list of TARGET numbers.
Each target is printed inside a table on this page. Transcribe the COMPLETE table row(s)
(and their table's header row if identifiable) that contain the targets — exactly as printed:
exact labels, exact values, no renaming, no computation, no currency-symbol changes.
Return rows as arrays of cell strings in column order. If the targets are only inside prose
sentences (not a table), return found=false with empty rows."""


def gap_fill(pdf_path: str, tables: list[RawTable], model: str | None = None,
             log=print) -> list[RawTable]:
    """One grounded mini call per gap page. Returns recovered RawTables only."""
    from src.llm import extract_json

    gaps = find_gap_pages(pdf_path, tables)
    if not gaps:
        return []
    doc = pymupdf.open(pdf_path)
    sections = {t.page: t for t in tables}
    recovered: list[RawTable] = []
    per_page_n = {t.page: t.n for t in tables}
    for pno, targets in sorted(gaps.items()):
        rows, _ = _prefilter_rows(_cluster_rows(_page_words(doc[pno - 1])))
        page_text = "\n".join(_row_text(r) for r in rows)
        page_digits = _digits(page_text)
        try:
            out = extract_json(
                instructions=_INSTRUCTIONS,
                user_input=(f"TARGET numbers: {', '.join(targets[:20])}\n\n"
                            f"PAGE TEXT:\n{page_text[:12000]}"),
                schema_name="recovered_table_rows",
                schema=_SCHEMA,
                model=model,
                max_output_tokens=2000,
            )
        except Exception as e:
            log(f"  p{pno}: call failed ({type(e).__name__}: {e})")
            continue
        if not out.get("found") or not out.get("rows"):
            log(f"  p{pno}: model says prose / nothing recoverable")
            continue
        # grounding: every digit-bearing cell must be printed on the page
        grid = []
        for row in out["rows"]:
            cells = [str(c) for c in row]
            ok = all((not _digits(c)) or len(_digits(c)) < 3 or _digits(c) in page_digits
                     for c in cells)
            if ok and any(c.strip() for c in cells):
                grid.append(cells)
        if len(grid) < 1:
            log(f"  p{pno}: all returned rows failed grounding — discarded")
            continue
        width = max(len(r) for r in grid)
        grid = [r + [""] * (width - len(r)) for r in grid]
        ref = sections.get(pno)
        per_page_n[pno] = per_page_n.get(pno, 0) + 1
        recovered.append(RawTable(
            page=pno, n=per_page_n[pno],
            title=f"(recovered) {out.get('title', '')[:60]}",
            scope=ref.scope if ref else "unknown",
            section=ref.section if ref else "",
            page_head=ref.page_head if ref else "",
            units=ref.units if ref else "",
            grid=grid))
        newly = sum(1 for t in targets if any(_norm(t) == _norm(tok)
                    for row in grid for cell in row for tok in cell.split()))
        log(f"  p{pno}: recovered {len(grid)} rows covering {newly}/{len(targets)} targets")
    doc.close()
    return recovered
