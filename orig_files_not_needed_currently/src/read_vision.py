"""Vision reader: render located page(s) and let gpt-5.4 read the table like a human.

Phase-0 proved this is the robust path for dense multi-column tables (PP&E gross /
accumulated-depreciation), where linearized text and pdfplumber both fail. The model
also REPORTS the scope it sees on the page (standalone / consolidated), so we can trust
the page's own context over the (noisy) region pre-tagging.
"""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
from collections import Counter
from typing import Any, Optional

import fitz

from src.config import config
from src.llm import client

_PDFTOTEXT = shutil.which("pdftotext")  # poppler; layout-preserved text (exact digits)

_DPI = 300        # default: clean digit reads
_DENSE_DPI = 400  # dense multi-column tables (PP&E gross/accum-dep): 300 stably misread 1,467->1,487
_MAX_PAGES = 4    # show top-N located pages; verified misses were on candidate page #4-5, so widen recall

_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "value": {"type": "string", "description": "CURRENT (most recent) year, as printed, sign preserved (parentheses=negative); '' if not found"},
        "value_prior": {"type": "string", "description": "PRIOR-year comparative column for the SAME line; '' if not shown"},
        "unit": {"type": "string", "description": "e.g. 'INR crore', 'INR million', 'shares', 'Rs per share', ''"},
        "reported_label": {"type": "string", "description": "label exactly as printed; '' if not found"},
        "evidence_quote": {"type": "string", "description": "the line containing label + number"},
        "observed_scope": {"type": "string", "enum": ["standalone", "consolidated", "unknown"]},
        "confidence": {"type": "number"},
    },
    "required": ["found", "value", "value_prior", "unit", "reported_label", "evidence_quote", "observed_scope", "confidence"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """You are a senior Indian equity-research analyst reading a page image from an annual report to extract ONE financial data point.

RULES:
- Map by MEANING using the concept definition + aliases, not exact string. Indian companies label the same concept differently.
- Only set found=false for a broad line when it is explicitly labelled "Others"/"Miscellaneous" AND is a grab-bag of several unrelated items — NOT when it is essentially the requested concept under a slightly broader name.
- Read the table/figures from the IMAGE. If the data point is not on this page, set found=false. NEVER invent a number.
- value = the MOST RECENT reporting year (current-year column / latest 'As at' date).
- value_prior = the SAME line's PRIOR-year comparative column (the immediately preceding year). '' if not shown.
- Numbers in parentheses are NEGATIVE. Preserve the value exactly as printed; do not convert units.
- Respect the column hint (e.g. GROSS carrying value/COST vs ACCUMULATED DEPRECIATION vs NET block).
- observed_scope: report whether THIS page belongs to the Standalone or Consolidated financial statements (from page titles/footers/context), else 'unknown'.
- evidence_quote: the exact label + number as printed.
"""

_TEXT_INSTRUCTIONS = """You are a senior Indian equity-research analyst extracting ONE financial data point from LAYOUT-PRESERVED text of annual-report pages. Columns are space-aligned: within a row, cells are separated by runs of spaces, in the printed left-to-right order.

RULES:
- Map by MEANING using the concept + aliases. A BROADER line may INCLUDE the requested item — honour footnotes such as "$ Includes Office Equipments" (so 'Office Equipment' = the 'Equipments$' line). Only set found=false for a broad line when it is explicitly labelled "Others"/"Miscellaneous" AND is a grab-bag of several unrelated items (not when it is essentially the requested concept under a slightly broader name).
- value = the MOST RECENT year; value_prior = the prior-year comparative for the SAME line.
- For movement schedules (PP&E / intangibles / investment property), the table is laid out ONE OF TWO ways — detect which:
  (a) ASSET CLASS PER ROW: each asset row runs across a GROSS section, a DEPRECIATION section and a NET section, each with an opening and a CLOSING 'As at <date>' column. Find the requested asset's ROW, then take the CLOSING column of the section named in the column hint.
  (b) ASSET CLASS PER COLUMN: the columns are the asset classes left-to-right (e.g. Land, Buildings, Plant & machinery, Office equipment, Computer equipment, Furniture & fixtures, Leasehold improvements, Vehicles, Total) and the ROWS are the movement lines ('Gross carrying value as at <date>', 'Additions', 'Deletions', 'Accumulated depreciation as at <date>', 'Carrying value as at <date>'). Pick the ROW matching the column hint at the LATEST date ('Gross carrying value as at <latest>' for GROSS, 'Accumulated depreciation as at <latest>' for ACCUMULATED DEPRECIATION, 'Carrying value as at <latest>' for NET), then read the value in the requested asset's COLUMN.
  In layout (b) match the asset column by LEFT-TO-RIGHT ORDER: the row's numeric values are in the SAME order as the column headers, so count headers and values in order and take the matching position (the Nth header → the Nth value). Do NOT rely on vertical character alignment — multi-line/wrapped headers are often offset from their numbers. Watch adjacent look-alikes: 'Office equipment' ≠ 'Computer equipment' ≠ 'Plant & machinery' ≠ 'Furniture & fixtures'.
  In BOTH layouts take the CLOSING/latest figure, never the opening, and never a different section.
- Numbers in parentheses are NEGATIVE. Preserve as printed; do not convert units.
- If genuinely not present, found=false. Never invent.
- evidence_quote: the row label + the figure as printed.
"""


def _layout_text(pdf_path: str, page_1: int) -> str:
    if not _PDFTOTEXT:
        return ""
    try:
        return subprocess.run(
            [_PDFTOTEXT, "-layout", "-f", str(page_1), "-l", str(page_1), pdf_path, "-"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return ""


# Canonical PP&E asset-class columns: (canonical, header-keyword regex, requested-concept regex).
# Order in the list is NOT used — actual column order is read from the page header.
_ASSET_COLS = [
    ("land",       r"\bland\b",        r"\bland\b"),
    ("buildings",  r"\bbuilding",      r"\bbuilding"),
    ("plant",      r"\bplant\b",       r"\bplant\b"),
    ("office",     r"\boffice\b",      r"\boffice\b"),
    ("computer",   r"\bcomputer",      r"\bcomputer"),
    ("furniture",  r"\bfurnitur",      r"\bfurnitur"),
    ("leasehold",  r"\bleasehold",     r"\bleasehold"),
    ("vehicles",   r"\bvehicle",       r"\bvehicle"),
]
_NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


def _is_asset_per_column_schedule(text: str) -> bool:
    """True if the page is a movement schedule laid out with ASSET CLASSES AS COLUMNS.

    In that layout layout-preserved TEXT misleads the model (wrapped column headers sit above
    the WRONG number), so we route to VISION (which sees the real grid). The far more common
    ASSET-PER-ROW layout (one asset per row, gross/dep/net as columns — Reliance, Hindalco, ITC)
    is read correctly from text and must stay on the text path. Two signatures fire here:

      (1) BANNER — a single line lists >=4 distinct asset-class headers (Land, Buildings, Plant,
          Office, Computer, Furniture, Leasehold, Vehicles). A clean column-header row. This never
          occurs in asset-per-ROW tables (one asset per line), so it cannot misfire on them.

      (2) GARBLED — a WIDE/landscape (often sideways-printed) per-column table whose headers
          pdftotext scatters onto separate number-less lines (e.g. Adani's PP&E, where 'Office
          Equipments' detaches from its column). Detected by: a genuine movement GRID (strict
          'gross carrying'/'gross block' + 'accumulated depreciation' gate, so accounting-policy
          pages that merely mention asset classes don't match), >=4 distinct asset classes present,
          >=4 asset-labelled lines that carry NO number (detached headers), and NOT being an
          asset-per-ROW schedule (which would show several lines that BEGIN with an asset name AND
          carry the row's many figures).

    Detection is layout-based, never company-based. (Sideways-printed pages are additionally
    rotated upright at render time — see _upright_rotation.)
    """
    low = text.lower()
    if "carrying value" not in low and "accumulated depreciation" not in low:
        return False
    present: set[str] = set()
    banner = False
    asset_only = 0   # asset-keyword lines carrying NO number (a detached header)
    asset_data = 0   # lines that BEGIN with an asset name AND carry >=4 numbers (a per-ROW row)
    for ln in low.splitlines():
        cls = {c for c, hreg, _ in _ASSET_COLS if re.search(hreg, ln)}
        if not cls:
            continue
        present |= cls
        if len(cls) >= 4:
            banner = True
        nums = len(_NUM_RE.findall(ln))
        if nums == 0:
            asset_only += 1
        elif nums >= 4 and any(re.search(hreg, ln[:40]) for _, hreg, _ in _ASSET_COLS):
            asset_data += 1
    if banner:
        return True
    strict = (("gross carrying" in low) or ("gross block" in low)) and ("accumulated depreciation" in low)
    return strict and len(present) >= 4 and asset_only >= 4 and asset_data < 3


def _reconcile_sign(rec: dict) -> dict:
    """Evidence-grounded sign guard: bracketed figures are negative, but the model sometimes
    returns the bare magnitude (e.g. treasury shares '(10,466,985)' read as '10,466,985';
    an impairment 'reversal' '(36)' read as '36'). If a value's magnitude appears in the cited
    evidence line ONLY inside parentheses, restore the parentheses.

    Conservative by construction: skips already-negative values, and never flips when the same
    magnitude also appears UN-bracketed on the line (ambiguous → trust the model). Deterministic
    and concept-agnostic, so it corrects sign-drops for every company, not just one.
    """
    ev = rec.get("evidence_quote") or ""
    if not ev:
        return rec
    for fld in ("value", "value_prior"):
        v = (rec.get(fld) or "").strip()
        if not v or v.startswith("(") or v.startswith("-"):
            continue
        m = re.search(r"\d[\d,]*\.?\d*", v)
        if not m:
            continue
        esc = re.escape(m.group(0))
        bounded = re.findall(r"(?<![\d.])" + esc + r"(?![\d.])", ev)      # all occurrences on the line
        bracketed = re.findall(r"\(\s*" + esc + r"\s*\)", ev)            # those wrapped in ( )
        if bounded and len(bounded) == len(bracketed):                   # every occurrence is negative
            rec[fld] = f"({v})"
    return rec


# Asset-key stem -> canonical PP&E column header. Used by the coordinate column-reader.
_PRIM_COL = [("land", "Land"), ("building", "Buildings"), ("plant", "Plant"), ("office", "Office"),
             ("computer", "Computer"), ("furnitur", "Furniture"), ("leasehold", "Leasehold"),
             ("vehicle", "Vehicles")]
_COLNUM = re.compile(r"^\(?[\d,]+\)?$|^[–-]$")


def _coord_target_asset(key: str) -> Optional[str]:
    kl = key.lower()
    for stem, col in _PRIM_COL:
        if stem in kl:
            return col
    return None


def _coord_read_page(pg, target: str, section: str) -> Optional[str]:
    """Read the target asset's CLOSING gross/accum cell from ONE page via word coordinates.

    Returns the value string, or None when the page is NOT a clean upright per-column schedule
    (rotated/garbled headers, combined columns, no matching closing row). Returning None lets the
    caller fall back to vision, so tables this can't read are never affected.
    """
    from collections import defaultdict
    W = [(round(x0), round((y0 + y1) / 2), round(x1), t)
         for x0, y0, x1, y1, t, *_ in pg.get_text("words")]
    if not W:
        return None
    # header line: a single y carrying >=4 distinct primary asset-column words
    byline: dict = defaultdict(set)
    for x0, yc, x1, t in W:
        k = t.lower().strip(",")
        for stem, col in _PRIM_COL:
            if k == col.lower() or k == stem:
                byline[yc].add(col)
    hdr_y = next((yc for yc, cset in byline.items() if len(cset) >= 4), None)
    if hdr_y is None:
        return None
    # column centres from header tokens within +-30px of the header line (multi-line headers)
    cols: dict = {}
    for x0, yc, x1, t in W:
        k = t.lower().strip(",")
        for stem, col in _PRIM_COL:
            if (k == col.lower() or k == stem) and abs(yc - hdr_y) <= 30:
                cols[col] = (x0 + x1) / 2
    if target not in cols or len(cols) < 4:
        return None
    # combined-header guard: if two asset words sit on top of each other (e.g. Reddy's
    # "Furniture, fixtures and office equipment"), the column is ambiguous -> decline.
    near = sorted(abs(cols[target] - v) for c, v in cols.items() if c != target)
    if near and near[0] < 18:
        return None
    labelbound = min(cols.values()) - 25
    nums = [(round((x0 + x1) / 2), yc, t) for x0, yc, x1, t in W
            if _COLNUM.match(t) and x0 >= labelbound and yc > hdr_y + 5]
    if not nums:
        return None
    nums.sort(key=lambda v: v[1])
    rows, cur = [], [nums[0]]
    for nv in nums[1:]:
        if nv[1] - cur[-1][1] <= 5:
            cur.append(nv)
        else:
            rows.append(cur)
            cur = [nv]
    rows.append(cur)
    years = [int(y) for y in re.findall(r"20\d\d", " ".join(t for _, _, _, t in W))]
    if not years:
        return None
    maxyear = str(max(years))
    need = "accumulated depreciation as at" if section == "accum" else "gross carrying value as at"
    for r in rows:
        yc = round(sum(n[1] for n in r) / len(r))
        label = " ".join(
            t for x0, y, x1, t in sorted(
                [(x0, y, x1, t) for x0, y, x1, t in W if x1 < labelbound and yc - 36 < y <= yc + 3],
                key=lambda v: v[1])).lower()
        if need in label and maxyear in label and "april" not in label and "beginning" not in label:
            tx = cols[target]
            best = min(r, key=lambda n: abs(n[0] - tx))
            # Robustness for unseen reports: the chosen number's NEAREST column (among all
            # detected columns) must itself be the target — otherwise the spacing is unusual and
            # we'd risk borrowing a neighbour's value, so decline and let vision handle it.
            nearest = min(cols, key=lambda c: abs(cols[c] - best[0]))
            if nearest == target and abs(best[0] - tx) <= 30:
                return best[2]
    return None


def _coord_read_ppe(pdf_path: str, pages: list[int], key: str, column_hint: Optional[str]) -> Optional[str]:
    """Deterministic column read for CLEAN upright per-column PP&E schedules (e.g. Infosys).

    Reads the exact column from PDF word coordinates, so it cannot 'slip one column' the way
    vision does on wide tables. Returns None for anything it can't confidently read (rotated
    headers like Adani, combined columns like Reddy, no closing row) -> caller uses vision, so
    no currently-working table regresses.
    """
    target = _coord_target_asset(key)
    if not target:
        return None
    # Derive section from the KEY prefix, not the column_hint — the gross hint literally
    # contains "...NOT accumulated depreciation", which would fool a substring check.
    section = "accum" if key.lower().startswith("accumulated") else "gross"
    doc = fitz.open(pdf_path)
    try:
        for p in pages:
            v = _coord_read_page(doc[p - 1], target, section)
            if v is not None:
                return v
    finally:
        doc.close()
    return None


# Extra discipline appended ONLY for small models (mini/nano). The flagship prompt is left
# untouched (zero regression risk); these target the small model's observed failure modes:
# over-grabbing, returning a sub-line instead of the total, and leaving currency/footnote noise.
_MINI_STRICT = """

EXTRA RULES (follow exactly):
- value / value_prior = the NUMBER ONLY. Keep digit grouping commas, the decimal point, and a
  leading minus or surrounding parentheses for negatives. REMOVE any currency symbol (₹, `, Rs,
  INR), unit word, or footnote mark (*, #, †). e.g. "` 1.00" → "1.00".
- If the concept is a TOTAL, or a heading whose value is the sum of sub-lines printed beneath it
  (e.g. "Auditors' remuneration" = audit fee + tax-audit fee + others; "Trade payables" = MSME +
  others), return the TOTAL of that block — never a single sub-line.
- Set found=true ONLY when a printed line genuinely denotes the requested concept. If nothing on
  the page is that concept, set found=false — do NOT return the nearest unrelated number just to
  avoid an empty answer."""


def _is_small_model(model: str) -> bool:
    m = (model or "").lower()
    return "mini" in m or "nano" in m


def _clean_value(rec: dict) -> dict:
    """Strip currency symbols / unit words / footnote marks from numeric fields (keeps sign,
    commas, decimals). Safe for any model — a flagship value is already clean, so this is a no-op
    there; it rescues a small model's '` 1.00' / '12.5*' style noise."""
    for fld in ("value", "value_prior"):
        v = rec.get(fld)
        if isinstance(v, str) and v.strip():
            cleaned = re.sub(r"₹|`|(?i:\bRs\.?|\bINR\b)|[*#†‡]", "", v).strip()
            if re.search(r"\d", cleaned):   # don't blank out a non-numeric 'Nil'/'-'
                rec[fld] = cleaned
    return rec


def _single_read_text(text: str, *, key, definition, aliases, column_hint, scope, model) -> dict[str, Any]:
    import json
    mdl = model or config.model_default
    instr = _TEXT_INSTRUCTIONS + (_MINI_STRICT if _is_small_model(mdl) else "")
    ui = (f"CONCEPT: {key}\nDEFINITION: {definition.strip()}\n"
          f"ALIASES: {', '.join(aliases)}\nCOLUMN HINT: {column_hint or '-'}\n"
          f"REQUESTED SCOPE: {scope}\n\nLAYOUT TEXT:\n{text}")
    resp = client().responses.create(
        model=mdl, instructions=instr, input=ui,
        text={"format": {"type": "json_schema", "name": "datapoint", "schema": _SCHEMA, "strict": True}},
        reasoning={"effort": config.reasoning_effort}, max_output_tokens=1200, store=False)
    txt = getattr(resp, "output_text", "") or ""
    return _clean_value(_reconcile_sign(json.loads(txt))) if txt.strip() else dict(_NOT_FOUND)


def _upright_rotation(pg) -> int:
    """Degrees to rotate the page so its text renders UPRIGHT before we hand it to vision.

    Some annual reports print very wide tables (PP&E movement schedules) SIDEWAYS — the text
    runs bottom-to-top (line dir ~ (0, -1)). Rendered as-is, vision sees a sideways grid and
    mis-tracks columns (Adani: it read accumulated-depreciation as the gross-closing cell).
    Rotating to upright restores normal left-to-right columns and fixes the read. Normal pages
    (dominant dir ~ (1, 0)) and scanned pages with no text layer return 0 → rendered unchanged,
    so no other company/page is affected. Layout-based, never company-based.
    """
    from collections import Counter
    c: Counter = Counter()
    for b in pg.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            di = ln.get("dir", (1.0, 0.0))
            c[(round(di[0]), round(di[1]))] += len(ln.get("spans", []))
    if not c:
        return 0
    dom = c.most_common(1)[0][0]
    return {(0, -1): 90, (0, 1): 270}.get(dom, 0)


def _render(doc, page_1: int, dpi: int = _DPI) -> str:
    pg = doc[page_1 - 1]
    rot = _upright_rotation(pg)
    if rot:
        pg.set_rotation(rot)   # render sideways tables upright so vision tracks columns correctly
    pix = pg.get_pixmap(dpi=dpi)
    return base64.b64encode(pix.tobytes("png")).decode()


_NOT_FOUND = {"found": False, "value": "", "value_prior": "", "unit": "", "reported_label": "",
              "evidence_quote": "", "observed_scope": "unknown", "confidence": 0.0}


def _norm_num(v: str) -> Optional[float]:
    """Normalize a printed value to a number for vote-grouping (parens=negative)."""
    if not v:
        return None
    neg = "(" in v and ")" in v
    m = re.search(r"-?\d[\d,]*\.?\d*", v.replace(" ", ""))
    if not m:
        return None
    try:
        x = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -x if neg else x


def _single_read(imgs, *, key, definition, aliases, column_hint, scope, model) -> dict[str, Any]:
    import json
    content: list[dict] = [{
        "type": "input_text",
        "text": (
            f"CONCEPT: {key}\nDEFINITION: {definition.strip()}\n"
            f"ALIASES: {', '.join(aliases)}\nCOLUMN HINT: {column_hint or '-'}\n"
            f"REQUESTED SCOPE: {scope}\n\nRead the page image(s) and extract the value."
        ),
    }]
    for b64 in imgs:
        content.append({"type": "input_image", "image_url": f"data:image/png;base64,{b64}"})
    mdl = model or config.model_default
    resp = client().responses.create(
        model=mdl,
        instructions=_INSTRUCTIONS + (_MINI_STRICT if _is_small_model(mdl) else ""),
        input=[{"role": "user", "content": content}],
        text={"format": {"type": "json_schema", "name": "datapoint", "schema": _SCHEMA, "strict": True}},
        reasoning={"effort": config.reasoning_effort},
        max_output_tokens=1200,
        store=False,
    )
    txt = getattr(resp, "output_text", "") or ""
    return _clean_value(_reconcile_sign(json.loads(txt))) if txt.strip() else dict(_NOT_FOUND)


def read_value(
    pdf_path: str,
    pages: list[int],
    *,
    key: str,
    definition: str,
    aliases: list[str],
    column_hint: Optional[str],
    scope: str,
    model: Optional[str] = None,
    n: Optional[int] = None,
) -> dict[str, Any]:
    """Render located pages once, read N times, and return the agreeing answer.

    Self-consistency: misread digits vary run-to-run, so the majority value across N
    independent reads is the reliable one. confidence = agreement fraction.

    TEXT-FIRST: layout-preserved text (pdftotext -layout) gives EXACT digits and aligned
    columns — it reads dense PP&E schedules correctly where vision slips columns, and is
    cheaper. Vision is the FALLBACK for scanned pages (no text layer).
    """
    if not pages:
        return dict(_NOT_FOUND)
    n = n or config.self_consistency_n
    use_pages = pages[:_MAX_PAGES]

    # Text-first: assemble layout text of the located pages.
    layout = ""
    if _PDFTOTEXT:
        layout = "\n".join(f"--- PAGE {p} ---\n{_layout_text(pdf_path, p)}" for p in use_pages)

    # ASSET-PER-COLUMN movement schedules (wide PP&E tables) defeat layout-text column reading
    # (wrapped headers sit above the wrong number) but VISION reads the grid correctly. Route
    # only that layout to vision; everything else stays text-first. Detection is layout-based,
    # not company-based, so asset-per-row tables (Reliance/Hindalco) are unaffected.
    wide_ppe = bool(column_hint) and layout and _is_asset_per_column_schedule(layout)

    if layout and len(layout.strip()) > 400 and not wide_ppe:   # text layer & not a wide-PP&E grid
        reads = [
            _single_read_text(layout, key=key, definition=definition, aliases=aliases,
                              column_hint=column_hint, scope=scope, model=model)
            for _ in range(n)
        ]
    else:
        # Wide per-column PP&E: first try the DETERMINISTIC coordinate column-reader. It reads the
        # exact column from PDF word coordinates, so it cannot 'slip one column' the way vision does
        # on wide tables (Infosys Office/Furniture). It returns None for tables it can't confidently
        # read (rotated headers like Adani, combined columns like Reddy) → we then use vision, so no
        # currently-working table regresses.
        if wide_ppe:
            coord_val = _coord_read_ppe(pdf_path, use_pages, key, column_hint)
            if coord_val is not None:
                r = dict(_NOT_FOUND)
                r.update(found=True, value=coord_val, value_prior="",
                         reported_label=f"{key.split('_')[-1]} (coordinate column read)",
                         evidence_quote="deterministic PDF-coordinate column extraction",
                         observed_scope=scope, confidence=1.0)
                r["votes"] = "coord"
                return r
        # Vision fallback (scanned / no text layer, or coord-read declined). Dense tables → higher DPI.
        dpi = _DENSE_DPI if column_hint else _DPI
        doc = fitz.open(pdf_path)
        try:
            imgs = [_render(doc, p, dpi) for p in use_pages]
        finally:
            doc.close()
        reads = [
            _single_read(imgs, key=key, definition=definition, aliases=aliases,
                         column_hint=column_hint, scope=scope, model=model)
            for _ in range(n)
        ]
    found = [r for r in reads if r.get("found")]
    if not found:
        # all agree it's absent → confident not-found
        return dict(_NOT_FOUND)

    # Vote by normalized numeric value (fall back to raw string for non-numerics).
    def vkey(r):
        nv = _norm_num(r.get("value", ""))
        return round(nv, 4) if nv is not None else (r.get("value") or "").strip().lower()

    tally = Counter(vkey(r) for r in found)
    winner_key, votes = tally.most_common(1)[0]
    winner = next(r for r in found if vkey(r) == winner_key)
    winner = dict(winner)
    winner["confidence"] = round(votes / n, 2)   # agreement across N reads
    winner["votes"] = f"{votes}/{n}"
    return winner
