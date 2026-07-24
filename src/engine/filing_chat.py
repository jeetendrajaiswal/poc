"""Ask questions about an uploaded filing — the frp chat pattern, verbatim.

Single file upload (deleted in `finally`, responses store=False — nothing
persists at OpenAI), then free-form questions answered in markdown:

    from src.engine.filing_chat import ask_filing
    print(ask_filing("q1.pdf", "Provide the full consolidated financial results, detailed."))

Statement extraction (`quarterly_statement_tables`) treats every transcribed
grid as a HYPOTHESIS: it is positionally reconciled against the PDF text layer
(src/engine/source_align.py — the authority for both value forms and column
placement), then verified against the statement's own printed identities
(src/engine/identities.py). A statement that still fails gets ONE informed
re-ask; whatever fails after that carries a visible ⚠ downstream — flagged,
never silently wrong.
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
                        max_output_tokens=max_output_tokens, temperature=0)


# --------------------------------------------------------------------------- internal statement questions

# Statement extraction is TRANSCRIPTION, not chat — it gets its own system
# prompt. The Q&A prompt above must NOT be reused here: its "prefer
# consolidated figures" rule actively fights the STANDALONE question, and its
# conversational rules are noise. The rules below directly target the observed
# failure modes: invented period columns (a model once fabricated a Dec-quarter
# column that is printed NOWHERE in the filing), blended twin printings,
# renamed rows, and missing denomination/period banners.
EXTRACT_PROMPT = """You transcribe financial statements from Indian company filings EXACTLY as printed.

