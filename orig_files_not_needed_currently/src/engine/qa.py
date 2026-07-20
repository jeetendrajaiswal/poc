"""Ask-anything layer: answer arbitrary questions about a report, with citations.

Pipeline (cheap, grounded, no hallucinated numbers):

  1. RETRIEVE — BM25 over per-page text finds the handful of pages most likely to
     hold the answer (synonyms from the KPI catalog widen recall).
  2. EXTRACT  — the mini model answers ONLY from those pages and must return the
     page number plus a verbatim quote it used.
  3. GROUND   — we check the quote actually appears on the cited page. An answer
     whose quote isn't on its page is downgraded to unverified, so the caller can
     trust `grounded=True` answers and flag the rest. This is the QA analogue of
     the statements' arithmetic tie-out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from src import llm
from src.engine.index import PageIndex

_TOPK = 6

_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "answer": {"type": "string", "description": "concise direct answer; '' if not found"},
        "value": {"type": ["string", "null"], "description": "the headline number, as printed; null if non-numeric"},
        "unit": {"type": ["string", "null"], "description": "e.g. 'INR crore', '%', 'per share'; null if n/a"},
        "page": {"type": ["integer", "null"], "description": "1-based page number the answer came from"},
        "quote": {"type": "string", "description": "verbatim line(s) from that page supporting the answer"},
        "confidence": {"type": "number"},
    },
    "required": ["found", "answer", "value", "unit", "page", "quote", "confidence"],
    "additionalProperties": False,
}

_INSTR = (
    "You are a senior Indian equity-research analyst. Answer the QUESTION using ONLY the "
    "provided annual-report pages. Indian filings label the same concept many ways — map by "
    "MEANING, not exact words. Rules:\n"
    "- If the answer is not in these pages, set found=false. NEVER invent a figure.\n"
    "- value = the headline number exactly as printed (parentheses = negative); null if the "
    "answer is not numeric. unit = the reporting unit (e.g. 'INR crore', '%', 'per share').\n"
    "- page = the 1-based PAGE number (from the '=== PAGE n ===' markers) the answer is on.\n"
    "- quote = the exact line(s) from that page containing the answer, copied VERBATIM "
    "character-for-character (same words/numbers as printed). Do NOT paraphrase or add words.\n"
    "- Prefer the audited financial statements over summary/highlights pages when both appear."
)


def _collapse(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


@dataclass
class Answer:
    question: str
    found: bool
    answer: str = ""
    value: Optional[str] = None
    unit: Optional[str] = None
    page: Optional[int] = None
    quote: str = ""
    confidence: float = 0.0
    grounded: bool = False           # quote verified to appear on the cited page
    pages_searched: list[int] = None

    def __str__(self) -> str:
        if not self.found:
            return f"[not found] {self.question}"
        mark = "✓" if self.grounded else "≈"
        loc = f"p{self.page}" if self.page else "?"
        val = f" = {self.value}{(' ' + self.unit) if self.unit else ''}" if self.value else ""
        return f"{mark} {self.answer}{val}  ({loc})"


def ask(index: PageIndex, question: str, expansion: list[str] | None = None,
        prefer_pages: list[int] | None = None, pages: list[int] | None = None) -> Answer:
    """Answer one question against a report's PageIndex, grounded to a citation.

    `expansion` adds retrieval synonyms; `prefer_pages` (e.g. a located statement
    page) are searched in addition to the BM25 hits. `pages` overrides retrieval
    entirely (the small-doc path passes the whole document).
    """
    if pages is None:
        pages = index.search(question, k=_TOPK, expansion=expansion)
        for p in (prefer_pages or []):
            if p not in pages:
                pages.append(p)
    if not pages:
        return Answer(question=question, found=False, pages_searched=[])

    out = llm.extract_json(
        instructions=_INSTR,
        user_input=f"QUESTION: {question}\n\nPAGES:\n{index.text_of(pages)}",
        schema_name="answer", schema=_SCHEMA,
        max_output_tokens=900, reasoning="low",
    )
    if out.get("_empty") or not out.get("found"):
        return Answer(question=question, found=False, pages_searched=pages)

    ans = Answer(
        question=question, found=True, answer=out.get("answer", ""),
        value=out.get("value"), unit=out.get("unit"), page=out.get("page"),
        quote=out.get("quote", ""), confidence=float(out.get("confidence", 0.0)),
        pages_searched=pages,
    )
    _ground(ans, index, pages)
    return ans


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


def _ground(ans: Answer, index: PageIndex, pages: list[int]) -> None:
    """Verify the answer against the source pages. For a numeric answer the value's
    digit-string must appear on a candidate page (robust to ₹/comma/paraphrase);
    otherwise we fall back to matching the quote text. On success the cited page is
    snapped to the page actually carrying the evidence."""
    cand = ([ans.page] if ans.page and 1 <= ans.page <= index.n_pages else []) + \
           [p for p in pages if p != ans.page]

    vd = _digits(ans.value) if ans.value else ""
    if len(vd) >= 3:                           # meaningful number -> match its digits on a page
        for p in cand:
            if vd in _digits(index.page_text[p - 1]):
                ans.page, ans.grounded = p, True
                return

    q = _collapse(ans.quote)[:60]              # else match a verbatim slice of the quote
    if q:
        for p in cand:
            if q in _collapse(index.page_text[p - 1]):
                ans.page, ans.grounded = p, True
                return
