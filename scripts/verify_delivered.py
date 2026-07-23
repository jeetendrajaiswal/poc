"""Post-write verification of a DELIVERED client workbook (wide format).

The last line of defence: after the workbook is written, read the file back
and check the numbers the client will actually see —

  * every template-formula field whose value was REPORTED from the filing
    must equal the sum of its mapped components (when enough components are
    mapped for the sum to be meaningful);
  * Total Assets == Total Equity And Liabilities (the one identity the
    template's own formulas cannot express);
  * no statement sheet may be EMPTY when the raw extraction contains that
    statement (an empty sheet is silent data loss, the worst failure class).

Offline and deterministic. Used two ways:
  * library — webapp._run_tables_job calls verify_workbook() and stamps every
    finding onto the workbook's Review sheet before publishing;
  * CLI     — python scripts/verify_delivered.py [PATH ...|--all] audits
    existing deliverables in output/client/.
"""
import glob
import os
import pickle
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKL_DIR = os.path.join(ROOT, "output", "qtr_raw")
TEMPLATE = os.path.join(ROOT, "config", "client_template_software.xlsx")

_SHEETK = {"Income Statement": "income", "Balance Sheet": "balance",
           "Cash Flow": "cashflow"}


def _num(v):
    return v if isinstance(v, (int, float)) else None


def _close(a, b):
    return abs(a - b) <= max(1.5, 0.005 * abs(b))


def _raw_has(raw_rows, stmt, scope) -> bool:
    from src.engine.client_map import statement_of
    for _p, _n, t, sc, sec, g in raw_rows or []:
        s = f"{sec} {t}".lower()
        if sc == scope and statement_of(sec, t) == stmt and "ifrs" not in s and len(g) >= 3:
            return True
    return False


# canonical template fids for the printed CROSS-IDENTITIES (correctness checks).
# These compare reported printed totals against each other — a break means a
# value was genuinely misread or mis-mapped.
_CROSS = {
    "balance": [
        ("Assets = Equity + Liabilities", [(1, "13771")], "13776", None),
        ("Non-current + Current assets = Total assets",
         [(1, "13777"), (1, "13774")], "13771", None),
    ],
    "income": [
        ("PBT − tax = Net Profit", [(1, "296"), (-1, "318"), (-1, "231")], "269",
         [(1, "296"), (-1, "318")]),
        ("Net Profit + OCI = Total Comprehensive Income",
         [(1, "269"), (1, "16484")], "20019", None),
        ("TCI attributable (owners) = Profit + OCI attributable",
         [(1, "22299"), (1, "22291")], "22303", None),
        ("TCI attributable (NCI) = Profit + OCI attributable",
         [(1, "22298"), (1, "22290")], "22302", None),
    ],
    "cashflow": [
        ("Operating + Investing + Financing (+fx) = Net change",
         [(1, "17538"), (1, "17537"), (1, "17536"), (1, "17529")], "17541",
         [(1, "17538"), (1, "17537"), (1, "17536")]),
        ("Opening + Net change (+fx) = Closing",
         [(1, "17519"), (1, "17541"), (1, "17529")], "30371",
         [(1, "17519"), (1, "17541")]),
    ],
}

# fids that reconcile opening to closing cash between the net-change and the
# closing line (cash acquired in a business combination, bank overdrafts).
# They are OPTIONAL: added to a cash-flow identity when the filing prints them,
# never required, so a filing without them keeps the plain opening+net=closing
# check.
_CF_OPTIONAL_ADJ = ("30513",)


def _printed_digitset(pdf_path=None, raw_rows=None):
    """Digit-strings (>=4 digits, separator-agnostic) printed in the source.
    From the PDF text layer when available (independent), else from the raw
    extracted grids. Corruption-tolerant: '242.363' and '242,363' both -> 242363."""
    dig = set()
    text = ""
    if pdf_path and os.path.exists(os.path.expanduser(pdf_path)):
        import pymupdf
        doc = pymupdf.open(os.path.expanduser(pdf_path))
        text = "\n".join(p.get_text() for p in doc)
        doc.close()
    elif raw_rows:
        text = "\n".join(str(c) for _p, _n, _t, _s, _se, g in raw_rows
                         for row in g for c in row)
    for m in re.finditer(r"\d[\d,.]*\d|\d", text):
        d = re.sub(r"\D", "", m.group(0))
        if len(d) >= 4:
            dig.add(d)
    return dig


