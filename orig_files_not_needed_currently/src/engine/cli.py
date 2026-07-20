"""CLI for the extraction engine.

    python -m src.engine.cli REPORT.pdf --statements
    python -m src.engine.cli REPORT.pdf --ask "What is the dividend per share?"
    python -m src.engine.cli REPORT.pdf --kpis
    python -m src.engine.cli REPORT.pdf --all
"""
from __future__ import annotations

import argparse
import sys

from src.engine.report import Report


def _print_statements(r: Report) -> None:
    print(f"  format: {r.format_label}" + ("  [image-only PDF -> vision]" if r.image_only else ""))
    s = r.statements()
    for k, name in (("bs", "Balance Sheet"), ("pl", "Profit & Loss"), ("cf", "Cash Flow")):
        st = s[k]
        if st.validated:
            print(f"  {name:14} ✓ tie-out  p{st.page}  ({st.scope or '?'}, {st.unit or '?'})  anchor={st.anchor:,.2f}")
        else:
            print(f"  {name:14} ✗ {st.status}")
    print(f"  fully validated: {r.fully_validated}")


def _print_answer(label: str, a) -> None:
    if not a.found:
        print(f"  {label:22} — not found")
        return
    mark = "✓" if a.grounded else "≈"
    val = f"{a.value}{(' ' + a.unit) if a.unit else ''}" if a.value else a.answer
    loc = f"p{a.page}" if a.page else "?"
    print(f"  {label:22} {mark} {val}  ({loc})")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine", description="Indian annual-report extraction engine")
    ap.add_argument("pdf")
    ap.add_argument("--statements", action="store_true", help="validate the 3 primary statements")
    ap.add_argument("--ask", metavar="QUESTION", help="answer one free-form question")
    ap.add_argument("--kpis", action="store_true", help="extract sector KPIs")
    ap.add_argument("--datapoints", metavar="SCOPE", nargs="?", const="standalone",
                    help="extract taxonomy datapoints (default scope: standalone)")
    ap.add_argument("--all", action="store_true", help="statements + KPIs")
    args = ap.parse_args(argv)

    r = Report(args.pdf)
    did = False
    if args.statements or args.all:
        print("STATEMENTS:"); _print_statements(r); did = True
    if args.ask:
        print("ANSWER:"); a = r.ask(args.ask); print(f"  Q: {args.ask}"); _print_answer("answer", a)
        if a.found and a.quote:
            print(f"  quote: {a.quote.strip()[:200]}")
        did = True
    if args.kpis or args.all:
        print(f"KPIs ({r.format_label}):")
        for key, a in r.kpis().items():
            _print_answer(key, a)
        did = True
    if args.datapoints:
        print(f"DATAPOINTS ({args.datapoints}):")
        dps = r.datapoints(args.datapoints)
        for key, dp in dps.items():
            mark = ("✓" if dp.grounded else "≈") if dp.present else "·"
            val = dp.value if dp.present else "absent"
            print(f"  {mark} {key[:46]:46} {val}")
        did = True
    if not did:
        ap.error("nothing to do: pass --statements, --ask, --kpis or --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
