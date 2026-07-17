"""MD&A extraction — locate the Management Discussion & Analysis section in an
annual report deterministically, then produce a DETAILED summary of it (and
nothing else) with the frp prompt discipline: exact figures, no fabrication,
no editorialising.

    from src.engine.mdna import summarize_mdna
    md = summarize_mdna("annual_report.pdf")

    .venv/bin/python -m src.engine.mdna report.pdf [out.md]
"""
from __future__ import annotations

import pymupdf

_MDNA = "management discussion"
# top-region heading lines that mark the NEXT chapter (section end)
_NEXT_SECTIONS = (
    "sustainability at", "board's report", "boards report", "directors' report",
    "corporate governance", "business responsibility", "brsr",
    "standalone financial", "consolidated financial", "financial statements",
    "independent auditor", "risk management report", "integrated report",
)


def _heading_lines(page, k: int = 8) -> list[str]:
    return [ln.strip().lower() for ln in page.get_text().strip().split("\n")[:k]]


def _is_toc_page(page) -> bool:
    """Contents pages: many lines ending in page numbers / dotted leaders."""
    lines = [ln.strip() for ln in page.get_text().split("\n") if ln.strip()]
    if len(lines) < 8:
        return False
    endnum = sum(1 for ln in lines if ln.rstrip(". ").split()
                 and ln.rstrip(". ").split()[-1].isdigit() and len(ln) < 80)
    dots = sum(1 for ln in lines if "..." in ln or ln.count(".") > 8)
    return endnum > 0.4 * len(lines) or dots > 0.3 * len(lines)


def locate_mdna(pdf_path: str, max_span: int = 60) -> tuple[int, int]:
    """(start_page, end_page) of the MD&A section, 1-based inclusive.
    Start: first NON-TOC page whose top region carries the MD&A heading as a
    short TITLE line (not a prose cross-reference). End: page before the next
    chapter heading, capped at max_span pages."""
    doc = pymupdf.open(pdf_path)
    n = len(doc)

    # ---- case A: chapter heading as a short TITLE line in the page top region
    candidates = []
    for i in range(n):
        for ln in _heading_lines(doc[i]):
            if _MDNA in ln and len(ln) <= 60 and len(ln.split()) <= 6:
                if not _is_toc_page(doc[i]):
                    candidates.append(i + 1)
                break
    best = None
    for start in candidates:
        end = min(start + max_span - 1, n)
        for i in range(start, min(start + max_span, n)):
            tops = _heading_lines(doc[i])
            # a page whose top region still mentions MD&A is a continuation
            if any(_MDNA in ln for ln in tops):
                continue
            if any(any(h in ln and len(ln) <= 60 for h in _NEXT_SECTIONS) for ln in tops):
                end = i    # page i (0-based) starts the next chapter
                break
        if end - start >= 2:               # a real section, not a stray mention
            doc.close()
            return start, end
        if best is None:
            best = (start, end)

    # ---- case B: running ribbon header ('STATUTORY REPORTS | Management
    # Discussion and Analysis') printed on every section page (anywhere on the
    # page — two-up spreads put it after the body text) -> span = the page run
    import re as _re
    ribbon_pages = []
    for i in range(n):
        for ln in doc[i].get_text().split("\n"):
            l = _re.sub(r"\s+", " ", ln.strip()).lower()
            if _MDNA in l and len(l) <= 80 and "|" in l:
                ribbon_pages.append(i + 1)
                break
    if len(ribbon_pages) >= 3:
        # first contiguous cluster (gap tolerance 1)
        start = end = ribbon_pages[0]
        for p in ribbon_pages[1:]:
            if p - end <= 2:
                end = p
            else:
                break
        if end - start >= 2:
            doc.close()
            return start, end

    # ---- case C: mid-page CAPS subsection inside the Board's/Directors'
    # Report (bank style); end = next statutory topic heading. TOC pages are
    # skipped and the heading must be followed by prose, not more nav entries.
    _C_END = ("OTHER STATUTORY DISCLOSURES", "ANNEXURE", "RESPONSIBILITY STATEMENT",
              "SECRETARIAL AUDIT", "CORPORATE GOVERNANCE", "ACKNOWLEDGEMENT",
              "ANNUAL RETURN", "CONSERVATION OF ENERGY", "STATUTORY AUDITOR")
    for i in range(n):
        if _is_toc_page(doc[i]):
            continue
        lines = [ln.strip() for ln in doc[i].get_text().split("\n") if ln.strip()]
        for k, ln in enumerate(lines):
            if _MDNA in ln.lower() and ln.isupper() and len(ln) <= 60:
                follow = lines[k + 1:k + 7]
                if not any(len(f) >= 55 and not f.isupper() for f in follow):
                    continue                      # nav/TOC entry, not a section start
                start = i + 1
                end = min(start + max_span - 1, n)
                for j in range(start, min(start + max_span, n)):
                    jl = [x.strip() for x in doc[j].get_text().split("\n") if x.strip()]
                    if any(x.isupper() and 8 < len(x) < 70 and any(m in x for m in _C_END)
                           for x in jl):
                        end = j
                        break
                doc.close()
                return start, end

    doc.close()
    if best:
        return best
    raise ValueError("MD&A section heading not found")


