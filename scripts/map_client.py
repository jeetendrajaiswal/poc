"""Map verified raw extractions to the client template workbooks.

Reads output/qtr_raw/<name>.pkl (verified raw tables), maps every statement
to the client taxonomy (LLM-assisted -> API cost ~$0.10-0.26 per filing),
and writes output/client/<NAME>_dummytest.xlsx in long format.

Usage:
  python scripts/map_client.py <name> [<name> ...]   # e.g. wipro_q4FY2026
  python scripts/map_client.py --all                 # all filings in output/qtr_raw
"""
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.engine.client_map import (load_template, load_taxonomy, map_quarter,
                                   write_client_workbook_long)
from src.llm import usage_cost, reset_usage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKL_DIR = os.path.join(ROOT, "output", "qtr_raw")
OUT_DIR = os.path.join(ROOT, "output", "client")
PDF_DIR = os.path.expanduser("~/Downloads/qtr_reports")
TEMPLATE = os.path.expanduser("~/Downloads/software/Software Template.xlsx")

_UNITS = {"lac": "lakhs", "lakh": "lakhs", "crore": "crores",
          "million": "millions", "billion": "billions", "thousand": "thousands"}


def filing_unit(pdf_path: str) -> str:
    """The filing's printed denomination ('(Rs. in lakhs)', '(₹ crore)').
    Anchored on the parenthetical unit text, which survives dirty OCR even
    when the currency symbol does not."""
    import collections
    import re
    import pymupdf
    votes = collections.Counter()
    doc = pymupdf.open(pdf_path)
    for page in doc:
        t = " ".join(page.get_text().split())
        if len(re.findall(r"\d[\d,]{2,}", t)) < 15:
            continue                              # statement pages only
        for m in re.finditer(r"\(.{0,20}\b(lakh|lac|crore|million|billion|thousand)s?\b.{0,20}\)", t, re.I):
            span = m.group(0).lower()
            if "$" in span or "usd" in span or "us$" in span:
                continue                          # foreign-currency note, not the denomination
            votes[_UNITS[m.group(1).lower()]] += 1
    doc.close()
    return votes.most_common(1)[0][0] if votes else ""

TAXONOMY = os.path.join(ROOT, "config", "client_taxonomy_software.yaml")


def run(name: str, template, taxonomy) -> str:
    cache = os.path.join(OUT_DIR, ".cache", f"{name}.pkl")
    rows = pickle.load(open(os.path.join(PKL_DIR, f"{name}.pkl"), "rb"))
    unit = filing_unit(os.path.join(PDF_DIR, f"{name}.pdf"))
    mapped = map_quarter(rows, template, taxonomy, default_unit=unit)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    pickle.dump(mapped, open(cache, "wb"))     # offline re-writes / debugging
    out = os.path.join(OUT_DIR, f"{name.upper()}_dummytest.xlsx")
    write_client_workbook_long(name.split("_")[0].upper(), mapped, template, out)
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--all" in sys.argv:
        args = sorted(fn[:-4] for fn in os.listdir(PKL_DIR)
                      if fn.endswith(".pkl") and not fn.startswith("_"))
    if not args:
        sys.exit(__doc__)
    template = load_template(TEMPLATE)
    taxonomy = load_taxonomy(TAXONOMY)
    for name in args:
        reset_usage()
        out = run(name, template, taxonomy)
        print(f"{name:22s} -> {os.path.basename(out)}  (${usage_cost():.2f})")