def _grounds(v, dig):
    """Does the value appear in the source? Try every plausible printed form —
    a printed '650.30' yields digits '65030', but the same value can also print
    as '650.3' ('6503') or the integer part; match if ANY (>=4 digits) is in
    the source digit-set. Avoids the trailing-zero mismatch that would falsely
    flag a correct decimal total."""
    a = abs(v)
    forms = {
        re.sub(r"\D", "", f"{a:.2f}"),                        # 650.30 -> 65030
        re.sub(r"\D", "", f"{a:.2f}".rstrip("0").rstrip(".")),  # -> 6503
        re.sub(r"\D", "", f"{a:.1f}"),                        # 650.3  -> 6503
        str(int(round(a))),                                   # 650
    }
    return any(len(d) >= 4 and d in dig for d in forms)


def verify_workbook(path: str, raw_rows=None, template=None, pdf_path=None) -> list[dict]:
    """Findings for one wide workbook. Each: {stmt, scope, kind, detail}.

    kind='identity' — a real value error:
      * a PRINTED CROSS-IDENTITY between reported totals is broken, OR
      * a template total's mapped components don't reconstruct it AND the
        reported total does NOT ground to the source (its digits are printed
        nowhere) — i.e. the total itself is wrong (e.g. Wipro TNCA=468).
      Both -> flagged (orange tab + Review).
    kind='empty' — a statement present in the raw extraction is missing from
      the workbook (silent data loss) -> flagged.
    kind='info' — components don't reconstruct a total but the total DOES
      ground (it is a correct printed figure; a component line just lacks a
      dedicated field). NOT a value error -> Audit note only, no flag. This is
      what stops a 100%-correct statement (TCS, HM) from being orange-tabbed.
    """
    import openpyxl
    from src.engine.client_map import load_template
    template = template or load_template(TEMPLATE)
    dig = _printed_digitset(pdf_path, raw_rows)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    findings: list[dict] = []
    for scope in ("standalone", "consolidated"):
        for disp, stmt in _SHEETK.items():
            sn = f"{disp} - {scope.title()}"
            in_raw = _raw_has(raw_rows, stmt, scope) if raw_rows is not None else None
            if sn not in wb.sheetnames or len(rows := list(wb[sn].iter_rows(values_only=True))) <= 1:
                if in_raw:
                    findings.append({"stmt": stmt, "scope": scope, "kind": "empty",
                                     "detail": f"{sn} is empty, but the statement WAS "
                                               "extracted from the filing — data was lost "
                                               "on the way to the workbook"})
                continue
            hdr = rows[0]
            pcols = [j for j, h in enumerate(hdr)
                     if isinstance(h, str) and re.search(r"\d{4}-\d{2}-\d{2}|\(as at\)|\(\d+M\)", h)]
            mcol = next((j for j, h in enumerate(hdr) if h == "Method"), None)
            byfid, method = {}, {}
            for r in rows[1:]:
                if r and r[0] is not None:
                    fid = str(r[0]).lstrip("⚠ ").strip()
                    byfid[fid] = r
                    method[fid] = str(r[mcol]) if mcol is not None and mcol < len(r) else ""
            fields = template.get((stmt, scope), [])
            row2fid = {f.row: f.fid for f in fields}

            def val(fid, j):
                r = byfid.get(str(fid))
                return _num(r[j]) if r and j < len(r) else None

            # (1) printed cross-identities between reported totals
            for name, lhs, rhs_fid, alt in _CROSS.get(stmt, []):
                if str(rhs_fid) not in byfid:
                    continue
                for j in pcols:
                    rv = val(rhs_fid, j)
                    if rv is None:
                        continue
                    # optional reconciling adjustments this filing actually
                    # prints (e.g. cash from a business combination) — added ONLY
                    # to the opening->closing identity (rhs = closing balance),
                    # never to the op+inv+fin = net-change identity
                    opt = ([(1, f) for f in _CF_OPTIONAL_ADJ
                            if str(f) in byfid and val(f, j) is not None]
                           if stmt == "cashflow" and str(rhs_fid) == "30371" else [])
                    ok, present = False, False
                    for form in (lhs, alt):
                        if not form or not all(str(f) in byfid and val(f, j) is not None
                                               for _s, f in form):
                            continue
                        present = True
                        if _close(sum(s * val(f, j) for s, f in form + opt), rv):
                            ok = True
                            break
                    if present and not ok:
                        tot = sum(s * (val(f, j) or 0) for s, f in lhs + opt)
                        findings.append({"stmt": stmt, "scope": scope, "kind": "identity",
                                         "detail": (f"{name} broken (col {j}): reported {rv:,.2f} "
                                                    f"vs {tot:,.2f} from the other reported totals")})
                        break

            # (2) template-total reconstruction, GATED ON GROUNDING: a mismatch
            # is a real error only when the reported total is not itself printed
            for f in fields:
                if not f.formula or str(f.fid) not in byfid or method.get(str(f.fid)) == "computed":
                    continue
                n_present = sum(1 for _sg, r_ in f.formula if str(row2fid.get(r_)) in byfid)
                for j in pcols:
                    tv = val(f.fid, j)
                    if tv is None:
                        continue
                    tot = have = 0
                    for sg, r_ in f.formula:
                        cv = val(row2fid.get(r_), j)
                        if cv is not None:
                            tot += sg * cv
                            have += 1
                    if have >= 2 and have >= 0.6 * max(1, n_present) and not _close(tot, tv):
                        grounded = _grounds(tv, dig)
                        findings.append({
                            "stmt": stmt, "scope": scope,
                            "kind": "info" if grounded else "identity",
                            "detail": (f"{f.name} [{f.fid}] col {j}: reported {tv:,.2f}, "
                                       f"components sum {tot:,.2f}"
                                       + ("" if grounded else
                                          " — reported total is NOT printed in the source "
                                          "(likely a misread/mis-mapped total)"))})
    wb.close()
    return findings


