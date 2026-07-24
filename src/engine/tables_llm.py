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

import os
import pymupdf

from src.engine.tables import (RawTable, _cluster_rows, _is_numericish,
                               _page_words, _prefilter_rows, _row_text)

_MONEY = re.compile(r"^\(?-?\d{1,3}(,\d{2,3})+(\.\d+)?\)?$|^\(?-?\d+\.\d+\)?$")


def _norm(t: str) -> str:
    return re.sub(r"[^\d.]", "", t or "").strip(".")


def _digits(t: str) -> str:
    return re.sub(r"\D", "", t or "")


def find_gap_pages(pdf_path: str, tables: list[RawTable],
                   all_pages: bool = False,
                   skip_pages: set[int] | None = None) -> dict[int, list[str]]:
    """page -> table-resident money values not captured by any table (free).

    all_pages=True also checks pages with NO extracted tables (small filings:
    a fully-missed results page must not be invisible)."""
    doc = pymupdf.open(pdf_path)
    by_page: dict[int, list[RawTable]] = defaultdict(list)
    for t in tables:
        by_page[t.page].append(t)
    if all_pages:
        for p in range(1, len(doc) + 1):
            by_page.setdefault(p, [])
    for p in (skip_pages or ()):
        by_page.pop(p, None)
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


# --------------------------------------------------------------------------- quarterly filings (whole-file upload)

_QTR_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer"},
                    "title": {"type": "string"},
                    "scope": {"type": "string",
                              "enum": ["standalone", "consolidated", "unknown"]},
                    "rows": {"type": "array",
                             "items": {"type": "array", "items": {"type": "string"}}},
                },
                "required": ["page", "title", "scope", "rows"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tables"],
    "additionalProperties": False,
}

# mirrors the proven frp quarterly prompt discipline: parentheses are negative
# and must never be stripped, nothing may be invented, exact printed line-item
# names, page numbers verifiable against [PAGE n] markers when text exists.
_QTR_INSTRUCTIONS = """You transcribe tables from Indian quarterly financial results filings.

Transcribe EVERY table printed on the requested pages of the attached PDF — the results
statement, segment information, balance sheet, cash flow, ratios, shareholding, notes tables.

MANDATORY rules:
- Copy each cell EXACTLY as printed: same digits, same Indian digit grouping (e.g. 1,45,575.77),
  same parentheses — numbers in parentheses are NEGATIVE, NEVER strip or convert them.
- One array entry per printed row, cells in column order, INCLUDING the particulars/label column
  and every period column. Include the column-header rows as the first rows.
- COLUMN PERIODS — make every period column self-identifying. A statement usually prints a merged
  heading ('Three months ended' / 'Year ended' / 'As at') ABOVE several columns, with only a date
  or a bare year beneath each. In the header row you return, EXPAND that heading into EVERY column
  it covers, so each period column's header cell states the COMPLETE period — e.g. 'Three months
  ended March 31, 2026', 'Year ended March 31, 2026', 'As at March 31, 2026' — never a bare '2026'
  or a lone date. Read these headings from the page layout; they are what the columns mean.
- In those period-HEADER cells ONLY, write the date in the normalized form 'Month DD, YYYY' — full
  month name, a space, the day, a comma, then the 4-digit year (e.g. 'March 31, 2026') — EVEN IF
  the page prints it run-together or abbreviated ('31March2026', '31.03.2026', '31-Mar-26'). This
  date normalization applies ONLY to the column period headers; every DATA cell is still copied
  EXACTLY as printed (rule above).
- Use the exact printed line-item names — do not rename, summarise, compute, or omit anything.
- Do NOT invent numbers: only transcribe what is visibly printed.
- page: the 1-based page of the attached PDF where the table is printed{page_hint}.
- scope: standalone / consolidated if the table's heading says so, else unknown.
- Skip pure-prose passages; letters and signatures are not tables."""


