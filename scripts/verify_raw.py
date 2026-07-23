"""Verify a cached raw extraction (output/qtr_raw/<name>.pkl) against its PDF.

Self-contained, offline, read-only — never touches the extraction pipeline.
The arithmetic identity suites live in src/engine/identities.py — the SAME
module the extractor, the repair loop and the mapping layer verify with.

Checks:
  1. digit-grounding — every extracted number must appear in the PDF text
     (exact, or digits-only for scanned pages with a dirty OCR layer);
  2. arithmetic identities + duplicate-column structure (identities.run_checks);
  3. cross-quarter consistency (--cross) — a column repeated in another
     quarter's filing must match it row by row (e.g. Q2's June-quarter column
     vs Q1; Q2's and Q4's March-2025 balance-sheet columns).

Usage: python scripts/verify_raw.py wipro_q2FY2026 [more names ...]
       python scripts/verify_raw.py --all
       python scripts/verify_raw.py --cross wipro
"""
import os
import pickle
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf

from src.engine.identities import (_num, check_balance, check_cashflow,   # noqa: F401
                                   check_results, run_checks, suite_for)

PDF_DIR = os.path.expanduser("~/Downloads/qtr_reports")
PKL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "output", "qtr_raw")

# legacy alias: repair_raw historically imported the dispatcher from here
_suite_for = suite_for


def _norm(s):
    s = re.sub(r"\(.*?\)", " ", str(s).lower())
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def _stem(norm_label):
    """Drop a trailing plural 's' from each word so labels agree across filings
    ('purchases…' vs 'purchase…', 'inventories' vs 'inventorie')."""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w
                    for w in norm_label.split())


def _uniq_stemmed(colmap):
    """Stem a {label: value} colmap's keys and KEEP ONLY labels that remain
    unique after stemming — an ambiguous label (repeated across a statement's
    sections) is dropped so it can never be matched to the wrong section."""
    seen, out = {}, {}
    for k, v in colmap.items():
        sk = _stem(k)
        seen[sk] = seen.get(sk, 0) + 1
        out[sk] = v
    return {k: v for k, v in out.items() if seen[k] == 1}


# ---------------------------------------------------------------- per-file verify

def _load(name):
    return pickle.load(open(os.path.join(PKL_DIR, f"{name}.pkl"), "rb"))


