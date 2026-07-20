"""Section-window vision-batch extraction (cheaper fast-path over the per-item reader).

Idea (validated on ITC share-capital: 11 datapoints / 1 call / $0.028, columns read correctly):
  1. locate() every datapoint -> its top page, per scope (free, local).
  2. Cluster datapoints whose top pages are ADJACENT (gap <= 1) into a "section".
  3. For a dense section (>= MIN_BATCH datapoints): render the page window (cluster +/- 1)
     and ask ONE vision call for ALL its datapoints. Vision SEES the table, so it reads
     columns/rows correctly (amount-vs-count, gross-vs-accum) where flat layout-text slips.
  4. Scattered singletons, and any datapoint the batch returns found=false for, fall back to
     the proven per-item reader (read_value) -- coordinate read / rotation / self-consistency
     stay as the accuracy floor.

Privacy: page images are sent INLINE (data: URI) with store=False -- nothing is persisted on
the OpenAI side, so there is nothing to delete (mirrors the per-item vision path).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Optional

import fitz

from src import llm
from src.config import config
from src.locate import locate
from src.read_vision import (_render, _reconcile_sign, _clean_value, read_value, _NOT_FOUND,
                             _layout_text)
from src.structure_map import build_structure_map

MIN_BATCH = 3          # a section needs >= this many datapoints to be worth a vision call
MAX_WINDOW = 6         # cap pages per section window (image cost / accuracy dilution)

_DASH = {"-", "–", "—", "nil", "Nil", "NIL", ""}


def _acceptable(v: str) -> bool:
    v = (v or "").strip()
    return any(c.isdigit() for c in v) or v in _DASH


_BATCH_INSTR = """You are a senior Indian equity-research analyst. You are shown the rendered IMAGE(S) of one
annual-report section (a few consecutive pages). For EACH data point id below, read its value from the image(s).

RULES (apply independently per id):
- Map by MEANING using the concept + aliases. Respect the COLUMN HINT (e.g. most-recent-year column;
  gross carrying value vs accumulated depreciation vs net).
- Carefully distinguish a MONETARY AMOUNT (share capital, ₹ in the stated unit) from a COUNT
  (number of shares) -- they are different rows with very different magnitudes.
- value = the NUMBER ONLY: keep digit-group commas, the decimal point, and a leading minus or surrounding
  parentheses for negatives. REMOVE ₹, `, Rs, INR, unit words and footnote marks. NEVER put words in value.
- If a concept is a TOTAL/heading summed from sub-lines beneath it, return the TOTAL, not one sub-line.
- found=true ONLY if a line in THESE images genuinely denotes that concept. If it is not here, found=false
  (do NOT borrow an unrelated number). One result object per id."""

_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "found": {"type": "boolean"},
                    "value": {"type": "string"},
                    "reported_label": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["id", "found", "value", "reported_label", "evidence_quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _vision_batch(doc, window: list[int], members: list[dict]) -> dict[int, dict]:
    """One vision call: render `window` pages, ask for every member datapoint. Returns {id: rec}."""
    imgs = [_render(doc, p) for p in window if 1 <= p <= doc.page_count]
    spec = "\n".join(
        f"[id={i}] {m['key']} | {m['definition'][:90]} | HINT: {m.get('column_hint') or '-'}"
        for i, m in enumerate(members)
    )
    content: list[dict] = [{"type": "input_text", "text": _BATCH_INSTR + "\n\nDATA POINTS:\n" + spec}]
    for b in imgs:
        content.append({"type": "input_image", "image_url": f"data:image/png;base64,{b}"})
    resp = llm.client().responses.create(
        model=config.model_default,
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "section", "schema": _BATCH_SCHEMA, "strict": True}},
        reasoning={"effort": config.reasoning_effort},
        max_output_tokens=4000,
        store=False,
    )
    txt = getattr(resp, "output_text", "") or ""
    out: dict[int, dict] = {}
    if txt.strip():
        for o in json.loads(txt).get("results", []):
            out[o["id"]] = o
    return out


def _cluster(pages_for_items: list[tuple[int, dict]]) -> list[tuple[list[int], list[dict]]]:
    """Group (top_page, item) pairs into sections of ADJACENT pages (gap <= 1)."""
    by_page: dict[int, list[dict]] = defaultdict(list)
    for pg, it in pages_for_items:
        by_page[pg].append(it)
    sections: list[tuple[list[int], list[dict]]] = []
    cur_pages: list[int] = []
    cur_items: list[dict] = []
    for pg in sorted(by_page):
        if cur_pages and pg - cur_pages[-1] > 1:
            sections.append((cur_pages, cur_items))
            cur_pages, cur_items = [], []
        cur_pages.append(pg)
        cur_items.extend(by_page[pg])
    if cur_pages:
        sections.append((cur_pages, cur_items))
    return sections


# ---------------------------------------------------------------------------------------------
# WHOLE-DOC path: for filings small enough to fit in context (e.g. quarterly results), skip locate
# entirely and read the ENTIRE document in ONE call. Validated on Reliance Q4: 1 call / $0.11 / same
# 16 datapoints the 112-read per-item path found. The size gate lives in phase0 (only small docs
# reach here); big docs (annual reports, large quarterly booklets) stay on locate + per-item.
# ---------------------------------------------------------------------------------------------

_WHOLE_INSTR = """You are a senior Indian equity-research analyst. Below is the FULL layout-preserved text of a
company's financial filing, followed by a list of data points (each with an id and a scope: standalone or
consolidated). For EACH id, extract its value from the filing for the requested scope.

RULES (apply independently per id):
- Map by MEANING using the concept + aliases. Respect the COLUMN HINT and take the MOST RECENT period.
- Distinguish the STANDALONE statements from the CONSOLIDATED ones, and a MONETARY AMOUNT from a COUNT.
- value = the NUMBER ONLY: keep digit-group commas, the decimal point, and a leading minus or surrounding
  parentheses for negatives. REMOVE ₹, `, Rs, INR, unit words and footnote marks. NEVER put words in value.
