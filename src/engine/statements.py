"""Primary-statement engine: locate -> extract -> arithmetic-verify.

Handles the three primary financial statements (Balance Sheet, Statement of
Profit & Loss, Cash Flow) across all four Indian regulatory formats
(Schedule III Div II manufacturer, Div III NBFC, Banking Reg. Act Form A bank,
IRDAI insurer) and both scopes (standalone / consolidated).

Design (validated on 39 reports, 117 statements, 38/38 text-based reports
self-validated, ~$0.0046/statement with the mini model):

  1. LOCATE  — generous candidate pages via content fingerprints UNION despaced
               titles. Recall matters here, precision does not: false candidates
               are cheap because step 3 rejects them.
  2. EXTRACT — the mini model transcribes the few figures each statement's
               identity needs (faithful, store=False, nothing uploaded).
  3. VERIFY  — the statement's accounting identity must hold to tolerance. The
               first candidate that ties wins; that tie IS the correctness proof.

A completeness re-ask recovers transient under-extraction (a side/total returned
null) once before giving up on a candidate.
"""
from __future__ import annotations

import functools
import re
import subprocess
from dataclasses import dataclass, field
from typing import Optional

import fitz  # PyMuPDF

from src import llm

KINDS = ("bs", "pl", "cf")
UNIT_ENUM = ["crore", "lakh", "million", "billion", "thousand"]
_CANDIDATE_CAP = 8


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def _collapse(s: str) -> str:
    """Lowercase + strip ALL whitespace, so letter-spaced/justified headings
    like 'B A L A N C E  S H E E T' match a plain regex."""
    return re.sub(r"\s+", "", s.lower())


def _layout_text(path: str, page: int) -> str:
    """One page of poppler layout-preserved text (columns kept aligned)."""
    return subprocess.run(
        ["pdftotext", "-layout", "-f", str(page), "-l", str(page), path, "-"],
        capture_output=True, text=True,
    ).stdout


def _num(s) -> Optional[float]:
    """Parse a reported figure. Parentheses mean negative; a leading minus (ASCII
    or the Unicode −/– glyphs) is parsed by the regex itself. A bare '-' (nil) and
    blanks parse to None so they never masquerade as zero."""
    if s is None:
        return None
    s = str(s).replace("−", "-").replace("–", "-").replace("—", "-")
    neg_paren = "(" in s and ")" in s
    m = re.search(r"-?\d[\d,]*\.?\d*", s.replace(" ", ""))
    if not m:
        return None
    val = float(m.group(0).replace(",", ""))   # leading '-' already included here
    return -abs(val) if neg_paren else val


def _tol(x: float, frac: float = 0.005, floor: float = 1.0) -> float:
    return max(abs(x) * frac, floor)


def _any(c: str, *toks: str) -> bool:
    return any(t in c for t in toks)


# --------------------------------------------------------------------------- #
# 1. LOCATE — generous candidate pages (financials-region first)
# --------------------------------------------------------------------------- #
def candidate_pages(path: str, kind: str) -> list[int]:
    """Return 1-based candidate page numbers for `kind`, best first.

    Fingerprints are deliberately broad (title UNION characteristic line-items);
    arithmetic verification downstream discards anything that doesn't tie. The
    financials region (back ~60% of the document) is searched first.
    """
    doc = fitz.open(path)
    n = doc.page_count
    hits: list[tuple[int, float]] = []
    for p in range(n):
        c = _collapse(doc[p].get_text())
        hit = False
        if kind == "bs":
            hit = (
                ("totalassets" in c and _any(
                    c, "totalequityandliabilities", "totalliabilitiesandequity",
                    "totalequity", "totalliabilities", "capitalandliabilities"))
                # assets side of a BS whose liabilities total spills to the next page
                # (NBFC Div III / some banks span two pages): anchor on the assets
                # total + an asset-section header; the extract window reads pg+1.
                or ("totalassets" in c and _any(c, "financialassets", "noncurrentassets"))
                or ("sourcesoffunds" in c and "applicationoffunds" in c)   # IRDAI / old Sch.VI
                or ("capitalandliabilities" in c and "deposits" in c)      # bank Form A
                or bool(re.search(r"balancesheetasat", c))                 # despaced title
            )
        elif kind == "pl":
            revenue = _any(c, "revenuefromoperations", "interestearned", "premiumearned",
                           "grosspremium", "incomefrominvestments", "totalincome", "totalrevenue")
            # EPS footer is the most rephrase-/image-robust P&L marker
            profit = _any(c, "earningspershare", "taxexpense", "profitbeforetax",
                          "profitbeforetaxation", "beforetax", "fortheyear", "totalexpenses")
            title = _any(c, "statementofprofitandloss", "profitandlossaccount", "revenueaccount")
            hit = (revenue and profit) or title
        elif kind == "cf":
            # CF always opens with operating activities; investing/financing may
            # spill onto later pages, captured by the wide extract window.
            hit = "operatingactivities" in c or "cashflowfromoperatingactivities" in c
        if hit:
            hits.append((p + 1, (p + 1) / n))
    doc.close()
    hits.sort(key=lambda x: (x[1] < 0.4, x[0]))   # financials region (back ~60%) first
    return [p for p, _ in hits][:_CANDIDATE_CAP]


