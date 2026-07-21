"""Ask questions about an uploaded filing — the frp chat pattern, verbatim.

Single file upload (deleted in `finally`, responses store=False — nothing
persists at OpenAI), then free-form questions answered in markdown:

    from src.engine.filing_chat import ask_filing
    print(ask_filing("q1.pdf", "Provide the full consolidated financial results, detailed."))
"""
from __future__ import annotations

import re

# frp's proven file-upload analysis prompt (core/utils/prompts.py), unchanged
# in every rule that matters: parentheses-negative, exact numbers, no guessing.
SYSTEM_PROMPT = """You are a senior financial analyst specializing in Indian equity markets. Answer questions about the uploaded document.

**RULES:**
- MANDATORY: In Indian financial statements, numbers in parentheses are NEGATIVE — this applies to ALL line items including Revenue, Expenses, Profit, and EPS. Example: Revenue (826.57) means Revenue is -₹826.57 lakhs. Never strip parentheses.
- ONLY use information from the document — NEVER guess or fabricate data
- Extract numbers EXACTLY as written (preserve decimals, units like ₹, Cr, Lakhs)
- Use metric names EXACTLY as they appear in the document
- Prefer consolidated figures. Use standalone only if consolidated is not available.
- No page numbers or source citations in the response.
- If the user gives a short reply (e.g., "sure", "yes") to your previous suggestion, follow through on it.

**RESPONSE STYLE:**
- Answer the question directly. Use markdown tables for data.
- Be crisp. No filler, no disclaimers, no preambles."""


def ask_filing(pdf_path: str, question: str, model: str | None = None,
               max_output_tokens: int = 4000) -> str:
    """Upload the filing, ask ONE question, return the markdown answer.
    The uploaded file is deleted before returning — even on error."""
    from src.llm import ask_text, ephemeral_file

    with open(pdf_path, "rb") as fh:
        content = fh.read()
    with ephemeral_file(content, "filing.pdf") as fid:
        return ask_text(instructions=SYSTEM_PROMPT, question=question,
                        file_ids=[fid], model=model,
                        max_output_tokens=max_output_tokens, temperature=0.1)


# --------------------------------------------------------------------------- internal statement questions

_DETAIL = ("for ALL periods shown, detailed, row by row, exactly as printed "
           "(exact line-item names, exact values, keep parentheses). "
           "CRITICAL: some filings present the SAME statement twice — once under Ind AS and "
           "once under IFRS — with different figures. These are DIFFERENT statements: transcribe "
           "each as its own SEPARATE table with a heading naming its GAAP (e.g. "
           "'Consolidated Financial Results — Ind AS' / '— IFRS'). NEVER mix rows from the two; "
           "every table must reproduce ONE printed statement from ONE page range only. "
           "Return ONLY markdown table(s), each preceded by a bold heading line. "
           "If the document does not contain it, reply exactly: NOT PRESENT")

_QUESTIONS = [
    ("standalone", "Standalone Financial Results (P&L incl. OCI & EPS)",
     f"Provide the full STANDALONE financial results table — statement of profit and loss "
     f"including other comprehensive income and EPS — {_DETAIL}"),
    ("consolidated", "Consolidated Financial Results (P&L incl. OCI & EPS)",
     f"Provide the full CONSOLIDATED financial results table — statement of profit and loss "
     f"including other comprehensive income and EPS — {_DETAIL}"),
    ("unknown", "Segment Information",
     f"Provide the full segment information table(s) — segment revenue, segment results, "
     f"segment assets/liabilities if shown — standalone and consolidated separately if both exist, {_DETAIL}"),
    ("unknown", "Statement of Assets and Liabilities (Balance Sheet)",
     f"Provide the full statement of assets and liabilities (balance sheet) — standalone and "
     f"consolidated separately if both exist — {_DETAIL}"),
    ("unknown", "Statement of Cash Flows",
     f"Provide the full statement of cash flows — standalone and consolidated separately "
     f"if both exist — {_DETAIL}"),
]