_SUMMARY_PROMPT = """You are a senior financial analyst specializing in Indian equity markets.
You are given the FULL TEXT of the Management Discussion & Analysis (MD&A) section of a company's annual report.

Write a DETAILED, faithful summary of the MD&A — and ONLY the MD&A.

RULES:
- Cover EVERY substantive theme the MD&A itself discusses (typically: industry/macro environment, business overview and strategy, segment/service-line performance, financial performance and ratios, liquidity/capital allocation, risks and mitigation, outlook, people/HR, internal controls) — use the themes actually present; do not force a template.
- Preserve EXACT figures as printed (₹/$ amounts, %, bps, headcounts, ratios) with their period context; numbers in parentheses are negative.
- Be comprehensive but summarised: condense prose, keep every material fact, figure and management statement. Use markdown headings and bullet points; short tables where the MD&A gives comparative numbers.
- Do NOT add your own analysis, opinions, or information from outside the provided text.
- Do NOT invent numbers — only use what is explicitly stated.
- Start directly with the summary content (no preamble)."""


def _digits(t: str) -> str:
    return "".join(c for c in t if c.isdigit())


def _ungrounded_figures(summary: str, source_digits: str) -> list[str]:
    """Figures in the summary whose digit-string is NOT in the MD&A text —
    a garbled or invented number can never pass this."""
    import re
    bad = []
    for tok in re.findall(r"[₹$]?\(?-?[\d,]+(?:\.\d+)?\)?%?", summary):
        d = _digits(tok)
        if len(d) >= 3 and d not in source_digits:
            bad.append(tok)
    return sorted(set(bad))


def summarize_mdna(pdf_path: str, model: str | None = None,
                   max_output_tokens: int = 8000, log=print) -> str:
    """Locate the MD&A, produce a detailed markdown summary, and VERIFY it:
    every figure in the summary must be printed in the MD&A source text.
    Ungrounded figures trigger one corrective re-ask; any that remain are
    listed in a verification footnote rather than passed off silently."""
    from src.llm import ask_text

    start, end = locate_mdna(pdf_path)
    log(f"  MD&A located: pages {start}-{end}")
    doc = pymupdf.open(pdf_path)
    pages = [doc[i - 1].get_text() for i in range(start, end + 1)]
    doc.close()
    # the section may begin mid-page (bank-style subsection): drop whatever
    # precedes the heading on the first page
    low = pages[0].lower()
    pos = low.find(_MDNA)
    if pos > 200:
        pages[0] = pages[0][pos:]
    text = "\n\n".join(f"[PAGE {start + i}]\n{t.strip()}" for i, t in enumerate(pages))
    source_digits = _digits(text)

    def gen(question: str) -> str:
        return ask_text(instructions=_SUMMARY_PROMPT, question=question,
                        model=model, max_output_tokens=max_output_tokens,
                        temperature=0.1)

    if len(text) > 240_000:
        mid = len(pages) // 2
        parts = [gen("MD&A TEXT:\n\n" + "\n\n".join(pages[:mid])),
                 gen("MD&A TEXT:\n\n" + "\n\n".join(pages[mid:]))]
        summary = gen("Merge these two partial MD&A summaries into ONE coherent detailed "
                      "summary, removing duplication, keeping every fact and figure:\n\n"
                      + "\n\n---\n\n".join(parts))
    else:
        summary = gen("MD&A TEXT:\n\n" + text)

    # verification: every figure must exist in the source
    bad = _ungrounded_figures(summary, source_digits)
    if bad:
        log(f"  {len(bad)} figure(s) not found in source -> corrective re-ask")
        summary = gen("MD&A TEXT:\n\n" + text +
                      "\n\nYour previous summary contained these figures which do NOT "
                      f"appear in the text (miscopied or invented): {', '.join(bad[:20])}. "
                      "Rewrite the detailed summary using ONLY figures printed in the text.")
        bad = _ungrounded_figures(summary, source_digits)
    if bad:
        summary += ("\n\n---\n*Verification note: the following figures could not be "
                    f"matched to the MD&A text and should be checked: {', '.join(bad)}*")
    log(f"  verification: {'all figures grounded' if not bad else f'{len(bad)} flagged'}")
    return summary


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: python -m src.engine.mdna report.pdf [out.md]")
    md = summarize_mdna(sys.argv[1])
    out = sys.argv[2] if len(sys.argv) > 2 else sys.argv[1].rsplit(".", 1)[0] + "_mdna.md"
    with open(out, "w") as fh:
        fh.write(md)
    print(f"\nMD&A summary -> {out}\n")
    print(md[:2000])