**RULES:**
- MANDATORY: numbers in parentheses are NEGATIVE — for ALL line items including Revenue, Expenses, Profit and EPS. Never strip parentheses.
- Copy every value EXACTLY as printed: same digits, same grouping (e.g. 1,45,575.77), same decimals. Never round, compute, convert or "correct" a number.
- Use line-item names exactly as printed. Do not rename, summarise, reorder or omit rows.
- NEVER invent data. Transcribe ONLY the period columns actually printed in the ONE table you are reading. If a period is not printed there, it must not appear in your answer — do not fill it in from another page, another table, or memory.
- Follow the reporting-basis constraint in the question. Do not return an alternative accounting-framework, foreign-reporting, or foreign-currency duplicate.
- Each table you return must reproduce ONE printed statement from ONE page range. Filings often print the same statement twice (earnings release + audited statements): pick the requested statutory printing and never blend them.
- Start each table with the heading and any denomination line printed above it (e.g. '(₹ in crore)', 'Rs. in lakhs'), then the FULL column-header rows including any period banner (e.g. 'For the quarter ended June 30, 2025').
- Answer with markdown tables only — each preceded by a bold heading line, no commentary."""

_DETAIL = ("for ALL periods shown, detailed, row by row, exactly as printed "
           "(exact line-item names, exact values, keep parentheses). "
           "Include the denomination line and the table's column-header rows exactly as "
           "printed, INCLUDING any period banner line above the columns "
           "(e.g. 'For the quarter ended June 30, 2025'). "
           "Every table must reproduce ONE printed statement from ONE page range only. "
           "When the same statement is printed once as the main quarterly results table and "
           "again later as a condensed/annual statement with fewer period columns, transcribe "
           "the MAIN quarterly results table with the widest printed set of period columns. "
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
    ("standalone", "Standalone Statement of Assets and Liabilities (Balance Sheet)",
     f"Provide the full STANDALONE statement of assets and liabilities (balance sheet) — "
     f"{_DETAIL}"),
    ("consolidated", "Consolidated Statement of Assets and Liabilities (Balance Sheet)",
     f"Provide the full CONSOLIDATED statement of assets and liabilities (balance sheet) — "
     f"{_DETAIL}"),
    ("unknown", "Statement of Cash Flows",
     f"Provide the full statement of cash flows — standalone and consolidated separately "
     f"if both exist — {_DETAIL}"),
]


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


def _normalize_grid_preamble(
        heading: str, grid: list[list[str]]) -> tuple[str, list[list[str]]]:
    """Canonicalize model variations in a printed statement's header rows."""
    grid = [list(row) for row in grid]
    if len(grid) >= 2:
        first = [str(cell).strip() for cell in grid[0]]
        second_first = str(grid[1][0]).strip().casefold()
        if (first[0] and all(not cell for cell in first[1:])
                and second_first == "particulars"):
            heading = f"{heading} — {first[0]}" if heading else first[0]
            grid = grid[1:]
    if grid:
        first_cell = str(grid[0][0]).strip()
        period_header = any(
            re.search(
                r"\b(?:quarter|year|months?|as\s+at|ended)\b",
                str(cell), re.IGNORECASE)
            for cell in grid[0][1:]
        )
        denomination = bool(re.search(
            r"(?:₹|\brs\.?\b|\binr\b|\bcrore\b|\blakhs?\b|"
            r"\bmillions?\b|\bbillions?\b)",
            first_cell, re.IGNORECASE))
        if (period_header and denomination
                and first_cell.casefold() != "particulars"):
            heading = f"{heading} — {first_cell}" if heading else first_cell
            grid[0][0] = "Particulars"
    # Shared banner plus a separate year row -> one canonical header row.
    if (len(grid) >= 2
            and str(grid[0][0]).strip().casefold() == "particulars"
            and str(grid[1][0]).strip().casefold() == "particulars"):
        years = [
            str(cell).strip()
            for cell in grid[1][1:]
            if str(cell).strip()
        ]
        banner = next(
            (str(cell).strip() for cell in grid[0][1:]
             if re.search(r"\b(?:ended|as\s+at)\b", str(cell),
                          re.IGNORECASE)),
            "",
        )
        if (banner and len(years) >= 2
                and all(re.fullmatch(r"(?:19|20)\d{2}", year)
                        for year in years)):
            grid[0] = ["Particulars"] + [
                f"{banner.rstrip(' ,')} {year}" for year in years
            ]
            grid.pop(1)
    # A model can preserve the first complete period heading but shorten a
    # later heading to its year.  Complete it from the nearest preceding
    # heading of the same column group.  This is structural normalization:
    # the printed year is preserved and no reporting date is invented.
    if grid:
        completed = [grid[0][0]]
        previous_heading = ""
        for cell in grid[0][1:]:
            text = str(cell).strip()
            if re.fullmatch(r"(?:19|20)\d{2}", text) and previous_heading:
                prior = re.fullmatch(
                    r"(.+?)(?:19|20)\d{2}", previous_heading,
                    re.IGNORECASE)
                text = f"{prior.group(1)}{text}" if prior else text
            if (re.search(r"\b(?:ended|as\s+at)\b", text, re.IGNORECASE)
                    and re.search(r"(?:19|20)\d{2}\s*$", text)):
                previous_heading = text
            completed.append(text)
        grid[0] = completed
    while grid and len(grid[0]) > 1 and all(
            not str(row[-1]).strip() for row in grid):
        for row in grid:
            row.pop()
    return heading, grid


def _widest_numeric_row(tables) -> int:
    """Maximum numeric-cell count carried by any row in one attempt.

    This measures value-column capacity while ignoring label and note
    columns. A verification retry may correct values, but must not win merely
    by switching to a shorter twin printing and deleting a reporting period.
    """
    return max(
        (
            sum(bool(re.search(r"\d", str(cell or ""))) for cell in row)
            for _heading, grid, *_rest in tables
            for row in grid
        ),
        default=0,
    )


_NUM_TOK = re.compile(r"\d[\d,.]*\d|\d")