def annotate_findings(path: str, findings: list[dict]) -> None:
    """Stamp post-write findings onto the workbook: Review rows + orange tabs."""
    if not findings:
        return
    import openpyxl
    from src.engine.client_map import _review_append, _review_sheet
    STMT_NAME = {"income": "Income Statement", "balance": "Balance Sheet",
                 "cashflow": "Cash Flow", "segment": "Segment Finance"}
    real = [f for f in findings if f.get("kind") != "info"]   # info = Audit-only
    if not real:
        return
    wb = openpyxl.load_workbook(path)
    rv = _review_sheet(wb)
    for f in real:
        _review_append(rv, [STMT_NAME.get(f["stmt"], f["stmt"]), str(f["scope"]).title(),
                            "⚠ " + f["detail"], "", "", "", ""])
        sn = f"{STMT_NAME.get(f['stmt'], f['stmt'])} - {str(f['scope']).title()}"[:31]
        if sn in wb.sheetnames:
            wb[sn].sheet_properties.tabColor = "ED8B00"
    wb.save(path)


_PDFDIR = os.path.expanduser("~/Downloads/qtr_reports")


def _pdf_for(path: str):
    name = os.path.basename(path).rsplit(".", 1)[0]
    m = re.match(r"(.+)_(Q[1-4])(FY\\d+)$", name)
    raw = f"{m.group(1).lower()}_{m.group(2).lower()}{m.group(3)}" if m else name.lower()
    for cand in (os.path.join(_PDFDIR, f"{raw}.pdf"),
                 os.path.join(_PDFDIR, "full", f"{raw}_full.pdf")):
        if os.path.exists(cand):
            return cand
    return None


def _raw_rows_for(path: str):
    name = os.path.basename(path).rsplit(".", 1)[0]
    m = re.match(r"(.+)_(Q[1-4])(FY\d+)$", name)
    raw = f"{m.group(1).lower()}_{m.group(2).lower()}{m.group(3)}" if m else name.lower()
    p = os.path.join(PKL_DIR, f"{raw}.pkl")
    return pickle.load(open(p, "rb")) if os.path.exists(p) else None


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args == ["--all"]:
        args = sorted(glob.glob(os.path.join(ROOT, "output", "client", "*.xlsx")))
    from src.engine.client_map import load_template
    template = load_template(TEMPLATE)
    n_bad = 0
    for path in args:
        base = os.path.basename(path)
        if base.startswith("~$") or base == "REMAINING_DIFFS.xlsx":
            continue
        fs = verify_workbook(path, raw_rows=_raw_rows_for(path), template=template,
                             pdf_path=_pdf_for(path))
        if fs:
            n_bad += 1
            print(f"\n{base}: {len(fs)} finding(s)")
            for f in fs:
                print(f"   [{f['kind']}] {f['stmt']}/{f['scope']}: {f['detail']}")
        else:
            print(f"{base}: OK")
    sys.exit(1 if n_bad else 0)
