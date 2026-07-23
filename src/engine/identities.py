"""The ONE arithmetic-identity suite for extracted financial statements.

Every layer of the pipeline verifies against THIS module — extraction
(filing_chat), the offline verifier (scripts/verify_raw.py), the repair loop
(scripts/repair_raw.py), the mapping layer (client_map) and the post-write
deliverable check (scripts/verify_delivered.py). One suite, one set of
tolerances, so a statement can never pass at one layer and silently fail the
same identity at another.

Checks are derived from what the statement itself PRINTS:
  results     : revenue + other income = total income; expense components =
                total expenses; PBT − tax = profit; profit + OCI = TCI;
                OCI components = total OCI
  balance     : assets = equity + liabilities; nc + current = totals (both sides)
  cash flow   : op + inv + fin (+fx) = net change; opening + net (+fx+adj) =
                closing; PBT + adjustments = operating profit before WC changes
  segment     : total segment revenue − inter-segment revenue = net revenue
  all         : no period column may repeat a neighbour (a transcription that
                copies one period's values into another is internally
                consistent, so only this structural check can see it)
"""
from __future__ import annotations

import re


# ---------------------------------------------------------------- parsing

_GROUPED_NUMBER = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}|\d+)"
    r"(?:\.\d+)?$"
)
_SPACE_GROUPED_NUMBER = re.compile(
    r"-?(?:\d{1,3}(?:\s+\d{3})+|\d{1,2}(?:\s+\d{2})+\s+\d{3})"
    r"(?:\.\d+)?$"
)


def _num(c):
    s = str(c).strip()
    # a value in parentheses is negative; tolerate an OCR-mangled CLOSING paren
    # ('(2,705' or '(17,253`' for '(2,705)') so the cell still parses — mirrors
    # client_map._num so the identity suite and the delivered numbers agree.
    neg = s.startswith("(") and (s.endswith(")") or bool(re.search(r"\d[)'`\"]?$", s)))
    s = s.strip("()'`\"").replace("₹", "").strip()
    punct = re.sub(r"\s*([,.])\s*", r"\1", s)
    if punct != s and _GROUPED_NUMBER.fullmatch(punct):
        s = punct
    elif _SPACE_GROUPED_NUMBER.fullmatch(s):
        s = re.sub(r"\s+", "", s)
    s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _find_row(grid, must, must_not=()):
    """First row whose label contains all `must` substrings; returns its numbers.

    Values start AFTER the label cell: grids may lead with an enumerator
    column ('Sr. No.' 5, 7, 9 ...) whose numbers are not data."""
    for row in grid:
        label = " ".join(c for c in row if c and _num(c) is None).lower()
        sq = label.replace(" ", "")              # OCR splits words: 'exceptiona l'
        if (all(m in label or m.replace(" ", "") in sq for m in must)
                and not any(m in label or m.replace(" ", "") in sq for m in must_not)):
            li = next((j for j, c in enumerate(row)
                       if str(c).strip() and _num(c) is None), -1)
            vals = [_num(c) for c in row[li + 1:]]
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


# ---------------------------------------------------------------- results

