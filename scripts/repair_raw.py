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
import traceback as _tb

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
    # 'Statement of Consolidated Audited Results' (Infosys) says "Results", not
    # "Financial Results" — match it the way statement_of() does, or the true
    # P&L page is missed and vision re-reads the wrong pages.
    "income": r"financial results|profit and loss|audited results|results (of|for)",
    "segment": r"segment",
}


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


def _locate_pages(pdf_path, kind, scope):
    """Pages whose heading names this statement (and scope when stated).
    Scanned pages have no text to match — when text-locate finds nothing,
    fall back to ALL scanned pages (consensus sorts out which tables are
    which; the caller matches replacements by label overlap)."""
    doc = pymupdf.open(pdf_path)
    hits, scans = [], []
    for i in range(len(doc)):
        text = doc[i].get_text()
        if len(text.strip()) < 100:
            scans.append(i + 1)
            continue
        head = " ".join(text.split())[:400].lower()
        # a REAL statement page carries many GROUPED figures (24,688 / 46,402);
        # an index/TOC or auditor-report page names the statement but prints only
        # small page refs — require grouped-number density to exclude them.
        ngrp = len(re.findall(r"\d{1,3}(?:,\d{3})+|\d{4,}", text))
        if ngrp < 12 or not re.search(_PAGE_PAT[kind], head):
            continue
        if scope == "consolidated" and "consolidated" not in head:
            continue
        if scope == "standalone" and "consolidated" in head and "standalone" not in head:
            continue
        hits.append(i + 1)
    doc.close()
    return hits or scans


def grid_of(rows, page, title):
    for r in rows:
        if r[0] == page and r[2] == title:
            return r[5]
    return []


def vision_reread(rows, findings, pdf_path, log=print):
    """Identity-driven vision re-read. When the POST-MAP verification
    (verify_delivered, which checks reconciliation via the TAXONOMY-mapped fids)
    reports a statement whose subtotals/totals do not reconcile, that
    statement's text extraction is corrupt (e.g. Wipro's garbled balance sheet:
    a blank 'Total non-current assets', '612,168' read as '168'). Re-read ONLY
    those pages from pixels (vision consensus) and adopt the pixel read when its
    printed identities reconcile better than the current grid. Triggered solely
    by an identity finding, so clean statements are never re-read. The caller
    re-maps the updated rows. Returns (rows, notes)."""
    from src.engine.tables_llm import vision_tables_consensus
    from src.engine.client_map import statement_of
    bad = {(f["scope"], f["stmt"]) for f in findings if f.get("kind") == "identity"}
    if not bad:
        return rows, []
    # the Ind-AS raw entry for each flagged (scope, statement)
    target = {}
    for i, (pg, n, ti, sc, sec, g) in enumerate(rows):
        st = statement_of(sec, ti)
        if "ifrs" not in f"{sec} {ti}".lower() and (sc, st) in bad and (sc, st) not in target:
            target[(sc, st)] = i
    out, notes = list(rows), []
    for (scope, stmt), idx in target.items():
        pg, n, ti, sc, sec, cur_g = out[idx]
        kind = _stmt_kind(sec, ti)
        pages = _locate_pages(pdf_path, kind, scope) if kind else []
        if not pages:
            log(f"VISION: [{scope[:4]}] {stmt} — could not locate pages; skipped")
            continue
        try:
            vts = vision_tables_consensus(pdf_path, pages, log=lambda m: log("    " + str(m)))
        except Exception:
            log("VISION: crashed —\n" + _tb.format_exc())
            continue
        # match a vision table to this statement: same kind, compatible scope,
        # most shared numbers (scope discriminator when labels alone can't)
        cur_vals = {re.sub(r"\D", "", str(c)) for r in cur_g for c in r[1:]
                    if re.search(r"\d", str(c))}
        best = None
        for t in vts:
            if statement_of(t.section, t.title) != stmt:
                continue
            if t.scope not in (scope, "unknown"):
                continue
            vv = {re.sub(r"\D", "", str(c)) for r in t.grid for c in r[1:]
                  if re.search(r"\d", str(c))}
            ov = len(cur_vals & vv)
            if best is None or ov > best[0]:
                best = (ov, t)
        if not best:
            continue
        t = best[1]
        # Two ways to use the pixel read; keep whichever ties the most printed
        # identities, preferring the LEAST invasive on ties:
        #   (A) keep the text grid (baseline),
        #   (B) reconcile the TEXT grid against the vision grid as a source —
        #       a cell-level, identity-gated correction. Fixes isolated
        #       duplicated/misread cells (e.g. a note-reference row whose Dec
        #       column was copied from the adjacent quarter) that the text-layer
        #       source could not reach because it does not print those rows,
        #       while keeping the verified text structure everywhere else.
        #   (C) replace the whole grid with the vision read — for a text grid
        #       that is structurally broken (blank subtotal, dropped column).
        from src.engine import source_align as _sa

        def _bad(g):
            return sum(1 for _n, ok in run_checks(sec, ti, g) if not ok)

        def _ok(g):
            return sum(1 for _n, ok in run_checks(sec, ti, g) if ok)

        cur_bad, cur_ok = _bad(cur_g), _ok(cur_g)
        try:
            g_rec, _rep = _sa.reconcile(cur_g, _grid_as_source_lines(t.grid),
                                        coverage=1.0)
        except Exception:
            g_rec = cur_g
        cands = [("kept text", cur_g),
                 ("cell-corrected from pixels", g_rec),
                 ("re-read whole statement from pixels", t.grid)]
        # rank by FEWEST failing identities, then MOST reconciling ones (a blank
        # subtotal leaves a check inactive on the corrupt grid; the pixel read
        # activates and passes it — same failing count, more ties), preferring
        # the least-invasive candidate on an exact tie.
        pick = min(range(len(cands)),
                   key=lambda i: (_bad(cands[i][1]), -_ok(cands[i][1]), i))
        if pick != 0 and (_bad(cands[pick][1]), -_ok(cands[pick][1])) < (cur_bad, -cur_ok):
            how, newg = cands[pick]
            nb, no = _bad(newg), _ok(newg)
            out[idx] = ((t.page or pg) if pick == 2 else pg, n, ti, scope, sec, newg)
            notes.append({"stmt": stmt, "scope": scope,
                          "note": (f"pixel re-read ({how}); "
                                   f"identities {no}/{no + nb} tie after re-read")})
            log(f"VISION: [{scope[:4]}] {stmt} — {how} "
                f"(failing {cur_bad}->{nb}, ties {cur_ok}->{no})")
        else:
            log(f"VISION: [{scope[:4]}] {stmt} — pixel read no better; kept text")
    return out, notes


def _grid_as_source_lines(vgrid):
    """Turn a vision-read grid into synthetic (x, token) source lines so the
    text→source reconcile machinery can use the PIXEL read as its source: the
    row label lands at a small x, each value column at a fixed increasing x, so
    reconcile clusters them into columns and applies its identity-gated,
    column-mapped correction exactly as it does for a real page."""
    lines = []
    for row in vgrid:
        toks = [(5.0, w) for w in str(row[0] or "").split()]
        for j, c in enumerate(row[1:], start=1):
            s = str(c or "").strip()
            if s:
                toks.append((100.0 * j, s))
        lines.append(toks)
    return lines


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
            pages = _locate_pages(pdf, kind, scope) if kind else []
            if not pages:
                log(f"   could not locate pages — statement stays ⚠-flagged")
                continue
            vt = vision_tables_consensus(pdf, pages, log=lambda m: log("    " + str(m)))
            best = None
            old_grid = grid_of(rows, page, title)
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