def quarterly_tables(pdf_path: str, model: str | None = None,
                     log=print) -> list[RawTable]:
    """Whole-file-upload extraction for small filings — the frp pattern.

    The PDF is uploaded ONCE (deleted in `finally`; responses store=False —
    nothing persists at OpenAI). Standalone and consolidated tables are asked
    for in TWO parallel calls — each response is half the size, which kills
    omission variance. A call whose response overflows the output budget is
    split over page halves. Verification: on pages with a text layer every
    digit-bearing cell must be printed in the document (rows failing are
    dropped); one targeted re-ask runs for any page left with uncaptured
    money values."""
    from concurrent.futures import ThreadPoolExecutor
    from src.llm import ephemeral_file, extract_json

    doc = pymupdf.open(pdf_path)
    n = len(doc)
    page_texts = [doc[i].get_text() for i in range(n)]
    has_text = [len(t.strip()) >= 40 for t in page_texts]
    page_digits = [_digits(t) for t in page_texts]
    doc_digits = "|".join(page_digits)
    doc.close()

    marked = "\n\n".join(f"[PAGE {i+1}]\n{t.strip()[:4000]}"
                         for i, t in enumerate(page_texts) if has_text[i])
    hint = (" — read it from the [PAGE n] markers in the PAGE-MARKED TEXT"
            if any(has_text) else " — count pages from the start of the PDF")
    instructions = _QTR_INSTRUCTIONS.replace("{page_hint}", hint)

    with open(pdf_path, "rb") as fh:
        content = fh.read()

    out: list[RawTable] = []
    with ephemeral_file(content, "quarterly.pdf") as fid:

        def ask(a: int, b: int, want: str):
            if want == "standalone":
                target = ("every STANDALONE table, plus every table with no stated scope "
                          "(shareholding, ratios, common notes). Do NOT return consolidated tables.")
            elif want == "consolidated":
                target = "every CONSOLIDATED table ONLY."
            else:
                target = "every table."
            user = (f"Transcribe {target} Look at pages {a}-{b} (1-based) of the attached filing.")
            if marked:
                user += ("\n\nPAGE-MARKED DOCUMENT TEXT (transcribe from the PDF; use this to "
                         "verify page numbers and exact digits):\n\n" + marked)
            res = extract_json(
                instructions=instructions, user_input=user,
                schema_name="quarterly_tables", schema=_QTR_SCHEMA,
                model=model, file_ids=[fid], max_output_tokens=8000,
                temperature=0)
            if res.get("_empty") and res.get("_status") == "incomplete" and b > a:
                mid = (a + b) // 2
                log(f"  {want} p{a}-{b}: response overflowed -> splitting")
                with ThreadPoolExecutor(max_workers=2) as ex:
                    for fut in [ex.submit(ask, a, mid, want),
                                ex.submit(ask, mid + 1, b, want)]:
                        fut.result()
                return
            kept = dropped = 0
            for t in res.get("tables", []):
                pno = min(max(int(t.get("page", a)), 1), n)
                rows_in = [[str(c) for c in r] for r in t.get("rows", [])
                           if any(str(c).strip() for c in r)]
                if len(rows_in) < 2:
                    continue
                grounded_page = has_text[pno - 1]
                grid = []
                for r in rows_in:
                    if grounded_page:
                        # page attribution can drift; drop a row only when its
                        # digits appear NOWHERE in the document text
                        ok = all((not _digits(c)) or len(_digits(c)) < 3
                                 or _digits(c) in page_digits[pno - 1]
                                 or _digits(c) in doc_digits for c in r)
                        if not ok:
                            dropped += 1
                            continue
                    grid.append(r)
                if len(grid) < 2:
                    continue
                width = max(len(r) for r in grid)
                grid = [r + [""] * (width - len(r)) for r in grid]
                kept += 1
                out.append(RawTable(
                    page=pno, n=0, title=t.get("title", "")[:70],
                    scope=t.get("scope", "unknown"),
                    section=t.get("title", "")[:70],
                    page_head=("(quarterly filing — LLM-transcribed, digit-grounded)"
                               if grounded_page else
                               "(quarterly filing, scanned page — vision-transcribed)"),
                    units="", grid=grid))
            log(f"  {want} p{a}-{b}: {kept} tables" +
                (f" ({dropped} ungrounded rows dropped)" if dropped else ""))

        with ThreadPoolExecutor(max_workers=2) as ex:
            for fut in [ex.submit(ask, 1, n, "standalone"),
                        ex.submit(ask, 1, n, "consolidated")]:
                fut.result()

        # verification-driven repair: a page with several uncaptured
        # money-formatted values gets ONE targeted re-ask (max 4 pages)
        def cap_digits():
            return {_digits(tok) for t in out for row in t.grid
                    for c in row for tok in c.split() if len(_digits(tok)) >= 4}
        cap = cap_digits()
        retry = []
        for p in range(1, n + 1):
            if not has_text[p - 1]:
                continue
            miss = 0
            for tok in page_texts[p - 1].split():
                core = tok.strip(".,;:()*")
                d = _digits(core)
                if (len(d) >= 4 and (("," in core) or ("." in core))
                        and _MONEY.match(core) and d not in cap):
                    miss += 1
            if miss >= 3:
                retry.append(p)
        for p in retry[:4]:
            log(f"  p{p}: uncovered values -> re-ask")
            before = len(out)
            ask(p, p, "all")
            added = out[before:]
            del out[before:]
            for t in added:
                new = sum(1 for row in t.grid for c in row for tok in c.split()
                          if len(_digits(tok)) >= 4 and _digits(tok) not in cap)
                if new >= 3:
                    out.append(t)
                    cap = cap_digits()

    # dedupe (both scope calls may return a common table)
    seen_keys = set()
    deduped = []
    for t in sorted(out, key=lambda t: (t.page, -len(t.grid))):
        key = (t.page, "|".join(_digits(c) for row in t.grid[:3] for c in row if _digits(c))[:80])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(t)
    out = sorted(deduped, key=lambda t: t.page)
    seen: dict[int, int] = {}
    for t in out:
        seen[t.page] = seen.get(t.page, 0) + 1
        t.n = seen[t.page]
    return out


