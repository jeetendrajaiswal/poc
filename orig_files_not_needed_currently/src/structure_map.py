"""Deterministic local structure map of an annual-report PDF.

Built locally with PyMuPDF — no model calls, no upload. Provides the backbone the
extraction pipeline scopes against:

- per-page text (cached)
- Standalone vs Consolidated **region** boundaries (handles either ordering —
  some companies present consolidated first, some standalone first)
- a notes index (Note N -> page) when present

The region map is the mechanism behind "don't grab the consolidated number for a
standalone field": every page is tagged STANDALONE / CONSOLIDATED / FRONT (narrative
/ pre-financials), so a locate can be scoped to the correct half.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Optional

import fitz  # PyMuPDF


# Region is decided ONLY from heading/footer lines, never from inline prose — an
# inline cross-reference like "note 2.10 of the consolidated financial statements"
# inside the standalone section must NOT flip the region. So we test, line by line:
#   - statement headings that START with an (optional scope word +) statement name
#     e.g. "Consolidated Balance Sheet", "BALANCE SHEET", "Statement of Profit and Loss"
#   - notes running titles / footers: "Notes ... financial statements",
#     "forming part of the <scope> financial statements"
# Standalone is the UNLABELED default (a heading without "consolidated" → standalone).
_STMT_NAMES = (
    r"(balance sheet|statement of profit and loss|statement of cash flows?|"
    r"cash flow statement|statement of changes in equity)"
)
# A statement HEADING: the title must be the line, or be followed by 'as at' / a date
# / '(' / ':' — so narrative like "balance sheet resilience." does NOT match.
_TITLE_TAIL = r"\s*($|[\(:]|as at|as on|for the (year|period)|\d{1,2}\b|₹|rs\.?\b|in\b)"
_HEAD_STMT = re.compile(rf"^(consolidated\s+|standalone\s+)?{_STMT_NAMES}{_TITLE_TAIL}", re.I)
_HEAD_NOTES = re.compile(r"^(notes\b|forming part\b).*financial statements?", re.I)
_FOOTER_FORMING = re.compile(r"forming part of the\s+(consolidated|standalone)?\s*financial statements?", re.I)
_FORMING_CONSOL = re.compile(r"forming part of the\s+consolidated\s+financial statements?", re.I)

# Where the financial-statements block begins: a STRONG anchor only — the auditor's
# report, a scope-qualified balance sheet, or "Balance Sheet as at <date>". A bare
# lowercase "balance sheet" (a chart label in MD&A) must NOT trigger it.
_FS_START = re.compile(
    r"^(independent auditor'?s report\b|"
    r"(consolidated|standalone)\s+balance sheet\b|"
    r"balance sheet\s+(as at|as on|\(|\d))", re.I
)

Region = str  # "STANDALONE" | "CONSOLIDATED" | "FRONT"


@dataclass
class StructureMap:
    path: str
    page_count: int
    page_text: list[str] = field(repr=False)
    region: list[Region] = field(repr=False)  # one tag per page (0-indexed)

    @cached_property
    def region_spans(self) -> list[tuple[Region, int, int]]:
        """Contiguous (region, start_page, end_page) spans, 1-indexed inclusive."""
        spans: list[tuple[Region, int, int]] = []
        if not self.region:
            return spans
        cur = self.region[0]
        start = 0
        for i in range(1, self.page_count):
            if self.region[i] != cur:
                spans.append((cur, start + 1, i))
                cur, start = self.region[i], i
        spans.append((cur, start + 1, self.page_count))
        return spans

    def pages_for(self, region: Region) -> list[int]:
        """1-indexed page numbers tagged with `region`."""
        return [i + 1 for i, r in enumerate(self.region) if r == region]

    @cached_property
    def has_consolidated(self) -> bool:
        """Does this company file Consolidated Financial Statements?"""
        if any(r == "CONSOLIDATED" for r in self.region):
            return True
        return any(
            re.search(r"consolidated\s+balance sheet", t, re.I)
            or _FORMING_CONSOL.search(t)
            for t in self.page_text
        )

    @cached_property
    def has_standalone(self) -> bool:
        """Standalone is always filed; True whenever any financials are present."""
        return any(r != "FRONT" for r in self.region)

    def text(self, page_1indexed: int) -> str:
        return self.page_text[page_1indexed - 1]


def _bs_markers(lc: str) -> bool:
    """True if page text looks like a Balance-Sheet statement (not a mention)."""
    structural = (
        "equity and liabilities" in lc
        or "total equity" in lc
        or ("total assets" in lc and "total liabilities" in lc)
    )
    return structural and ("as at" in lc or "as on" in lc)


def detect_bs_anchors(page_text: list[str]) -> tuple[Optional[int], Optional[int]]:
    """Auto-detect the standalone & consolidated Balance-Sheet *statement* pages.

    Content-based and general (no hardcoded pages, works on any report/year): a page
    is the BS statement when it has a short 'Balance Sheet' heading AND statement
    markers (equity/liabilities/total-equity + 'as at'). The markers requirement
    rejects narrative mentions and cross-references. Returns (standalone_bs,
    consolidated_bs), 1-indexed; either may be None.
    """
    sbs = cbs = None
    for i, t in enumerate(page_text):
        if not _bs_markers(t.lower()):
            continue
        for ln in t.splitlines():
            s = ln.strip()
            if len(s) > 70 or not re.match(r"(consolidated\s+|standalone\s+)?balance sheet\b", s, re.I):
                continue
            if "consolidated" in s.lower():
                if cbs is None:
                    cbs = i + 1
            elif sbs is None:
                sbs = i + 1
            break
    return sbs, cbs


def _is_fs_start(text: str) -> bool:
    """True if a page begins the financial-statements block (auditor report / BS heading)."""
    for raw in text.splitlines():
        line = raw.strip()
        if line and len(line) < 70 and _FS_START.match(line):
            return True
    return False


def _scope_signal(text: str) -> Optional[Region]:
    """Decide a page's scope from HEADING/FOOTER lines only (never inline prose).

    A heading with 'consolidated' → CONSOLIDATED; a statement/notes heading WITHOUT
    'consolidated' → STANDALONE (the unlabeled default). A page that yields both
    returns None so it doesn't flip the running region.
    """
    consolidated = standalone = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or len(line) > 70:
            continue
        lc = line.lower()
        is_head = bool(_HEAD_STMT.match(line) or _HEAD_NOTES.match(line) or _FOOTER_FORMING.search(line))
        if not is_head:
            continue
        if "consolidated" in lc:
            consolidated = True
        else:
            standalone = True
    if consolidated and not standalone:
        return "CONSOLIDATED"
    if standalone and not consolidated:
        return "STANDALONE"
    return None


def build_structure_map(
    path: str,
    standalone_bs: Optional[int] = None,
    consolidated_bs: Optional[int] = None,
) -> StructureMap:
    doc = fitz.open(path)
    n = doc.page_count
    page_text = [doc[i].get_text("text") for i in range(n)]
    doc.close()

    # AUTO-DETECT the two Balance-Sheet anchor pages from content (general — no
    # hardcoded page numbers). Explicit args act only as an override for edge cases.
    if not (standalone_bs and consolidated_bs):
        det_s, det_c = detect_bs_anchors(page_text)
        standalone_bs = standalone_bs or det_s
        consolidated_bs = consolidated_bs or det_c

    # Anchor segmentation: block boundaries are the two BS statement pages.
    # Everything before the first BS is FRONT. (Robust to scope ordering.)
    if standalone_bs and consolidated_bs:
        a, b = sorted((standalone_bs, consolidated_bs))
        scope_a = "STANDALONE" if a == standalone_bs else "CONSOLIDATED"
        scope_b = "STANDALONE" if b == standalone_bs else "CONSOLIDATED"
        region = ["FRONT"] * n
        for i in range(a - 1, b - 1):
            region[i] = scope_a
        for i in range(b - 1, n):
            region[i] = scope_b
        return StructureMap(path=path, page_count=n, page_text=page_text, region=region)

    # Locate where the financial-statements block begins; everything before stays
    # FRONT (board's report, MD&A, BRSR — narrative that incidentally says
    # "consolidated"/"standalone").
    fs_start = next(
        (i for i in range(n) if _is_fs_start(page_text[i])), n
    )

    # Within the financials block, STANDALONE is the unlabeled default (some
    # reports — e.g. Dr. Reddy's, Reliance — only label consolidated notes). Flip
    # to CONSOLIDATED on a consolidated signal, back to STANDALONE on a standalone
    # signal; carry-forward fills unlabeled pages.
    region: list[Region] = ["FRONT"] * n
    cur: Region = "FRONT"
    for i in range(fs_start, n):
        sig = _scope_signal(page_text[i])
        if sig is not None:
            cur = sig
        elif cur == "FRONT":
            cur = "STANDALONE"
        region[i] = cur

    return StructureMap(path=path, page_count=n, page_text=page_text, region=region)


if __name__ == "__main__":
    import os

    home = os.path.expanduser("~/Downloads/")
    for f in ["reddy", "adani", "reliance", "itc", "infosys", "hindalco"]:
        p = home + f + ".pdf"
        if not os.path.exists(p):
            print(f"{f:10} MISSING")
            continue
        sm = build_structure_map(p)
        sa = sm.pages_for("STANDALONE")
        co = sm.pages_for("CONSOLIDATED")
        fr = sm.pages_for("FRONT")
        # which scope's financials appear first?
        first = "—"
        for r in sm.region:
            if r in ("STANDALONE", "CONSOLIDATED"):
                first = r
                break
        print(
            f"{f:10} {sm.page_count:4d}p | FRONT={len(fr):3d} "
            f"STANDALONE={len(sa):3d} CONSOLIDATED={len(co):3d} | first-financials={first}"
        )
        # show the span structure compactly
        spans = [f"{r[:2]}{a}-{b}" for r, a, b in sm.region_spans]
        print("           spans:", " ".join(spans))