# --------------------------------------------------------------------------- #
# 2. EXTRACT — schemas + instructions per statement
# --------------------------------------------------------------------------- #
def _obj(props: dict, req: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False}


_STR = {"type": "string"}
_SCOPE = {"type": "string", "enum": ["standalone", "consolidated", "unknown"]}
_NUM = {"type": ["string", "null"]}            # numbers arrive as strings; '-'/blank -> null
_UNIT = {"type": "string", "enum": UNIT_ENUM}  # real enum: stops the model echoing the choice list

_SCHEMA = {
    "bs": _obj({"scope": _SCOPE, "unit": _UNIT, "grand_total_a": _NUM, "grand_total_b": _NUM},
               ["scope", "unit", "grand_total_a", "grand_total_b"]),
    "pl": _obj({"scope": _SCOPE, "unit": _UNIT, "total_income": _NUM, "total_expenses": _NUM,
                "profit_before_tax": _NUM, "tax_total": _NUM, "profit_for_year": _NUM},
               ["scope", "unit", "total_income", "total_expenses",
                "profit_before_tax", "tax_total", "profit_for_year"]),
    "cf": _obj({"scope": _SCOPE, "unit": _UNIT, "net_operating": _NUM, "net_investing": _NUM,
                "net_financing": _NUM, "forex_effect": _NUM, "net_change": _NUM,
                "opening_cash": _NUM, "closing_cash": _NUM},
               ["scope", "unit", "net_operating", "net_investing", "net_financing",
                "forex_effect", "net_change", "opening_cash", "closing_cash"]),
}

_INSTR = {
    "bs": ("From this Balance Sheet extract ONLY the two grand totals (current year). "
           "grand_total_a = grand TOTAL of the assets / Application-of-Funds side. "
           "grand_total_b = grand TOTAL of the equity+liabilities / Capital&Liabilities / "
           "Sources side. Both grand totals MUST be filled."),
    "pl": ("From this Statement of Profit & Loss extract: total_income (Total Income/Revenue), "
           "total_expenses (Total Expenses), profit_before_tax (Profit before tax, after "
           "exceptional items), tax_total (total tax = current+deferred), profit_for_year "
           "(Profit for the year/period). Numbers only; () or leading - means negative."),
    "cf": ("From this Cash Flow Statement extract the NET figures: net_operating (net cash from "
           "operating activities), net_investing, net_financing, forex_effect (effect of "
           "exchange-rate changes; null if none), net_change (net increase/decrease in cash & "
           "cash equivalents), opening_cash (at beginning), closing_cash (at end). "
           "Numbers only; () means negative."),
}

_REASK = {
    "bs": " You previously MISSED one grand total. Re-read and return BOTH grand_total_a (assets) "
          "AND grand_total_b (equity+liabilities).",
    "pl": " You previously missed a figure. Re-read and fill profit_before_tax, tax_total and "
          "profit_for_year.",
    "cf": " You previously missed a figure. Re-read and fill net_operating, net_investing, "
          "net_financing, net_change, opening_cash and closing_cash.",
}

_EXTRACT_SPAN = {"bs": 2, "pl": 2, "cf": 3}    # CF may spread operating/investing/financing over 3 pages
_MAX_TOK = 4000