def _pdf_number_sets(pdf_path):
    doc = pymupdf.open(pdf_path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    exact, digits = set(), set()
    for m in re.finditer(r"\d[\d,]*\.?\d*", text):
        s = m.group(0).replace(",", "")
        exact.add(s)
        if "." in s:
            exact.add(s.rstrip("0").rstrip("."))
        d = re.sub(r"\D", "", m.group(0))
        if len(d) >= 4:
            digits.add(d)
    return exact, digits


def verify(name):
    tables = _load(name)
    pdf = os.path.join(PDF_DIR, f"{name}.pdf")
    grounding = ""
    if os.path.exists(pdf):
        exact, digits = _pdf_number_sets(pdf)
        checked = grounded = 0
        for _p, _n, _t, _sc, _sec, grid in tables:
            for row in grid:
                for cell in row[1:]:
                    for m in re.finditer(r"\d[\d,]*\.?\d*", str(cell)):
                        v = m.group(0).replace(",", "")
                        vv = v.rstrip("0").rstrip(".") if "." in v else v
                        d = re.sub(r"\D", "", m.group(0))
                        checked += 1
                        if (v in exact or vv in exact or v.lstrip("0") in exact
                                or (len(d) >= 4 and d in digits)):
                            grounded += 1
        pct = 100 * grounded / checked if checked else 100.0
        grounding = f"{grounded}/{checked} numbers grounded ({pct:.1f}%)"
    else:
        grounding = "PDF not found — identities only"
    ties = fails = 0
    fail_lines = []
    for _p, _n, title, scope, section, grid in tables:
        for cname, ok in run_checks(section, title, grid):
            ties += ok
            fails += not ok
            if not ok:
                fail_lines.append(f"      FAIL [{scope[:4]}] {title[:52]}: {cname}")
    print(f"{name}: {grounding} | identities {ties}/{ties + fails} tie")
    for line in fail_lines:
        print(line)
    return fails == 0


# ---------------------------------------------------------------- cross-quarter

def cross_quarter_flags(raw_name: str, rows) -> list[dict]:
    """Job-time cross-quarter consistency: a period column REPEATED from the
    company's other cached filings must match it row by row (Q2 reprints Q1's
    June quarter; Q2 and Q4 both print the March balance sheet). Mismatches
    are review FLAGS, never blocks — a genuine restatement looks the same.

    Returns [{stmt, scope, note}] ready to attach to MappedStatement flags."""
    from src.engine.client_map import _parse_period
    comp = raw_name.split("_")[0]
    sibs = [f[:-4] for f in os.listdir(PKL_DIR)
            if f.endswith(".pkl") and f.startswith(comp + "_") and f[:-4] != raw_name
            and not os.path.exists(os.path.join(PKL_DIR, f"{f[:-4]}.review"))]
    if not sibs:
        return []

    from src.engine.client_map import statement_of
    def grids(rws):
        out = {}
        for _p, _n, t, sc, sec, g in rws:
            s = f"{sec} {t}".lower()
            if "ifrs" in s or sc not in ("standalone", "consolidated"):
                continue
            # most-specific-first (statement_of): a balance sheet titled
            # '… Financial Results — Balance Sheet' must not be filed as income
            stmt = statement_of(sec, t)
            if stmt in ("income", "balance"):
                out.setdefault((stmt, sc), g)
        return out

    def cols_by_period(g, stmt):
        """{period key: column}. Income periods need a KNOWN span — a '?' span
        can be FY in one filing and a quarter in the other (same end date), so
        matching on it compares different periods. Balance sheets are
        point-in-time: the end date alone identifies the column."""
        out = {}
        for j in range(1, max(len(r) for r in g)):
            text = " ".join(str(r[j]) for r in g[:4] if j < len(r))
            p = _parse_period(text, j)
            if not p.end:
                continue
            if stmt == "balance":
                out.setdefault(("as at", p.end), j)
            elif p.span != "?":
                out.setdefault((p.span, p.end), j)
        return out

    mine = grids(rows)
    notes = []
    for sib in sibs:
        try:
            rws = pickle.load(open(os.path.join(PKL_DIR, f"{sib}.pkl"), "rb"))
        except Exception:
            continue
        # Cross-quarter evidence is useful only from a verified sibling.  An
        # internally failing extraction must not make a clean current filing
        # look wrong merely because the same period appears in both.
        if any(not ok for _p, _n, t, _sc, sec, g in rws
               for _name, ok in run_checks(sec, t, g)):
            continue
        theirs = grids(rws)
        for (stmt, sc), g in mine.items():
            g2 = theirs.get((stmt, sc))
            if g2 is None:
                continue
            c2 = cols_by_period(g2, stmt)
            for per, j1 in cols_by_period(g, stmt).items():
                j2 = c2.get(per)
                if j2 is None:
                    continue
                a, b = _colmap(g, j1), _colmap(g2, j2)
                common = [k for k in a if k in b]
                bad = [(k, a[k], b[k]) for k in common if abs(a[k] - b[k]) > 1.0]
                if len(common) >= 5 and bad:
                    ex = "; ".join(f"'{k[:32]}' {x:g} vs {y:g}" for k, x, y in bad[:3])
                    notes.append({"stmt": stmt, "scope": sc,
                                  "note": (f"{per[0]} {per[1]} disagrees with "
                                           f"{sib} on {len(bad)}/{len(common)} repeated "
                                           f"rows ({ex}) — review (misread or restatement)")})
    return notes


def reconcile_comparatives(raw_name: str, rows, log=None):
    """Correct a COMPARATIVE (prior-period) column of a filing from the same
    company's OTHER filings, which reported that period too.

    A quarterly filing reprints prior periods as comparatives; the SAME period
    is a CURRENT column in an earlier filing (higher confidence) or a
    comparative there too. When independent filings corroborate a value that
    DISAGREES with this filing's comparative cell — the classic scanned-filing
    misread — we adopt the corroborated value. Vote weight is 2 when a sibling
    reported the period as its OWN current (latest) column, else 1; a cell is
    replaced only at total agreeing weight >= 2 (two comparatives, or one
    authoritative current reading). Replacements are applied per column and
    kept only if the statement's identity suite is NO WORSE afterwards, so a
    correction can never break a statement that already tied. The CURRENT
    (latest-date) column is never touched — only prior comparatives. Every
    change is returned as a transparent review note (a genuine restatement is
    rare and stays visible, never silently rewritten).

    Returns (new_rows, notes)."""
    from src.engine.client_map import _parse_period, statement_of
    comp = raw_name.split("_")[0]
    sibs = [f[:-4] for f in os.listdir(PKL_DIR)
            if f.endswith(".pkl") and f.startswith(comp + "_") and f[:-4] != raw_name]
    if not sibs:
        return rows, []

    def _pcols(g):
        """[(colidx, span, end)] for a grid, plus max end date and end-counts."""
        out, ends = [], {}
        for j in range(1, max(len(r) for r in g)):
            text = " ".join(str(g[i][j]) for i in range(min(4, len(g))) if j < len(g[i]))
            p = _parse_period(text, j)
            if not p.end:
                continue
            out.append((j, p.span, p.end))
            ends[p.end] = ends.get(p.end, 0) + 1
        return out, (max(ends) if ends else None), ends

    # gather sibling readings: {(stmt,scope): [(end, is_current, uniq_stemmed_colmap)]}
    sib_data = {}
    for sib in sibs:
        try:
            rws = pickle.load(open(os.path.join(PKL_DIR, f"{sib}.pkl"), "rb"))
        except Exception:
            continue
        for _p, _n, t, sc, sec, g in rws:
            s = f"{sec} {t}".lower()
            if "ifrs" in s or sc not in ("standalone", "consolidated"):
                continue
            stmt = statement_of(sec, t)
            if stmt not in ("income", "balance", "cashflow"):
                continue
            cols, cur_end, ends = _pcols(g)
            lct = {}
            for row in g:
                lct[_norm(row[0])] = lct.get(_norm(row[0]), 0) + 1
            for j, span, end in cols:
                cm = {k: v for k, v in _colmap(g, j).items() if lct.get(k, 0) == 1}
                sib_data.setdefault((stmt, sc), []).append(
                    (end, end == cur_end, _uniq_stemmed(cm)))

    notes, new_rows, changed = [], [], 0
    for r in rows:
        page, n, title, scope, section, grid = r
        s = f"{section} {title}".lower()
        stmt = statement_of(section, title)
        if "ifrs" in s or scope not in ("standalone", "consolidated") \
                or stmt not in ("income", "balance", "cashflow") \
                or (stmt, scope) not in sib_data:
            new_rows.append(r)
            continue
        base_fail = sum(1 for _c, ok in run_checks(section, title, grid) if not ok)
        # BLAST-RADIUS GUARD: a statement whose printed arithmetic already ties
        # is never a candidate — there is nothing to fix and the strictly-better
        # gate below could not adopt a change anyway. This makes the whole
        # reconciliation a hard no-op for every healthy statement (the working
        # digital filings), so it can only ever act on a demonstrably-broken
        # comparative — the case it exists for.
        if base_fail == 0:
            new_rows.append(r)
            continue
        cols, cur_end, _ends = _pcols(grid)
        g2 = [list(row) for row in grid]
        stmt_changes = []
        sib_reads_all = sib_data[(stmt, scope)]
        # unique stemmed labels of THIS grid (drop labels repeated across
        # sections, e.g. a balance sheet's two 'other financial assets')
        cur_label_ct = {}
        for row in grid:
            cur_label_ct[_stem(_norm(row[0]))] = cur_label_ct.get(_stem(_norm(row[0])), 0) + 1
        for j, span, end in cols:
            if end == cur_end:               # never touch the current period
                continue
            cur_col = {}
            for row in grid:
                lab = _stem(_norm(row[0]))
                v = _num(row[j]) if j < len(row) else None
                if lab and v is not None and cur_label_ct.get(lab, 0) == 1:
                    cur_col[lab] = v
            # DATA-DRIVEN period matching: a sibling column reports the SAME
            # period only when it ENDS on the same date AND most of its shared
            # line-items already AGREE in value. This proves period identity
            # from the data itself, so an ambiguous '?' span (a Mar-25 column
            # that could be the quarter or the year) can no longer be matched to
            # the wrong period — a quarter's values never agree with a year's.
            matched = []
            for sib_end, is_cur, cmap in sib_reads_all:
                if sib_end != end:
                    continue
                shared = [k for k in cur_col if k in cmap]
                if len(shared) < 5:
                    continue
                agree = sum(1 for k in shared
                            if abs(cur_col[k] - cmap[k]) <= max(1.0, 0.005 * abs(cmap[k])))
                if agree >= 0.6 * len(shared):
                    matched.append((is_cur, cmap))
            if not matched:
                continue
            for ri, row in enumerate(grid):
                lab = _stem(_norm(row[0]))
                cur = _num(row[j]) if j < len(row) else None
                if not lab or cur is None or cur_label_ct.get(lab, 0) != 1:
                    continue
                votes = {}
                for is_cur, cmap in matched:
                    if lab in cmap:
                        v = round(cmap[lab], 2)
                        votes[v] = votes.get(v, 0) + (2 if is_cur else 1)
                if not votes:
                    continue
                best, w = max(votes.items(), key=lambda it: it[1])
                if w >= 2 and abs(best - cur) > 1.0 and votes.get(round(cur, 2), 0) < w:
                    orig = str(row[j])
                    g2[ri][j] = f"({abs(best):g})" if best < 0 else f"{best:g}"
                    stmt_changes.append((row[0][:40], span, end, orig, best))
        if stmt_changes:
            new_fail = sum(1 for _c, ok in run_checks(section, title, g2) if not ok)
            # adopt ONLY when the correction makes the statement's arithmetic
            # STRICTLY better — i.e. a printed identity that was broken now
            # ties. A comparative that already reconciles internally is left
            # alone (a disagreement there is a restatement or a wash, and the
            # cross-quarter check still flags it); this is what keeps the
            # reconciliation from rewriting correctly-read digital comparatives.
            if new_fail < base_fail:
                changed += len(stmt_changes)
                for lbl, span, end, orig, best in stmt_changes:
                    ex = f"'{lbl}' {orig}→{best:g} ({span} ended {end})"
                    if log:
                        log(f"{raw_name}: [{scope[:4]}] {stmt} comparative reconciled — {ex}")
                    notes.append({"stmt": stmt, "scope": scope,
                                  "note": (f"comparative {ex} — corrected from the company's "
                                           "other filings (prior-period misread on this scan)")})
                r = (page, n, title, scope, section, g2)
        new_rows.append(r)
    return new_rows, notes


def _grid(name, kind, scope):
    for _p, _n, title, sc, section, grid in _load(name):
        s = f"{section} {title}".lower()
        if sc != scope or "ifrs" in s:
            continue
        if kind == "pnl" and "results" in s and "segment" not in s:
            return grid
        if kind == "bs" and ("assets and liabilities" in s or "balance" in s):
            return grid
    return None


def _col(grid, *pats):
    ncol = max(len(r) for r in grid)
    for j in range(1, ncol):
        text = " ".join(str(r[j]) for r in grid[:4] if j < len(r)).lower()
        if all(re.search(p, text) for p in pats):
            return j
    return None


def _colmap(grid, j):
    out = {}
    for r in grid:
        v = _num(r[j]) if j < len(r) else None
        if v is not None:
            k = _norm(r[0])
            if k and k not in out:
                out[k] = v
    return out


def _compare(tag, a, b):
    common = [k for k in a if k in b]
    bad = [(k, a[k], b[k]) for k in common if abs(a[k] - b[k]) > 1.0]
    print(f"  {tag}: {len(common) - len(bad)}/{len(common)} rows match")
    for k, x, y in bad[:8]:
        print(f"       MISMATCH '{k[:44]}': {x} vs {y}")
    return not bad


def cross(company):
    """Columns repeated across quarterly filings must agree."""
    q = {n: f"{company}_{n}FY2026" for n in ("q1", "q2", "q3", "q4")}
    ok = True
    for scope in ("standalone", "consolidated"):
        pn = {n: _grid(q[n], "pnl", scope) for n in q}
        if not all(pn.values()):
            continue
        print(f"===== {company} {scope} =====")
        for tag, (na, pa), (nb, pb) in [
                ("P&L: q2 repeats q1 June qtr", ("q1", r"june 30, 2025"), ("q2", r"june 30, 2025")),
                ("P&L: q3 repeats q2 Sep qtr", ("q2", r"september 30, 2025"), ("q3", r"september 30, 2025")),
                ("P&L: q4 repeats q3 Dec qtr", ("q3", r"december 31, 2025"), ("q4", r"december 31, 2025"))]:
            ja, jb = _col(pn[na], pa), _col(pn[nb], pb)
            if ja and jb:
                ok &= _compare(tag, _colmap(pn[na], ja), _colmap(pn[nb], jb))
        b2, b4 = _grid(q["q2"], "bs", scope), _grid(q["q4"], "bs", scope)
        if b2 is not None and b4 is not None:
            j2, j4 = _col(b2, r"march 31, 2025"), _col(b4, r"march 31, 2025")
            if j2 and j4:
                ok &= _compare("BS: q2 and q4 March-2025 columns",
                               _colmap(b2, j2), _colmap(b4, j4))
    return ok


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "--cross":
        sys.exit(0 if cross(args[1]) else 1)
    if args == ["--all"]:
        args = sorted(f[:-4] for f in os.listdir(PKL_DIR) if f.endswith(".pkl"))
    all_ok = all([verify(n) for n in args])
    sys.exit(0 if all_ok else 1)
