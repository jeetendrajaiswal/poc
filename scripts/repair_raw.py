"""Automated verify-and-repair for a quarterly raw extraction.

This packages the exact repair loop that produced the verified corpus:

  1. VERIFY   — arithmetic identity suites per statement (offline).
  2. LOCATE   — for failing statements, find their pages in the PDF by
                heading keywords.
  3. REPAIR   — re-read those pages from pixels (vision consensus: two
                independent reads, disagreeing cells flagged ⚠), swap the
                repaired tables into the extraction.
  4. RE-VERIFY— identities again; anything still failing keeps a visible
                ⚠ flag in the table title (never silently wrong).
  5. CROSS    — optional: compare against the company's other verified
                filings (columns repeated across filings must match) and
                report conflicts for page adjudication.

Usage:
  python scripts/repair_raw.py <name>            # e.g. wipro_q1FY2027
  python scripts/repair_raw.py <name> --cross    # also cross-check history
  python scripts/repair_raw.py --check-all       # verify-only pass, no repairs

Vision repair costs ~$0.01-0.03/page and runs ONLY when identities fail.
"""
import os
import pickle
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.engine.identities import run_checks  # noqa: E402

PDF_DIR = os.path.expanduser("~/Downloads/qtr_reports")
PKL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "output", "qtr_raw")

_PAGE_PAT = {
    "balance": r"balance sheet|assets and liabilit",
    "cashflow": r"cash flow",
    "income": r"financial results|profit and loss",
    "segment": r"segment",
}

_PAGE_ANCHORS = {
    "balance": (
        r"\bnon-current assets\b", r"\bcurrent assets\b", r"\btotal assets\b",
        r"\bequity and liabilities\b", r"\btotal equity\b",
    ),
    "cashflow": (
        r"\boperating activities\b", r"\binvesting activities\b",
        r"\bfinancing activities\b", r"\bcash and cash equivalents\b",
    ),
    "income": (
        r"\brevenue from operations\b", r"\btotal income\b",
        r"\btotal expenses\b", r"\bprofit before tax\b",
        r"\bprofit (?:for|after tax)\b", r"\bearnings per (?:equity )?share\b",
    ),
    "segment": (
        r"\brevenue by (?:business )?segment\b", r"\bsegment (?:profit|results)\b",
        r"\binter-segment revenue\b", r"\bprofit before tax\b",
    ),
}


def _statement_page_score(text: str, kind: str, scope: str) -> float:
    """Rank actual statement tables above references to those statements.

    Quarterly packages often put a long company/address banner before the
    statement heading, so the heading window is deliberately wider than the
    old 400-character prefix.  Strong row anchors and numeric density prevent
    an auditor-report paragraph mentioning "consolidated financial results"
    from being selected for pixel repair.
    """
    norm = " ".join((text or "").split()).lower()
    if not norm or kind not in _PAGE_PAT:
        return 0.0
    heading = norm[:2000]
    nnum = len(re.findall(r"(?<![a-z])\(?-?\d[\d,]*\.?\d*\)?", norm))
    anchors = sum(bool(re.search(pat, norm)) for pat in _PAGE_ANCHORS[kind])
    if nnum < 25 or anchors < 2:
        return 0.0

    has_con = "consolidated" in heading
    has_std = "standalone" in heading
    if scope == "consolidated" and has_std and not has_con:
        return 0.0
    if scope == "standalone" and has_con and not has_std:
        return 0.0

    score = anchors * 20.0 + min(nnum, 250) / 10.0
    if re.search(_PAGE_PAT[kind], heading):
        score += 15.0
    if scope == "consolidated" and has_con:
        score += 12.0
    elif scope == "standalone" and has_std:
        score += 12.0
    # Auditor pages can contain many numbers in annexures.  They remain
    # eligible only with very strong table evidence, and rank below the table.
    if re.search(r"\bindependent auditor(?:'s|s)? report\b", heading):
        if anchors < 4:
            return 0.0
        score -= 30.0
    return score


def _failing_statements(rows):
    """Statements failing ANY check — identity suites AND the duplicate-column
    structural check (a duplicated column is internally consistent, so only
    run_checks sees it; it must trigger a pixel re-read like any other fail)."""
    fails = []
    for page, n, title, scope, section, grid in rows:
        bad = [name for name, ok in run_checks(section, title, grid) if not ok]
        if bad:
            fails.append((page, title, scope, section, bad))
    return fails