def _digits_only(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _page_number_forms(pdf_path: str) -> list:
    """Per page: {digit-sequence -> set of distinct printed numeric forms} from
    the text layer. Used to locate each statement's source page(s)."""
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


_CONSOLIDATED_FINANCIAL_HEADING = re.compile(
    r"\b(?:statement\s+of\s+)?(?:audited\s+|unaudited\s+)?consolidated\s+"
    r"financial\s+(?:results|statements)\b",
    re.IGNORECASE,
)
_SINGLE_ENTITY_DISCLOSURE = re.compile(
    r"\b(?:does\s+not\s+have\s+any|has\s+no|no)\s+"
    r"subsidiar(?:y|ies)\b.{0,180}\bassociate(?:s)?\b.{0,180}"
    r"\bjoint\s+venture(?:s)?\b",
    re.IGNORECASE,
)


def document_scope_from_text(text: str) -> str:
    """Resolve filing-wide scope only from decisive document evidence.

    Some single-entity result packages omit the word "standalone" from every
    statement heading. A disclosure that the company has no subsidiary,
    associate, or joint venture proves those statements are standalone.
    Explicit consolidated-financial-statement headings take precedence.

    Generic uses of "consolidation" (for example a share split/consolidation)
    are intentionally ignored.
    """
    normalized = " ".join(str(text or "").split())
    if _CONSOLIDATED_FINANCIAL_HEADING.search(normalized):
        return "unknown"
    if _SINGLE_ENTITY_DISCLOSURE.search(normalized):
        return "standalone"
    return "unknown"


def filing_document_scope(pdf_path: str) -> str:
    """Read the local PDF and return a proven filing-wide scope, if any."""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return document_scope_from_text(text)


def _questions_for_document_scope(
        document_scope: str,
        extraction_policy: dict | None = None,
) -> list[tuple[str, str, str]]:
    """Build extraction questions consistent with the proven filing scope."""
    excluded = ", ".join(
        str(rule.get("description", "")).strip()
        for rule in (extraction_policy or {}).get(
            "statement_exclusions", ())
        if str(rule.get("description", "")).strip()
    )
    basis_constraint = (
        "\n\nEXCLUDE these alternative statement variants: " + excluded
        + ". Do not transcribe or use them as a source."
        if excluded else ""
    )
    if document_scope != "standalone":
        return [
            (scope, label, question + basis_constraint)
            for scope, label, question in _QUESTIONS
        ]
    questions = []
    constraint = (
        "\n\nThe filing explicitly states that the company has no subsidiary, "
        "associate, or joint venture. Return the STANDALONE statement only; "
        "do not invent or return a consolidated version."
    )
    for requested_scope, label, question in _QUESTIONS:
        if requested_scope == "consolidated":
            continue
        if requested_scope == "unknown":
            question += constraint
        questions.append(
            (requested_scope, label, question + basis_constraint))
    return questions


def recover_missing_statements(
    pdf_path: str,
    missing: list[str],
    *,
    document_scope: str,
    page_forms: list,
    page_lines: list,
    scans: set,
    excluded_pages: set[int] | None = None,
    log=print,
) -> list:
    """Recover model-omitted statements from the local digital text layer.

    `unextracted_statements` proves that a number-heavy statement page exists.
    If the model nevertheless returns no parseable table, leaving only a
    warning would still allow silent data loss. This fallback uses the existing
    deterministic table extractor, then subjects every recovered grid to the
    same source-position reconciliation and identity suite as model output.

    It is intentionally limited to statement kinds already proven present by
    the completeness detector; it never searches for or invents extra tables.
    """
    if not missing:
        return []

    from src.engine import identities, source_align
    from src.engine.tables import RawTable, extract_tables

    wanted = set(missing)
    excluded_pages = excluded_pages or set()
    candidates = []
    for table in extract_tables(pdf_path, financial_only=False):
        if table.page in excluded_pages:
            continue
        text = f"{table.section} {table.title}".lower()
        kinds = [
            kind for kind, pattern in _STMT_HEADINGS.items()
            if kind in wanted and re.search(pattern, text)
        ]
        if not kinds or len(table.grid) < 3:
            continue
        # Prefer the fullest local table if the same statement is printed more
        # than once. Separate standalone/consolidated versions remain separate
        # unless a filing-wide scope proves only one can exist.
        scope = document_scope if document_scope in (
            "standalone", "consolidated"
        ) else table.scope
        candidates.append((kinds[0], scope, len(table.grid), table))

    best = {}
    for kind, scope, size, table in candidates:
        key = (kind, scope)
        if key not in best or size > best[key][0]:
            best[key] = (size, table)

    recovered = []
    for (kind, scope), (_size, table) in best.items():
        grid, report = source_align.reconcile_with_source(
            table.grid, table.section, table.title,
            page_forms, page_lines, scan_pages=scans,
            excluded_pages=excluded_pages,
        )
        grid = source_align.repair_dropped_decimals(grid)
        failures = identities.failing(table.section, table.title, grid)
        title = table.title or kind
        notes = ["deterministic completeness recovery"]
        if report:
            notes.append(
                "source-reconciled p" + "/".join(map(str, report["pages"]))
            )
        if failures:
            title += "  ⚠ verification failed — review"
            notes.append("FAILED: " + "; ".join(failures))
        else:
            checks = identities.run_checks(table.section, table.title, grid)
            if checks:
                notes.append(
                    f"{sum(ok for _name, ok in checks)}/{len(checks)} "
                    "identities tie"
                )
        recovered.append(RawTable(
            page=table.page,
            n=table.n,
            title=title,
            scope=scope,
            section=table.section or kind,
            page_head="(" + "; ".join(notes) + ")",
            units=table.units,
            grid=grid,
        ))
        log(
            f"  COMPLETENESS RECOVERY: {kind} -> local table p{table.page}, "
            f"{len(grid)} rows, scope={scope}; {notes[-1]}"
        )
    return recovered


# --------------------------------------------------------------------------- statement extraction

def quarterly_statement_tables(
        pdf_path: str,
        model: str | None = None,
        log=print,
        extraction_policy: dict | None = None,
) -> list:
    """Upload the filing ONCE; internally ask for each detailed statement
    (standalone results, consolidated results, segments, balance sheet, cash
    flow) in parallel; return the answers as RawTable sheets.

    Every parsed grid is (1) positionally reconciled against the PDF text
    layer, (2) checked against its printed identities, (3) re-asked ONCE with
    the failure spelled out if checks fail, and (4) ⚠-flagged in its title if
    it still fails — the flag travels with the table into mapping and the
    deliverable."""
    from concurrent.futures import ThreadPoolExecutor
    from src.engine import identities, source_align
    from src.engine.tables import RawTable
    from src.llm import ask_text, ephemeral_file

    page_forms = _page_number_forms(pdf_path)
    page_lines = source_align.page_word_lines(pdf_path)
    scans = source_align.untrusted_text_pages(pdf_path)
    import pymupdf
    _scope_doc = pymupdf.open(pdf_path)
    page_texts = [" ".join(page.get_text().split()) for page in _scope_doc]
    page_heads = [text[:1200].lower() for text in page_texts]
    _scope_doc.close()
    excluded_pages = {
        page_number
        for page_number, text in enumerate(page_texts, 1)
        if source_align.is_excluded_statement_text(
            text, extraction_policy)
    }
    if excluded_pages:
        log(
            "  BASIS: excluded "
            f"{len(excluded_pages)} alternative-framework page(s) using "
            "sector extraction policy"
        )

    def _statement_kind(text: str) -> str:
        value = str(text or "").lower()
        if re.search(r"cash\s+flo", value):
            return "cashflow"
        if re.search(r"balance\s+sheet|assets\s+and\s+liabilit", value):
            return "balance"
        if re.search(r"segment", value):
            return "segment"
        if re.search(
                r"financial\s+results|profit\s+and\s+loss|"
                r"statement\s+of\s+(?:comprehensive\s+income|audited\s+results)",
                value):
            return "income"
        return ""

    def _page_scope(page_number: int, wanted_kind: str) -> str:
        if not (1 <= page_number <= len(page_heads)):
            return "unknown"
        head = page_heads[page_number - 1]
        if wanted_kind and _statement_kind(head) != wanted_kind:
            return "unknown"
        found = set()
        if re.search(
                r"\bconsolidated\b.{0,100}\b(?:financial\s+results|"
                r"audited\s+results|balance\s+sheet|cash\s+flo|segment)"
                r"|(?:financial\s+results|audited\s+results|balance\s+sheet|"
                r"cash\s+flo|segment).{0,100}\bconsolidated\b",
                head):
            found.add("consolidated")
        if re.search(r"\bstandalone\b", head):
            found.add("standalone")
        return next(iter(found)) if len(found) == 1 else "unknown"

    def source_scope(report, requested_label: str) -> str:
        """Scope proved only by source pages of the same statement kind."""
        wanted_kind = _statement_kind(requested_label)
        found = {
            scope
            for page_number in (report or {}).get("pages", [])
            for scope in [_page_scope(page_number, wanted_kind)]
            if scope != "unknown"
        }
        return next(iter(found)) if len(found) == 1 else "unknown"

    def _opposite_scope_pages(
            requested_scope: str, requested_label: str) -> set[int]:
        if requested_scope not in {"standalone", "consolidated"}:
            return set()
        opposite = (
            "consolidated"
            if requested_scope == "standalone" else "standalone"
        )
        wanted_kind = _statement_kind(requested_label)
        return {
            page_number
            for page_number in range(1, len(page_heads) + 1)
            if _page_scope(page_number, wanted_kind) == opposite
        }

    def _is_statement_grid(grid: list[list[str]]) -> bool:
        """Reject markdown note blocks masquerading as a second statement."""
        numeric_rows = 0
        for row in grid[1:]:
            if any(
                    re.fullmatch(r"\(?-?[\d,.]+\)?", str(cell).strip())
                    for cell in row[1:]):
                numeric_rows += 1
        return numeric_rows >= 3

    document_scope = filing_document_scope(pdf_path)
    questions = _questions_for_document_scope(
        document_scope, extraction_policy)
    if document_scope == "standalone":
        log(
            "  SCOPE: filing proves it is single-entity (no subsidiary, "
            "associate, or joint venture) -> standalone-only extraction; "
            "consolidated question skipped"
        )

    def _finalize(
            label: str, answer: str, requested_scope: str = "unknown"):
        """markdown → grids → positional reconcile → residual decimal repair
        → identity checks. Returns [(heading, grid, report, fails)]."""
        out = []
        for heading, grid in _md_tables(answer):
            heading, grid = _normalize_grid_preamble(heading, grid)
            table_scope = requested_scope
            heading_lower = str(heading or "").lower()
            if "consolidated" in heading_lower:
                table_scope = "consolidated"
            elif "standalone" in heading_lower:
                table_scope = "standalone"
            table_text = " ".join(
                [heading]
                + [str(cell) for row in grid[:10] for cell in row]
            )
            if source_align.is_excluded_statement_text(
                    table_text, extraction_policy):
                log(
                    f"  {label}: rejected alternative reporting-basis "
                    "table before source reconciliation"
                )
                continue
            width = max(len(r) for r in grid)
            grid = [r + [""] * (width - len(r)) for r in grid]
            if not _is_statement_grid(grid):
                log(
                    f"  {label}: rejected non-statement markdown table "
                    "(fewer than three numeric data rows)"
                )
                continue
            grid, rep = source_align.reconcile_with_source(
                grid, label, heading or label, page_forms, page_lines,
                scan_pages=scans,
                excluded_pages=(
                    excluded_pages
                    | _opposite_scope_pages(table_scope, label)
                ))
            grid = source_align.repair_dropped_decimals(grid)
            fails = identities.failing(label, heading or label, grid)
            out.append((heading, grid, rep, fails))
        return out

    with open(pdf_path, "rb") as fh:
        content = fh.read()
    out = []
    with ephemeral_file(content, "filing.pdf") as fid:
        def _ask(q, budget=6000):
            return ask_text(instructions=EXTRACT_PROMPT, question=q,
                            file_ids=[fid], model=model,
                            max_output_tokens=budget, temperature=0,
                            with_status=True)

        def one(args):
            scope, label, q = args
            answer, status = _ask(q)
            truncated = False
            # a truncated answer LOSES ITS TAIL — typically the second of two
            # statements in one response. Escalate the budget; if the combined
            # standalone+consolidated question still overflows, ask each scope
            # separately (halving the response kills the overflow).
            if status == "incomplete":
                log(f"  {label}: response truncated at 6000 tokens -> retry at 12000")
                answer, status = _ask(q, 12000)
                if status == "incomplete" and scope == "unknown":
                    log(f"  {label}: still truncated -> one ask per scope")
                    parts = []
                    for want in ("STANDALONE", "CONSOLIDATED"):
                        a3, s3 = _ask(q + f"\n\nIMPORTANT: return ONLY the {want} "
                                      "version of this statement — the other is being "
                                      "requested separately.", 12000)
                        parts.append(a3)
                        truncated = truncated or s3 == "incomplete"
                    answer = "\n\n".join(parts)
                else:
                    truncated = status == "incomplete"
                if truncated:
                    log(f"  {label}: STILL truncated — content may be missing ⚠")
            tables = _finalize(label, answer, scope)
            fails = [f for _h, _g, _r, fl in tables for f in fl]
            if fails and "NOT PRESENT" not in answer[:400]:
                log(f"  {label}: verification FAILED ({'; '.join(fails)[:90]}) -> re-ask")
                answer2, _s = _ask(
                    q + "\n\nIMPORTANT: your previous attempt failed these arithmetic "
                    "checks against the printed statement: " + "; ".join(fails)[:400]
                    + ". Common causes: repeating one period column's numbers into an "
                    "adjacent column, mixing rows from two different printed statements "
                    "under different reporting bases, or misplacing values against their "
                    "labels. Read the table column by column and transcribe EACH "
                    "period's own values exactly as printed, from ONE statement only.")
                tables2 = _finalize(label, answer2, scope)
                if tables2 and sum(len(fl) for _h, _g, _r, fl in tables2) < len(fails):
                    before_width = _widest_numeric_row(tables)
                    after_width = _widest_numeric_row(tables2)
                    if after_width >= before_width:
                        tables, answer = tables2, answer2
                    else:
                        log(
                            f"  {label}: retry reduced numeric columns "
                            f"{before_width}->{after_width}; keeping the wider "
                            "source table and its review flag"
                        )
            return scope, label, answer, tables, truncated
        with ThreadPoolExecutor(max_workers=len(questions)) as ex:
            results = list(ex.map(one, questions))

    for i, (scope, label, answer, tables, truncated) in enumerate(results, 1):
        if "NOT PRESENT" in answer[:400] and answer.count("|") < 10:
            log(f"  {label}: not present in filing")
            continue
        if not tables:
            log(f"  {label}: no table parsed ⚠")
            continue
        for k, (heading, grid, rep, fails) in enumerate(tables, 1):
            sc = scope
            head = (heading or label).lower()
            if document_scope == "standalone" and "consolidated" in head:
                log(
                    f"    · [skip] {heading or label}: consolidated table "
                    "contradicts the filing's single-entity disclosure"
                )
                continue
            if document_scope == "standalone":
                sc = "standalone"
            elif "consolidated" in head:
                sc = "consolidated"
            elif "standalone" in head:
                sc = "standalone"
            proved_scope = source_scope(rep, label)
            if (proved_scope != "unknown" and sc in ("standalone", "consolidated")
                    and sc != proved_scope):
                log(
                    f"    · [skip] {heading or label}: requested/model scope "
                    f"{sc} contradicts source page scope {proved_scope}"
                )
                continue
            if proved_scope != "unknown":
                sc = proved_scope
            if rep and any(
                    page in excluded_pages for page in rep.get("pages", [])):
                log(
                    f"    · [skip] {heading or label}: source page belongs "
                    "to an excluded reporting basis"
                )
                continue
            title = heading or label
            notes = []
            if rep:
                n_fix = (
                    len(rep["corrections"]) + rep["filled"]
                    + rep.get("labels_filled", 0)
                    + len(rep.get("label_corrections", ()))
                )
                notes.append(f"source-reconciled p{'/'.join(map(str, rep['pages']))}"
                             + (f", {n_fix} cell(s) corrected" if n_fix else ""))
                if rep["structure_mismatch"] or rep["conservative"]:
                    notes.append("column structure could not be fully verified")
            elif not fails:
                notes.append("identities tie (no text-layer source page)")
            if truncated:
                title += "  ⚠ response truncated — rows may be missing"
                notes.append("TRUNCATED response")
            if fails:
                title += "  ⚠ verification failed — review"
                notes.append("FAILED: " + "; ".join(fails))
            else:
                checks = identities.run_checks(label, heading or label, grid)
                if checks:
                    notes.append(f"{sum(ok for _n, ok in checks)}/{len(checks)} identities tie")
            section = label
            up = head.upper()
            if "IND AS" in up or "IND-AS" in up:
                section += " — Ind AS"
            log(f"    · [{sc[:4]}] {title[:56]}: {'; '.join(notes)[:140]}")
            out.append(RawTable(
                page=((rep or {}).get("pages") or [i])[0],
                n=k, title=title, scope=sc,
                section=section,
                page_head=("(quarterly filing — LLM statement extraction; "
                           + "; ".join(notes) + ")"),
                units="", grid=grid))
        log(f"  {label}: {len(tables)} table(s)"
            + (f" — {sum(len(fl) for _h, _g, _r, fl in tables)} check(s) failing ⚠"
               if any(fl for _h, _g, _r, fl in tables) else ""))
    unique = []
    seen_grids = set()
    for table in out:
        normalized_grid = tuple(
            tuple(re.sub(r"\s+", " ", str(cell).strip()).casefold()
                  for cell in row)
            for row in table.grid
        )
        key = (table.scope, normalized_grid)
        if key in seen_grids:
            log(
                f"  DEDUP: removed repeated [{table.scope[:4]}] "
                f"{table.title[:60]}"
            )
            continue
        seen_grids.add(key)
        unique.append(table)
    out = unique
    missing = unextracted_statements(
        pdf_path, out, extraction_policy=extraction_policy)
    if missing:
        out.extend(recover_missing_statements(
            pdf_path,
            missing,
            document_scope=document_scope,
            page_forms=page_forms,
            page_lines=page_lines,
            scans=scans,
            excluded_pages=excluded_pages,
            log=log,
        ))
        missing = unextracted_statements(
            pdf_path, out, extraction_policy=extraction_policy)
    for kind in missing:
        log(f"  ⚠ COMPLETENESS: the filing prints a {kind} heading on a "
            "number-heavy page, but NO such table was extracted")
    return out


_STMT_HEADINGS = {
    "balance sheet": r"balance sheet|assets and liabilit",
    # OCR commonly turns the final "w" into "v", "vv", or punctuation
    # ("Cash Flov.~"). The stable stem is enough and remains specific.
    "cash flow statement": r"cash\s+flo",
    "segment information": r"segment",
    "financial results (P&L)": r"financial results|profit and loss",
}


def unextracted_statements(
        pdf_path: str,
        tables,
        extraction_policy: dict | None = None,
) -> list[str]:
    """FREE completeness check: statement kinds whose heading is printed on a
    number-heavy TEXT page of the filing but for which no table was extracted.
    (Scan pages carry no text to match — absence there stays the double-read's
    problem.) A model answering 'NOT PRESENT' for a statement the PDF prints
    is silent data loss; this is the tripwire."""
    import pymupdf
    got = set()
    for t in tables:
        s = f"{t.section} {t.title}".lower()
        for kind, pat in _STMT_HEADINGS.items():
            if re.search(pat, s):
                got.add(kind)
    present = set()
    doc = pymupdf.open(pdf_path)
    for page in doc:
        text = " ".join(page.get_text().split())
        from src.engine import source_align
        if source_align.is_excluded_statement_text(
                text, extraction_policy):
            continue
        if len(re.findall(r"\d[\d,]*\.?\d*", text)) < 25:
            continue
        head = text[:400].lower()
        for kind, pat in _STMT_HEADINGS.items():
            if re.search(pat, head):
                present.add(kind)
    doc.close()
    return sorted(present - got)


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