# --------------------------------------------------------------------------- #
# 3. VERIFY — accounting identities
# --------------------------------------------------------------------------- #
def tie_out(kind: str, o: dict) -> tuple[bool, Optional[float]]:
    """Return (identity_holds, anchor_value). The anchor is the figure the
    identity pins down — its presence is the per-statement correctness proof."""
    if kind == "bs":
        a, b = _num(o.get("grand_total_a")), _num(o.get("grand_total_b"))
        if a and b and abs(a - b) < _tol(a):
            return True, a
        return False, None

    if kind == "pl":
        pbt, tax, pat = (_num(o.get("profit_before_tax")), _num(o.get("tax_total")),
                         _num(o.get("profit_for_year")))
        # primary: PBT - tax = PAT (near-universal across all four formats)
        if None not in (pbt, tax, pat) and abs((pbt - tax) - pat) < _tol(pat, floor=2):
            return True, pat
        # fallback: Total income - Total expenses = PBT (looser; exceptional items vary)
        ti, te = _num(o.get("total_income")), _num(o.get("total_expenses"))
        if None not in (ti, te, pbt) and abs((ti - te) - pbt) < _tol(pbt, frac=0.02, floor=5):
            return True, pbt
        return False, None

    if kind == "cf":
        op, inv, fin = (_num(o.get("net_operating")), _num(o.get("net_investing")),
                        _num(o.get("net_financing")))
        fx = _num(o.get("forex_effect")) or 0.0
        nc, opening, closing = (_num(o.get("net_change")), _num(o.get("opening_cash")),
                                _num(o.get("closing_cash")))
        # anchor A: operating + investing + financing (+ forex) = net change
        if None not in (op, inv, fin, nc) and abs((op + inv + fin + fx) - nc) < _tol(nc, frac=0.02, floor=5):
            return True, nc
        # anchor B: opening + net change (+ forex) = closing
        if None not in (nc, opening, closing) and abs((opening + nc + fx) - closing) < _tol(closing, frac=0.02, floor=5):
            return True, closing
        return False, None

    raise ValueError(f"unknown statement kind: {kind!r}")


def _extract_once(path: str, page: int, kind: str, extra: str = "") -> dict:
    span = _EXTRACT_SPAN[kind]
    text = "\n".join(f"=== PAGE {q} ===\n{_layout_text(path, q)}" for q in range(page, page + span))
    return llm.extract_json(
        instructions=_INSTR[kind] + extra,
        user_input=text,
        schema_name=kind,
        schema=_SCHEMA[kind],
        max_output_tokens=_MAX_TOK,
        reasoning="low",
    )


def _extract_with_reask(path: str, page: int, kind: str) -> tuple[dict, bool, Optional[float]]:
    o = _extract_once(path, page, kind)
    ok, anchor = tie_out(kind, o)
    if not ok:                                   # completeness re-ask, once
        o2 = _extract_once(path, page, kind, _REASK[kind])
        ok2, anchor2 = tie_out(kind, o2)
        if ok2:
            return o2, True, anchor2
    return o, ok, anchor


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class StatementResult:
    kind: str
    status: str                       # "tie" | "no-tie" | "no-candidate"
    page: Optional[int] = None
    unit: Optional[str] = None
    scope: Optional[str] = None
    anchor: Optional[float] = None
    data: dict = field(default_factory=dict)

    @property
    def validated(self) -> bool:
        return self.status == "tie"


@dataclass
class ReportResult:
    path: str
    image_only: bool
    statements: dict[str, StatementResult]

    @property
    def fully_validated(self) -> bool:
        return (not self.image_only
                and all(self.statements[k].validated for k in KINDS))


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=256)
def validate_statement(path: str, kind: str) -> StatementResult:
    """Locate -> extract -> verify a single statement. First tie wins.

    Memoized per (path, kind): the locate->tie-out loop is the expensive multi-call
    step, and it is re-needed by the statement layer, by both scopes of the datapoint
    layer, etc. Caching returns the identical result without re-running it — purely a
    cost saving, no behaviour change. (Per-process cache; a fresh run recomputes.)
    """
    pages = candidate_pages(path, kind)
    if not pages:
        return StatementResult(kind=kind, status="no-candidate")
    for page in pages:
        o, ok, anchor = _extract_with_reask(path, page, kind)
        if ok:
            return StatementResult(kind=kind, status="tie", page=page,
                                   unit=o.get("unit"), scope=o.get("scope"),
                                   anchor=anchor, data=o)
    return StatementResult(kind=kind, status="no-tie", page=pages[0])


def is_image_pdf(path: str, sample_pages: int = 60, min_chars: int = 1500) -> bool:
    """True when the financials carry no extractable text (scanned/image PDF) and
    must be routed to the vision fallback. Samples evenly across the document so a
    text cover page can't mask image-only statements."""
    doc = fitz.open(path)
    n = doc.page_count
    step = max(1, n // sample_pages)
    total = sum(len(doc[p].get_text()) for p in range(0, n, step))
    doc.close()
    return total < min_chars


def validate_report(path: str) -> ReportResult:
    """Locate + extract + verify all three primary statements for one report."""
    if is_image_pdf(path):
        return ReportResult(path=path, image_only=True,
                            statements={k: StatementResult(kind=k, status="no-candidate") for k in KINDS})
    statements = {k: validate_statement(path, k) for k in KINDS}
    # a report whose statements are entirely unlocatable in text is effectively image-only
    image_only = all(s.status == "no-candidate" for s in statements.values())
    return ReportResult(path=path, image_only=image_only, statements=statements)