def check_results(grid):
    pbt = _find_row(grid, ["profit before tax"], must_not=["exceptional"])
    tax = (_find_row(grid, ["total tax"]) or
           _find_row(grid, ["tax expense"], must_not=["current", "deferred"]))
    profit = _find_row_any(grid, [("profit for the period",), ("profit for the year",),
                                  ("profit for the quarter",), ("profit after tax",)],
                           must_not=["attributable", "before"])
    oci = _find_row(grid, ["total other comprehensive"])
    tci = _find_row(grid, ["total comprehensive income"], must_not=["attributable"])
    ti = _find_row(grid, ["total income"])
    rev = _find_row(grid, ["revenue from operations"])
    oi = _find_row(grid, ["other income"], must_not=["comprehensive", "total"])
    te = _find_row(grid, ["total expenses"])
    checks = []
    if ti and rev and oi:
        n = min(len(ti), len(rev), len(oi))
        checks.append(("revenue + other income = total income",
                       _eq([rev[i] + oi[i] for i in range(n)], ti[:n])))
    # total expenses = sum of the component rows printed between the income
    # block and the total-expenses row (catches component-cell misreads that
    # the profit identities cannot see)
    if te:
        comp = []
        started = False
        for row in grid:
            label = " ".join(c for c in row if c and _num(c) is None).lower()
            sq = label.replace(" ", "")
            if "totalincome" in sq or (not started and "expenses" in sq and "total" not in sq):
                started = True
                comp = []
                continue
            if "totalexpense" in sq:
                break
            if started and not label.startswith("total"):
                li = next((j for j, c in enumerate(row)
                           if str(c).strip() and _num(c) is None), -1)
                vals = [_num(c) for c in row[li + 1:]]
                if any(v is not None for v in vals):
                    comp.append(vals)
        if len(comp) >= 3:
            n = min(len(te), min(len(v) for v in comp))
            sums = [sum(v[i] for v in comp if v[i] is not None) for i in range(n)]
            checks.append(("expense components = total expenses",
                           _eq(sums, te[:n])))
    if pbt and tax and profit:
        n = min(len(pbt), len(tax), len(profit))
        checks.append(("PBT - tax = profit",
                       _eq([pbt[i] - tax[i] for i in range(n)], profit[:n])))
    if profit and oci and tci:
        n = min(len(profit), len(oci), len(tci))
        checks.append(("profit + OCI = TCI",
                       _eq([profit[i] + oci[i] for i in range(n)], tci[:n])))
    # OCI COMPONENTS must sum to the Total OCI row — catches a single miscopied
    # cell that the subtotal identities cannot see
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
        # rows that are NOT OCI components even when printed inside the span:
        # the TCI line and the attribution blocks (owners / NCI / repeats of
        # profit) — some filings print those between profit and the OCI total
        _not_comp = re.compile(r"total comprehensive|attributable|owners of|"
                               r"non[\s-]*controlling|profit after tax|net profit")
        comp_rows = [r for r in grid[prof_i + 1:toci_i]
                     if any(_num(c) is not None for c in r)
                     and not _not_comp.search(
                         " ".join(c for c in map(str, r) if _num(c) is None).lower())]

        def _labelled(r):
            return any(str(c).strip() and _num(c) is None
                       and re.search(r"[a-z]", str(c), re.IGNORECASE) for c in r)
        # filings differ in what they print between Profit and Total OCI:
        # only components; components + UNLABELLED block subtotals (summing
        # everything would double-count); or only the block subtotals. The
        # identity holds if ANY one interpretation ties on EVERY column.
        candidates = [comp_rows,
                      [r for r in comp_rows if _labelled(r)],
                      [r for r in comp_rows if not _labelled(r)]]
        if len(comp_rows) >= 2 and cols:
            ok = False
            for rows_ in candidates:
                if not rows_:
                    continue
                good = True
                for j in cols:
                    s = sum((_num(r[j]) or 0) for r in rows_ if j < len(r))
                    tv = _num(total_row[j])
                    if tv is None or abs(s - tv) > max(2.0, 0.005 * abs(tv)):
                        good = False
                        break
                if good:
                    ok = True
                    break
            checks.append(("OCI components sum to Total OCI", ok))
    return checks


# ---------------------------------------------------------------- balance

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


# ---------------------------------------------------------------- segment

def check_segment(grid):
    """Validate the printed segment-revenue bridge.

    Inter-segment revenue is commonly printed as dashes.  Cell positions are
    retained so those dashes mean zero in the corresponding period rather than
    shifting later values left.
    """
    ncol = max((len(r) for r in grid), default=0)

    def _label(row):
        return " ".join(str(c) for c in row if str(c).strip()
                        and _num(c) is None).lower()

    def _aligned(row):
        li = next((j for j, c in enumerate(row)
                   if str(c).strip() and _num(c) is None
                   and re.search(r"[a-z]", str(c), re.IGNORECASE)), -1)
        if li < 0:
            return None
        return [_num(row[j]) if j < len(row) else None
                for j in range(li + 1, ncol)]

    inter_i = next((i for i, r in enumerate(grid)
                    if "inter segment revenue" in
                    re.sub(r"[^a-z0-9]+", " ", _label(r))), None)
    net_i = next((i for i, r in enumerate(grid)
                  if ("net revenue from operations" in _label(r)
                      or "net segment revenue" in _label(r))), None)
    if inter_i is None or net_i is None or inter_i >= net_i:
        return []

    total_i = next(
        (i for i in range(inter_i - 1, -1, -1)
         if (_label(grid[i]).strip() == "total"
             or "total segment revenue" in _label(grid[i]))
         and any(_num(c) is not None for c in grid[i])),
        None,
    )
    if total_i is None:
        return []

    total = _aligned(grid[total_i])
    inter = _aligned(grid[inter_i])
    net = _aligned(grid[net_i])
    if total is None or inter is None or net is None:
        return []
    cols = [j for j in range(min(len(total), len(net)))
            if total[j] is not None and net[j] is not None]
    if not cols:
        return []
    ok = all(
        abs(total[j] - abs(inter[j] or 0.0) - net[j])
        <= max(2.0, 0.005 * abs(net[j]))
        for j in cols
    )
    return [("segment total - intersegment = net revenue", ok)]