# --------------------------------------------------------------------------- scanned pages (vision)

_VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "scope": {"type": "string",
                              "enum": ["standalone", "consolidated", "unknown"]},
                    "rows": {"type": "array",
                             "items": {"type": "array", "items": {"type": "string"}}},
                },
                "required": ["title", "scope", "rows"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tables"],
    "additionalProperties": False,
}

_VISION_INSTRUCTIONS = """This is a page image from an Indian company's financial results filing.
Transcribe EVERY table on the page exactly as printed: exact row labels, exact values
(keep Indian digit grouping, parentheses, dashes), one array entry per printed row,
cells in column order including the particulars/label column and every period column.
Include column-header rows as the first row(s). Do not compute, rename, or omit anything.
Tag each table standalone/consolidated if the page says so, else unknown.
If the page has no tables, return an empty list."""


def scanned_pages(pdf_path: str) -> list[int]:
    """1-based pages that are scans: either no text layer at all, or a
    full-page image with an embedded OCR layer (whose text is unreliable —
    'afte1·', '141.543' — and must NOT be treated as ground truth)."""
    doc = pymupdf.open(pdf_path)
    out = []
    for i in range(len(doc)):
        page = doc[i]
        if len(page.get_text().strip()) < 40 and page.get_images(full=True):
            out.append(i + 1)
            continue
        parea = abs(page.rect)
        for img in page.get_images(full=True):
            try:
                if abs(page.get_image_bbox(img)) > 0.8 * parea:
                    out.append(i + 1)
                    break
            except Exception:
                pass
    doc.close()
    return out


def vision_tables(pdf_path: str, pages: list[int], model: str | None = None,
                  log=print) -> list[RawTable]:
    """Transcribe tables from scanned pages via the vision model (1 call/page).

    No text layer exists, so digit-grounding is impossible — sheets are marked
    '(vision)' so consumers know these rows are model-transcribed, not copied."""
    from concurrent.futures import ThreadPoolExecutor
    from src.engine.vision import render_page
    from src.llm import extract_json

    def one_page(pno):
        img = render_page(pdf_path, pno, dpi=200)
        return pno, extract_json(
            instructions=_VISION_INSTRUCTIONS,
            user_input=f"Page {pno} of the filing. Transcribe every table.",
            schema_name="page_tables",
            schema=_VISION_SCHEMA,
            model=model,
            images_b64=[img],
            max_output_tokens=4000,
        )

    out: list[RawTable] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [ex.submit(one_page, p) for p in pages]
        results = []
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:
                log(f"  vision call failed ({type(e).__name__}: {e})")
    for pno, res in sorted(results):
        n = 0
        for t in res.get("tables", []):
            rows = [[str(c) for c in r] for r in t.get("rows", []) if any(str(c).strip() for c in r)]
            if len(rows) < 2:
                continue
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            n += 1
            out.append(RawTable(
                page=pno, n=n, title=f"(vision) {t.get('title', '')[:60]}",
                scope=t.get("scope", "unknown"), section=t.get("title", "")[:70],
                page_head="(scanned page — vision-transcribed)", units="", grid=rows))
        log(f"  p{pno}: {n} tables transcribed (vision)")
    return out