def _num(cell: str):
    """Parse a printed Indian-format value; parentheses are negative."""
    s = str(cell).strip().strip("*^")
    if not s or s in "-–—":
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("₹", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _find_row(grid, must, must_not=()):
    for row in grid:
        label = " ".join(c for c in row if c and _num(c) is None).lower()
        if all(m in label for m in must) and not any(m in label for m in must_not):
            vals = [_num(c) for c in row]
            if any(v is not None for v in vals):
                return [v for v in vals if v is not None]
    return None


def _ties(a, b, out):
    """a - b == out per column, within rounding tolerance."""
    n = min(len(a), len(b), len(out))
    if n == 0:
        return False
    for i in range(n):
        if abs((a[i] - b[i]) - out[i]) > max(2.0, 0.005 * abs(out[i])):
            return False
    return True


def tie_out_results(grid) -> tuple[bool, str]:
    """A results statement must satisfy its own printed identities:
    PBT - tax = profit;  profit + OCI = total comprehensive income.
    A sheet blending two GAAP versions CANNOT pass this."""
    pbt = _find_row(grid, ["profit before tax"])
    tax = (_find_row(grid, ["total tax"]) or
           _find_row(grid, ["tax expense"], must_not=["current", "deferred"]))
    profit = (_find_row(grid, ["profit for the period"], must_not=["attributable"]) or
              _find_row(grid, ["profit for the year"], must_not=["attributable"]))
    oci = _find_row(grid, ["total other comprehensive"])
    tci = _find_row(grid, ["total comprehensive income"], must_not=["attributable"])
    ti = _find_row(grid, ["total income"])
    te = _find_row(grid, ["total expenses"])
    fin_exp = _find_row(grid, ["finance expenses"])
    fin_inc = _find_row(grid, ["finance and other income"])
    share = _find_row(grid, ["share of net profit"])
    # statements with exceptional items tie TI-TE to the PRE-exceptional profit
    pbt_pre = _find_row(grid, ["profit before exceptional"])
    checks = []
    if ti and te and (pbt or pbt_pre):
        combos = []
        for target in (t for t in (pbt, pbt_pre) if t):
            n = min(len(ti), len(te), len(target))
            for use_fin in (False, True):
                for use_share in (False, True):
                    ok = True
                    for i in range(n):
                        v = ti[i] - te[i]
                        if use_fin and fin_exp and fin_inc and i < min(len(fin_exp), len(fin_inc)):
                            v += fin_inc[i] - fin_exp[i]
                        if use_share and share and i < len(share):
                            v += share[i]
                        if abs(v - target[i]) > max(2.0, 0.005 * abs(target[i])):
                            ok = False
                            break
                    combos.append(ok)
        checks.append(("income − expenses ties to PBT", any(combos)))
    if pbt and tax and profit:
        checks.append(("PBT − tax = profit", _ties(pbt, tax, profit)))
    if profit and oci and tci:
        checks.append(("profit + OCI = TCI",
                       _ties(tci, oci, profit) or _ties(tci, profit, oci)))
    # OCI COMPONENTS must sum to the Total OCI row (catches a single miscopied
    # cell that the subtotal identities cannot see)
    prof_i = next((i for i, r in enumerate(grid)
                   if "profit for the" in " ".join(str(c) for c in r).lower()
                   and "attributable" not in " ".join(str(c) for c in r).lower()
                   and any(_num(c) is not None for c in r)), None)
    toci_i = next((i for i, r in enumerate(grid)
                   if "total other comprehensive" in " ".join(str(c) for c in r).lower()
                   and any(_num(c) is not None for c in r)), None)
    if prof_i is not None and toci_i is not None and toci_i - prof_i >= 3:
        total_row = grid[toci_i]
        cols = [j for j, c in enumerate(total_row) if _num(c) is not None]
        comp_rows = [r for r in grid[prof_i + 1:toci_i]
                     if any(_num(c) is not None for c in r)]
        if len(comp_rows) >= 2 and cols:
            ok = True
            for j in cols:
                s = sum((_num(r[j]) or 0) for r in comp_rows if j < len(r))
                tv = _num(total_row[j])
                if tv is None or abs(s - tv) > max(2.0, 0.005 * abs(tv)):
                    ok = False
                    break
            checks.append(("OCI components sum to Total OCI", ok))
    if not checks:
        return True, "no verifiable identity rows found"
    failed = [name for name, ok in checks if not ok]
    if failed:
        return False, "; ".join(failed) + " FAILS"
    return True, f"{len(checks)} identities tie"


def _md_tables(answer: str) -> list[tuple[str, list[list[str]]]]:
    """Parse markdown into (heading, grid) tables. Separator rows dropped."""
    tables: list[tuple[str, list[list[str]]]] = []
    heading = ""
    grid: list[list[str]] = []
    for line in answer.splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip().strip("*").strip() for c in s.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):     # |---|---| separator
                continue
            grid.append(cells)
            continue
        if grid:
            tables.append((heading, grid))
            grid = []
        if s and not s.startswith("|"):
            heading = s.lstrip("#* ").rstrip("*: ")
    if grid:
        tables.append((heading, grid))
    return [(h, g) for h, g in tables if len(g) >= 2 and len(g[0]) >= 2]