# ---------------------------------------------------------------- cash flow

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
                       aligned(["cash", "acquired", "acquisition"], must_not=["payment", "business"]),
                       aligned(["business combination"], must_not=["payment"]))
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

    # PBT + Σ(adjustments) = operating profit before working-capital changes.
    # Section subtotals stay intact even when a misread scrambles the add-back
    # rows, so this finer identity is what catches value/label misalignment in
    # the operating adjustments block. Runs ONLY when the subtotal is printed.
    def _row_idx(must, must_not=()):
        for i, row in enumerate(grid):
            label = " ".join(c for c in row if c and _num(c) is None).lower()
            if all(m in label for m in must) and not any(m in label for m in must_not):
                return i
        return None

    opbwc = aligned(["before working capital"])
    i_pbt = _row_idx(["profit before tax"])
    i_sub = _row_idx(["before working capital"])
    if opbwc and i_pbt is not None and i_sub is not None and i_pbt < i_sub:
        ok = True
        for j in range(ncol - 1):
            if opbwc[j] is None:
                continue
            tot, seen = 0.0, False
            for r in grid[i_pbt:i_sub]:          # PBT row + all adjustment rows
                v = _num(r[j + 1]) if j + 1 < len(r) else None
                if v is not None:
                    tot += v
                    seen = True
            if not seen:
                continue
            if abs(tot - opbwc[j]) > max(0.05, 0.005 * abs(opbwc[j])):
                ok = False
                break
        checks.append(("PBT + adjustments = op profit before WC", ok))

    # cash generated from operations + every row down to the operating-cash
    # subtotal (income tax paid, and any interest/dividend rows in between) =
    # net cash from operating activities. Band-summed so intervening lines are
    # covered. Catches a mangled 'cash generated' figure (e.g. a decimal point
    # rendered as a comma, '1,833.09' -> '1,833,09' -> parsed 183309) that the
    # section subtotals do not see. Runs only when both rows are printed with
    # the subtotal below.
    cgo = aligned(["cash generated from operations"])
    i_cgo = _row_idx(["cash generated from operations"])
    i_nop = next((_row_idx([a] if isinstance(a, str) else list(a),
                           must_not=["before", "investing", "financing"])
                  for a in ("net cash generated from operating",
                            "net cash used in operating",
                            "net cash flow from operating",
                            "net cash from operating")
                  if _row_idx([a] if isinstance(a, str) else list(a),
                             must_not=["before", "investing", "financing"]) is not None),
                 None)
    if cgo and op and i_cgo is not None and i_nop is not None and i_cgo < i_nop:
        ok = True
        for j in range(ncol - 1):
            if op[j] is None or (j < len(cgo) and cgo[j] is None):
                continue
            tot, seen = 0.0, False
            for r in grid[i_cgo:i_nop]:          # cash-generated row + all below it
                v = _num(r[j + 1]) if j + 1 < len(r) else None
                if v is not None:
                    tot += v
                    seen = True
            if seen and abs(tot - op[j]) > max(0.05, 0.005 * abs(op[j])):
                ok = False
                break
        checks.append(("cash from operations + items = net operating cash", ok))
    return checks


# ---------------------------------------------------------------- structure

def dup_columns(grid) -> bool:
    """A wide-table transcription error: the model lost column alignment and
    repeated one period's values into an adjacent column. Adjacent reporting
    periods never legitimately share a RUN of 5+ identical NON-ZERO line
    items, so such a run between two value columns is a near-certain
    duplication. Arithmetic tie-outs CANNOT see this (a duplicated column is
    internally consistent) — only this structural check can."""
    def _n(c):
        s = str(c).replace(",", "").replace("(", "-").replace(")", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    ncol = max((len(r) for r in grid), default=0)
    cols = {j: [(_n(r[j]) if j < len(r) else None) for r in grid]
            for j in range(1, ncol)}
    js = list(cols)
    for i in range(len(js)):
        for k in range(i + 1, len(js)):
            a, b, run = cols[js[i]], cols[js[k]], 0
            for x, y in zip(a, b):
                if (x is not None and y is not None and abs(x) > 0.005
                        and abs(x - y) < 0.005 * max(1.0, abs(x))):
                    run += 1
                    if run >= 5:
                        return True
                else:
                    run = 0
    return False


# ---------------------------------------------------------------- dispatch

def suite_for(section, title):
    s = f"{section} {title}".lower()
    if "segment" in s:
        return check_segment
    if "cash flow" in s:
        return check_cashflow
    if "assets and liabilities" in s or "balance" in s:
        return check_balance
    if "results" in s and "segment" not in s:
        return check_results
    return None


def run_checks(section, title, grid) -> list[tuple[str, bool]]:
    """Full check list for one statement grid: its identity suite (if it has
    one) plus the duplicate-column structural check (all statements — segment
    tables can duplicate a period column too)."""
    suite = suite_for(section, title)
    checks = list(suite(grid)) if suite else []
    s = f"{section} {title}".lower()
    if suite or "segment" in s:
        checks.append(("no duplicated period column", not dup_columns(grid)))
    return checks


def failing(section, title, grid) -> list[str]:
    return [name for name, ok in run_checks(section, title, grid) if not ok]
