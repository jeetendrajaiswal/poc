"""Debug entry point — runs the REAL pipeline so your breakpoints hit the actual code.

It just calls extract_datapoints() (the real function). Set breakpoints anywhere in
src/engine/datapoints.py (extract_datapoints, run, _reconciled_section, _extract_section,
_statement_lines, _parent, _note_pages) or src/llm.py and step with F10 / F11.

Two things keep it cheap + debuggable (set in .vscode/launch.json env, no code branching):
  - DP_MAX_WORKERS=1  -> sections run SERIALLY, so stepping doesn't jump across threads.
  - SECTION below     -> filter to one note's datapoints so only that section runs.
Set SECTION=None to run the whole scope. Change COMPANY / SCOPE / SECTION as needed.
"""
import glob
import os

from src.engine import datapoints as dp
from src.engine.index import PageIndex

COMPANY = "reliance"
SCOPE = "standalone"          # or "consolidated"
SECTION = "other_expenses"    # a key of dp.SECTIONS, or None for all sections


def find(nm):
    for b in ("~/Downloads", "~/Downloads/nifty", "~/Downloads/nifty100"):
        for p in glob.glob(os.path.expanduser(b) + "/*.pdf"):
            if os.path.basename(p)[:-4].lower() == nm.lower():
                return p
    raise FileNotFoundError(nm)


def main():
    idx = PageIndex(find(COMPANY))
    concepts = dp.load_concepts()
    if SECTION:
        concepts = [c for c in concepts if c.section == SECTION]
    print(f"Debugging {COMPANY}/{SCOPE} section={SECTION or 'ALL'}  ({len(concepts)} datapoints)")

    # >>> the REAL entry point — step into it (F11) <<<
    result = dp.extract_datapoints(idx, SCOPE, concepts)

    for k, d in result.items():
        print(f"  {k[:50]:50} value={d.value!r}  present={d.present}  conf={d.confidence}")


if __name__ == "__main__":
    main()
