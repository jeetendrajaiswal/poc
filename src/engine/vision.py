"""Vision fallback for scanned / image-only PDFs (the ~3% with no text layer).

Text fingerprints can't locate statements when a page has no extractable text, so
we fall back to the model's eyes:

  LOCATE  — render the financials region (back ~60%) at low DPI and ask the model,
            in small batches, which pages are the Balance Sheet / P&L / Cash Flow.
  EXTRACT — re-render the chosen page at high DPI and read the same figures the
            text engine reads, so the SAME arithmetic tie-out verifies the result.

Cost is higher than the text path but bounded, and only paid for image PDFs.
"""
from __future__ import annotations

import base64
from typing import Optional

import fitz

from src import llm
from src.engine import statements as st

_SCAN_DPI = 110          # cheap, legible enough to classify a page's role
_READ_DPI = 300          # crisp digits for extraction
_BATCH = 4               # pages per classification call
_REGION_START = 0.35     # statements live in the back of the document
_MAX_SCAN = 80           # hard cap on pages scanned per report


def render_page(path: str, page: int, dpi: int) -> str:
    """Render a 1-based page to a base64 PNG."""
    doc = fitz.open(path)
    pix = doc[page - 1].get_pixmap(dpi=dpi)
    data = pix.tobytes("png")
    doc.close()
    return base64.b64encode(data).decode()


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "roles": {
            "type": "array",
            "description": "one entry per image, in the SAME order as provided",
            "items": {
                "type": "object",
                "properties": {
                    "page_label": {"type": "string"},
                    "role": {"type": "string",
                             "enum": ["balance_sheet", "profit_loss", "cash_flow", "other"]},
                },
                "required": ["page_label", "role"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["roles"],
    "additionalProperties": False,
}

_CLASSIFY_INSTR = (
    "You are shown page images from an Indian annual report. For EACH image, classify "
    "whether it is the primary Balance Sheet (balance_sheet), the Statement of Profit & "
    "Loss / Revenue Account (profit_loss), the Cash Flow Statement (cash_flow), or "
    "anything else (other). Classify only the MAIN statement pages, not notes/schedules "
    "that merely reference them. Return one role per image, in order."
)

_KIND_ROLE = {"bs": "balance_sheet", "pl": "profit_loss", "cf": "cash_flow"}


def locate_vision(path: str) -> dict[str, list[int]]:
    """Scan the financials region visually; return candidate pages per kind."""
    doc = fitz.open(path)
    n = doc.page_count
    doc.close()
    start = int(n * _REGION_START)
    pages = list(range(start + 1, n + 1))[:_MAX_SCAN]
    found: dict[str, list[int]] = {"bs": [], "pl": [], "cf": []}
    for i in range(0, len(pages), _BATCH):
        batch = pages[i:i + _BATCH]
        imgs = [render_page(path, p, _SCAN_DPI) for p in batch]
        labels = ", ".join(f"image {j + 1} = page {p}" for j, p in enumerate(batch))
        out = llm.extract_json(
            instructions=_CLASSIFY_INSTR,
            user_input=f"Classify these {len(batch)} page images ({labels}).",
            schema_name="page_roles", schema=_CLASSIFY_SCHEMA,
            images_b64=imgs, max_output_tokens=600, reasoning="low",
        )
        for j, r in enumerate(out.get("roles", [])):
            if j >= len(batch):
                break
            for kind, role in _KIND_ROLE.items():
                if r.get("role") == role:
                    found[kind].append(batch[j])
        if all(found[k] for k in found):          # got at least one of each -> stop early
            break
    return found


_VISION_EXTRACT_INSTR = {
    "bs": ("Read this Balance Sheet PAGE IMAGE. Extract ONLY the two grand totals (current year). "
           "grand_total_a = grand TOTAL of the assets/Application side. "
           "grand_total_b = grand TOTAL of the equity+liabilities/Capital&Liabilities/Sources side."),
    "pl": ("Read this Statement of Profit & Loss PAGE IMAGE. Extract total_income, total_expenses, "
           "profit_before_tax (after exceptional items), tax_total (current+deferred), profit_for_year."),
    "cf": ("Read this Cash Flow PAGE IMAGE. Extract net_operating, net_investing, net_financing, "
           "forex_effect (null if none), net_change, opening_cash, closing_cash."),
}


def _extract_vision(path: str, page: int, kind: str) -> dict:
    img = render_page(path, page, _READ_DPI)
    return llm.extract_json(
        instructions=_VISION_EXTRACT_INSTR[kind] + " Numbers only; () means negative.",
        user_input="Extract from the attached page image.",
        schema_name=kind, schema=st._SCHEMA[kind],
        images_b64=[img], max_output_tokens=st._MAX_TOK, reasoning="low",
    )


def validate_statement_vision(path: str, kind: str,
                              candidates: Optional[list[int]] = None) -> st.StatementResult:
    """Vision locate (if needed) + vision extract + the same arithmetic tie-out."""
    pages = candidates if candidates is not None else locate_vision(path).get(kind, [])
    if not pages:
        return st.StatementResult(kind=kind, status="no-candidate")
    for page in pages:
        o = _extract_vision(path, page, kind)
        ok, anchor = st.tie_out(kind, o)
        if ok:
            return st.StatementResult(kind=kind, status="tie", page=page,
                                      unit=o.get("unit"), scope=o.get("scope"),
                                      anchor=anchor, data=o)
    return st.StatementResult(kind=kind, status="no-tie", page=pages[0])


def validate_report_vision(path: str) -> st.ReportResult:
    """Full 3-statement validation for an image-only PDF, via vision."""
    located = locate_vision(path)
    out = {k: validate_statement_vision(path, k, located.get(k, [])) for k in st.KINDS}
    return st.ReportResult(path=path, image_only=True, statements=out)
