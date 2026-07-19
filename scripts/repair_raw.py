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
from verify_raw import _suite_for as suite_for

PDF_DIR = os.path.expanduser("~/Downloads/qtr_reports")
PKL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "output", "qtr_raw")

_PAGE_PAT = {
    "balance": r"balance sheet|assets and liabilit",
    "cashflow": r"cash flow",
    "income": r"financial results|profit and loss",
    "segment": r"segment",
}


def _failing_statements(rows):
    fails = []
    for page, n, title, scope, section, grid in rows:
        suite = suite_for(section, title)
        if suite is None:
            continue
        checks = suite(grid)
        bad = [name for name, ok in checks if not ok]
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
    """Pages whose heading names this statement (and scope when stated)."""
    doc = pymupdf.open(pdf_path)
    hits = []
    for i in range(len(doc)):
        head = " ".join(doc[i].get_text().split())[:400].lower()
        nnum = len(re.findall(r"\d[\d,]*\.?\d*", doc[i].get_text()))
        if nnum < 25 or not re.search(_PAGE_PAT[kind], head):
            continue
        if scope == "consolidated" and "consolidated" not in head:
            continue
        if scope == "standalone" and "consolidated" in head and "standalone" not in head:
            continue
        hits.append(i + 1)
    doc.close()
    return hits


def repair(name: str, cross: bool = False) -> bool:
    pdf = os.path.join(PDF_DIR, f"{name}.pdf")
    pkl = os.path.join(PKL_DIR, f"{name}.pkl")
    rows = pickle.load(open(pkl, "rb"))
    fails = _failing_statements(rows)
    if not fails:
        print(f"{name}: all identities tie — nothing to repair")
    else:
        from src.engine.tables_llm import vision_tables_consensus
        for page, title, scope, section, bad in fails:
            kind = _stmt_kind(section, title)
            print(f"{name}: REPAIRING [{scope[:4]}] '{title[:44]}' ({'; '.join(bad)[:70]})")
            pages = _locate_pages(pdf, kind, scope) if kind else []
            if not pages:
                print(f"   could not locate pages — statement stays ⚠-flagged")
                continue
            vt = vision_tables_consensus(pdf, pages, log=lambda m: print("   ", m))
            best = None
            for t in vt:
                checks = suite_for(section, title)(t.grid) if suite_for(section, title) else []
                score = sum(1 for _n, ok in checks if ok) - sum(1 for _n, ok in checks if not ok)
                if best is None or score > best[0]:
                    best = (score, t)
            if best is None:
                print("   vision produced nothing — statement stays ⚠-flagged")
                continue
            _sc, t = best
            new_rows = []
            replaced = False
            for r in rows:
                if r[0] == page and r[2] == title:
                    flag = "" if all(ok for _n, ok in (suite_for(section, title)(t.grid) or [])) \
                        else "  ⚠ review"
                    new_rows.append((t.page, t.n, (t.title or title) + flag, scope, section, t.grid))
                    replaced = True
                else:
                    new_rows.append(r)
            rows = new_rows
            print(f"   {'replaced from pixels' if replaced else 'no replacement made'}")
        pickle.dump(rows, open(pkl, "wb"))
        residual = _failing_statements(rows)
        print(f"{name}: after repair — {len(residual)} statement(s) still failing "
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