def _stmt_kind(section, title):
    s = f"{section} {title}".lower()
    for kind, pat in (("cashflow", _PAGE_PAT["cashflow"]),
                      ("balance", _PAGE_PAT["balance"]),
                      ("segment", _PAGE_PAT["segment"]),
                      ("income", _PAGE_PAT["income"])):
        if re.search(pat, s):
            return kind
    return None


def _locate_pages(pdf_path, kind, scope, grid=None):
    """Pages whose heading names this statement (and scope when stated).
    Scanned pages have no text to match — when text-locate finds nothing,
    fall back to ALL scanned pages (consensus sorts out which tables are
    which; the caller matches replacements by label overlap)."""
    doc = pymupdf.open(pdf_path)
    def _digits(value):
        return "".join(re.findall(r"\d", str(value)))

    grid_digits = {
        _digits(c) for row in (grid or []) for c in row
        if len(_digits(c)) >= 3
    }
    hits, scans = [], []
    for i in range(len(doc)):
        text = doc[i].get_text()
        if len(text.strip()) < 100:
            scans.append(i + 1)
            continue
        score = _statement_page_score(text, kind, scope)
        if score:
            if grid_digits:
                page_digits = {
                    _digits(tok) for tok in
                    re.findall(r"\(?-?\d[\d, ]*\.?\d*\)?", text)
                    if len(_digits(tok)) >= 3
                }
                coverage = len(grid_digits & page_digits) / len(grid_digits)
                # The failing read's digits may sit on the wrong rows, but they
                # still come from the correct table page.  This is a powerful
                # discriminator when a package embeds multiple annual reports.
                score += 200.0 * coverage
            hits.append((score, i + 1))
    doc.close()
    if not hits:
        return scans
    hits.sort(key=lambda x: (-x[0], x[1]))
    best = hits[0][0]
    # Keep near-best continuation/alternate-layout pages, but bound a paid
    # repair call.  Low-scoring prose references are excluded by the anchor
    # threshold above.
    threshold = 0.90 if grid_digits else 0.60
    return [page for score, page in hits[:4] if score >= threshold * best]


def grid_of(rows, page, title):
    for r in rows:
        if r[0] == page and r[2] == title:
            return r[5]
    return []


