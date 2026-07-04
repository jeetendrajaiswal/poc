"""Datapoint extraction by MEANING + STRUCTURE — no company names, no aliases.

The 59 (or N) target datapoints are note-level line items. Companies label them
however they like, so string/alias matching does not scale. Instead:

  1. GROUP each target by the standard Ind AS / Schedule III NOTE it lives in
     (Other Equity, PP&E schedule, Borrowings, Other Expenses, Share Capital …).
     These note categories are mandated by the framework — they are structural,
     not company-specific.
  2. LOCATE that note by its structure (BM25 over framework terms, not names).
  3. EXTRACT every target in the note in ONE grounded pass: the model maps each
     target to a line BY MEANING (definition + selector), and returns a value
     ONLY if a line genuinely IS that concept — otherwise 'absent'. Seeing the
     whole note at once is what lets it pick the right column/line and refuse
     look-alikes (the two failure modes of per-item generic Q&A).

The target list (what to extract) still comes from the taxonomy — that is the
output contract — but ONLY its meaning (`concept`) and selector (`column_hint`)
are used. Aliases are ignored on purpose.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from itertools import product, zip_longest

import yaml

from src import llm
from src.engine.index import PageIndex

_TAXONOMY = os.path.join(os.path.dirname(__file__), "..", "..", "taxonomy", "definitions.yaml")

# Canonical Ind AS / Schedule III notes -> structural locate terms (framework
# vocabulary, never a company's chosen label).
SECTIONS: dict[str, str] = {
    "other_equity": "other equity reserves and surplus securities premium retained earnings general reserve "
                    "capital reserve hedging reserve statement of changes in equity closing balance",
    "investment_property": "investment property gross carrying amount cost accumulated depreciation",
    "investments": "investments non-current current quoted unquoted equity instruments debentures bonds "
                   "joint ventures associates partnership firms",
    "ppe": "property plant and equipment gross carrying value cost accumulated depreciation additions "
           "disposals net carrying capital work in progress furniture fixtures office equipment",
    "trade_payables": "trade payables micro small and medium enterprises MSME other than creditors",
    "borrowings": "borrowings non-current current secured unsecured loans debentures term loans "
                  "subordinated debt deferred payment from related parties from others",
    "other_nc_liabilities": "other non-current liabilities long term other long term liabilities total",
    "other_cur_liabilities": "other current liabilities revenue received in advance unearned accrued salary "
                             "payable employee stock options outstanding",
    "loans_advances": "loans advances security deposits related parties allowance for bad and doubtful "
                      "other current financial assets short term",
    "other_cur_assets": "other current assets advances to related parties advances to suppliers export "
                        "incentives receivable doubtful advances",
    "share_capital": "share capital authorised issued subscribed paid up equity shares face value "
                     "reconciliation of shares outstanding buyback treasury stock split",
    "finance_costs": "finance costs interest expense on borrowings interest on debt",
    "other_expenses": "other expenses power and fuel consumption of stores and spare parts brokerage "
                      "commission carriage outwards legal and professional payment to auditor donations "
                      "csr corporate social responsibility bad debts written off provision doubtful impairment",
    "deferred_tax": "deferred tax assets liabilities components net movement",
    "pl_changes_inventory": "changes in inventories of finished goods work in progress stock in trade",
    "consolidated_equity": "equity attributable to owners of the company non-controlling interest "
                           "interests in joint ventures depreciation",
}

SECTION_LABEL = {
    "other_equity": "Other Equity / Reserves note",
    "investment_property": "Investment Property note",
    "investments": "Investments note (non-current & current)",
    "ppe": "Property, Plant & Equipment movement schedule (and CWIP)",
    "trade_payables": "Trade Payables note",
    "borrowings": "Borrowings notes (non-current & current, secured & unsecured)",
    "other_nc_liabilities": "Other Non-current Liabilities note",
    "other_cur_liabilities": "Other Current Liabilities note",
    "loans_advances": "Loans & Advances / Other Financial Assets note",
    "other_cur_assets": "Other Current Assets note",
    "share_capital": "Share Capital note",
    "finance_costs": "Finance Costs note",
    "other_expenses": "Other Expenses note (P&L)",
    "deferred_tax": "Deferred Tax note",
    "pl_changes_inventory": "Statement of Profit & Loss (changes in inventories line)",
    "consolidated_equity": "Consolidated equity / interests in joint ventures",
}

# location_hint keyword -> section. Checked in order (specific before general).
_ROUTES: list[tuple[str, str]] = [
    ("attributable", "consolidated_equity"),
    ("non-controlling", "consolidated_equity"),
    ("owners of the company", "consolidated_equity"),
    ("investment property", "investment_property"),
    ("changes in inventor", "pl_changes_inventory"),
    ("deferred tax", "deferred_tax"),
    ("finance cost", "finance_costs"),
    ("interest expense", "finance_costs"),
    ("auditor", "other_expenses"),
    ("other expenses", "other_expenses"),
    ("manufacturing expenses", "other_expenses"),
    ("csr", "other_expenses"),
    ("trade payable", "trade_payables"),
    ("borrowing", "borrowings"),
    ("other non-current liabilit", "other_nc_liabilities"),
    ("other current liabilit", "other_cur_liabilities"),
    ("share-based", "other_cur_liabilities"),
    ("other current assets", "other_cur_assets"),
    ("loans", "loans_advances"),
    ("other current financial assets", "loans_advances"),
    ("investments note", "investments"),
    ("investment", "investments"),
    ("property, plant", "ppe"),
    ("pp&e", "ppe"),
    ("cwip", "ppe"),
    ("depreciat", "ppe"),
    ("carrying value", "ppe"),
    ("other equity", "other_equity"),
    ("reserves", "other_equity"),
    ("changes in equity", "other_equity"),
    ("share capital", "share_capital"),
    ("consolidated", "consolidated_equity"),
]


def _route(key: str, location_hint: str) -> str:
    text = f"{key} {location_hint}".lower()
    for kw, sec in _ROUTES:
        if kw in text:
            return sec
    return "other_expenses"          # sensible default bucket (broad P&L items)


@dataclass
class Concept:
    key: str
    meaning: str            # economic definition (from taxonomy 'concept')
    selector: str           # column/which-row hint (from 'column_hint'); may be ''
    value_type: str
    section: str
    examples: list[str] = field(default_factory=list)   # ILLUSTRATIVE labels, not a match-list


def load_concepts(path: str | None = None) -> list[Concept]:
    items = yaml.safe_load(open(path or os.path.normpath(_TAXONOMY)))["items"]
    out = []
    for it in items:
        # representative labels purely to CALIBRATE the model on how the concept tends to look;
        # matching is still by meaning. Was capped at 3, but the cap directly caused misses —
        # e.g. adani prints 'Employee Benefits Liability' for leave-encashment DTA, which IS
        # alias #5; the strict never-substitute rule then refused the line. Alias lists are
        # small (max 8), so pass them all.
        ex = [a.strip() for a in (it.get("aliases") or []) if a.strip()]
        out.append(Concept(
            key=it["key"],
            meaning=(it.get("concept") or "").strip(),
            selector=(it.get("column_hint") or "").strip(),
            value_type=it.get("value_type", "monetary"),
            section=_route(it["key"], it.get("location_hint", "")),
            examples=ex,
        ))
    return out


@dataclass
class Datapoint:
    key: str
    present: bool
    value: str | None = None
    evidence: str = ""
    grounded: bool = False
    section: str = ""
    pages: list[int] = field(default_factory=list)
    confidence: str = "absent"   # "reconciled" | "grounded" | "unverified" | "absent"


_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "present": {"type": "boolean"},
                    "value": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    # COMPOSITE support: printed components CODE will sum (see _INSTR). The
                    # single-line contract structurally cannot produce items the report splits
                    # across lines (treasury shares held via two trusts; auditor remuneration
                    # as several fee lines; subtotal minus a 'Less:' line) — the model returns
                    # the printed addends, deterministic code does the arithmetic.
                    "addends": {"type": ["array", "null"], "items": {"type": "string"}},
                },
                "required": ["key", "present", "value", "evidence", "addends"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_INSTR = (
    "You are a senior Indian financial analyst reading ONE note/section of an Ind AS annual report "
    "({scope} financial statements). For EACH requested item, find the SINGLE line that IS that item "
    "BY MEANING — companies word lines differently, so map using the definition, NOT exact words. "
    "Return its value for the CURRENT (most recent) reporting year, {scope}, applying the item's "
    "selector (closing balance / gross / accumulated-depreciation column / current vs non-current / "
    "specific asset column / COUNT vs AMOUNT / contra-deduction).\n"
    "STRICT RULES — these matter more than finding a value:\n"
    "- Return a value ONLY if a line in the provided text genuinely represents this exact item. If it "
    "is not present here, set present=false and value=null. NEVER substitute a broader, narrower, "
    "total, parent, or merely similar line — a wrong line is worse than 'absent'.\n"
    "{composite}"
    "- Contra/deduction lines ('less: allowance/doubtful') keep their deduction sign.\n"
    "- Numbers exactly as printed; parentheses or a leading minus mean negative; a bare '-' = null.\n"
    "- value = the NUMBER ONLY: keep digit-grouping commas, the decimal point, and a leading minus or "
    "surrounding parentheses for negatives, but REMOVE any currency symbol (₹, `, Rs, INR), unit word, "
    "or footnote mark (*, #, †).\n"
    "- evidence = the exact line you used, copied verbatim.\n"
    "VERIFY BEFORE ANSWERING: for every value you return, re-locate that exact line in the TEXT and "
    "confirm (a) the digits match what is printed, and (b) the line truly means this item for the "
    "{scope} current year. If you cannot confirm both, set present=false / value=null rather than guess."
)


def _digits(s) -> str:
    return re.sub(r"[^\d]", "", str(s or ""))


# COMPOSITE datapoints — items that are the SUM/NET of several printed lines with no single
# printed line to cite (treasury shares held via multiple trusts; auditor remuneration split
# into fee lines). The model returns the printed components in `addends`; CODE does the
# arithmetic. Opted in per section on evidence (same discipline as the prompt-enrichment sets
# below): share_capital (hindalco treasury = 2 trust lines) and other_expenses (itc auditor
# total = 5 fee lines). Elsewhere the rule is a one-line "addends=null", so the strict-schema
# field can never invite improvised summation.
_COMPOSITE_SECTIONS = {"share_capital", "other_expenses"}
_COMPOSITE_RULE = (
    "- COMPOSITE items: when the item is genuinely the SUM/NET of SEVERAL printed lines and no single "
    "printed line IS the item (e.g. shares held via multiple trusts; several fee lines making up a "
    "total; a printed figure minus a printed 'Less:' line), set value=null and return each component "
    "in `addends` EXACTLY as printed (parentheses = negative), quoting those lines in evidence — the "
    "code will do the arithmetic. Otherwise addends=null. Never use addends to combine unrelated or "
    "guessed numbers.\n")
_NO_COMPOSITE_RULE = "- Set addends=null for every item.\n"


# Sections whose PROMPTS get enrichment, each opted in on RUN EVIDENCE (extending enrichment to
# all sections regressed other_expenses 17 -> 25 on 2026-07-04 — extra refusal pressure + shifted
# calibration — so it is per-section opt-in, never global):
#   deferred_tax  — hint + FULL aliases: umbrella labels dominate there ('Employee Benefits
#                   Liability' IS leave encashment, alias #5 beyond the [:3] cap); confirmed
#                   10 -> 5 errors on the 2026-07-04 run.
#   share_capital — hint only: the capital block's paid-up subtotal (after forfeiture) and final
#                   total (after treasury, = the BS carrying amount) are printed as UNLABELED
#                   bare-number rows under the 'Less:' lines (hindalco p369/p292, both scopes) —
#                   without the structural hint the model can't cite them and returns the gross
#                   line (225 vs 222) or nothing.
# Every OTHER section keeps the validated baseline [:3] examples and no fallback hint.
_FALLBACK_HINT_SECTIONS = {"deferred_tax", "share_capital"}
_FULL_EXAMPLE_SECTIONS = {"deferred_tax"}


def _examples(c: "Concept", section: str) -> list[str]:
    return c.examples if section in _FULL_EXAMPLE_SECTIONS else c.examples[:3]


def _fmt_num(x: float) -> str:
    """Indian-report style: thousands commas, 2dp only when fractional, parens for negative."""
    whole = abs(x - round(x)) < 0.005
    body = f"{int(round(abs(x))):,}" if whole else f"{abs(x):,.2f}"
    return f"({body})" if x < 0 else body


def _collapse(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def page_scopes(index: PageIndex) -> list[str]:
    """Tag every page 'standalone' / 'consolidated' / 'unknown' by forward-filling
    from block anchors (statement titles & section headers). Indian reports contain
    a full standalone block AND a full consolidated block, each with its own notes;
    restricting extraction to the right block removes cross-scope contamination."""
    n = index.n_pages
    tags: list[str] = ["unknown"] * n
    cur = "unknown"
    for i in range(n):
        # Anchors are read from the page's TOP REGION only (first ~6 non-empty lines): that is
        # where statement titles and running headers live. Matching the whole page let PROSE
        # mentions anchor ('…report on the consolidated financial statements…' in an auditor
        # report / Board's report) — PNB's entire standalone statements block inherited
        # 'consolidated' from a p16 mention, and ONGC's tags flapped 12 times in 15 pages of
        # a section that discusses both. This mis-tagging was the cause of ALL 14 share-capital
        # locate gaps on the nifty100 held-out sweep.
        top_lines = [l for l in index.page_text[i].splitlines() if l.strip()][:6]
        top = _collapse(" ".join(top_lines))
        # anchors are TIERED: a statement TITLE ('Standalone Balance Sheet as at …') outranks
        # a section-nav phrase ('… Consolidated Financial Statements' in a header ribbon).
        # ONGC's standalone BS page carries BOTH — the title plus a nav mention of the other
        # section — and untiered matching cancelled them out, leaving the stale tag.
        con_title = any(t in top for t in (
            "consolidatedbalancesheet", "consolidatedstatementofprofit",
            "consolidatedstatementofcashflow"))
        std_title = any(t in top for t in (
            "standalonebalancesheet", "standalonestatementofprofit",
            "standalonestatementofcashflow"))

        # tier-2 SECTION phrases must occupy (nearly) their own line: a genuine divider or
        # running header ('Notes to the Consolidated Financial Statements') is a line of its
        # own, while a nav RIBBON embeds the phrase among the other sections' names
        # ('Overview  Annexures to the Board's Report  Consolidated Financial Statements' —
        # ONGC prints that on EVERY standalone page and it flipped the whole block).
        def _own_line(*phrases) -> bool:
            for ln in top_lines:
                cl = _collapse(ln)
                for ph in phrases:
                    if ph in cl and len(cl) - len(ph) <= 25:
                        return True
            return False

        con_sec = _own_line("consolidatedfinancialstatement", "consolidatedfinancialresults")
        std_sec = _own_line("standalonefinancialstatement")
        if con_title or std_title:
            is_con, is_std = con_title, std_title
        else:
            is_con, is_std = con_sec, std_sec
        # Many ARs title the standalone statements PLAINLY ('Balance Sheet as at …' — no
        # 'standalone' prefix), so that transition was invisible. Convention: consolidated
        # statements always carry the word 'consolidated' in their title/header — a bare
        # statement TITLE with no 'consolidated' in the top region anchors STANDALONE.
        # The title match tolerates a short gap: on a TWO-UP page the BS and P&L titles
        # interleave in -layout ('balancesheet statementofprofitandloss asat…' — adani p230).
        if not is_std and not is_con and "consolidated" not in top:
            if re.search(r"balancesheet.{0,40}asat|statementofprofitandloss.{0,40}forthe", top):
                is_std = True
        if is_con and not is_std:
            cur = "consolidated"
        elif is_std and not is_con:
            cur = "standalone"
        tags[i] = cur
    return tags


def _extract_section(index: PageIndex, scope: str, section: str,
                     concepts: list[Concept], allowed: set[int] | None = None,
                     note_pages: list[int] | None = None) -> list[Datapoint]:
    # When the note was deterministically located, read THAT focused page (+overflow) rather than a
    # bag of BM25 hits — otherwise the note's signal is buried under unrelated pages (Hindalco's share
    # capital sits on a page dominated by the inventory note; 8 BM25 pages drowned it). BM25 is the
    # fallback only when there is no note ref to navigate from.
    n = index.n_pages
    if note_pages:
        pages = []
        for p in note_pages[:2]:
            for q in (p - 1, p, p + 1):   # p-1: the note can start on the preceding page
                if 1 <= q <= n and q not in pages:
                    pages.append(q)
    else:
        # enrich the retrieval query with example labels -> pages bearing real line wording rank higher
        ex_terms = " ".join(e for c in concepts for e in _examples(c, section))
        ranked = index.search(SECTIONS[section] + " " + scope + " " + ex_terms, k=20)
        if allowed:
            in_scope = [p for p in ranked if p in allowed]
            pages = (in_scope or ranked)[:8]    # fall back to global if scope block has no hit
        else:
            pages = ranked[:8]
    if not pages:
        return [Datapoint(key=c.key, present=False, section=section) for c in concepts]
    text = index.text_of(pages, columns=section in COLUMN_SECTIONS)
    items_desc = "\n".join(
        f"- key: {c.key}\n  meaning: {c.meaning[:400]}"
        + (f"\n  selector: {c.selector}" if c.selector else "")
        + (f"\n  may look like (EXAMPLES ONLY, real label may differ — map by meaning): "
           f"{'; '.join(_examples(c, section))}" if c.examples else "")
        for c in concepts)
    # structural note-type guidance for the fallback read — deferred_tax ONLY (its mixed-sign
    # matrix never reconciles, so it always lands here and was reading blind; the hint + full
    # aliases took it 10 -> 5 errors). Threading hints into every section's fallback was tried
    # on the 2026-07-04 run and REGRESSED other_expenses 17 -> 25: the other_expenses hint text
    # ('return only if that exact line exists') adds refusal pressure to a prompt that was
    # already the strictest, so the model started refusing garbled-but-present lines. Keep every
    # other section's fallback prompt byte-identical to the validated baseline.
    hint = _SECTION_HINT.get(section, "") if section in _FALLBACK_HINT_SECTIONS else ""
    out = llm.extract_json(
        instructions=_INSTR.format(
            scope=scope,
            composite=_COMPOSITE_RULE if section in _COMPOSITE_SECTIONS else _NO_COMPOSITE_RULE),
        user_input=(f"SECTION: {SECTION_LABEL[section]}\n"
                    + (f"STRUCTURE: {hint}\n" if hint else "")
                    + f"\nThere are EXACTLY {len(concepts)} items below — "
                    f"your `results` array MUST contain all {len(concepts)}, one per key, none omitted "
                    f"(present=false only if genuinely absent).\n\nITEMS:\n{items_desc}\n\nTEXT:\n{text}"),
        schema_name="datapoints", schema=_SCHEMA,
        max_output_tokens=2000, reasoning="low",
    )
    found = {r["key"]: r for r in out.get("results", [])} if not out.get("_empty") else {}
    page_digits = _digits(text)
    results = []
    for c in concepts:
        r = found.get(c.key)
        if not r:
            results.append(Datapoint(key=c.key, present=False, section=section, pages=pages))
            continue
        ev = r.get("evidence", "")
        # COMPOSITE path (opt-in sections only): the model returned printed components; CODE
        # does the arithmetic — and code always WINS over any model-computed value, since the
        # model's own arithmetic is exactly what we don't trust. Grounding = EVERY addend's
        # digits appear in the read text, so a fabricated component kills the whole composite.
        adds = [a for a in (r.get("addends") or []) if _num(_clean_value(a)) is not None]
        if section in _COMPOSITE_SECTIONS and r.get("present") and 2 <= len(adds) <= 12:
            grounded = all(len(_digits(a)) >= 3 and _digits(a) in page_digits for a in adds)
            total = sum(_num(_clean_value(a)) for a in adds)
            results.append(Datapoint(
                key=c.key, present=True, value=_fmt_num(total),
                evidence="(code-summed addends) " + " + ".join(a.strip() for a in adds) + f" | {ev}",
                grounded=grounded, section=section, pages=pages))
            continue
        if not r.get("present") or r.get("value") in (None, "", "-"):
            results.append(Datapoint(key=c.key, present=False, section=section, pages=pages))
            continue
        val = _hygiene(r["value"], ev, sign=c.value_type != "count")
        vd = _digits(val)
        grounded = len(vd) >= 3 and vd in page_digits          # anti-hallucination backstop
        results.append(Datapoint(key=c.key, present=True, value=val, evidence=ev,
                                 grounded=grounded, section=section, pages=pages))
    return results


def _num(s):
    if s is None:
        return None
    s = str(s).replace("−", "-").replace("–", "-")
    neg = "(" in s and ")" in s
    m = re.search(r"-?\d[\d,]*\.?\d*", s.replace(" ", ""))
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    return -abs(v) if neg else v


# --- Value hygiene (learned from the vision pipeline; deterministic, model-agnostic) ---
# The flagship model emitted clean values; the mini model leaves currency/footnote noise
# ('` 1.00', '12.5*') and occasionally drops a negative's parentheses. Both are fixed here
# with zero extra API calls — concept-agnostic, so they correct every company, not one.
_CURRENCY_RE = re.compile(r"₹|`|(?i:\bRs\.?)|(?i:\bINR\b)|[*#†‡]")


# A datapoint whose DEFINITION says to return the note/BS TOTAL (an aggregate), not a sub-line.
# Such targets legitimately equal the note total, so the refusal guard must NOT demote them.
# Driven by the definition text, never the key name. Scans the SELECTOR too: taxonomy authors
# state total-ness either in the concept prose ("— total trade payables") or tersely in the
# selector ("TOTAL trade payables (MSME + others)") — scanning only the meaning force-demoted
# adani's correctly-reconciled Sundry Creditors 4,474.96 to absent (its MSME split is material,
# so the >=2-material-components exemption didn't save it as it silently does elsewhere).
def _wants_total(meaning: str, selector: str = "") -> bool:
    m = (meaning or "").lower()
    s = (selector or "").lower()
    if any(p in m or p in s for p in ("the total", "aggregate figure", "note's total",
                                      "total line", "single aggregate")):
        return True
    # a selector that OPENS with 'total', or a dash-appositive '— total X' in the meaning, is
    # the same instruction written tersely. The dash must be a standalone appositive (preceded
    # by whitespace/start), so 'sub-total' can never falsely exempt an item.
    return bool(re.match(r"\s*total\b", s) or re.search(r"(?:^|\s)[—–-]\s*total\b", m))


def _clean_value(v):
    """Strip currency symbols / unit words / footnote marks; keep sign, commas, decimals.
    No-op on an already-clean value; never blanks a non-numeric ('Nil'/'-')."""
    if not isinstance(v, str) or not v.strip():
        return v
    cleaned = _CURRENCY_RE.sub("", v).strip()
    return cleaned if re.search(r"\d", cleaned) else v


def _reconcile_sign(value, evidence):
    """If a value's magnitude appears in the cited evidence line ONLY inside parentheses,
    restore the parentheses (the model sometimes returns the bare magnitude). Conservative:
    skips already-negative values; never flips when the same magnitude also appears
    un-bracketed on the line (ambiguous -> trust the model)."""
    if not isinstance(value, str):
        return value
    v = value.strip()
    if not v or v.startswith("(") or v.startswith("-"):
        return value
    ev = evidence or ""
    m = re.search(r"\d[\d,]*\.?\d*", v)
    if not (ev and m):
        return value
    esc = re.escape(m.group(0))
    bounded = re.findall(r"(?<![\d.])" + esc + r"(?![\d.])", ev)   # every occurrence on the line
    bracketed = re.findall(r"\(\s*" + esc + r"\s*\)", ev)          # those wrapped in ( )
    if bounded and len(bounded) == len(bracketed):                 # all negative -> restore sign
        return f"({v})"
    return value


def _hygiene(value, evidence, sign: bool = True):
    """Clean noise then restore dropped negative signs. Applied to every accepted value.
    sign=False skips the negative-restore — used for COUNT-type datapoints, where a
    parenthesized number on the evidence line is typography, not a negative (infosys prints
    'current (prior)' share-count pairs: '404,69,40,812 (414,36,07,528)' — the restore turned
    the opening share count negative on the 2026-07-04 run). A genuine contra count
    (buyback/treasury) is returned parenthesized by the model itself and passes through."""
    v = _clean_value(value)
    return _reconcile_sign(v, evidence) if sign else v


# section -> (statement, keywords identifying the PARENT line whose value the note
# must reconcile to). The parent comes from the already-tied-out statement, so it
# is a trusted anchor — the right note is the one whose components sum to it.
_PARENT: dict[str, tuple[str, list[str]]] = {
    "other_equity": ("bs", ["other equity"]),
    "ppe": ("bs", ["property, plant", "property plant"]),
    "investment_property": ("bs", ["investment propert"]),
    "trade_payables": ("bs", ["trade payable"]),
    "borrowings": ("bs", ["borrowing"]),
    "share_capital": ("bs", ["equity share capital", "share capital"]),
    "deferred_tax": ("bs", ["deferred tax"]),
    "other_expenses": ("pl", ["other expense"]),
    "finance_costs": ("pl", ["finance cost"]),
    # routed through reconciliation so granular sub-line targets get the stronger
    # specificity rule + the refusal guard (a residual sub-line that isn't disclosed must
    # not collapse to the note total — e.g. 'Other Long Term' grabbing the whole 4,040).
    "other_nc_liabilities": ("bs", ["other non-current liabilit", "other non current liabilit"]),
    "other_cur_liabilities": ("bs", ["other current liabilit"]),
}

_LINE_SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "object",
    "properties": {"label": {"type": "string"}, "note_ref": {"type": ["string", "null"]},
                   "value": {"type": ["string", "null"]}},
    "required": ["label", "note_ref", "value"], "additionalProperties": False}}},
    "required": ["lines"], "additionalProperties": False}

_NOTE_SCHEMA = {"type": "object", "properties": {
    "components": {"type": "array", "items": {"type": "object", "properties": {
        "label": {"type": "string"}, "value": {"type": ["string", "null"]}},
        "required": ["label", "value"], "additionalProperties": False}},
    "total": {"type": ["string", "null"]},
    "targets": {"type": "array", "items": {"type": "object", "properties": {
        "key": {"type": "string"}, "present": {"type": "boolean"}, "value": {"type": ["string", "null"]},
        "evidence": {"type": "string"}}, "required": ["key", "present", "value", "evidence"],
        "additionalProperties": False}}},
    "required": ["components", "total", "targets"], "additionalProperties": False}


def _statement_lines(index: PageIndex, scope: str, kind: str,
                     allowed: set[int] | None = None) -> list[dict]:
    """Extract a statement's line items (label,value) for `scope` — the parent anchors.
    `allowed` = the scope's page block: the statement is located INSIDE it, so standalone
    and consolidated get their own statements (previously one shared page served both
    scopes and cross-contaminated note refs/parents for every BS-anchored section)."""
    from src.engine import statements as st
    r = st.validate_statement(index.path, kind, frozenset(allowed) if allowed else None)
    if not r.page:
        return []
    o = llm.extract_json(
        instructions=(f"Extract every line item of this {scope} "
                      f"{'Balance Sheet' if kind == 'bs' else 'Statement of Profit and Loss'} "
                      "with its Note reference number (from the 'Note' column; null if none) and "
                      "current-year value (as printed; () = negative)."),
        user_input=index.text_of([r.page, r.page + 1]),
        schema_name="lines", schema=_LINE_SCHEMA, max_output_tokens=2500, reasoning="low")
    return [] if o.get("_empty") else o.get("lines", [])


# Sections whose note is printed TWO-UP (side-by-side) and so must be read with the coordinate
# column-reflow instead of `-layout` (which interleaves the two columns into garbage). Proven on
# share_capital (a BS note); other_expenses is the same mechanism on a P&L note — its Establishment/
# Other-Expenses table sits beside an unrelated note (EPS, Finance costs, …), so -layout welds the
# two together and the line items (CSR, Donations, Professional Fees, Power & fuel, Auditor) can't be
# parsed. The reflow is a no-op on single-column notes (e.g. ITC's full-width table) so it is safe to
# opt a section in whenever its note is two-up.
# 2026-07-03 forensics (all 104 errors traced to printed lines, $0): the same interleave was the
# proven cause of misses in deferred_tax (6 of its 10 errors — worst case: the two-up weld detaches
# the 'Deferred tax assets'/'Deferred tax liabilities' block headers, so the model picks the
# same-labelled row from the WRONG orientation matrix), loans_advances, finance_costs,
# other_cur_assets, other_cur_liabilities and trade_payables (GT digits + labels verified intact on
# ONE reflowed line for every affected page; zero numeric-token loss layout->reflow; single-column
# pages byte-identical). So ALL text-read note sections now opt in. Sections reading via other
# machinery (ppe matrix, borrowings vision, investments vision) are unaffected by this set.
COLUMN_SECTIONS = {"share_capital", "other_expenses", "deferred_tax", "loans_advances",
                   "finance_costs", "other_cur_assets", "other_cur_liabilities", "trade_payables"}

# Sections that additionally use the deterministic note-ref / signature-page LOCATE fallback. This is
# BS-anchored (navigates from a balance-sheet note ref, or ranks pages by the section's signature
# terms) and is kept to share_capital ONLY: it is unvalidated for P&L notes, and for other_expenses
# the signature actively mislands (reliance -> p157/p101, not the true note p117), whereas plain BM25
# already surfaces the correct note page for every company. So other_expenses keeps baseline locate.
_NOTE_LOCATE_SECTIONS = {"share_capital"}

# Notes whose printed lines are NOT additive components of the balance-sheet parent, so the
# "components sum to total == parent" reconciliation is STRUCTURALLY MEANINGLESS for them and must
# never be used to SELECT a page. Share capital is the canonical case: Authorised / Issued /
# Subscribed & Paid-up are successive STAGES of the same capital (250 / 225 / 225), not addends —
# the true note can never reconcile. Worse, wrong pages DO tie out arithmetically and win instead:
#   - the Statement of Changes in Equity (opening + changes = closing == BS parent)  [hindalco p345]
#   - rights-issue prose ("increased from 115.42 to 129.27": 115.42+13.85=129.27)    [adani p315]
#   - a shares-movement/buyback sub-table (opening - buyback = closing)              [infosys p324]
# each of which then returns the whole note as absent (the 2026-07-03 run's 3 whole-note collapses,
# 24 misses). This is a property of the Schedule III note TYPE — it holds for every Indian company —
# so these sections skip _reconciled_section and go straight to the deterministically-located,
# grounded _extract_section read (the path that produced the validated 9/11 & 10/10 share_capital
# results). Verification for them is groundedness (digits present on the page), not arithmetic.
NON_ADDITIVE_SECTIONS = {"share_capital"}


def _parent(section: str, bs_lines: list[dict], pl_lines: list[dict]) -> tuple[float | None, list[str]]:
    """Return (parent value summed over matching statement lines, their note refs)."""
    spec = _PARENT.get(section)
    if not spec:
        return None, []
    stmt, kws = spec
    lines = bs_lines if stmt == "bs" else pl_lines
    matched = [l for l in lines if any(k in (l["label"] or "").lower() for k in kws)]
    vals = [_num(l["value"]) for l in matched if _num(l["value"]) is not None]
    refs = [str(l["note_ref"]).strip() for l in matched if l.get("note_ref")]
    return (sum(vals) if vals else None), refs


def _note_pages(index: PageIndex, note_refs: list[str], title_kws: list[str],
                allowed: set[int]) -> list[int]:
    """Find pages where the parent's note appears as a HEADING. Deterministic
    navigation from the statement's own cross-reference — far more reliable than
    keyword retrieval. Two patterns, so two-column pages don't defeat it:
      (a) note number at LINE START (clean single-column pages), OR
      (b) the CONTIGUOUS heading '<ref>. <title>' anywhere on the line — pdftotext
          linearizes two-column pages so the heading is pushed off the line start
          (e.g. reliance 'Other Expenses' note: '31. Other Expenses' sits mid-line),
          which made the line-start-only check miss the real note page."""
    if not note_refs:
        return []
    title_re = "|".join(re.escape(k) for k in title_kws) if title_kws else None
    out = []
    for i, t in enumerate(index.page_text):
        if allowed and (i + 1) not in allowed:
            continue
        tl = t.lower()
        if not any(k in tl for k in title_kws):
            continue
        for ref in note_refs:
            esc = re.escape(ref)
            line_start = re.search(r"(?mi)^\s*(note\s*)?" + esc + r"[\.\)\s:]", t)
            heading = title_re and re.search(r"(?i)\b" + esc + r"\s*[\.\)]\s*(?:" + title_re + r")", t)
            if line_start or heading:
                out.append(i + 1)
                break
    return out


# Per-section CORE term groups for the signature locate. Each group is the alternative spellings of
# ONE structural element that the note's CORE page must carry (framework vocabulary — Schedule III
# mandates these stages for every Indian company, so this scales; never a company's label). Pages are
# ranked by how many groups they hit BEFORE raw term count: raw counts alone let wordy look-alikes
# outrank the real note (a Directors'-Report/Shareholder-Information page mentions share capital
# vocabulary a lot, but only the note's core table prints Authorised AND Issued AND Subscribed
# together — verified: the core-group rank uniquely tops the true note page on all 10 corpus
# scope-combos). Purely a re-ranking: with no core groups defined (or none hit) the behaviour
# degrades to the original distinct-term-count order.
_SIGNATURE_CORE: dict[str, list[list[str]]] = {
    "share_capital": [["authorised", "authorized"], ["issued"], ["subscribed"]],
}


def _signature_pages(index: PageIndex, section: str, allowed: set[int]) -> list[int]:
    """DETERMINISTIC structural locate — find the note by the co-occurrence of its own defining
    terms, with NO dependence on the stochastic balance-sheet LLM read. `_note_pages` navigates from
    the note ref, which vanishes whenever `_statement_lines` drops the parent line and collapses the
    whole section; this finds the note anyway. Fires only when the term signature is strong (>=4
    distinct terms on the page) so weak/ambiguous signatures don't return a wrong page (better a miss
    than a confident wrong). Ranks by CORE-group co-occurrence first (see _SIGNATURE_CORE), then
    distinct-term count. Returns the top pages."""
    terms = {t for t in SECTIONS.get(section, "").lower().split() if len(t) > 3}
    if len(terms) < 4:
        return []
    core = _SIGNATURE_CORE.get(section, [])

    def _scan(pages_ok) -> list[tuple]:
        scored = []
        for i, t in enumerate(index.page_text):
            if pages_ok and (i + 1) not in pages_ok:
                continue
            tl = t.lower()
            hits = sum(1 for w in terms if w in tl)
            if hits >= 4:
                ch = sum(1 for grp in core if any(w in tl for w in grp))
                scored.append((ch, hits, i + 1))
        return scored

    scored = _scan(allowed)
    if not scored and allowed:
        # the scope block has NO page carrying the note's signature — typical of a
        # STANDALONE-ONLY company (Nestle/ABB/SBI Life publish no consolidated FS) queried
        # for the consolidated scope. The document's only note IS the answer for either
        # scope, so degrade to an unrestricted scan rather than going blind.
        scored = _scan(None)
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    return [p for *_, p in scored[:2]]


_NOTE_INSTR = (
    "You are reading ONE note of an Indian Ind AS report ({scope} financials). Do BOTH:\n"
    "(A) List EVERY component line of this note with its CURRENT-year closing value, and the note TOTAL "
    "(for reconciliation). Use closing balances only, not movements/opening.\n"
    "(B) Extract the specific TARGET items below — map each to a component BY MEANING (definition + "
    "examples; the real label may differ), applying its selector. A target must match at its EXACT level "
    "of specificity: if it names a qualifier (e.g. 'to related parties', 'unquoted', a specific asset "
    "class) and no line carries that qualifier, set present=false — do NOT return a broader line or a "
    "TOTAL/subtotal in its place.\n"
    "{hint}"
    "Numbers as printed; () or minus = negative; bare '-' = null. value = the NUMBER ONLY — keep commas, "
    "decimal point and the negative sign/parentheses, but strip currency symbols (₹, `, Rs, INR), unit "
    "words and footnote marks (*, #).\n"
    "VERIFY BEFORE ANSWERING: re-check that the components sum to the note TOTAL, and that each TARGET "
    "value is copied from a line that genuinely matches it; if a target can't be confirmed, set present=false."
)

# Structural guidance per note TYPE (framework structure, not company-specific).
_SECTION_HINT: dict[str, str] = {
    "ppe": ("This is a PP&E MOVEMENT SCHEDULE laid out as a wide table: asset classes are COLUMNS "
            "(Land, Buildings, Plant, Furniture & Fixtures, Office Equipment, …) and rows are Gross "
            "block / Accumulated depreciation / Net block. For component reconciliation use the NET "
            "carrying value per asset class (they sum to total net PP&E). For each TARGET read the named "
            "asset's COLUMN at the LATEST date: GROSS carrying value, or ACCUMULATED DEPRECIATION, as the "
            "selector says. Match the asset column by left-to-right order; watch look-alikes "
            "(Office Equipment vs Computers vs Plant).\n"),
    "other_expenses": ("Components are the individual expense lines that sum to TOTAL other/manufacturing "
                       "expenses. Targets are specific expense lines — return only if that exact line exists.\n"),
    "finance_costs": ("Components are the finance-cost lines summing to total finance costs; 'interest on "
                      "borrowings' is usually one of them.\n"),
    "share_capital": ("The capital block runs: Authorised -> Issued -> Subscribed & Paid-up -> "
                      "'Less: shares/calls forfeited' -> [SUBTOTAL row] -> 'Less: Treasury Shares' "
                      "(sometimes several trust lines) -> [FINAL TOTAL row = the balance-sheet carrying "
                      "amount]. The subtotal/final rows are often UNLABELED bare-number rows directly "
                      "under the 'Less:' lines — they ARE valid answer lines: the paid-up share COUNT "
                      "after forfeiture is the subtotal row's count; the final paid-up AMOUNT is the "
                      "final total row (after ALL deductions). Columns pair COUNT columns with AMOUNT "
                      "columns and current-year with prior-year — take the current-year column matching "
                      "the item's COUNT-vs-AMOUNT type. Treasury/buyback shares held via several trusts "
                      "are a COMPOSITE — return the trust lines as addends.\n"),
    "deferred_tax": ("The deferred-tax note is usually a MOVEMENT MATRIX: rows are tax components, columns "
                     "are opening balance / recognised in P&L / recognised in OCI / closing balance — ALWAYS "
                     "take the CLOSING (latest 'As at') column, never opening or movement columns. Component "
                     "rows sit under 'Deferred tax assets' vs 'Deferred tax liabilities' blocks (or carry "
                     "signs in a single 'assets/(liabilities)' table) — honour the block/orientation the "
                     "target names; the same row label can appear in BOTH orientations' matrices. Component "
                     "labels are idiosyncratic umbrellas: a depreciation / PP&E / written-down-value line IS "
                     "the accumulated-depreciation component; a leave / compensated-absences / employee-"
                     "benefits / 'separation and retirement' line IS the leave-encashment component.\n"),
}


def _reconciled_section(index: PageIndex, scope: str, section: str, concepts: list[Concept],
                        parent: float | None, note_pages: list[int],
                        allowed: set[int]) -> list[Datapoint] | None:
    """Try candidate note pages; accept the one whose components reconcile to the
    parent statement line. Note-ref pages (deterministic) are tried first, then
    keyword retrieval. Returns trusted Datapoints, or None if none reconcile."""
    if parent is None:
        return None
    ex_terms = " ".join(e for c in concepts for e in _examples(c, section))
    bm25 = [p for p in index.search(SECTIONS[section] + " " + scope + " " + ex_terms, k=20)
            if not allowed or p in allowed]
    ranked = note_pages + [p for p in bm25 if p not in note_pages]   # note-ref first
    items_desc = "\n".join(
        f"- key: {c.key}\n  meaning: {c.meaning[:400]}"
        + (f"\n  selector: {c.selector}" if c.selector else "")
        + (f"\n  examples: {'; '.join(_examples(c, section))}" if c.examples else "")
        for c in concepts)
    hint = _SECTION_HINT.get(section, "")
    # `_note_pages` puts the correct note page FIRST in `ranked`, so the forward [0,1]
    # window already spans a note split across a page boundary. [-1,0] is fallback
    # insurance for the case where only BM25 surfaces the note and it lands on the note's
    # SECOND page (heading + half the lines on the preceding page) — added only for the
    # multi-page sections, so the common single-page case keeps its original cost.
    offsets = ([[0], [0, 1], [-1, 0], [0, 1, 2]] if section in ("ppe", "other_expenses")
               else [[0], [0, 1]])
    n = index.n_pages
    cand_windows = []
    for pg in ranked[:4]:
        for off in offsets:
            w = [pg + o for o in off if 1 <= pg + o <= n]
            if w and w not in cand_windows:
                cand_windows.append(w)

    # Column-sections read reflowed here too. (A historical carve-out excluded share_capital to
    # keep its reconcile read byte-identical, but share_capital is NON_ADDITIVE now and never
    # reaches this function — the carve-out was dead complexity and is gone.)
    _reconcile_reflow = section in COLUMN_SECTIONS
    best = None  # (n_targets_present, datapoints)
    for pages in cand_windows[:6]:        # bound cost (reconcile early-exits when found)
        o = llm.extract_json(
            instructions=_NOTE_INSTR.format(scope=scope, hint=hint),
            user_input=(f"SECTION: {SECTION_LABEL[section]}\n\nThere are EXACTLY {len(concepts)} TARGETS — "
                        f"your `targets` array MUST contain all {len(concepts)}, one per key, none omitted "
                        f"(present=false only if genuinely absent).\n\nTARGETS:\n{items_desc}\n\nTEXT:\n{index.text_of(pages, columns=_reconcile_reflow)}"),
            schema_name="note", schema=_NOTE_SCHEMA, max_output_tokens=2600, reasoning="low")
        if o.get("_empty"):
            continue
        comps = o.get("components", [])
        tot = _num(o.get("total"))
        csum = sum(_num(c["value"]) or 0 for c in comps)
        if tot is None or not comps:
            continue
        reconciled = (abs(csum - tot) < max(abs(tot) * 0.01, 1)
                      and abs(tot - parent) < max(abs(parent) * 0.01, 1))
        if not reconciled:
            continue
        tmap = {t["key"]: t for t in o.get("targets", [])}
        out, n_present = [], 0
        for c in concepts:
            t = tmap.get(c.key)
            if not t or not t.get("present") or t.get("value") in (None, "", "-"):
                out.append(Datapoint(key=c.key, present=False, section=section, pages=pages,
                                     confidence="absent"))
            else:
                ev = t.get("evidence", "")
                val = _hygiene(t["value"], ev, sign=c.value_type != "count")
                vnum = _num(val)
                # REFUSAL GUARD: a specific sub-item cannot equal the note's grand TOTAL when
                # that total is a genuine sum of >=2 MATERIALLY non-zero components — that is a
                # parent/total grab. Counting only material components avoids a false positive when
                # one component dominates and the others are ~0 (then that single line legitimately
                # equals the total, e.g. MSME=0). EXEMPT datapoints whose DEFINITION asks for the
                # total (e.g. 'Other Long Term Liabilities' = the BS total line) — they SHOULD equal it.
                material = [cc for cc in comps
                           if abs(_num(cc.get("value")) or 0) > max(abs(tot) * 0.005, 0.5)]
                if (vnum is not None and not _wants_total(c.meaning, c.selector) and len(material) >= 2
                        and abs(vnum - tot) < max(abs(tot) * 0.005, 0.5)):
                    out.append(Datapoint(key=c.key, present=False, section=section, pages=pages,
                                         confidence="absent"))
                    continue
                n_present += 1
                out.append(Datapoint(key=c.key, present=True, value=val,
                                     evidence=ev, grounded=True,
                                     section=section, pages=pages, confidence="reconciled"))
        # keep the RECONCILED page that also surfaces the most targets (recall + precision)
        if best is None or n_present > best[0]:
            best = (n_present, out)
        if n_present == len(concepts):
            break
    return best[1] if best else None


MATRIX_SECTIONS = {"ppe"}    # wide movement schedules — read via vision, not flattened text

_MATRIX_SCHEMA = {"type": "object", "properties": {
    "assets": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "gross": {"type": ["string", "null"]},
        "accumulated_depreciation": {"type": ["string", "null"]}, "net": {"type": ["string", "null"]}},
        "required": ["name", "gross", "accumulated_depreciation", "net"], "additionalProperties": False}},
    "total_net": {"type": ["string", "null"]},
    "targets": {"type": "array", "items": {"type": "object", "properties": {
        "key": {"type": "string"}, "present": {"type": "boolean"}, "value": {"type": ["string", "null"]},
        "evidence": {"type": "string"}}, "required": ["key", "present", "value", "evidence"],
        "additionalProperties": False}}},
    "required": ["assets", "total_net", "targets"], "additionalProperties": False}

_MATRIX_INSTR = (
    "This page IMAGE is a {scope} Property, Plant & Equipment MOVEMENT SCHEDULE — a wide table with "
    "asset classes as COLUMNS and Gross block / Accumulated depreciation / Net block as row groups. "
    "Return EACH asset-class column: name, GROSS carrying value and ACCUMULATED DEPRECIATION at the "
    "LATEST year-end, and NET. Read footnotes (e.g. '$ includes office equipment' merges that into the "
    "marked column). Also total_net.\n"
    "Then for each TARGET below, return the value by MEANING + selector (GROSS or ACCUMULATED "
    "DEPRECIATION of the named asset column at the latest date); present=false if that asset column "
    "doesn't exist. Numbers as printed; () = negative."
)


# asset-class keyword sets for the PPE matrix backstop (framework asset classes, not company
# labels). 'equipment' matches a row labelled 'Equipments'/'Office Equipment'; reports often
# merge office equipment into a single 'Equipments' row (footnote '$ Includes Office Equipments').
_PPE_ASSET_KW: list[tuple[str, list[str]]] = [
    ("office equip", ["office equip", "equipment"]),
    ("furnitur", ["furnitur", "fixture"]),
    ("vehicle", ["vehicle"]),
    ("building", ["building"]),
    ("plant", ["plant"]),
    ("land", ["land"]),
    ("computer", ["computer"]),
]


def _ppe_backstop(key: str, assets: list[dict]):
    """Map a PPE asset-class target (e.g. gross/accum of Office Equipment) to its asset row by
    keyword when the model failed the target step. Returns the cell value or None."""
    kl = key.lower()
    col = "gross" if kl.startswith("gross") else \
          "accumulated_depreciation" if kl.startswith("accumulated") else None
    if col is None:
        return None
    leaf = kl.split("_")[-1]
    kws = next((kw for stem, kw in _PPE_ASSET_KW if stem in leaf), None)
    if not kws:
        return None
    for a in assets:
        nm = (a.get("name") or "").lower()
        if any(k in nm for k in kws) and a.get(col) not in (None, "", "-"):
            return a.get(col)
    return None


# ---------------------------------------------------------------------------
# Deterministic per-ROW PPE reader, self-validated by the Ind AS 16 movement identity.
# WHY: the page-level Σnet==BS-parent gate in _matrix_text structurally FAILS on mixed schedules
# (reliance prints PPE+Spectrum+Intangibles+Goodwill in ONE table; adani mixes PPE+ROU+Intangible
# columns), so correct grounded text reads were discarded and the vision fallback produced ALL 12
# ppe errors of the 2026-07-03 run (wrong "(Contd.)" pages, prior-year blocks, and outright digit
# hallucinations). The movement identity is PER-ROW and mandated by Schedule III for every Indian
# company: gross_open ± movements = gross_close;  dep_open ± movements = dep_close;
# gross_close − dep_close = net. A row parse is accepted ONLY when all three close — far stronger
# and more general than Σnet==parent, and immune to mixed schedules. Proven deterministically
# ($0): recovers 16/16 GT values across the four failing company/scope cases, including the
# pdftotext-shredded adani p304 via PyMuPDF word y-clusters.
# ---------------------------------------------------------------------------

def _yc_lines(path: str, pno: int) -> list[str]:
    """Rebuild a page's visual rows from PyMuPDF word y-clusters. When pdftotext shreds a wide
    table (each cell dumped one-per-line) or the table is stored rotated, the words still carry
    true coordinates — clustering by y reconstitutes each visual row (for a rotated table a
    visual column becomes one y-run, which is exactly the asset row we need)."""
    import fitz
    doc = fitz.open(path)
    rows: dict[int, list] = {}
    for w in doc[pno - 1].get_text("words"):
        if w[4].strip():
            rows.setdefault(round(w[1] / 4.0), []).append(w)
    out = []
    for k in sorted(rows):
        out.append(" ".join(w[4] for w in sorted(rows[k], key=lambda w: w[0])))
    doc.close()
    return out


def _row_nums(s: str) -> list[float]:
    """Numeric tokens of a schedule row, in print order; a lone '-' cell is 0."""
    out = []
    for t in re.findall(r"\(?-?[\d,]*\.?\d+\)?|(?<=\s)-(?=\s)", s):
        t = t.strip()
        if t == "-":
            out.append(0.0)
        elif any(c.isdigit() for c in t):
            neg = t.startswith("(")
            v = float(t.strip("()").replace(",", ""))
            out.append(-v if neg else v)
    return out


def _block_closes(vals: list[float], tol: float = 0.02) -> bool:
    """True if vals = [open, m1..mk, close] satisfies open ± m_i == close for some signs."""
    if len(vals) < 2:
        return False
    o, c, mids = vals[0], vals[-1], vals[1:-1]
    if len(mids) > 8:
        return False
    for signs in product((1, -1), repeat=len(mids)):
        if abs(o + sum(s * m for s, m in zip(signs, mids)) - c) < tol:
            return True
    return False


def _solve_ppe_row(v: list[float], tol: float = 0.02) -> list[tuple[float, float, float]]:
    """Split a row's number sequence into gross block | dep block | net and return every
    arithmetic-consistent (gross_close, dep_close, net) split."""
    n = len(v)
    sols = []
    for ge in range(1, n - 2):
        for de in range(ge + 2, n - 1):
            net = v[de + 1]
            if abs((v[ge] - v[de]) - net) > tol:
                continue
            if _block_closes(v[:ge + 1], tol) and _block_closes(v[ge + 1:de + 1], tol):
                sols.append((v[ge], v[de], net))
    return sols


def _row_segments(ln: str, kws: list[str]) -> list[list[float]]:
    """Split a line at repeated asset-label occurrences (two year-blocks printed side by side)
    into per-year number segments. Numbers BEFORE the first label are dropped: the label column
    is leftmost in a per-row schedule, so anything numeric ahead of it is foreign weld (hindalco
    p348 welds policy prose '…Rules, 2024 and' onto the row — the stray 2024 poisoned the
    movement identity and let the clean PRIOR-year row win instead)."""
    low = ln.lower()
    idxs = sorted({m.start() for kw in kws for m in re.finditer(re.escape(kw), low)})
    merged: list[int] = []
    for i in idxs:
        if not merged or i - merged[-1] > 40:
            merged.append(i)
    if not merged:
        return [_row_nums(ln)]                # label matched via the ±2 window, not this line
    if len(merged) == 1:
        return [_row_nums(ln[merged[0]:])]
    segs = [_row_nums(ln[a:b]) for a, b in zip(merged, merged[1:] + [len(ln)])]
    return [s for s in segs if len(s) >= 6]


def _current_ppe_segment(segs: list[list[float]], tol: float = 0.02) -> list[float]:
    """Pick the CURRENT-year segment by year chaining: the segment whose opening equals another
    segment's solved gross_close is the LATER year. No date parsing needed. A single segment that
    actually welds BOTH years into one number run (rotated grids print the label once) is split
    numerically: both halves must independently satisfy the movement identity AND chain."""
    if len(segs) == 1:
        seg = segs[0]
        for k in range(6, len(seg) - 5):
            a, b = seg[:k], seg[k:]
            sa, sb = _solve_ppe_row(a, tol), _solve_ppe_row(b, tol)
            if sa and sb:
                if any(abs(b[0] - gc) < tol for gc, _, _ in sa):
                    return b                 # b opens where a closed -> b is the later year
                if any(abs(a[0] - gc) < tol for gc, _, _ in sb):
                    return a
        return seg
    solved = [(i, _solve_ppe_row(s)) for i, s in enumerate(segs)]
    for i, s in enumerate(segs):
        for j, sols in solved:
            if j == i or not sols:
                continue
            if any(abs(s[0] - gc) < tol for gc, _, _ in sols):
                return s
    return segs[-1]


def _matrix_rows(index: PageIndex, scope: str, section: str, concepts: list[Concept],
                 note_pages: list[int], allowed: set[int]) -> dict[str, "Datapoint"]:
    """Deterministic asset-per-ROW reader: returns {key: Datapoint} ONLY for targets whose row
    parse self-validates by the movement identity — never a guess, so callers can trust every
    entry and let the model paths fill the rest. Empty dict when nothing validates."""
    bm25 = [p for p in index.search(SECTIONS[section] + " " + scope, k=12) if not allowed or p in allowed]
    cands = note_pages + [p for p in bm25 if p not in note_pages]
    all_stems = [kw for _, kws in _PPE_ASSET_KW for kw in kws]

    def yr(t: str) -> int:
        # the page's FISCAL year = the LATEST year appearing at least TWICE (a genuine
        # statement year repeats in headers/'As at' column dates; a stray future year like a
        # lease maturing 2040 appears once and must not outrank it). Plain max is vulnerable
        # to the stray; plain mode mis-ranks rotated grids where the prior year is the modal
        # token (adani p304) — rep-max matches max on every corpus schedule page AND survives
        # the stray-year case. Falls back to plain max when no year repeats.
        ys = [int(y) for y in re.findall(r"\b(20\d{2})\b", t)]
        rep = [y for y in set(ys) if ys.count(y) >= 2]
        return max(rep) if rep else (max(ys) if ys else 0)

    def _target_map(key: str) -> tuple[str | None, list[str]]:
        kl = key.lower()
        block = "gross" if kl.startswith("gross") else "dep" if kl.startswith("accumulated") else None
        if block is None:
            return None, []
        leaf = kl.split("_")[-1]
        return block, next((kw for stem, kw in _PPE_ASSET_KW if stem in leaf), [])

    def _line_ok(low: str, kws: list[str]) -> bool:
        # the row must be THIS asset: specific keyword always accepted; a generic secondary
        # keyword (e.g. 'equipment' for office equipment) is accepted only when no OTHER asset
        # stem shares the line (rejects 'Plant and Equipment', ROU 'Right-of-Use ... Equipment')
        if "right of use" in low or "right-of-use" in low:
            return False
        if kws[0] in low:
            return True
        others = [s for s in all_stems if s not in kws]
        return any(kw in low for kw in kws[1:]) and not any(s in low for s in others)

    found: dict[str, tuple[float, str, int, int]] = {}   # key -> (value, line, page, page_year)
    for pg in cands[:6]:
        page_year = yr(index.page_text[pg - 1])
        lay, *_ = _ppe_layout(index.text_of([pg]))
        if lay == "shredded":
            # -layout destroyed the grid: its welded fragments can still 'solve' arithmetically
            # by accident, so DON'T scan them — rebuild the true rows from word coordinates only
            sources = [_yc_lines(index.path, pg)]
        else:
            # layout rows first; y-clusters as a second chance (a rotated/per-column table has no
            # asset-label rows in layout text, but its visual columns become y-cluster rows)
            sources = [index.page_text[pg - 1].splitlines(), _yc_lines(index.path, pg)]
        for lines in sources:
            low_lines = [ln.lower() for ln in lines]
            for c in concepts:
                block, kws = _target_map(c.key)
                if not kws or c.key in found and found[c.key][3] >= page_year:
                    continue
                # Candidate rows are SCORED, not first-match: rotated grids scatter the label's
                # words into ADJACENT y-cluster lines ('Office' lands one y-run over from the
                # 'Equipment 344.90 …' data run) while an unrelated 'Equipments' fragment (the
                # tail of a wrapped 'Plant & Equipments' header) sits earlier in y-order — so a
                # specific label ON the row (2) beats label words in the ±2 window (1) beats a
                # bare generic fragment (0.5). Verified to separate the true office-equipment
                # run from the plant/'Equipments' fragment runs on both adani schedules while
                # keeping the reliance 'Equipments $' merged-row match.
                best = None                   # (score, -line_idx) -> value, line
                others = [k[0] for _, k in _PPE_ASSET_KW if k[0] not in kws]
                for i, ln in enumerate(lines):
                    low = low_lines[i]
                    if "right of use" in low or "right-of-use" in low or "total" in low:
                        continue
                    if any(all(w in low for w in k0.split()) for k0 in others):
                        continue              # the row itself names a DIFFERENT asset
                    win = " ".join(low_lines[max(0, i - 2):i + 3])
                    if all(w in low for w in kws[0].split()):
                        score = 2.0
                    elif all(w in win for w in kws[0].split()):
                        score = 1.0
                    elif _line_ok(low, kws):
                        score = 0.5
                    else:
                        continue
                    if best is not None and best[0] >= score:
                        continue              # already have an equal-or-better candidate
                    if len(_row_nums(ln)) < 6:
                        continue
                    seg = _current_ppe_segment(_row_segments(ln, kws))
                    sols = _solve_ppe_row(seg)
                    if not sols:
                        continue
                    gcs, dcs = {round(s[0], 2) for s in sols}, {round(abs(s[1]), 2) for s in sols}
                    val = None
                    if block == "gross" and len(gcs) == 1:
                        val = sols[0][0]
                    elif block == "dep" and len(dcs) == 1:
                        val = abs(sols[0][1])
                    if val is None:           # ambiguous splits disagree -> refuse (miss > wrong)
                        continue
                    best = (score, val, ln.strip())
                if best is not None:
                    found[c.key] = (best[1], best[2], pg, page_year)
    if not found:
        return {}
    latest = max(v[3] for v in found.values())
    out: dict[str, Datapoint] = {}
    for key, (val, ln, pg, py) in found.items():
        if py < latest:                       # prior-year comparative page — its closing is stale
            continue
        whole = abs(val - round(val)) < 0.005
        sval = f"{int(round(val)):,}" if whole else f"{val:,.2f}"
        out[key] = Datapoint(key=key, present=True, value=sval,
                             evidence=f"(movement-identity validated row) {ln[:140]}",
                             grounded=True, section=section, pages=[pg], confidence="grounded")
    return out


def _matrix_vision(index: PageIndex, scope: str, section: str, concepts: list[Concept],
                   parent: float | None, note_pages: list[int], allowed: set[int]) -> list[Datapoint] | None:
    """Vision read of a wide schedule; reconcile Σnet to the BS line; map targets."""
    from src.engine import vision
    bm25 = [p for p in index.search(SECTIONS[section] + " " + scope, k=12) if not allowed or p in allowed]
    cands = note_pages + [p for p in bm25 if p not in note_pages]
    items = "\n".join(f"- {c.key}: {c.meaning[:400]} [{c.selector}]" for c in concepts)
    best = None
    for pg in cands[:4]:
        img = vision.render_page(index.path, pg, 400)   # dense multi-column grid: 300 misreads digits
        o = llm.extract_json(
            instructions=_MATRIX_INSTR.format(scope=scope),
            user_input=f"TARGETS:\n{items}\n\nExtract the schedule from the attached image.",
            schema_name="ppe_matrix", schema=_MATRIX_SCHEMA, images_b64=[img],
            max_output_tokens=2500, reasoning="low")
        if o.get("_empty"):
            continue
        assets = o.get("assets", [])
        if not assets:
            continue
        snet = sum(_num(a["net"]) or 0 for a in assets)
        reconciled = parent is not None and abs(snet - parent) < max(abs(parent) * 0.01, 1)
        tmap = {t["key"]: t for t in o.get("targets", [])}
        # GROUNDING GUARD: vision misreads/invents digits on dense wide grids (reliance cons
        # returned 13,287 and 61,473 — printed NOWHERE in the whole PDF — and a page-level Σnet
        # tie-out even blessed a 6->8 single-digit misread as 'reconciled'). Shredding destroys
        # the text's GEOMETRY, never its DIGITS, so any true cell value appears verbatim in the
        # page's raw text — a vision value whose digit-string doesn't is a fabrication. Skipped
        # only on genuinely image-only pages (no extractable text to ground against).
        page_digits = _digits(index.page_text[pg - 1])
        can_ground = len(index.page_text[pg - 1].strip()) >= 200
        def _ungrounded(v) -> bool:
            vd = _digits(v)
            return can_ground and len(vd) >= 3 and vd not in page_digits
        out, n = [], 0
        for c in concepts:
            t = tmap.get(c.key)
            conf = "reconciled" if reconciled else "unverified"
            # Prefer the model's per-target mapping (it reads the 'Equipments $' row correctly); fall
            # back to the deterministic asset-row backstop ONLY when the model didn't map the target.
            # (Making the backstop authoritative regressed gross — its 'equipment' keyword matched a
            # wrong asset row, 21,042 vs the correct 71,504.)
            if t and t.get("present") and t.get("value") not in (None, "", "-") \
                    and not _ungrounded(t["value"]):
                n += 1
                ev = t.get("evidence", "")
                out.append(Datapoint(key=c.key, present=True, value=_hygiene(t["value"], ev), evidence=ev,
                                     grounded=reconciled, section=section, pages=[pg], confidence=conf))
            else:
                bv = _ppe_backstop(c.key, assets)
                if bv is not None and not _ungrounded(str(bv)):
                    n += 1
                    out.append(Datapoint(key=c.key, present=True, value=_hygiene(str(bv), ""),
                                         evidence="(matrix backstop: asset-row map)", grounded=reconciled,
                                         section=section, pages=[pg], confidence=conf))
                else:
                    out.append(Datapoint(key=c.key, present=False, section=section, pages=[pg], confidence="absent"))
        if reconciled:
            return out                       # tree closes -> trust it
        if best is None or n > best[0]:
            best = (n, out)
    return best[1] if best else None


_MATRIX_TEXT_INSTR = (
    "The TEXT below is a {scope} PROPERTY, PLANT & EQUIPMENT movement schedule. It is laid out ONE OF TWO "
    "ways — detect which:\n"
    "  (A) ASSET-PER-ROW: each asset class is a ROW; columns are Gross Block (opening … CLOSING) then "
    "Depreciation (opening … CLOSING) then Net.\n"
    "  (B) ASSET-PER-COLUMN: asset classes are the COLUMN HEADERS (Land, Buildings, Plant, Office "
    "equipment, Computer, Furniture & fixtures, …) and the ROWS are movement lines — 'Gross carrying value "
    "as at <opening date>', 'Additions', 'Deletions', 'Gross carrying value as at <closing date>', then "
    "'Accumulated depreciation as at <opening>' … 'Accumulated depreciation as at <closing>'.\n"
    "CRITICAL — ALWAYS take the CLOSING / CURRENT-YEAR-END figure (the LATER 'As at 31 March <current "
    "year>' / 'Closing Balance' row or column). NEVER take the OPENING balance ('As at 1 April' / "
    "'as at <prior-year start>' / 'Opening Balance') — that is the prior year and is WRONG.\n"
    "Return EVERY asset class: name; gross = its GROSS-block CLOSING value; accumulated_depreciation = its "
    "DEPRECIATION CLOSING value; net = its Net-block current value. In layout (B) match each asset by its "
    "COLUMN position left-to-right (count headers and values in the same order; watch look-alikes — Office "
    "equipment vs Computer equipment vs Plant). Honour footnotes ('$ Includes Office Equipments' → the "
    "'Equipments' class IS Office Equipment).\n"
    "Then for each TARGET return its value by the selector (GROSS or ACCUMULATED DEPRECIATION of the named "
    "asset, CLOSING); present=false if that asset is absent. Numbers exactly as printed; () = negative."
)


# Standard Ind AS / Schedule II PPE asset classes. GENERIC accounting vocabulary (NOT company- or
# report-specific) — used only to read the printed LEFT-TO-RIGHT position of each column header so
# the model can't mis-map a column when pdftotext merges adjacent headers ("Land – Buildings" -> one
# token) or stacks them across lines. Specific multi-word terms first so they win the offset.
_PPE_COL_VOCAB = [
    ("Right-of-Use Assets", ("right-of-use", "right of use")),
    ("Office Equipment", ("office equip", "office")),
    ("Plant & Machinery", ("plant and machinery", "plant & machinery", "plant and equipment", "plant")),
    ("Furniture & Fixtures", ("furniture", "furnitur", "fixture")),
    ("Computers", ("computer", "data processing")),
    ("Electrical", ("electric",)),
    ("Leasehold Improvements", ("leasehold",)),
    ("Buildings", ("building",)),
    ("Aircraft", ("aircraft", "air craft")),
    ("Ships/Vessels", ("vessel", "ship")),
    ("Vehicles", ("vehicle",)),
    ("Land", ("land",)),
    ("Total", ("total",)),
]


def _ppe_layout(text: str) -> tuple[str, list[str], int]:
    """Classify a PPE schedule's TEXT layout from GEOMETRY alone (no company/report knowledge):
      'per_row'    -> assets are rows (Reliance/Hindalco/ITC) — existing model read handles it.
      'per_column' -> assets are column headers AND the header sits on ONE line so the printed
                      left-to-right offsets are TRUE x-positions; returns that trusted asset COLUMN
                      ORDER so the model maps targets by position, not by the merged header (Infosys).
      'shredded'   -> the wide table did NOT survive pdftotext as aligned rows (each cell isolated
                      one-per-line); text is unusable -> caller routes to vision.
    A per-column table whose header is STACKED across several lines (Adani's A3 schedule) is NOT
    trustworthy — cross-line char offsets don't reflect column x-positions — so it is reported as
    'per_row' (no injection): the plain model read + reconciliation guard still apply, with no risk
    of injecting a scrambled order. Returns (layout, ordered_asset_labels, n_data_cols)."""
    lines = text.splitlines()
    def ncount(ln: str) -> int:
        return len([t for t in re.findall(r"\(?-?\d[\d,]*\.?\d*\)?", ln) if any(c.isdigit() for c in t)])
    wide = [i for i in range(len(lines)) if ncount(lines[i]) >= 5]   # aligned data rows
    if len(wide) < 3:
        return ("shredded", [], 0)
    n_cols = max(ncount(lines[i]) for i in wide)
    header = lines[max(0, wide[0] - 8):wide[0]]                      # region above the first data row
    # In pdftotext -layout the CHARACTER OFFSET is the horizontal x-position, independent of which
    # header line a word lands on. So sort purely by offset: this recovers the true left-to-right
    # column order even when the header is stacked across lines (Infosys: 'Land – Buildings' on one
    # line, the rest on the next), while a genuinely scrambled header (Adani A3, where words wrap out
    # of column order) fails the Total-is-rightmost sanity check below and is rejected.
    found = []                                                       # (col_offset, label)
    for label, kws in _PPE_COL_VOCAB:
        best = None
        for ln in header:
            low = ln.lower()
            for kw in kws:
                c = low.find(kw)
                if c >= 0 and (best is None or c < best):
                    best = c
        if best is not None:
            found.append((best, label))
    found.sort()
    ordered, seen = [], set()
    for _, lab in found:                                            # de-dup, keep printed x-order
        if lab not in seen:
            ordered.append(lab); seen.add(lab)
    assets = [a for a in ordered if a != "Total"]
    # TRUST the geometric order only when >=3 asset columns AND Total is the rightmost column. A
    # scrambled header puts Total mid-list (or an asset right of it) -> not trusted -> 'per_row'
    # (plain model read + reconciliation guard still apply; no risk of injecting a wrong order).
    trusted = len(assets) >= 3 and ordered and ordered[-1] == "Total"
    if trusted:
        return ("per_column", ordered, n_cols)
    return ("per_row", [], n_cols)


def _per_column_closing(text: str, col_order: list[str]) -> dict[str, dict[str, str]]:
    """DETERMINISTIC read of an asset-per-column PPE schedule (only valid when _ppe_layout returned a
    TRUSTED col_order). The schedule stacks current- and prior-year movements as rows; the model picks
    the OPENING/prior row ~half the time (Infosys std 2,126 vs 2,200) even when told not to. So pick
    the row in code: within the GROSS and the DEPRECIATION blocks, take the row dated the LATEST year
    (the closing balance) and map its values to assets by the trusted column order. No company logic —
    block keywords + max-year + column order. Returns {'gross': {label: value}, 'dep': {label: value}}."""
    K = len(col_order)
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text)]
    if not years or K == 0:
        return {"gross": {}, "dep": {}}
    cy = max(years)
    out: dict[str, dict[str, str]] = {"gross": {}, "dep": {}, "net": {}}
    block = None
    for ln in text.splitlines():
        low = ln.lower()
        if "gross" in low and ("carry" in low or "block" in low or "cost" in low):
            block = "gross"
        elif "depreciation" in low or "amortis" in low:
            block = "dep"
        elif "carrying value" in low or "net block" in low or "net carrying" in low:
            block = "net"
        if block not in ("gross", "dep", "net"):
            continue
        nums = [t for t in re.findall(r"\(?-?[\d,]+\.?\d*\)?", ln) if any(c.isdigit() for c in t)]
        rowyrs = [int(y) for y in re.findall(r"\b(20\d{2})\b", ln)]
        # a current-year CLOSING data row: dated the latest year, with a full set of column values
        if rowyrs and max(rowyrs) == cy and len(nums) >= K:
            vals = nums[-K:]                          # last K tokens are the columns (drops the date)
            for i, lab in enumerate(col_order):
                out[block].setdefault(lab, vals[i])   # first closing row per block wins
    return out


def _ppe_det_target(key: str, col_order: list[str]) -> tuple[str | None, str | None]:
    """Map a PPE asset-class concept to (block, column-label) for the deterministic reader. Generic:
    'gross …'->gross block, 'accumulated …'->dep block; asset matched by framework keyword."""
    kl = key.lower()
    block = "gross" if kl.startswith("gross") else "dep" if kl.startswith("accumulated") else None
    if block is None:
        return (None, None)
    leaf = kl.split("_")[-1]
    kws = next((kw for stem, kw in _PPE_ASSET_KW if stem in leaf), None)
    if not kws:
        return (None, None)
    lab = next((L for L in col_order if any(k in L.lower() for k in kws)), None)
    return (block, lab)


def _matrix_columns(index: PageIndex, scope: str, section: str, concepts: list[Concept],
                    parent: float | None, note_pages: list[int], allowed: set[int]) -> list[Datapoint] | None:
    """DETERMINISTIC reader for asset-per-COLUMN PPE schedules (Infosys-style). No model call, so no
    run-to-run variance: it locates the genuine CURRENT-YEAR per-column page and reads the closing
    gross/depreciation rows straight from the de-rotated layout text, mapping columns by the trusted
    header geometry (_ppe_layout). Returns None when no such page exists (asset-per-row or shredded
    layouts) so the caller falls through to the model text read / vision.

    Why deterministic-first instead of patching the model loop: the schedule survives pdftotext as
    perfectly aligned rows here, so the only thing the model added was variance (column shift, and
    opening-vs-closing / prior-year-page row picks). Code reads it exactly once, the same way, and the
    arithmetic tie-out (schedule net total == balance-sheet PPE line) verifies it."""
    bm25 = [p for p in index.search(SECTIONS[section] + " " + scope, k=12) if not allowed or p in allowed]
    cands = note_pages + [p for p in bm25 if p not in note_pages]
    yr = lambda t: max((int(y) for y in re.findall(r"\b(20\d{2})\b", t)), default=0)
    # Classify each candidate on its OWN single-page text (no pg+1 bleed — that mixed the prior-year
    # comparative page into the window). Collect only the trusted asset-per-COLUMN pages; the current
    # reporting year is the max year AMONG THOSE (a stray '2033' on an unrelated page must not define it).
    pcs = []                                           # (page, text, col_order, page_year)
    for p in cands[:8]:
        text = index.text_of([p])
        layout, col_order, _ = _ppe_layout(text)
        if layout == "per_column" and col_order:
            pcs.append((p, text, col_order, yr(text)))
    if not pcs:
        return None
    doc_year = max(py for *_, py in pcs)
    best = None
    for p, text, col_order, py in pcs:
        if doc_year and py < doc_year:                 # prior-year comparative page — its closing is last year's
            continue
        det = _per_column_closing(text, col_order)
        if not det["gross"] and not det["dep"]:
            continue
        # tie-out: the schedule's own net-block Total equals the balance-sheet PPE line.
        total_net = _num(det["net"].get("Total", ""))
        reconciled = parent is not None and total_net is not None and abs(total_net - parent) < max(abs(parent) * 0.01, 1)
        conf = "reconciled" if reconciled else "grounded"
        out = []
        for c in concepts:
            block, lab = _ppe_det_target(c.key, col_order)
            dv = det.get(block, {}).get(lab) if block else None
            if dv not in (None, "", "-"):
                out.append(Datapoint(key=c.key, present=True, value=_hygiene(dv, ""),
                                     evidence=f"(deterministic per-column closing-row read: {lab}, FY{doc_year})",
                                     grounded=True, section=section, pages=[p], confidence=conf))
            else:
                out.append(Datapoint(key=c.key, present=False, section=section, pages=[p], confidence="absent"))
        if reconciled:
            return out                                 # tie closed -> trust it, stop scanning
        if best is None:
            best = out
    return best


def _matrix_text(index: PageIndex, scope: str, section: str, concepts: list[Concept],
                 parent: float | None, note_pages: list[int], allowed: set[int]) -> list[Datapoint] | None:
    """Asset-per-ROW PPE schedule read from LAYOUT TEXT. pdftotext -layout de-rotates the sideways
    wide table into clean ordered rows, so this is DETERMINISTIC — no vision hallucination (vision
    invented 23,110/15,232 from the rotated image). Returns None if the text can't be read as a
    per-row schedule (likely an asset-per-COLUMN layout), so the caller falls back to vision."""
    bm25 = [p for p in index.search(SECTIONS[section] + " " + scope, k=12) if not allowed or p in allowed]
    cands = note_pages + [p for p in bm25 if p not in note_pages]
    items = "\n".join(f"- {c.key}: {c.meaning[:400]} [{c.selector}]" for c in concepts)
    best = None
    best_grounded = None  # most-grounded read seen across candidates (fallback when none reconcile)
    for pg in cands[:6]:
        pages = [pg, pg + 1] if pg + 1 <= index.n_pages else [pg]
        text = index.text_of(pages)
        page_digits = _digits(text)
        # Shredded wide tables (pdftotext dumped each cell one-per-line, no row/column adjacency —
        # Adani's A3 schedule) are unreadable as text: skip so the caller falls through to vision,
        # instead of returning a wrong-but-grounded cell. (Asset-per-COLUMN pages were already handled
        # deterministically by _matrix_columns; this path is the asset-per-ROW model read.)
        if _ppe_layout(text)[0] == "shredded":
            continue
        o = llm.extract_json(
            instructions=_MATRIX_TEXT_INSTR.format(scope=scope),
            user_input=f"TARGETS:\n{items}\n\nTEXT:\n{text}",
            schema_name="ppe_matrix", schema=_MATRIX_SCHEMA, max_output_tokens=2500, reasoning="low")
        if o.get("_empty"):
            continue
        assets = o.get("assets", [])
        if not assets:
            continue
        snet = sum(_num(a["net"]) or 0 for a in assets)
        reconciled = parent is not None and abs(snet - parent) < max(abs(parent) * 0.01, 1)
        tmap = {t["key"]: t for t in o.get("targets", [])}
        out, n, grounded_hits = [], 0, 0
        for c in concepts:
            t = tmap.get(c.key)
            val, ev = None, ""
            if t and t.get("present") and t.get("value") not in (None, "", "-"):
                val, ev = _hygiene(t["value"], t.get("evidence", "")), t.get("evidence", "")
            else:
                bv = _ppe_backstop(c.key, assets)
                if bv is not None:
                    val, ev = _hygiene(str(bv), ""), "(matrix text backstop: asset-row map)"
            if val is None:
                out.append(Datapoint(key=c.key, present=False, section=section, pages=pages, confidence="absent"))
                continue
            # GROUNDING: the value must actually appear in the layout text (kills hallucinations the
            # way vision couldn't — 23,110 would NOT be in the text, so it can't slip through here).
            g = len(_digits(val)) >= 3 and _digits(val) in page_digits
            if g:
                grounded_hits += 1
            n += 1
            out.append(Datapoint(key=c.key, present=True, value=val, evidence=ev,
                                 grounded=g or reconciled, section=section, pages=pages,
                                 confidence="reconciled" if reconciled else ("grounded" if g else "unverified")))
        # A reconciled read is the gold standard — its net block ties out to the CURRENT-year PPE
        # parent total, which a prior-year COMPARATIVE schedule (same asset names, last-year's
        # closing date) never will. So return immediately on reconcile, but otherwise keep scanning
        # all candidates and keep the most-grounded read — never early-return on the first merely-
        # grounded page, or BM25 surfacing the prior-year page first wins (infosys std: 2,126 = last
        # year's close vs 2,200 = this year's).
        if reconciled:
            return out
        if grounded_hits >= 1 and (best_grounded is None or grounded_hits > best_grounded[0]):
            best_grounded = (grounded_hits, out)
        elif best is None or n > best[0]:
            best = (n, out)
    # No candidate tied out. When we HAVE a parent to check against, a non-reconciling text read is
    # untrustworthy (Adani cons: the A3 schedule is shredded, so the model returns grounded-but-wrong
    # cells like 23,783) — return None so the caller tries vision. Only fall back to the grounded read
    # when there's no parent to verify against (can't tell right from wrong, so keep best effort).
    if parent is not None:
        return None
    if best_grounded is not None:
        return best_grounded[1]
    return None


# Garbled SPECIFIC-LINE notes — dense multi-column tables that pdftotext scrambles, where
# the answer is a specific CELL (not a sum). Read via vision. Proven on reliance Borrowings:
# text grabbed a maturity-profile '917' for unsecured Term Loans from Others; the rendered
# table reads the real cell (1,487) cleanly.
VISION_TARGET_SECTIONS = {"borrowings"}

_TARGETS_SCHEMA = {"type": "object", "properties": {
    "targets": {"type": "array", "items": {"type": "object", "properties": {
        "key": {"type": "string"}, "present": {"type": "boolean"},
        "value": {"type": ["string", "null"]}, "evidence": {"type": "string"}},
        "required": ["key", "present", "value", "evidence"], "additionalProperties": False}}},
    "required": ["targets"], "additionalProperties": False}

_BORROW_VISION_INSTR = (
    "The attached page IMAGE(S) are the BORROWINGS note of an Indian Ind AS report ({scope} financials). "
    "It is a dense table split into 'Secured - At Amortised Cost' and 'Unsecured - At Amortised Cost' "
    "blocks, with instrument rows (Non-Convertible Debentures, Bonds, Term Loans - from Banks, Term Loans - "
    "from Others, etc.) and columns 'Non-Current' and 'Current' for the CURRENT year (first) then the prior "
    "year.\n"
    "For each TARGET, return the value from the MATCHING cell BY MEANING + selector — respect the "
    "secured/unsecured block AND the non-current/current column. Read the value straight from the main "
    "borrowings table; IGNORE the maturity-profile / rate-of-interest sub-tables (those list redemption "
    "buckets, not carrying values). If the target's exact combination isn't present, present=false.\n"
    "Numbers exactly as printed; () = negative; value = NUMBER ONLY. evidence = the row label + the cell."
)


def _targets_vision(index: PageIndex, scope: str, section: str, concepts: list[Concept],
                    instr: str, note_pages: list[int], allowed: set[int]) -> list[Datapoint] | None:
    """Vision read of a garbled SPECIFIC-LINE note: map each target to its cell from the
    rendered image (avoids the column/sub-table mis-grabs that defeat flattened text)."""
    from src.engine import vision
    bm25 = [p for p in index.search(SECTIONS[section] + " " + scope, k=12) if not allowed or p in allowed]
    cands = note_pages + [p for p in bm25 if p not in note_pages]
    items = "\n".join(f"- {c.key}: {c.meaning[:400]}" + (f" [{c.selector}]" if c.selector else "")
                      for c in concepts)
    best = None
    for pg in cands[:3]:
        # symmetric window: the note often spans 2 pages, and it can START one page BEFORE the
        # top-ranked candidate (adani cons: unsecured Inter-Corporate Loans 9,726.16 sits on p316
        # while ranking surfaced p317 — the forward-only window missed the whole unsecured block)
        pages = [q for q in (pg - 1, pg, pg + 1) if 1 <= q <= index.n_pages]
        imgs = [vision.render_page(index.path, p, 320) for p in pages]
        o = llm.extract_json(
            instructions=instr.format(scope=scope),
            user_input=f"TARGETS:\n{items}\n\nRead the borrowings table from the attached image(s).",
            schema_name="targets", schema=_TARGETS_SCHEMA, images_b64=imgs,
            max_output_tokens=2000, reasoning="low")
        if o.get("_empty"):
            continue
        tmap = {t["key"]: t for t in o.get("targets", [])}
        out, n = [], 0
        for c in concepts:
            t = tmap.get(c.key)
            if not t or not t.get("present") or t.get("value") in (None, "", "-"):
                out.append(Datapoint(key=c.key, present=False, section=section, pages=pages, confidence="absent"))
            else:
                n += 1
                ev = t.get("evidence", "")
                out.append(Datapoint(key=c.key, present=True, value=_hygiene(t["value"], ev), evidence=ev,
                                     grounded=True, section=section, pages=pages, confidence="grounded"))
        if best is None or n > best[0]:
            best = (n, out)
        if best and best[0] == len(concepts):
            break
    return best[1] if best else None


# INVESTMENTS = class-of-items datapoints (Unquoted Debentures, JV/Partnership), each the SUM of
# member holdings in a dense multi-page note that pdftotext garbles. Read by VISION; CODE sums.
# Restored after discovering the engine's vision-sum was actually RIGHT where GT was wrong (std
# debentures 1,105 — the GT had missed Sintex 900). The sharpened taxonomy `concept` text (exclude
# subsidiaries / debentures / measurement-basis buckets) is fed to the model to bound each class.
VISION_CATEGORY_SECTIONS = {"investments"}

# Still flagged low-confidence: the note's idiosyncratic boundaries mean some cells stay imperfect
# (e.g. std JV historically pulled in debentures). Cap at 'unverified' so a consumer re-checks.
LOW_CONFIDENCE_SECTIONS = {"investments"}

_CATEGORY_SCHEMA = {"type": "object", "properties": {
    "targets": {"type": "array", "items": {"type": "object", "properties": {
        "key": {"type": "string"}, "present": {"type": "boolean"},
        "lines": {"type": "array", "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "value": {"type": ["string", "null"]}},
            "required": ["label", "value"], "additionalProperties": False}}},
        "required": ["key", "present", "lines"], "additionalProperties": False}}},
    "required": ["targets"], "additionalProperties": False}

_CATEGORY_VISION_INSTR = (
    "The attached page IMAGE(S) are the NON-CURRENT INVESTMENTS note of an Indian Ind AS report "
    "({scope} financials) — a dense, multi-page table grouped by MEASUREMENT BASIS (Cost / Amortised "
    "Cost / FVTOCI / FVTPL) and within each by relationship+instrument sub-headings, e.g. "
    "'In Debentures or Bonds - Unquoted', 'In Equity Shares - Quoted/Unquoted', 'Investment in Joint "
    "Ventures'.\n"
    "For each TARGET (a CLASS), list EVERY individual holding LINE belonging to that class with its "
    "CARRYING AMOUNT (rupee-crore 'Amount', CURRENT year / first column). NOT the per-unit face value "
    "('of Rs 100 each'), NOT the Units count, and NEVER a measurement-basis TOTAL line ('Total of "
    "Investment measured at ...'). The code will SUM the lines you return.\n"
    "Class rules (INSTRUMENT-FIRST — follow exactly):\n"
    "  - UNQUOTED DEBENTURES/BONDS: list EVERY line reading 'In Debentures or Bonds - Unquoted', under "
    "ALL measurement bases (Cost/Amortised/FVTOCI/FVTPL) AND all relationships (other companies, "
    "associates, joint ventures). A line like 'In Debentures or Bonds - Unquoted ... 13,828' under "
    "FVTPL IS a debenture — INCLUDE it. EXCLUDE only QUOTED debentures and SUBSIDIARY debentures.\n"
    "  - JOINT VENTURE / PARTNERSHIP: list JV/partnership EQUITY shares (BOTH 'Quoted' AND 'Unquoted' "
    "sub-blocks) and JV/partnership PREFERENCE shares. EXCLUDE JV DEBENTURES (they belong to the "
    "debenture class) and EXCLUDE subsidiaries/associates. Do NOT return the printed 'Total of "
    "Investments in Joint Ventures' if it bundles debentures.\n"
    "If a class has no holdings, present=false. Read amounts exactly as printed; () = negative."
)


# Holdings-line signatures to LOCATE the investments note deterministically (BM25 mis-ranks it:
# reliance std note is p104 but BM25 ranked decoys p107/p109 above it). Find the first in-scope
# page listing actual holdings; the class subtotals we sum sit there and on the following pages.
_INV_HOLDINGS_SIG = (
    "in debentures", "in preference shares", "in equity shares", "in bonds", "in mutual fund",
    "in government securities", "unquoted, fully paid", "debentures or bonds", "fully paid up",
    "in units of", "measured at amortised cost", "measured at fair value through")


def _investments_pages(index: PageIndex, allowed: set[int]) -> list[int]:
    """First in-scope holdings page plus the next two (the note spans ~3 pages; class subtotals
    are scattered across them — e.g. JV debentures on p104 AND p105)."""
    for p in range(1, index.n_pages + 1):
        if allowed and p not in allowed:
            continue
        tl = index.page_text[p - 1].lower()
        if sum(1 for s in _INV_HOLDINGS_SIG if s in tl) >= 2:
            return [q for q in (p, p + 1, p + 2) if q <= index.n_pages]
    return []


# CONSOLIDATED statements carry JV/associate stakes in a separate EQUITY-METHOD note (Ind AS 28
# 'accounted for using the equity method'), NOT inside the fair-value investments note that
# _investments_pages locates — so consolidated JV targets were structurally invisible (hindalco's
# MNH Shakti 7 on p263, itc's ITC Filtrona 146.42 on p272, adani's 6,705.74 on p283 all live
# outside the read window). Framework vocabulary only; investee tables have >=3 numeric rows.
_EQUITY_METHOD_SIG = ("equity method", "equity accounted", "interests in joint ventures",
                      "investment in joint ventures")


def _equity_method_pages(index: PageIndex, allowed: set[int]) -> list[int]:
    """Top pages of the consolidated equity-method (JV/associates) note, best first."""
    hits = []
    for p in range(1, index.n_pages + 1):
        if allowed and p not in allowed:
            continue
        t = index.page_text[p - 1]
        tl = t.lower()
        s = sum(1 for k in _EQUITY_METHOD_SIG if k in tl)
        if s >= 1 and ("joint venture" in tl or "associate" in tl):
            nrows = sum(1 for ln in t.splitlines() if len(re.findall(r"\d[\d,]*\.?\d*", ln)) >= 2)
            if nrows >= 3:
                hits.append((s, nrows, p))
    hits.sort(key=lambda x: (-x[0], -x[1]))
    return [p for *_, p in hits[:2]]


def _category_vision(index: PageIndex, scope: str, section: str,
                     concepts: list[Concept], allowed: set[int]) -> list[Datapoint]:
    """Vision read of the garbled class-listing investments note. Each target is a CLASS; the
    model lists clean member amounts from the IMAGE(S), and CODE sums them deterministically."""
    from src.engine import vision
    pages = _investments_pages(index, allowed)
    if not pages:                            # fallback: BM25 if the signature locator finds nothing
        ex_terms = " ".join(e for c in concepts for e in _examples(c, section))
        pages = [p for p in index.search(SECTIONS[section] + " " + scope + " " + ex_terms, k=20)
                 if not allowed or p in allowed][:3]
    if scope == "consolidated":
        # consolidated JV/associate stakes live in the separate equity-method note, never in the
        # fair-value investments note — append its pages or those targets are structurally absent
        pages = pages + [p for p in _equity_method_pages(index, allowed) if p not in pages]
    if not pages:
        return [Datapoint(key=c.key, present=False, section=section) for c in concepts]
    imgs = [vision.render_page(index.path, p, 320) for p in pages]
    items_desc = "\n".join(
        f"- key: {c.key}\n  class: {c.meaning[:320]}"
        + (f"\n  examples: {'; '.join(_examples(c, section))}" if c.examples else "")
        for c in concepts)
    o = llm.extract_json(
        instructions=_CATEGORY_VISION_INSTR.format(scope=scope),
        user_input=f"TARGETS (each is a CLASS — list ALL its member holdings):\n{items_desc}",
        schema_name="categories", schema=_CATEGORY_SCHEMA, images_b64=imgs,
        max_output_tokens=2500, reasoning="low")
    def _fmt(x):
        whole = abs(x - round(x)) < 0.5
        body = f"{int(round(abs(x))):,}" if whole else f"{abs(x):,.2f}"
        return f"({body})" if x < 0 else body

    tmap = {t["key"]: t for t in o.get("targets", [])} if not o.get("_empty") else {}
    out = []
    for c in concepts:
        t = tmap.get(c.key)
        members = [(l.get("label", ""), _num(_clean_value(l.get("value"))))
                   for l in ((t or {}).get("lines") or [])]
        members = [(lab, v) for lab, v in members if v is not None]
        if not t or not t.get("present") or not members:
            out.append(Datapoint(key=c.key, present=False, section=section, pages=pages, confidence="absent"))
            continue
        # INSTRUMENT-FIRST: value = SUM of the member lines the model listed (debentures = every
        # 'In Debentures or Bonds - Unquoted' line across measurement bases; JV = equity+preference).
        val = _fmt(sum(v for _, v in members))
        evidence = " + ".join(f"{lab[:24].strip()}={v:g}" for lab, v in members)
        out.append(Datapoint(key=c.key, present=True, value=val, evidence=evidence,
                             grounded=True, section=section, pages=pages, confidence="grounded"))
    return out


# ---------------------------------------------------------------------------
# Deterministic DEFERRED-TAX movement-matrix reader — the same "arithmetic identity, silent
# when ambiguous" pattern that took ppe 12 -> 0. Every corpus format prints the DT note as a
# movement matrix whose rows satisfy   opening ± movements = closing   (reliance 37,869-334=
# 37,535; itc 1843.74+97.91-439.69=1501.96; adani 204.80+103.52=308.32; hindalco (5,932)+181=
# (5,751); infosys 296-62=234). A value is returned ONLY from an identity-validated row whose
# label matches the target's framework keywords; when the SAME target matches identity-valid
# rows with different closings (hindalco cons prints per-orientation matrices with identical
# row labels), the reader stays SILENT and the model path decides — miss beats wrong.
# ---------------------------------------------------------------------------

# framework keyword ALTERNATIVES per deferred-tax target (key-substring -> list of token
# sets; a row matches when ALL tokens of one set appear in its label context). Data, not code.
_DT_TARGET_KW: dict[str, list[set]] = {
    "Accumulated Depreciation": [{"property", "plant"}, {"ppe"},
                                 {"depreciation", "intangible"}, {"written", "down", "value"}],
    "Leave Encashment": [{"leave"}, {"compensated"}, {"employee", "benefit"},
                         {"separation", "retirement"}],
}


def _dt_row_identity(nums: list[float], tol_frac: float = 0.001) -> bool:
    """opening ± movements = closing, movements 1..6, signs as printed (enumeration-free:
    DT matrices print signed cells, so a plain sum must close). Tolerance is TIGHT (0.1%):
    the identity is printed-exact in every corpus format, and a loose 1% let a balance-sheet
    line ('PPE [note] 1 2,68,923 2,67,096') pass at large magnitudes."""
    if len(nums) < 3 or len(nums) > 8:
        return False
    tol = max(abs(nums[-1]) * tol_frac, 0.6)
    return abs(sum(nums[:-1]) - nums[-1]) < tol


def _dt_rows(index: PageIndex, scope: str, concepts: list[Concept],
             allowed: set[int]) -> dict[str, "Datapoint"]:
    """{key: Datapoint} for deferred-tax targets proven by the movement identity. Empty when
    nothing validates or matches are ambiguous."""
    targets = [(c, kws) for c in concepts
               for frag, kws in _DT_TARGET_KW.items() if frag in c.key]
    if not targets:
        return {}
    bm25 = [p for p in index.search(SECTIONS["deferred_tax"] + " " + scope, k=10)
            if not allowed or p in allowed]
    # the note often ANNOUNCES the matrix at a page's foot ('the gross movement in the
    # deferred tax account…') with the matrix itself overleaf — scan each hit's successor too
    pages = []
    for p in bm25[:8]:
        for q in (p, p + 1):
            if 1 <= q <= index.n_pages and q not in pages and (not allowed or q in allowed):
                pages.append(q)
    rows: list[tuple[int, str, list[float], str]] = []  # (page, label_context, nums, block)
    for p in pages:
        text = index.column_text[p - 1] if index._reflow_safe(p) else index.page_text[p - 1]
        low_all = text.lower()
        if "deferred tax" not in low_all and "tax asset" not in low_all \
                and "tax liabilit" not in low_all:
            continue
        lines = text.splitlines()
        # ORIENTATION block per line: a row belongs to the block CLOSED by the next
        # 'Total deferred tax liabilities/assets' line below it (itc prints the DTL rows,
        # then that total, then the DTA rows). '' when the format doesn't print such totals.
        blocks = [""] * len(lines)
        nxt = ""
        for i in range(len(lines) - 1, -1, -1):
            li = lines[i].lower()
            if "total deferred tax liabilit" in li:
                nxt = "dtl"
            elif "total deferred tax asset" in li:
                nxt = "dta"
            blocks[i] = nxt
        for i, ln in enumerate(lines):
            s = ln.strip()
            nums = _row_nums(s)
            if not _dt_row_identity(nums):
                continue
            # label context = the row's own text plus up to 2 preceding NUMBERLESS lines
            # (itc wraps 'On fiscal allowances on property, plant and equipment,' onto the
            # line above the numeric row)
            ctx = [s]
            for j in (i - 1, i - 2):
                if j >= 0 and not re.search(r"\d", lines[j]):
                    ctx.append(lines[j].strip())
            label = " ".join(ctx).lower()
            if "total" in label or label.startswith("net "):
                continue
            rows.append((p, label, nums, blocks[i]))
        # mode B — 2-column COMPONENT TABLE (adani prints 'Major Components … / DEFERRED TAX
        # LIABILITIES … Gross Deferred Tax Liabilities'): a block is accepted only when the
        # component rows sum to the Gross/Total row in BOTH printed columns (CY and PY — two
        # independent constraints). Restricted to totals with EXACTLY 2 numeric columns so it
        # can never fire on a movement matrix (whose first column is the OPENING balance).
        for i, ln in enumerate(lines):
            m = re.match(r"\s*(gross|total)\s+deferred\s+tax\s+(asset|liabilit)", ln.strip().lower())
            if not m:
                continue
            tot = _row_nums(ln.strip())
            if len(tot) != 2:
                continue
            blk = "dta" if m.group(2) == "asset" else "dtl"
            comps: list[tuple[str, list[float]]] = []
            for j in range(i - 1, max(0, i - 14), -1):
                s2 = lines[j].strip()
                nums2 = _row_nums(s2)
                low2 = s2.lower()
                if not s2 or not nums2:
                    if re.search(r"deferred\s+tax\s+(asset|liabilit)", low2):
                        break                            # block heading reached
                    continue                             # label-wrap line
                if len(nums2) != 2 or "total" in low2 or "gross" in low2:
                    break
                # wrapped labels: prepend up to 2 preceding numberless lines
                ctx2 = [s2] + [lines[q].strip() for q in (j - 1, j - 2)
                               if q >= 0 and not re.search(r"\d", lines[q])]
                comps.append((" ".join(ctx2).lower(), nums2))
            if len(comps) < 2:
                continue
            ok_cols = all(abs(sum(n[col] for _, n in comps) - tot[col])
                          < max(abs(tot[col]) * 0.001, 0.6) for col in (0, 1))
            if ok_cols:
                for lab2, nums2 in comps:
                    # value = current-year column; store as [v, v] so the year-chaining and
                    # closing logic treat it uniformly
                    rows.append((p, lab2, [nums2[0], nums2[0]], blk))
    out: dict[str, Datapoint] = {}
    for c, kws in targets:
        matches = [(p, lab, nums, blk) for p, lab, nums, blk in rows
                   if any(ts <= {_stem(w) for w in re.findall(r"[a-z][a-z&/\-]+", lab)}
                          for ts in kws)]
        if not matches:
            continue
        # YEAR CHAINING: a prior-year matrix's row closes where the current-year row opens —
        # drop any match whose CLOSING equals another match's OPENING (it is the earlier year)
        current = [m for m in matches
                   if not any(abs(m[2][-1] - o[2][0]) < max(abs(m[2][-1]) * 0.005, 0.02)
                              for o in matches if o is not m)]
        # ORIENTATION: the target key names its block ('..._Deferred tax liabilities_...' is
        # the DTL component) — when the note prints DTL/DTA block totals, keep only that block
        want_blk = "dtl" if "_deferred tax liabilities_" in c.key.lower() else \
                   "dta" if "_deferred tax assets_" in c.key.lower() else ""
        if want_blk and any(blk for *_, blk in current):
            current = [m for m in current if m[3] in (want_blk, "")] or current
        closings = {round(nums[-1], 2) for _, _, nums, _ in current}
        if len(closings) != 1:                          # ambiguous (multi-orientation) -> silent
            continue
        p, lab, nums, _blk = current[0]
        val = nums[-1]
        whole = abs(val - round(val)) < 0.005
        sval = (f"({int(round(abs(val))):,})" if whole else f"({abs(val):,.2f})") if val < 0 \
            else (f"{int(round(val)):,}" if whole else f"{val:,.2f}")
        out[c.key] = Datapoint(key=c.key, present=True, value=sval,
                               evidence=f"(movement-identity validated DT row) {lab[:140]}",
                               grounded=True, section="deferred_tax", pages=[p],
                               confidence="grounded")
    return out


# ---------------------------------------------------------------------------
# RESCUE pass for ABSENT targets — the single largest residual failure shape is "the value IS
# printed in the in-scope text but the section read returned absent": synonym labels the model
# refused ('Freight and Forwarding' for carriage outwards), two-up garble, or the value living
# in a NEIGHBOURING note (P&L face, Other Income, related-party note) that the section's window
# never reads. The rescue is data-driven and company-agnostic: (1) deterministically scan the
# scope's pages for printed lines whose label carries ALL content words of one of the target's
# taxonomy aliases and which look like table rows, then (2) ONE strict re-ask per section
# quoting ONLY those lines. It fills absents exclusively — a present value is never touched —
# and a returned value must be byte-grounded in the quoted lines, so it cannot hallucinate.
# ---------------------------------------------------------------------------

_STOPW = {"of", "and", "on", "for", "the", "to", "in", "from", "with", "a", "an", "net"}


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _alias_tokens(alias: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z][a-z&/\-]+", alias.lower())
            if w not in _STOPW and len(w) > 2}


def _alias_candidates(index: PageIndex, concepts: list[Concept], allowed: set[int],
                      read_pages: set[int]) -> dict[str, list[tuple[int, str]]]:
    """For each concept, printed TABLE-ROW lines matching one of its aliases: every content
    word (stemmed) of the alias on the line, at least one numeric token, and the label
    starting within the first ~10 words (rejects prose that merely mentions the words).
    Scans the REFLOWED text of genuinely two-up pages (where the clean rows live; -layout
    would show welded fragments). ALL matches are collected then RANKED — alias specificity
    (more matched tokens = a more specific label), then pages the section already read, then
    earlier label position — and the top 3 per concept kept: first-found ordering let junk
    from early prose pages fill the cap before the true note page was ever reached."""
    tok_sets = {c.key: [t for t in (_alias_tokens(a) for a in c.examples) if t] for c in concepts}
    big_num = re.compile(r"\d[\d,]*\.\d+|\d{1,3}(?:,\d{2,3})+|\d{4,}")   # value-like, not a note ref

    def _snippet(lines: list[str], i: int) -> str:
        """The matched line, extended downward when it looks like a NOTE HEADING or a wrapped
        label (no value-like number on it): the actual figure sits on the following rows
        ('21. Power and Fuel' -> components -> Total). Extend until a row with a value-like
        number AND a 'total' row have been seen, cap 12 lines / ~700 chars."""
        s = lines[i].strip()
        if big_num.search(s):
            return s
        chunk, seen_val, seen_total = [s], False, False
        for j in range(i + 1, min(i + 13, len(lines))):
            nxt = lines[j].strip()
            if not nxt:
                continue
            chunk.append(nxt)
            seen_val = seen_val or bool(big_num.search(nxt))
            seen_total = seen_total or "total" in nxt.lower()
            if (seen_val and seen_total) or sum(len(x) for x in chunk) > 700:
                break
        return "\n      ".join(chunk)

    found: dict[str, list[tuple[tuple, int, str]]] = {c.key: [] for c in concepts}
    for p in sorted(allowed):
        text = index.column_text[p - 1] if index._reflow_safe(p) else index.page_text[p - 1]
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            s = ln.strip()
            if not (8 < len(s) < 220) or not re.search(r"\d", s):
                continue
            first_num = re.search(r"\d", s)
            label_words = len(s[:first_num.start()].split())
            if label_words > 10:
                continue
            line_toks = {_stem(w) for w in re.findall(r"[a-z][a-z&/\-]+", s.lower())}
            for key, sets in tok_sets.items():
                best = max((len(ts) for ts in sets if ts <= line_toks), default=0)
                if best:
                    rank = (-best, 0 if p in read_pages else 1, label_words)
                    found[key].append((rank, p, _snippet(lines, i)))
    out: dict[str, list[tuple[int, str]]] = {}
    for key, hits in found.items():
        if hits:
            hits.sort(key=lambda h: h[0])
            out[key] = [(p, s) for _, p, s in hits[:3]]
    return out


_RESCUE_INSTR = (
    "You previously marked these items ABSENT in a note of an Ind AS annual report ({scope} "
    "financials). The lines below were found elsewhere in the SAME {scope} financial statements and "
    "carry each item's typical wording. For EACH item, decide whether one quoted line genuinely IS "
    "that item BY MEANING (apply its definition and selector: current-year column, closing balance, "
    "current-vs-non-current, contra sign). If yes, return the value exactly as printed on that line "
    "and quote the line as evidence. If every quoted line is broader, narrower, a different scope "
    "or period, or merely similar — keep present=false. Never use any number that is not in the "
    "quoted lines. () or minus = negative; value = NUMBER ONLY (keep commas/decimals/sign)."
)


def _rescue_absents(index: PageIndex, scope: str, section: str, concepts: list[Concept],
                    results: list[Datapoint], allowed: set[int]) -> list[Datapoint]:
    absent = [c for c in concepts
              if c.examples and not next((d for d in results if d.key == c.key), Datapoint(c.key, False)).present]
    if not absent:
        return results
    read_pages = {p for d in results for p in d.pages}
    cands = _alias_candidates(index, absent, allowed, read_pages)
    if not cands:
        return results
    items = "\n".join(
        f"- key: {c.key}\n  meaning: {c.meaning[:300]}"
        + (f"\n  selector: {c.selector}" if c.selector else "")
        + "\n  candidate lines:\n" + "\n".join(f"    [p{p}] {ln[:700]}" for p, ln in cands[c.key])
        for c in absent if c.key in cands)
    o = llm.extract_json(
        instructions=_RESCUE_INSTR.format(scope=scope),
        user_input=f"SECTION: {SECTION_LABEL[section]}\n\nITEMS with candidate lines:\n{items}",
        schema_name="targets", schema=_TARGETS_SCHEMA, max_output_tokens=1500, reasoning="low")
    if o.get("_empty"):
        return results
    tmap = {t["key"]: t for t in o.get("targets", [])}
    by_key = {d.key: d for d in results}
    for c in absent:
        t, lines = tmap.get(c.key), cands.get(c.key)
        if not (t and lines and t.get("present") and t.get("value") not in (None, "", "-")):
            continue
        val = _hygiene(t["value"], t.get("evidence", ""), sign=c.value_type != "count")
        vd = _digits(val)
        line_digits = _digits(" ".join(ln for _, ln in lines))
        if not (len(vd) >= 2 and vd in line_digits):          # must come from the quoted lines
            continue
        src = next((p for p, ln in lines if vd in _digits(ln)), lines[0][0])
        by_key[c.key] = Datapoint(
            key=c.key, present=True, value=val,
            evidence=f"(rescued from p{src}) {t.get('evidence', '')[:160]}",
            grounded=True, section=section, pages=[src],
            # a line from the pages the section already read is note-context ('grounded');
            # one found elsewhere in the scope is weaker context -> flag for review
            confidence="grounded" if src in read_pages else "unverified")
    return [by_key[c.key] for c in concepts]


def extract_datapoints(index: PageIndex, scope: str = "standalone",
                       concepts: list[Concept] | None = None) -> dict[str, Datapoint]:
    """Extract all target datapoints for one scope.

    Per note: try NOTE-ANCHORED + RECONCILED extraction first (the note's
    components must sum to the tied-out parent statement line — deterministic
    trust). If no candidate reconciles, fall back to best-effort grounded
    extraction so coverage never regresses.
    """
    concepts = concepts or load_concepts()
    by_section: dict[str, list[Concept]] = {}
    for c in concepts:
        by_section.setdefault(c.section, []).append(c)

    tags = page_scopes(index)
    if scope not in tags:
        # the document has NO block for this scope at all — a standalone-only company
        # (Nestle/ABB/SBI Life publish no consolidated FS) queried for 'consolidated'.
        # Its single set of statements IS the answer for either scope: use the whole doc
        # rather than the leftover 'unknown' pages (front-matter prose).
        allowed = set(range(1, index.n_pages + 1))
    else:
        allowed = {i + 1 for i, t in enumerate(tags) if t in (scope, "unknown")}
    bs_lines = _statement_lines(index, scope, "bs", allowed)
    pl_lines = _statement_lines(index, scope, "pl", allowed)

    def _run_core(sec: str, cs: list[Concept]) -> list[Datapoint]:
        if sec in VISION_CATEGORY_SECTIONS:   # garbled class-listing note (investments) -> vision + code-sum
            return _category_vision(index, scope, sec, cs, allowed)
        parent, refs = _parent(sec, bs_lines, pl_lines)
        title_kws = _PARENT.get(sec, (None, []))[1]
        npages = _note_pages(index, refs, title_kws, allowed)
        if sec in _NOTE_LOCATE_SECTIONS:
            # The DETERMINISTIC signature locate ALWAYS runs for these sections — not only when the
            # ref navigation comes up empty. The note refs inherit the stochastic balance-sheet LLM
            # read: a bad draw drops or corrupts the parent's note ref, and the ref can also land a
            # look-alike page (the SOCIE carries the same note number; a '(Contd.)' continuation page
            # matches the heading). The signature pages are computed from the page text alone, and on
            # the 5-company corpus their top-2 contain the note's core table for all 10 scope-combos.
            # Interleave signature-first with the ref pages so the read window always covers BOTH
            # locate mechanisms (window = top-2 pages ±1 in _extract_section).
            sig = _signature_pages(index, sec, allowed)
            merged: list[int] = []
            for pair in zip_longest(sig, npages):
                for p in pair:
                    if p and p not in merged:
                        merged.append(p)
            npages = merged
        if sec in MATRIX_SECTIONS:
            # Read each layout by the method that fits how it survived extraction:
            #   asset-per-COLUMN clean text -> deterministic code read (no model, no variance);
            #   asset-per-ROW              -> deterministic movement-identity row read first,
            #                                 model text read for what it can't prove;
            #   shredded / rotated         -> y-cluster row read, then vision.
            m = _matrix_columns(index, scope, sec, cs, parent, npages, allowed)
            if m is None:
                det = _matrix_rows(index, scope, sec, cs, npages, allowed)
                if len(det) == len(cs):       # every target proven arithmetically — done, no model
                    return [det[c.key] for c in cs]
                m = _matrix_text(index, scope, sec, cs, parent, npages, allowed)
                if m is None:
                    m = _matrix_vision(index, scope, sec, cs, parent, npages, allowed)
                if det:                       # proven values override the model's, never vice versa
                    if m is None:
                        m = [det.get(c.key) or Datapoint(key=c.key, present=False, section=sec,
                                                         confidence="absent") for c in cs]
                    else:
                        m = [det.get(d.key, d) for d in m]
            if m is not None:
                return m
        if sec in VISION_TARGET_SECTIONS:     # garbled specific-line note -> vision cell-read
            v = _targets_vision(index, scope, sec, cs, _BORROW_VISION_INSTR, npages, allowed)
            if v is not None:
                return v
        # Non-additive notes NEVER go through reconciliation — for them the tie-out selects the
        # WRONG page (see NON_ADDITIVE_SECTIONS) and its near-empty result would short-circuit the
        # grounded fallback read below, collapsing the whole note to absent.
        rec = (None if sec in NON_ADDITIVE_SECTIONS
               else _reconciled_section(index, scope, sec, cs, parent, npages, allowed))
        if rec is not None:
            return rec
        # Column-sections read their note with column reflow (share_capital + other_expenses); every
        # other section uses the exact baseline best-effort (BM25 pages, -layout text). Only the
        # note-locate sections pass deterministically-located note_pages; the rest use BM25 pages.
        fb = _extract_section(index, scope, sec, cs, allowed,
                              npages if sec in _NOTE_LOCATE_SECTIONS else None)
        for dp in fb:
            dp.confidence = ("grounded" if dp.present and dp.grounded
                             else "unverified" if dp.present else "absent")
        return fb

    def run(sec: str, cs: list[Concept]) -> list[Datapoint]:
        res = _run_core(sec, cs)
        # rescue pass for TEXT-read sections (matrix/vision sections have their own backstops
        # and different absent semantics): one cheap re-ask over deterministically-found
        # alias-matching lines, fills absents only
        if sec not in MATRIX_SECTIONS | VISION_TARGET_SECTIONS | VISION_CATEGORY_SECTIONS:
            res = _rescue_absents(index, scope, sec, cs, res, allowed)
        # deferred-tax deterministic reader: arithmetic-proven values override everything
        # (movement identity / column-sum validated; silent when ambiguous — corpus-validated
        # 16 correct / 0 wrong; see _dt_rows)
        if sec == "deferred_tax":
            det = _dt_rows(index, scope, cs, allowed)
            if det:
                res = [det.get(d.key, d) for d in res]
        # (Removed the name-based 'residual guard': it keyed off the KEY name '...Other Long Term'
        # and demoted correct answers, fighting the DEFINITION. e.g. 'Other Long Term Liabilities'
        # is defined as the BS TOTAL line — the engine's total is correct, so a name heuristic that
        # refuses it is wrong. Definition is authoritative, not the key's wording.)
        # LOW-CONFIDENCE FLAG: sections proven intractable for ANY model (investments class
        # boundaries — both mini and gpt-5.4 over-collect). Never let these surface as trusted
        # ('grounded'/'reconciled'); cap at 'unverified' so a consumer always re-checks them.
        for d in res:
            if d.present and d.section in LOW_CONFIDENCE_SECTIONS and d.confidence in ("grounded", "reconciled"):
                d.confidence = "unverified"
        return res

    results: dict[str, Datapoint] = {}
    workers = int(os.getenv("DP_MAX_WORKERS", "6"))   # set DP_MAX_WORKERS=1 to debug serially
    if workers <= 1:
        for sec, cs in by_section.items():
            for d in run(sec, cs):
                results[d.key] = d
        return results
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run, sec, cs): sec for sec, cs in by_section.items()}
        for f in futs:
            for d in f.result():
                results[d.key] = d
    return results
