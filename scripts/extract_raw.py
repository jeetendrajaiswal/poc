"""Extract a quarterly filing's statements into output/qtr_raw/<name>.pkl.

Runs the verified original pipeline (whole-file upload + internal statement
questions with arithmetic tie-out), saves the raw tables in the canonical
6-tuple pkl format, then immediately verifies (digit-grounding + identity
suites via scripts/verify_raw.py). API cost ~$0.20-0.65 per filing.

Usage:
  python scripts/extract_raw.py <name> [<name> ...]   # e.g. mastek_q1FY2026
"""
import os
import pickle
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.engine.tables_llm import extract_tables_smart
from src.llm import usage_cost, reset_usage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = os.path.expanduser("~/Downloads/qtr_reports")
PKL_DIR = os.path.join(ROOT, "output", "qtr_raw")


def run(name: str) -> str:
    pdf = os.path.join(PDF_DIR, f"{name}.pdf")
    if not os.path.exists(pdf):
        sys.exit(f"missing PDF: {pdf}")
    reset_usage()
    from src.engine.sector_config import load_sector_assets
    sector, _template, _taxonomy = load_sector_assets()
    from src.engine.tables_llm import maybe_trim_large_filing
    pdf_in = maybe_trim_large_filing(
        pdf, log=lambda m: print(str(m), flush=True),
        extraction_policy=sector.extraction_policy)
    tables = extract_tables_smart(pdf_in, mode="quarterly",
                                  log=lambda m: print("  " + str(m), flush=True),
                                  extraction_policy=sector.extraction_policy)
    rows = [(t.page, t.n, t.title, t.scope, t.section, t.grid) for t in tables]
    out = os.path.join(PKL_DIR, f"{name}.pkl")
    pickle.dump(rows, open(out, "wb"))
    print(f"{name}: {len(rows)} tables -> {out}  (${usage_cost():.2f})", flush=True)
    return out


if __name__ == "__main__":
    names = sys.argv[1:]
    if not names:
        sys.exit(__doc__)
    for n in names:
        run(n)
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "verify_raw.py"), n])