def repair(name: str, cross: bool = False, pdf_path: str | None = None,
           log=print) -> bool:
    pdf = pdf_path or os.path.join(PDF_DIR, f"{name}.pdf")
    pkl = os.path.join(PKL_DIR, f"{name}.pkl")
    rows = pickle.load(open(pkl, "rb"))
    fails = _failing_statements(rows)
    if not fails:
        log(f"{name}: all identities tie — nothing to repair")
        if cross:
            os.system(f"{sys.executable} scripts/verify_raw.py --cross {name.split('_')[0]}")
        return True

    # FREE pass first: positional reconciliation against the text layer often
    # settles a failing statement outright; and when it had FULL authority
    # (digital page, complete coverage) yet the statement still fails, the
    # pixels say the same thing the text does — a paid vision re-read cannot
    # do better, so it is skipped and the statement stays ⚠-flagged.
    from src.engine import source_align as _sa
    from src.engine.filing_chat import _page_number_forms
    _forms = _page_number_forms(pdf)
    _lines = _sa.page_word_lines(pdf)
    _scans = _sa.untrusted_text_pages(pdf)
    no_vision: set = set()
    new_rows = []
    for r in rows:
        page, n, title, scope, section, grid = r
        if not any(f[0] == page and f[1] == title for f in fails):
            new_rows.append(r)
            continue
        g2, rep = _sa.reconcile_with_source(grid, section, title, _forms,
                                            _lines, scan_pages=_scans)
        # Deterministic glyph recovery for boxed-total text-layer corruption
        # ('(' rendered as '1', comma rendered as space) that positional
        # reconcile cannot see because the pixels match the corrupt text. Only
        # adopted when the printed identities tie out — never a guess, no cost.
        g3, fixed = _sa.repair_glyph_by_identity(g2, section, title)
        if fixed:
            log(f"{name}: [{scope[:4]}] '{title[:44]}' — glyph repair tied out "
                f"{fixed} identity check(s) deterministically")
            g2 = g3
        n_old = sum(1 for _c, ok in run_checks(section, title, grid) if not ok)
        n_new = sum(1 for _c, ok in run_checks(section, title, g2) if not ok)
        if n_new < n_old:
            log(f"{name}: [{scope[:4]}] '{title[:44]}' — source reconciliation "
                  f"fixed {n_old - n_new}/{n_old} check(s) for free")
            title2 = title.replace("  ⚠ verification failed — review", "") \
                          .replace("  ⚠ arithmetic does not tie — review", "")
            if n_new:
                title2 += "  ⚠ verification failed — review"
            r = (page, n, title2, scope, section, g2)
        if rep and not rep["conservative"] and not rep["abstained"] and n_new:
            no_vision.add((page, scope, section))
        new_rows.append(r)
    rows = new_rows
    pickle.dump(rows, open(pkl, "wb"))     # free-pass repairs survive even if
    fails = _failing_statements(rows)      # a later vision call crashes
    if not fails:
        log(f"{name}: repaired offline — all identities tie")
    else:
        from src.engine.tables_llm import vision_tables_consensus
        for page, title, scope, section, bad in fails:
            if (page, scope, section) in no_vision:
                log(f"{name}: [{scope[:4]}] '{title[:44]}' — text layer is "
                      "authoritative and still fails; vision would read the same "
                      "pixels — stays ⚠-flagged")
                continue
            kind = _stmt_kind(section, title)
            log(f"{name}: REPAIRING [{scope[:4]}] '{title[:44]}' ({'; '.join(bad)[:70]})")
            old_grid = grid_of(rows, page, title)
            pages = _locate_pages(pdf, kind, scope, old_grid) if kind else []
            if not pages:
                log(f"   could not locate pages — statement stays ⚠-flagged")
                continue
            vt = vision_tables_consensus(pdf, pages, log=lambda m: log("    " + str(m)))
            best = None
            labs_old = {str(r[0]).strip().lower() for r in old_grid if str(r[0]).strip()}
            vals_old = {str(c).strip() for r in old_grid for c in r[1:]
                        if re.search(r"\d", str(c))}
            for t in vt:
                checks = run_checks(section, title, t.grid)
                if not checks:
                    continue
                # candidate must BE this statement: labels overlap AND most
                # of the original's numbers reappear (standalone/consolidated
                # share labels but not magnitudes — value overlap is the
                # scope discriminator label overlap cannot be)
                labs_new = {str(r[0]).strip().lower() for r in t.grid if str(r[0]).strip()}
                if labs_old and len(labs_old & labs_new) < min(4, max(1, len(labs_old) // 3)):
                    continue
                vals_new = {str(c).strip() for r in t.grid for c in r[1:]
                            if re.search(r"\d", str(c))}
                if vals_old and len(vals_old & vals_new) < len(vals_old) // 2:
                    continue
                score = sum(1 for _n, ok in checks if ok) - sum(2 for _n, ok in checks if not ok)
                if best is None or score > best[0]:
                    best = (score, t)
            if best is None:
                log("   vision produced nothing — statement stays ⚠-flagged")
                continue
            _sc, t = best
            old_bad = sum(1 for _n, ok in run_checks(section, title, grid_of(rows, page, title)) if not ok)
            new_bad = sum(1 for _n, ok in run_checks(section, title, t.grid) if not ok)
            if new_bad >= old_bad:
                log("   re-read no better — statement stays ⚠-flagged")
                continue
            new_rows = []
            replaced = False
            for r in rows:
                if r[0] == page and r[2] == title:
                    flag = "" if new_bad == 0 else "  ⚠ verification failed — review"
                    new_rows.append((t.page, t.n, (t.title or title) + flag, scope, section, t.grid))
                    replaced = True
                else:
                    new_rows.append(r)
            rows = new_rows
            log(f"   {'replaced from pixels' if replaced else 'no replacement made'}")
        pickle.dump(rows, open(pkl, "wb"))
        residual = _failing_statements(rows)
        log(f"{name}: after repair — {len(residual)} statement(s) still failing "
              f"({'⚠ flagged for review' if residual else 'clean'})")

    if cross:
        comp = name.split("_")[0]
        os.system(f"{sys.executable} scripts/verify_raw.py --cross {comp}")
    return not _failing_statements(rows)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if "--check-all" in flags:
        ok = True
        for fn in sorted(os.listdir(PKL_DIR)):
            if fn.endswith(".pkl") and not fn.startswith("_"):
                rows = pickle.load(open(os.path.join(PKL_DIR, fn), "rb"))
                fails = _failing_statements(rows)
                status = "OK" if not fails else f"{len(fails)} FAILING"
                print(f"{fn[:-4]:20s} {status}")
                ok = ok and not fails
        sys.exit(0 if ok else 1)
    if not args:
        sys.exit(__doc__)
    sys.exit(0 if repair(args[0], cross="--cross" in flags) else 1)
