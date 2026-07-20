"""Apply taxonomy_audit.csv to definitions.yaml WITH expert review (not blind).

Rule: accept an audit suggestion only when it is an established Ind AS / Schedule III
fact (validated by domain knowledge and, where relevant, the actual reports). DEFER any
suggestion that depends on the taxonomy's intended use-case (ChatGPT is guessing) —
keep the original definition and flag it for the user to decide.
"""
from __future__ import annotations

import csv
import os
import re
import shutil

import yaml

TAX = os.path.join(os.path.dirname(__file__), "..", "taxonomy", "definitions.yaml")
AUDIT = os.path.expanduser("~/Downloads/taxonomy_audit.csv")

# Use-case-judgment items: ChatGPT is guessing intent. Keep original; flag for user.
DEFER = {
    "Other CWIP":
        "audit would mark Not disclosed unless a line is literally 'Other' — may suppress total CWIP if that's intended",
    "No Of Shares bought back or treasury shares":
        "buyback-during-year vs treasury-held-at-year-end — needs user's intent",
    "Other Current Liabilities_Employees Stock Options Outstanding":
        "audit returns Not-disclosed-as-liability; ESOP is usually an equity reserve — user may want the reserve",
}


def _norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def main():
    audit = {_norm(r.get("key")): r for r in csv.DictReader(open(AUDIT))}
    data = yaml.safe_load(open(TAX))
    accepted, deferred = [], []
    for it in data["items"]:
        a = audit.get(_norm(it["key"]))
        if not a or a.get("severity", "").strip().upper() == "OK":
            continue
        if it["key"] in DEFER:
            it["_review"] = f"DEFER ({a.get('severity','').strip()}): {DEFER[it['key']]}"
            deferred.append(it["key"])
            continue
        sc = (a.get("suggested_concept") or "").strip()
        if sc:
            it["concept"] = sc
        ch = (a.get("suggested_column_hint") or "").strip()
        if ch:
            it["column_hint"] = ch
        sa = (a.get("suggested_aliases") or "").strip()
        if sa:
            new = [x.strip() for x in re.split(r"[;|]", sa) if x.strip()]
            it["aliases"] = list(dict.fromkeys([*(it.get("aliases") or []), *new]))
        it["_review"] = f"ACCEPTED ({a.get('severity','').strip()}) — accounting-validated"
        accepted.append(it["key"])

    shutil.copy(TAX, TAX + ".bak")
    with open(TAX, "w") as fh:
        fh.write("# definitions.yaml — audit applied with EXPERT REVIEW (see _review). "
                 "DEFER items kept original pending user intent.\n\n")
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=100)

    print(f"ACCEPTED (validated): {len(accepted)}")
    print(f"DEFERRED (need user intent): {len(deferred)}")
    for k in deferred:
        print(f"  - {k}\n      {DEFER[k]}")


if __name__ == "__main__":
    main()
