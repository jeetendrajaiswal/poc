"""Phase-0 thin-slice baseline.

Validate-first: simplest local-locate -> read candidate pages -> one structured
model call per (item, scope). Produces real values + evidence to eyeball, and saves
output/<company>_thin.json. NO vector store, NO dual-model, NO self-correction yet —
this measures the floor so we add machinery only where it's needed.

Usage:  .venv/bin/python -m src.phase0 hindalco
"""
from __future__ import annotations

import json
import os
import sys

import yaml

from src.config import config
from src.locate import locate
from src.read_vision import read_value
from src.structure_map import build_structure_map

PDF_DIR = os.path.expanduser("~/Downloads/")
TAX_DIR = os.path.join(os.path.dirname(__file__), "..", "taxonomy")
DEFS = os.path.join(TAX_DIR, "definitions_thin.yaml")
PROFILES = os.path.join(TAX_DIR, "company_profiles.yaml")
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")

def run(company: str, defs_path: str = DEFS, out_suffix: str = "_thin", pdf_dir: str = PDF_DIR):
    pdf = os.path.join(pdf_dir, f"{company}.pdf")
    if not os.path.exists(pdf):
        sys.exit(f"PDF not found: {pdf}")
    defs = yaml.safe_load(open(defs_path))["items"]
    profile = yaml.safe_load(open(PROFILES)).get("companies", {}).get(company, {})
    na_items = set(profile.get("na_items", []))
    print(f"Building structure map for {company} "
          f"(sector={profile.get('sector','?')}, order={profile.get('scope_order','auto')}) ...", flush=True)
    sm = build_structure_map(
        pdf,
        standalone_bs=profile.get("standalone_bs"),
        consolidated_bs=profile.get("consolidated_bs"),
    )
    print(f"  {sm.page_count} pages | model={config.model_default}\n", flush=True)

    print(f"  scopes available: standalone={sm.has_standalone} consolidated={sm.has_consolidated}\n", flush=True)

    # Size gate: a filing small enough to fit in context (e.g. a quarterly results filing) is read
    # in ONE whole-document call — far cheaper than ~114 per-item reads, same recall (validated on
    # Reliance Q4: 1 call / $0.11 vs 112 reads / $1.19). Big docs (annual reports, large quarterly
    # booklets like Infosys' 379-page Q4) exceed the gate and stay on the locate + per-item path
    # below, byte-for-byte unchanged.
    est_tokens = sum(len(sm.text(p) or "") for p in range(1, sm.page_count + 1)) // 4
    if sm.page_count <= 60 and est_tokens <= 60_000:
        from src import section_batch
        print(f"  small filing (~{est_tokens:,} tok, {sm.page_count} pp) -> whole-doc single-call extraction\n",
              flush=True)
        results = section_batch.whole_doc_extract(pdf, sm, defs, na_items)
        os.makedirs(OUT_DIR, exist_ok=True)
        out = os.path.join(OUT_DIR, f"{company}{out_suffix}.json")
        json.dump(results, open(out, "w"), indent=2)
        found = sum(1 for r in results if r.get("found"))
        print(f"\nFound {found}/{len(results)} | saved -> {out}  (whole-doc)")
        return results, out

    results = []
    for item in defs:
        # Sector-driven applicability: not-applicable items are N/A, never a miss.
        if item["key"] in na_items:
            results.append({"key": item["key"], "tier": item.get("tier",""), "scope": "both",
                            "status": "N/A (sector)", "found": False})
            print(f"  [N/A] {item['key'][:42]:42} | not applicable for sector")
            continue
        want = (
            ["standalone", "consolidated"]
            if item["scope"] == "both"
            else [item["scope"]]
        )
        for scope in want:
            # Skip a scope the company does not report (N/A, not a miss).
            if scope == "consolidated" and not sm.has_consolidated:
                results.append({"key": item["key"], "tier": item.get("tier",""), "scope": scope,
                                "status": "N/A (no consolidated statements)", "found": False})
                print(f"  [N/A] {item['key'][:42]:42} | {scope:12} | no consolidated statements")
                continue

            cands = locate(sm, item["aliases"], scope, item.get("column_hint"), item.get("location_hint"))
            pages = [c.page for c in cands]
            res = read_value(
                pdf, pages,
                key=item["key"], definition=item["concept"],
                aliases=item["aliases"], column_hint=item.get("column_hint"),
                scope=scope,
            )
            scope_match = res.get("observed_scope") in (scope, "unknown")
            row = {
                "key": item["key"], "tier": item.get("tier",""), "scope": scope,
                "candidate_pages": pages, "scope_match": scope_match, **res,
            }
            results.append(row)
            mark = "OK " if res.get("found") else "-- "
            flag = "" if scope_match else " ⚠SCOPE"
            val = (res.get("value") or "")[:22]
            print(f"  [{mark}] {item['key'][:42]:42} | {scope:12} | {val:22} | "
                  f"obs={res.get('observed_scope','?')[:4]} p{pages[:2]} "
                  f"conf={res.get('confidence',0):.2f}{flag}")

    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, f"{company}{out_suffix}.json")
    json.dump(results, open(out, "w"), indent=2)
    found = sum(1 for r in results if r.get("found"))
    print(f"\nFound {found}/{len(results)} | saved -> {out}")
    return results, out


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "hindalco")
