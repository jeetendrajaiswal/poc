"""CLI entry point: extract the financial-parameter taxonomy from an annual report.

  .venv/bin/python -m src.run <company> [--thin] [--score GT.csv]

Pipeline (per PLAN.md):
  build structure map (auto SA/CO anchors) -> locate (alias + numeric density,
  region-scoped) -> read (gpt-5.4 vision, dense tables @400 DPI) -> verify/score.

Privacy & cleanup: the pipeline reads the PDF LOCALLY and sends only rendered page
images inline with store=False. It NEVER uploads files or creates vector stores on
OpenAI, so there is nothing persisted to delete — the strongest reading of the
"no data retained on the AI platform" requirement, satisfied by construction.
"""
from __future__ import annotations

import argparse
import os

from src import phase0, scoring

TAX_DIR = os.path.join(os.path.dirname(__file__), "..", "taxonomy")
FULL = os.path.join(TAX_DIR, "definitions.yaml")
THIN = os.path.join(TAX_DIR, "definitions_thin.yaml")


def main():
    ap = argparse.ArgumentParser(description="Annual-report taxonomy extraction (gpt-5.4 vision)")
    ap.add_argument("company", help="company key (PDF expected at ~/Downloads/<company>.pdf)")
    ap.add_argument("--thin", action="store_true", help="use the 12-item thin slice instead of the full taxonomy")
    ap.add_argument("--score", metavar="GT.csv", help="score the output against a ground-truth CSV")
    args = ap.parse_args()

    defs = THIN if args.thin else FULL
    suffix = "_thin" if args.thin else "_full"
    print(f"Taxonomy: {os.path.basename(defs)}  | privacy: store=False, no OpenAI uploads (nothing to delete)\n")

    _, out = phase0.run(args.company, defs_path=defs, out_suffix=suffix)

    if args.score:
        scoring.score(args.company, args.score, results_path=out)


if __name__ == "__main__":
    main()