_NUM_TOK = re.compile(r"\d[\d,.]*\d|\d")


def _digits_only(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _is_2dp(form) -> bool:
    """A canonically-printed amount: dot decimal + exactly 2 fractional digits
    (thousands separators removed). '48.20' / '1,323.33' -> True; '4,820' /
    '296,99' / '29699' -> False."""
    s = str(form).strip().strip("()").replace(",", "").replace(" ", "")
    return bool(re.fullmatch(r"\d+\.\d{2}", s))


def _page_number_forms(pdf_path: str) -> list:
    """Per page: {digit-sequence -> set of distinct printed numeric forms} from
    the text layer. The authority for reconciling LLM-vision misreads."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    forms = []
    for pg in doc:
        d: dict = {}
        for tok in _NUM_TOK.findall(pg.get_text()):
            dig = _digits_only(tok)
            if dig:
                d.setdefault(dig, set()).add(tok)
        forms.append(d)
    doc.close()
    return forms


def _best_page(grid, forms) -> int:
    """Index of the text page whose tokens best cover this grid's numbers."""
    gd = [_digits_only(c) for row in grid for c in row if re.search(r"\d", str(c))]
    gd = [d for d in gd if len(d) >= 3]
    best, best_hits = -1, 0
    for i, d in enumerate(forms):
        hits = sum(1 for x in gd if x in d)
        if hits > best_hits:
            best, best_hits = i, hits
    return best if best_hits >= 3 else -1


def _reconcile_grid(grid, page_forms):
    """Fix LLM decimal/grouping misreads against the page's text layer. For each
    numeric cell, among {its own form} ∪ {text tokens sharing its digits}, if
    exactly one form is canonically 2-decimal, adopt it. This corrects
    '4,820'->'48.20' on clean pages AND keeps a correct model read ('296.99')
    when the text layer itself is the corrupted one ('296,99'). Never touches
    year-like integers, non-numeric cells, or ambiguous ones."""
    out = []
    for row in grid:
        newrow = []
        for c in row:
            s = str(c or "").strip()
            core = s.strip("()").strip()
            bare = core.replace(",", "").replace(" ", "")
            if (not re.search(r"\d", core) or not re.fullmatch(r"[\d,. ]+", core)
                    or re.fullmatch(r"(19|20)\d{2}", bare)):     # letters / year -> leave
                newrow.append(c); continue
            D = _digits_only(core)
            if len(D) < 3:
                newrow.append(c); continue
            cands = set(page_forms.get(D, set())); cands.add(core)
            valid = {v.strip().strip("()").strip() for v in cands if _is_2dp(v)}
            if len(valid) == 1:
                chosen = next(iter(valid))
                if _digits_only(chosen) == D and chosen != core:
                    neg = s.startswith("(") and s.endswith(")")
                    newrow.append(f"({chosen})" if neg else chosen); continue
            newrow.append(c)
        out.append(newrow)
    return out


def _cell_value(form):
    """(value, is_negative) for a numeric cell, else (None, False)."""
    s = str(form or "").strip()
    if not re.fullmatch(r"[()\d,.\s₹-]+", s) or not re.search(r"\d", s):
        return None, False
    neg = s.startswith("(") and s.endswith(")")
    s2 = s.strip("()").replace(",", "").replace(" ", "").replace("₹", "")
    try:
        v = float(s2)
    except ValueError:
        return None, False
    return (-v if neg else v), neg


def _is_bare_int(form):
    """A dropped-decimal candidate: 3+ digit integer with NO decimal point
    (thousands separators allowed) — e.g. '4,820' or '406'."""
    s = str(form).strip().strip("()").replace(",", "").replace(" ", "").replace("₹", "")
    return bool(re.fullmatch(r"\d{3,}", s))


def _repair_dropped_decimals(grid):
    """Recover decimals that are ABSENT from the source text (e.g. '4820' for
    48.20 — no comma/dot to signal it). Uses cross-period magnitude: Indian
    statement amounts are 2 dp, and one line item's value never varies ~100x
    across quarter/half-year/full-year columns (FY is ~4x a quarter at most). So
    a bare integer that is ≥20x the row's decimal-bearing peers, and whose ÷100
    lands back in their range, is a dropped decimal → correct it. Requires ≥2
    clean 2-dp peers in the row, so headers/year rows and all-integer rows are
    never touched."""
    import statistics
    out = [list(r) for r in grid]
    for ri, row in enumerate(out):
        cells = []
        for ci, c in enumerate(row):
            v, neg = _cell_value(c)
            if v is not None:
                cells.append((ci, v, str(c).strip(), neg))
        peers = [abs(v) for _ci, v, form, _neg in cells if _is_2dp(form) and abs(v) > 0]
        if len(peers) < 2:
            continue
        med = statistics.median(peers)
        if med <= 0:
            continue
        for ci, v, form, neg in cells:
            if _is_bare_int(form) and abs(v) >= 20 * med:
                corr = v / 100.0
                if med / 5 <= abs(corr) <= med * 5:
                    out[ri][ci] = f"({abs(corr):.2f})" if neg else f"{corr:.2f}"
    return out


def quarterly_statement_tables(pdf_path: str, model: str | None = None,
                               log=print) -> list:
    """Upload the filing ONCE; internally ask for each detailed statement
    (standalone results, consolidated results, segments, balance sheet, cash
    flow) in parallel; return the answers as RawTable sheets."""
    from concurrent.futures import ThreadPoolExecutor
    from src.engine.tables import RawTable
    from src.llm import ask_text, ephemeral_file

    with open(pdf_path, "rb") as fh:
        content = fh.read()
    out = []
    with ephemeral_file(content, "filing.pdf") as fid:
        def one(args):
            scope, label, q = args
            answer = ask_text(instructions=SYSTEM_PROMPT, question=q,
                              file_ids=[fid], model=model,
                              max_output_tokens=6000, temperature=0.1)
            # arithmetic tie-out on results statements: a blended/garbled
            # statement cannot satisfy its own printed identities
            if "Financial Results" in label:
                bad = [h for h, g in _md_tables(answer) if not tie_out_results(g)[0]]
                if bad:
                    log(f"  {label}: tie-out FAILED ({len(bad)} table(s)) -> re-ask")
                    answer = ask_text(
                        instructions=SYSTEM_PROMPT,
                        question=(q + "\n\nIMPORTANT: your previous attempt mixed rows from "
                                  "two different printed statements (e.g. Ind AS and IFRS "
                                  "versions), so the totals did not add up. Transcribe each "
                                  "printed statement separately and completely, copying every "
                                  "row from ONE statement only."),
                        file_ids=[fid], model=model,
                        max_output_tokens=6000, temperature=0.1)
            return scope, label, answer
        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(one, _QUESTIONS))
    # Text layer = the authority for reconciling decimal/grouping misreads the
    # LLM made while visually transcribing (e.g. '48.20' read as '4,820').
    page_forms = _page_number_forms(pdf_path)
    for i, (scope, label, answer) in enumerate(results, 1):
        if "NOT PRESENT" in answer[:400] and answer.count("|") < 10:
            log(f"  {label}: not present in filing")
            continue
        parsed = _md_tables(answer)
        if not parsed:
            log(f"  {label}: no table parsed")
            continue
        for k, (heading, grid) in enumerate(parsed, 1):
            width = max(len(r) for r in grid)
            grid = [r + [""] * (width - len(r)) for r in grid]
            _pg = _best_page(grid, page_forms)
            if _pg >= 0:
                grid = _reconcile_grid(grid, page_forms[_pg])
            # recover decimals that are absent from the source text entirely
            grid = _repair_dropped_decimals(grid)
            sc = scope
            head = (heading or label).lower()
            if "consolidated" in head:
                sc = "consolidated"
            elif "standalone" in head:
                sc = "standalone"
            title = heading or label
            verified = ""
            if "Financial Results" in label:
                ok, detail = tie_out_results(grid)
                verified = f"tie-out: {detail}"
                if not ok:
                    title += "  ⚠ arithmetic does not tie — review"
            section = label
            up = head.upper()
            if "IFRS" in up:
                section += " — IFRS"
            elif "IND AS" in up or "IND-AS" in up:
                section += " — Ind AS"
            out.append(RawTable(
                page=i, n=k, title=title, scope=sc,
                section=section,
                page_head=("(quarterly filing — LLM statement extraction"
                           + (f"; {verified}" if verified else "") + ")"),
                units="", grid=grid))
        log(f"  {label}: {len(parsed)} table(s)")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        sys.exit('usage: python -m src.engine.filing_chat report.pdf "your question"\n'
                 '       python -m src.engine.filing_chat report.pdf --statements out.xlsx')
    if sys.argv[2] == "--statements":
        from src.engine.tables import write_workbook
        tables = quarterly_statement_tables(sys.argv[1])
        out = sys.argv[3] if len(sys.argv) > 3 else sys.argv[1].rsplit(".", 1)[0] + "_statements.xlsx"
        write_workbook(tables, out)
        print(f"{len(tables)} statement tables -> {out}")
    else:
        print(ask_filing(sys.argv[1], sys.argv[2]))