def vision_tables_consensus(pdf_path: str, pages: list[int],
                            model: str | None = None, log=print) -> list[RawTable]:
    """Two INDEPENDENT vision transcriptions per scanned page. Cells both
    passes agree on are accepted; disagreeing cells are flagged '⚠a | b' and
    structure mismatches are flagged in the title — nothing can be silently
    wrong, only verified or visibly uncertain."""
    silent = lambda m: None
    a_pass = vision_tables(pdf_path, pages, model=model, log=silent)
    b_pass = vision_tables(pdf_path, pages, model=model, log=silent)

    def dset(t):
        return {_digits(tok) for row in t.grid for c in row
                for tok in c.split() if len(_digits(tok)) >= 3}

    by_page_b: dict[int, list[RawTable]] = defaultdict(list)
    for t in b_pass:
        by_page_b[t.page].append(t)
    out: list[RawTable] = []
    for t in a_pass:
        peers = by_page_b.get(t.page, [])
        da = dset(t)
        # mate = peer with the most shared values (row segmentation may differ)
        mate = None
        best = 0.0
        for p in peers:
            db = dset(p)
            j = len(da & db) / max(len(da | db), 1)
            if j > best:
                best, mate = j, p
        if mate is None or best < 0.3:
            if da:                       # numeric content seen by only one pass
                t.title += "  ⚠ single-pass only — review"
            out.append(t)
            continue
        peers.remove(mate)
        if (len(mate.grid), len(mate.grid[0])) == (len(t.grid), len(t.grid[0])):
            flags = 0
            for i, row in enumerate(t.grid):
                for j2, cell in enumerate(row):
                    other = mate.grid[i][j2]
                    if " ".join(cell.split()) != " ".join(other.split()):
                        if _digits(cell) != _digits(other):
                            row[j2] = f"⚠ {cell} | {other}"
                            flags += 1
            if flags:
                t.title += f"  ⚠ {flags} cell(s) differ — review flagged cells"
        else:
            diff = (da - dset(mate)) | (dset(mate) - da)
            if diff:
                t.title += f"  ⚠ {len(diff)} value(s) differ between passes — review"
        out.append(t)
    # pass-B leftovers: keep only genuinely NEW numeric content
    covered = {d for pno in by_page_b for t in out if t.page == pno for d in dset(t)}
    for pno, rest in by_page_b.items():
        page_cov = {d for t in out if t.page == pno for d in dset(t)}
        for p in rest:
            db = dset(p)
            if db and len(db - page_cov) >= max(3, 0.4 * len(db)):
                p.title += "  ⚠ single-pass only — review"
                out.append(p)
                page_cov |= db
    n_flagged = sum(1 for t in out if "⚠" in t.title)
    log(f"  consensus: {len(out)} tables, {n_flagged} need review")
    return sorted(out, key=lambda t: (t.page, t.n))


_STATEMENT_PAGE_HEADING = re.compile(
    r"\bstatement\s+of\b.{0,80}\b(?:financial\s+)?"
    r"(?:results|profit\s+and\s+loss|assets\s+and\s+liabilit(?:y|ies)|"
    r"cash\s+flows?)\b|"
    r"\bbalance\s+sheet\b|\bfinancial\s+results\b|"
    r"\bsegment\s+(?:information|report(?:ing)?)\b",
    re.IGNORECASE,
)


def _is_statement_page_text(text: str) -> bool:
    """Return whether text is a number-heavy primary-statement page.

    Filing titles do not use one fixed word order. For example, a primary
    quarterly statement may be titled ``Statement of Consolidated Audited
    Results`` without the adjacent phrase "financial results".
    """
    normalized = " ".join(str(text or "").split())
    return (
        len(re.findall(r"\d[\d,]*\.?\d*", normalized)) >= 25
        and bool(_STATEMENT_PAGE_HEADING.search(normalized[:1500]))
    )


