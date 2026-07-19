"""Verify a cached raw extraction (output/qtr_raw/<name>.pkl) against its PDF.

Self-contained, offline, read-only — never touches the extraction pipeline.

Checks:
  1. digit-grounding — every extracted number must appear in the PDF text
     (exact, or digits-only for scanned pages with a dirty OCR layer);
  2. arithmetic identities — each statement must satisfy its own printed
     identities (P&L: PBT-tax=profit, profit+OCI=TCI; BS: A=E+L and subtotal
     sums; CF: op+inv+fin(+fx)=net change, opening+net(+fx)=closing);
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

PDF_DIR = os.path.expanduser("~/Downloads/qtr_reports")
PKL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "output", "qtr_raw")


# ---------------------------------------------------------------- basic parsing

def _num(c):
    s = str(c).strip().replace(",", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _norm(s):
    s = re.sub(r"\(.*?\)", " ", str(s).lower())
    return " ".join(re.sub(r"[^a-z ]", " ", s).split())


def _find_row(grid, must, must_not=()):
    """First row whose label contains all `must` substrings; returns its numbers."""
    for row in grid:
        label = " ".join(c for c in row if c and _num(c) is None).lower()
        if all(m in label for m in must) and not any(m in label for m in must_not):
            vals = [_num(c) for c in row]
            if any(v is not None for v in vals):
                return [v for v in vals if v is not None]
    return None


def _find_row_any(grid, alternatives, must_not=()):
    for alt in alternatives:
        must = [alt] if isinstance(alt, str) else list(alt)
        row = _find_row(grid, must, must_not)
        if row is not None:
            return row
    return None


def _eq(a, b, tol_abs=2.0):
    n = min(len(a), len(b))
    return n > 0 and all(abs(a[i] - b[i]) <= max(tol_abs, 0.005 * abs(b[i]))
                         for i in range(n))


# ---------------------------------------------------------------- identity suites

def check_results(grid):
    pbt = _find_row(grid, ["profit before tax"])
    tax = (_find_row(grid, ["total tax"]) or
           _find_row(grid, ["tax expense"], must_not=["current", "deferred"]))
    profit = _find_row_any(grid, [("profit for the period",), ("profit for the year",),
                                  ("profit for the quarter",)], must_not=["attributable"])
    oci = _find_row(grid, ["total other comprehensive"])
    tci = _find_row(grid, ["total comprehensive income"], must_not=["attributable"])
    checks = []
    if pbt and tax and profit:
        n = min(len(pbt), len(tax), len(profit))
        checks.append(("PBT - tax = profit",
                       _eq([pbt[i] - tax[i] for i in range(n)], profit[:n])))
    if profit and oci and tci:
        n = min(len(profit), len(oci), len(tci))
        checks.append(("profit + OCI = TCI",
                       _eq([profit[i] + oci[i] for i in range(n)], tci[:n])))
    return checks


def check_balance(grid):
    ta = _find_row(grid, ["total assets"], must_not=["non-current", "current"])
    tel = _find_row(grid, ["total equity and liabilities"])
    teq = _find_row(grid, ["total equity"], must_not=["liabilities"])
    tl = _find_row(grid, ["total liabilities"], must_not=["equity", "non-current", "current"])
    tnca = _find_row(grid, ["total non-current assets"])
    tca = _find_row(grid, ["total current assets"])
    tncl = _find_row(grid, ["total non-current liabilities"])
    tcl = _find_row(grid, ["total current liabilities"])
    checks = []
    if ta and tel:
        checks.append(("assets = equity+liabilities", _eq(ta, tel)))
    if teq and tl and ta:
        n = min(len(teq), len(tl), len(ta))
        checks.append(("equity + liabilities = assets",
                       _eq([teq[i] + tl[i] for i in range(n)], ta[:n])))
    if tnca and tca and ta:
        n = min(len(tnca), len(tca), len(ta))
        checks.append(("nc + current assets = total",
                       _eq([tnca[i] + tca[i] for i in range(n)], ta[:n])))
    if tncl and tcl and tl:
        n = min(len(tncl), len(tcl), len(tl))
        checks.append(("nc + current liabilities = total",
                       _eq([tncl[i] + tcl[i] for i in range(n)], tl[:n])))
    return checks


def check_cashflow(grid):
    ncol = max(len(r) for r in grid)

    def aligned(must, must_not=()):
        """Values aligned to cell positions (None where blank) — sparse rows
        like '- | 2,768' keep their column."""
        for row in grid:
            label = " ".join(c for c in row if c and _num(c) is None).lower()
            if all(m in label for m in must) and not any(m in label for m in must_not):
                vals = [_num(row[j]) if j < len(row) else None for j in range(1, ncol)]
                if any(v is not None for v in vals):
                    return vals
        return None

    def aligned_any(alts, must_not=()):
        for alt in alts:
            r = aligned([alt] if isinstance(alt, str) else list(alt), must_not)
            if r is not None:
                return r
        return None

    def section_total(word):
        return aligned_any([("net cash", word), ("net " + word,),
                            (word + " activities",)], must_not=["before"])

    op, inv, fin = section_total("operating"), section_total("investing"), section_total("financing")
    fx = aligned_any([("effect of exchange",), ("exchange rate", "cash"),
                      ("effect of foreign",), ("exchange difference",),
                      ("net foreign exchange",)],
                     must_not=["unrealised", "unrealized"])
    net = aligned_any(["net increase", "net decrease", "net cash flow",
                       "net cash inflow", "net change in cash"],
                      must_not=["operating", "investing", "financing"])
    beg = aligned(["at the beginning"], must_not=["overdraft"])
    end = aligned(["at the end"])
    # opening-balance adjustment rows some filings print between net and closing
    adj = [r for r in (aligned(["bank overdraft", "beginning"]),
                       aligned(["cash", "acquired", "acquisition"], must_not=["payment", "business"]))
           if r]

    def cols(*rows):
        return [j for j in range(ncol - 1)
                if all(r[j] is not None for r in rows if r is not None)]

    checks = []
    if op and inv and fin and not net:
        # some filings (e.g. TCS) print no explicit net-change row: derive it
        net = [op[j] + inv[j] + fin[j] if j in cols(op, inv, fin) else None
               for j in range(ncol - 1)]
    if op and inv and fin and net:
        ok = True
        for j in cols(op, inv, fin, net):
            s = op[j] + inv[j] + fin[j]
            tol = max(0.05, 0.005 * abs(net[j]))
            f = fx[j] if fx and fx[j] is not None else 0.0
            if not (abs(s - net[j]) <= tol or abs(s + f - net[j]) <= tol):
                ok = False
                break
        checks.append(("op+inv+fin(+fx) = net change", ok))
    if beg and end and net:
        ok = True
        for j in cols(beg, end, net):
            tol = max(0.05, 0.005 * abs(end[j]))
            a = sum(r[j] for r in adj if r[j] is not None)
            f = fx[j] if fx and fx[j] is not None else 0.0
            if not (abs(beg[j] + net[j] + a - end[j]) <= tol
                    or abs(beg[j] + net[j] + a + f - end[j]) <= tol):
                ok = False
                break
        checks.append(("opening + net(+fx+adj) = closing", ok))
    return checks


def _suite_for(section, title):
    s = f"{section} {title}".lower()
    if "cash flow" in s:
        return check_cashflow
    if "assets and liabilities" in s or "balance" in s:
        return check_balance
    if "results" in s and "segment" not in s:
        return check_results
    return None


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
    exact, digits = _pdf_number_sets(os.path.join(PDF_DIR, f"{name}.pdf"))
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
    ties = fails = 0
    fail_lines = []
    for _p, _n, title, scope, section, grid in tables:
        suite = _suite_for(section, title)
        if suite is None:
            continue
        for cname, ok in suite(grid):
            ties += ok
            fails += not ok
            if not ok:
                fail_lines.append(f"      FAIL [{scope[:4]}] {title[:52]}: {cname}")
    pct = 100 * grounded / checked if checked else 100.0
    print(f"{name}: {grounded}/{checked} numbers grounded ({pct:.1f}%) | "
          f"identities {ties}/{ties + fails} tie")
    for line in fail_lines:
        print(line)
    return fails == 0


# ---------------------------------------------------------------- cross-quarter

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
