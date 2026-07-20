"""Local, region-scoped locate: find candidate pages for a concept WITHOUT a model.

Scans the structure map's per-page text for a concept's aliases, scoped to the
correct region (STANDALONE / CONSOLIDATED). Returns the best candidate pages so the
extractor only reads the relevant note — not the whole 400-page doc.

This is the cheap, privacy-friendly Phase-0 locator (no upload). If it proves too
weak, the vector-store locate is the next lever (per PLAN.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.structure_map import StructureMap


@dataclass
class Candidate:
    page: int          # 1-indexed PDF page
    region: str        # STANDALONE / CONSOLIDATED / FRONT
    score: float
    alias_hits: list[str]


def _norm(s: str) -> str:
    # Unify '&' with 'and' so an alias like "Legal and professional" matches a report that prints
    # "Legal & Professional", and "Consumption of stores and spares" matches "Stores & Spares".
    # Both the alias and the page text pass through here, so this only EQUATES the two spellings of
    # the same word -- it can never drop a correct match, only add the equivalent-spelling one.
    return re.sub(r"\s+", " ", s.lower().replace("&", " and ")).strip()


_STOP = {"note", "the", "and", "for", "within", "under", "sub", "section", "of", "in",
         "p&l", "pl", "statement", "balance", "sheet", "current", "year", "end"}


def _hint_terms(location_hint: str | None) -> list[str]:
    """Meaningful note-heading words from location_hint (e.g. 'finance costs', 'investments')."""
    if not location_hint:
        return []
    words = re.findall(r"[a-z][a-z&-]{3,}", location_hint.lower())
    return [w for w in words if w not in _STOP]


def locate(
    sm: StructureMap,
    aliases: list[str],
    scope: str,            # "standalone" | "consolidated" | "both"
    column_hint: str | None = None,
    location_hint: str | None = None,
    top_k: int = 5,
) -> list[Candidate]:
    """Rank pages by alias presence (+ note-heading hint) within the in-scope regions."""
    want_regions = {
        "standalone": {"STANDALONE"},
        "consolidated": {"CONSOLIDATED"},
        "both": {"STANDALONE", "CONSOLIDATED"},
    }[scope]
    # If region detection found nothing for a scope (it can be noisy), fall back to
    # all non-FRONT pages so we never miss purely due to mis-tagged regions.
    in_scope_pages = [
        p for p in range(1, sm.page_count + 1) if sm.region[p - 1] in want_regions
    ]
    if not in_scope_pages:
        in_scope_pages = [
            p for p in range(1, sm.page_count + 1) if sm.region[p - 1] != "FRONT"
        ]
    # Final fallback: if region detection tagged EVERY page FRONT (e.g. a quarterly results
    # filing, which lacks the annual report's "Standalone/Consolidated Financial Statements"
    # section headers), search all pages rather than returning nothing. Annual reports always
    # have detected regions, so this branch never fires for them.
    if not in_scope_pages:
        in_scope_pages = list(range(1, sm.page_count + 1))

    norm_aliases = [(_norm(a), a) for a in aliases]
    col = _norm(column_hint) if column_hint else None
    hint_terms = _hint_terms(location_hint)
    # The Cash Flow Statement re-states P&L items as add-back adjustments, often LUMPED into one
    # combined line (e.g. "Bad Debts ... and Provision for Doubtful Debts" = a single figure). For
    # those P&L expense/income items the itemised note is the primary source, so de-rank cash-flow
    # pages. SCOPE it to P&L items only — equity/PP&E/balance-sheet reads (e.g. a hedge reserve on
    # a SOCIE page that happens to sit beside the cash-flow statement) must NOT be penalised.
    _loc = (location_hint or "").lower()
    cf_derank = ("cash flow" not in _loc) and any(
        t in _loc for t in ("p&l", "profit and loss", "expense", "finance cost"))

    cands: list[Candidate] = []
    for p in in_scope_pages:
        raw = sm.text(p)
        t = _norm(raw)
        hits = [orig for na, orig in norm_aliases if na and na in t]
        # Note-heading recall: a page that matches the note's location_hint terms is a
        # candidate even with NO alias hit (the note may use a label we don't list).
        hint_hits = sum(1 for w in hint_terms if w in t)
        if not hits and hint_hits < 2:
            continue
        score = float(len(hits)) + 0.6 * hint_hits
        # Numeric-density boost: real statement/note tables are number-dense, while
        # policy/narrative pages just repeat the term in prose.
        num_count = len(re.findall(r"\d[\d,]*\.?\d+", raw))
        score += min(num_count / 25.0, 3.0)
        # boost pages that also contain the disambiguating column word (gross/accum dep)
        if col:
            for w in re.findall(r"[a-z]+", col):
                if len(w) > 3 and w in t:
                    score += 0.5
        # de-rank the Cash Flow Statement (lumped add-backs) for P&L items only (see cf_derank).
        # Key on the "...from operating activities" section header — that appears ONLY in the
        # actual statement, whereas "statement of cash flow" also shows up as incidental policy
        # text on unrelated notes (e.g. Adani's PP&E note), which must NOT be penalised.
        if cf_derank and "cash flow from operating activities" in t:
            score -= 3.0
        cands.append(Candidate(page=p, region=sm.region[p - 1], score=score, alias_hits=hits))

    cands.sort(key=lambda c: c.score, reverse=True)
    return cands[:top_k]
