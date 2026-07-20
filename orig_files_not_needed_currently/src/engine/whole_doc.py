"""Whole-document path for small reports (e.g. quarterly SEBI Reg-33 results).

When a report fits comfortably in the model's context there's no need to locate
pages — we feed the ENTIRE document at once. Simpler and more accurate (no
locate-recall risk). Statements are pulled in a single call and still pass the
same arithmetic tie-out; free-form questions are answered from the full text.

Quarterly specifics handled here:
  * P&L is multi-column (current quarter / preceding quarter / year-ago quarter /
    YTD / previous year) — we instruct the model to use the MOST RECENT period.
  * Q1/Q3 results carry no Balance Sheet or Cash Flow — those come back null and
    are reported as status "absent", which is correct, not a failure.
"""
from __future__ import annotations

from src import llm
from src.engine import statements as st
from src.engine.statements import StatementResult, _NUM, _SCOPE, _UNIT, _obj, tie_out

# A small doc is one whose extracted text fits with room to spare. ~chars/4 tokens.
SMALL_TOKEN_BUDGET = 100_000


def is_small(page_text: list[str]) -> bool:
    return sum(len(t) for t in page_text) / 4 <= SMALL_TOKEN_BUDGET


# one schema covering all three statements; every block is nullable (absent in Q1/Q3)
_WHOLE_SCHEMA = _obj({
    "scope": _SCOPE, "unit": _UNIT,
    "bs_total_assets": _NUM, "bs_total_equity_and_liabilities": _NUM,
    "pl_total_income": _NUM, "pl_total_expenses": _NUM,
    "pl_profit_before_tax": _NUM, "pl_tax_total": _NUM, "pl_profit_for_period": _NUM,
    "cf_net_operating": _NUM, "cf_net_investing": _NUM, "cf_net_financing": _NUM,
    "cf_forex_effect": _NUM, "cf_net_change": _NUM,
    "cf_opening_cash": _NUM, "cf_closing_cash": _NUM,
}, ["scope", "unit",
    "bs_total_assets", "bs_total_equity_and_liabilities",
    "pl_total_income", "pl_total_expenses", "pl_profit_before_tax", "pl_tax_total", "pl_profit_for_period",
    "cf_net_operating", "cf_net_investing", "cf_net_financing", "cf_forex_effect",
    "cf_net_change", "cf_opening_cash", "cf_closing_cash"])

_WHOLE_INSTR = (
    "This is a full (small) financial report — often a quarterly SEBI results filing. Extract the "
    "primary-statement figures. For the Statement of Profit & Loss use the MOST RECENT period column "
    "(the latest 'quarter / 3 months ended', else the latest year). For any statement NOT present in "
    "this document (quarterly Q1/Q3 filings often omit the Balance Sheet and Cash Flow), set its "
    "fields to null. Numbers only; parentheses or a leading minus mean negative; '-'/blank = null. "
    "bs_total_assets = grand total of assets; bs_total_equity_and_liabilities = grand total of the "
    "equity+liabilities side; pl_* = profit & loss; cf_* = cash flow net figures."
)


def _bucket(o: dict, prefix: str, fields: dict[str, str]) -> dict:
    """Remap whole-doc keys (bs_total_assets) to per-statement keys (grand_total_a)."""
    return {dst: o.get(src) for dst, src in fields.items()}


_MAP = {
    "bs": {"grand_total_a": "bs_total_assets", "grand_total_b": "bs_total_equity_and_liabilities"},
    "pl": {"total_income": "pl_total_income", "total_expenses": "pl_total_expenses",
           "profit_before_tax": "pl_profit_before_tax", "tax_total": "pl_tax_total",
           "profit_for_year": "pl_profit_for_period"},
    "cf": {"net_operating": "cf_net_operating", "net_investing": "cf_net_investing",
           "net_financing": "cf_net_financing", "forex_effect": "cf_forex_effect",
           "net_change": "cf_net_change", "opening_cash": "cf_opening_cash",
           "closing_cash": "cf_closing_cash"},
}


def statements_whole(full_text: str | None = None, *, file_id: str | None = None) -> dict[str, StatementResult]:
    """Extract & tie-out all three statements in ONE call from the whole document.

    Pass `full_text` for a text PDF, or `file_id` (an uploaded PDF) for a scanned
    one so the model reads it natively. Returns {bs,pl,cf} StatementResult; a
    statement absent from the document gets status 'absent'.
    """
    o = llm.extract_json(
        instructions=_WHOLE_INSTR,
        user_input=("Extract from the attached file." if file_id
                    else f"REPORT:\n{full_text}"),
        schema_name="whole", schema=_WHOLE_SCHEMA,
        file_ids=[file_id] if file_id else None,
        max_output_tokens=1200, reasoning="low",
    )
    out: dict[str, StatementResult] = {}
    for k in st.KINDS:
        sub = _bucket(o, k, _MAP[k]) if not o.get("_empty") else {}
        present = any(sub.get(f) not in (None, "") for f in sub)
        ok, anchor = tie_out(k, sub)
        if ok:
            out[k] = StatementResult(kind=k, status="tie", unit=o.get("unit"),
                                     scope=o.get("scope"), anchor=anchor, data=sub)
        else:
            out[k] = StatementResult(kind=k, status="no-tie" if present else "absent", data=sub)
    return out