def maybe_trim_large_filing(
        pdf_path: str,
        max_pages: int = 100,
        log=print,
        extraction_policy: dict | None = None,
) -> str:
    """Disclosure PACKAGES (100+ pages) waste upload cost on non-statement
    content. For those — and only those — locate the statement pages by
    heading + digit density (the annual engine's locate step), write a
    trimmed copy to the temp dir, and extract from that. Small filings are
    returned unchanged and flow through the proven whole-file path."""
    import re
    import tempfile
    doc = pymupdf.open(pdf_path)
    n = len(doc)
    if n <= max_pages:
        doc.close()
        return pdf_path
    keep = {0}                                   # cover page: banner/units context
    from src.engine import source_align
    for i in range(n):
        t = " ".join(doc[i].get_text().split())
        if len(t.strip()) < 100:
            keep.add(i)                          # scanned page: text heuristics are
            continue                             # blind here — never drop it
        if source_align.is_excluded_statement_text(
                t, extraction_policy):
            continue
        # heading window is generous: filings open with long registered-office
        # preambles before the statement title
        if _is_statement_page_text(t):
            keep.add(i)
            next_text = (
                " ".join(doc[i + 1].get_text().split())
                if i + 1 < n else ""
            )
            if (i + 1 < n
                    and not source_align.is_excluded_statement_text(
                        next_text, extraction_policy)):
                keep.add(i + 1)                  # statements span page breaks
    out = pymupdf.open()
    for p in sorted(keep):
        out.insert_pdf(doc, from_page=p, to_page=p)
    trimmed = os.path.join(tempfile.gettempdir(),
                           os.path.basename(pdf_path).replace(".pdf", "_trimmed.pdf"))
    out.save(trimmed)
    out.close()
    doc.close()
    log(f"  {n}-page package -> {len(keep)} statement pages (annual-style locate)")
    return trimmed


def extract_tables_smart(pdf_path: str, financial_only: bool = True,
                         vision: bool = True, progress=None,
                         log=print, mode: str = "auto",
                         extraction_policy: dict | None = None) -> list[RawTable]:
    """The verified hybrid.

    mode='annual': deterministic pipeline (TEXT pages, 100% faithful by
    construction) + digit-grounded gap-fill + consensus vision for scanned
    pages. mode='quarterly': whole-file upload + internal statement questions
    with arithmetic tie-out. mode='auto': quarterly when <=35 pages."""
    from src.engine.tables import extract_tables

    doc = pymupdf.open(pdf_path)
    n_pages = len(doc)
    doc.close()
    small = n_pages <= 35 if mode == "auto" else (mode == "quarterly")
    if small and vision:
        # quarterly filing: upload once, internally ask for each detailed
        # statement (standalone + consolidated results, segments, BS, CF)
        from src.engine.filing_chat import quarterly_statement_tables
        log("  quarterly filing -> statement extraction (file upload + internal questions)")
        return quarterly_statement_tables(
            pdf_path, log=log,
            extraction_policy=extraction_policy)
    tables = extract_tables(pdf_path, progress=progress,
                            financial_only=financial_only and not small)
    if not vision:
        return tables
    # scan pages (no text layer OR full-page image with unreliable OCR text)
    # -> consensus vision; their OCR-derived deterministic tables are dropped
    scans = scanned_pages(pdf_path)
    if not small:
        in_span = {t.page for t in tables}
        lo, hi = (min(in_span), max(in_span)) if in_span else (1, n_pages)
        scans = [p for p in scans if lo <= p <= hi]
    scan_set = set(scans)
    if scans:
        log(f"  {len(scans)} scan page(s) -> double-pass consensus vision")
        recovered = vision_tables_consensus(pdf_path, scans, log=log)
        tables = [t for t in tables if t.page not in scan_set]
        tables = sorted(tables + recovered, key=lambda t: (t.page, t.n))
    # true-text pages -> digit-grounded gap-fill for coverage residuals
    filled = gap_fill(pdf_path, tables, log=log, all_pages=small,
                      skip_pages=scan_set)
    if filled:
        tables = sorted(tables + filled, key=lambda t: (t.page, t.n))
    return tables


def gap_fill(pdf_path: str, tables: list[RawTable], model: str | None = None,
             log=print, all_pages: bool = False,
             skip_pages: set[int] | None = None) -> list[RawTable]:
    """One grounded mini call per gap page. Returns recovered RawTables only."""
    from src.llm import extract_json

    gaps = find_gap_pages(pdf_path, tables, all_pages=all_pages, skip_pages=skip_pages)
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