- A TOTAL/heading returns the TOTAL of its block, not one sub-line.
- found=true ONLY if the filing genuinely discloses that concept for that scope; otherwise found=false
  (do NOT borrow an unrelated number). Return exactly one result object per id."""

_WHOLE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "found": {"type": "boolean"},
                    "value": {"type": "string"},
                    "reported_label": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                },
                "required": ["id", "found", "value", "reported_label", "evidence_quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _full_text(pdf_path: str, sm) -> str:
    parts = []
    for p in range(1, sm.page_count + 1):
        t = _layout_text(pdf_path, p) or sm.text(p) or ""
        parts.append(f"=== PAGE {p} ===\n{t}")
    return "\n".join(parts)


def whole_doc_extract(pdf_path: str, sm, defs: list[dict], na_items: set) -> list[dict]:
    """Read the entire (small) document in ONE call. Returns phase0-shaped rows."""
    full = _full_text(pdf_path, sm)
    na_rows: list[dict] = []
    pairs: list[tuple[dict, str]] = []
    for it in defs:
        if it["key"] in na_items:
            na_rows.append({"key": it["key"], "tier": it.get("tier", ""), "scope": "both",
                            "status": "N/A (sector)", "found": False})
            continue
        for sc in (["standalone", "consolidated"] if it["scope"] == "both" else [it["scope"]]):
            if sc == "consolidated" and not sm.has_consolidated:
                na_rows.append({"key": it["key"], "tier": it.get("tier", ""), "scope": sc,
                                "status": "N/A (no consolidated statements)", "found": False})
                continue
            pairs.append((it, sc))

    spec = "\n".join(
        f"[id={i}] {it['key']} ({sc}) | {it['concept'][:90]} | HINT: {it.get('column_hint') or '-'}"
        for i, (it, sc) in enumerate(pairs)
    )
    resp = llm.client().responses.create(
        model=config.model_default,
        instructions=_WHOLE_INSTR,
        input=f"FILING TEXT:\n{full}\n\nDATA POINTS:\n{spec}",
        text={"format": {"type": "json_schema", "name": "wholedoc", "schema": _WHOLE_SCHEMA, "strict": True}},
        reasoning={"effort": config.reasoning_effort},
        max_output_tokens=8000,
        store=False,
    )
    txt = getattr(resp, "output_text", "") or ""
    res = {o["id"]: o for o in json.loads(txt).get("results", [])} if txt.strip() else {}

    rows: list[dict] = []
    for i, (it, sc) in enumerate(pairs):
        o = res.get(i)
        base = {"key": it["key"], "tier": it.get("tier", ""), "scope": sc,
                "candidate_pages": [], "scope_match": True}
        if o and o.get("found") and _acceptable(o.get("value", "")):
            rec = _clean_value(_reconcile_sign(dict(
                found=True, value=o.get("value", ""), value_prior="",
                reported_label=o.get("reported_label", ""), evidence_quote=o.get("evidence_quote", ""),
                observed_scope=sc, confidence=0.9)))
            rows.append({**base, **rec})
        else:
            rows.append({**base, **dict(_NOT_FOUND)})
    return na_rows + rows


def extract(company_key: str, pdf_path: str, defs: list[dict], profile: dict,
            on_log=lambda m: None) -> list[dict]:
    """Run section vision-batch + per-item fallback. Returns phase0-shaped rows."""
    na = set(profile.get("na_items", []))
    sm = build_structure_map(pdf_path, standalone_bs=profile.get("standalone_bs"),
                             consolidated_bs=profile.get("consolidated_bs"))
    doc = fitz.open(pdf_path)
    results: dict[tuple[str, str], dict] = {}
    cand_pages: dict[tuple[str, str], list[int]] = {}

    # locate everything, per scope
    per_scope: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    singletons: list[tuple[dict, str]] = []
    for it in defs:
        if it["key"] in na:
            continue
        scopes = ["standalone", "consolidated"] if it["scope"] == "both" else [it["scope"]]
        for sc in scopes:
            if sc == "consolidated" and not sm.has_consolidated:
                continue
            cands = locate(sm, it["aliases"], sc, it.get("column_hint"), it.get("location_hint"))
            pages = [c.page for c in cands]
            cand_pages[(it["key"], sc)] = pages
            # Precision-critical items (a column_hint means the value sits in a specific column of a
            # multi-column table -- PP&E gross/accum, share count-vs-capital, deferred-tax sub-lines)
            # go straight to the precise per-item reader, where the coordinate read / rotation /
            # self-consistency disambiguate the column. Vision-batch a whole section reliably reads
            # SINGLE-VALUE note lines but slips columns on these -- so we keep them off the batch path.
            if it.get("column_hint"):
                singletons.append((it, sc))
            elif pages:
                per_scope[sc].append((pages[0], it))
            else:
                singletons.append((it, sc))

    n_batch = n_fb = 0
    for sc, pairs in per_scope.items():
        for cluster_pages, items in _cluster(pairs):
            if len(items) < MIN_BATCH:
                singletons.extend((it, sc) for it in items)
                continue
            window = list(range(min(cluster_pages) - 1, max(cluster_pages) + 2))
            window = [p for p in window if 1 <= p <= doc.page_count][:MAX_WINDOW]
            members = [{"key": it["key"], "definition": it["concept"], "column_hint": it.get("column_hint")}
                       for it in items]
            on_log(f"  section {sc} pp{window}: {len(items)} datapoints (1 vision call)")
            res = _vision_batch(doc, window, members)
            n_batch += 1
            for i, it in enumerate(items):
                o = res.get(i)
                if o and o.get("found") and _acceptable(o.get("value", "")):
                    rec = _clean_value(_reconcile_sign(dict(
                        found=True, value=o.get("value", ""), value_prior="",
                        reported_label=o.get("reported_label", ""),
                        evidence_quote=o.get("evidence_quote", ""),
                        observed_scope=sc, confidence=0.9)))
                    if _acceptable(rec.get("value", "")):
                        results[(it["key"], sc)] = rec
                    else:
                        singletons.append((it, sc))
                else:
                    singletons.append((it, sc))   # not found in batch -> precise fallback

    # per-item fallback (singletons + batch misses) via the proven reader
    for it, sc in singletons:
        if (it["key"], sc) in results:
            continue
        pages = cand_pages.get((it["key"], sc), [])
        if not pages:
            continue
        rec = read_value(pdf_path, pages, key=it["key"], definition=it["concept"],
                         aliases=it["aliases"], column_hint=it.get("column_hint"), scope=sc)
        n_fb += 1
        if rec.get("found"):
            results[(it["key"], sc)] = rec

    on_log(f"  -> {n_batch} vision-batch calls + {n_fb} per-item fallbacks")

    # assemble phase0-shaped rows
    rows: list[dict] = []
    for it in defs:
        scopes = ["standalone", "consolidated"] if it["scope"] == "both" else [it["scope"]]
        for sc in scopes:
            if it["key"] in na:
                rows.append(dict(key=it["key"], scope=sc, found=False, value="", status="N/A (sector)"))
                continue
            if sc == "consolidated" and not sm.has_consolidated:
                rows.append(dict(key=it["key"], scope=sc, found=False, value="",
                                 status="N/A (no consolidated statements)"))
                continue
            rec = results.get((it["key"], sc))
            rows.append(dict(key=it["key"], scope=sc, **rec) if rec else
                        dict(key=it["key"], scope=sc, **dict(_NOT_FOUND)))
    return rows
