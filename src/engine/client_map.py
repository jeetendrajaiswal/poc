"""Client-format mapping layer — map raw extracted statements to a client's
field taxonomy BY MEANING, not by label (the proven taxonomy/definitions.yaml
approach from the datapoint engine, applied to client fields).

Pieces:
  * config/client_taxonomy_software.yaml — per client field: fid, name,
    concept (economic DEFINITION with inclusion/exclusion/total-vs-component
    guards), illustrative aliases. Generated from evidence (client template +
    human mapping files + filing corpus), human-reviewable, versioned.
  * The client template's 'Field Calculation / Logic' formulas are used for
    VERIFICATION: after mapping, each aggregate's mapped components must sum
    to its mapped printed value. A wrong mapping breaks arithmetic and flags.
  * Every value carries (value, unit, currency); periods are normalised to
    3M/6M/9M/FY with end dates.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# --------------------------------------------------------------------------- template (structure + verification formulas)

_SHEET_KEYS = {"income statement": "income", "balance sheet": "balance",
               "cash flow": "cashflow", "segment finance": "segment"}


@dataclass
class ClientField:
    fid: str
    name: str
    order: int
    row: int
    formula: list[tuple[int, int]] | None    # [(sign, template_row), ...]
    group: str = ""                          # parent aggregate name(s), derived from formulas


def _parse_formula(expr) -> list[tuple[int, int]] | None:
    if not expr or not str(expr).startswith("="):
        return None
    out = [(-1 if s == "-" else 1, int(r)) for s, r in re.findall(r"([+\-]?)C(\d+)", str(expr))]
    return out or None


def load_template(path: str) -> dict[tuple[str, str], list[ClientField]]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    out: dict[tuple[str, str], list[ClientField]] = {}
    for sn in wb.sheetnames:
        snl = sn.lower()
        stmt = next((v for k, v in _SHEET_KEYS.items() if k in snl), None)
        if stmt is None:
            continue
        scope = "consolidated" if "consolidated" in snl else "standalone"
        fields = []
        for i, r in enumerate(wb[sn].iter_rows(values_only=True), 1):
            if i == 1 or not r or r[3] is None:
                continue
            fields.append(ClientField(str(r[3]).strip(), str(r[2] or "").strip(),
                                      int(r[5] or 0), i,
                                      _parse_formula(r[6] if len(r) > 6 else None)))
        # derive each field's GROUP (parent aggregate chain) from the formulas —
        # this is what disambiguates duplicate display names (two 'Basic EPS',
        # two 'Interest Received') without any hand-written conventions
        byrow = {f.row: f for f in fields}
        parent: dict[int, int] = {}
        for f in fields:
            if f.formula:
                for _sign, rr in f.formula:
                    if rr in byrow and rr not in parent:
                        parent[rr] = f.row
        for f in fields:
            chain = []
            cur = f.row
            for _ in range(3):
                p = parent.get(cur)
                if p is None:
                    break
                chain.append(byrow[p].name)
                cur = p
            f.group = " < ".join(chain)
        out[(stmt, scope)] = fields
    wb.close()
    return out


def derived_section_pairs(fields: list[ClientField],
                          defs: list[dict] | None = None) -> dict[str, dict[str, str]]:
    """For labels that belong to MORE THAN ONE field (duplicate display names,
    or a name/alias shared across fids — 'same concept, different id'), derive
    which fid is the CURRENT vs NON-CURRENT (or OPERATING/INVESTING/FINANCING)
    variant from the template's own group chains. Fully generic — no hand-written
    table; sides come from formula hierarchy, labels from template names +
    taxonomy aliases."""
    def side_of(group: str) -> str:
        g = group.lower()
        if re.search(r"comprehensive income.*attributable", g):
            return "TCI-ATTRIBUTION"
        if re.search(r"profit.*attributable", g):
            return "PROFIT-ATTRIBUTION"
        if "total segment revenue" in g:
            return "SEGMENT-REVENUE"
        if "ordinary activities before tax" in g:
            return "SEGMENT-RESULTS"
        if "total segment assets" in g:
            return "SEGMENT-ASSETS"
        if "total segment liabilit" in g:
            return "SEGMENT-LIABILITIES"
        if re.search(r"non[\s-]*current|long[\s-]*term", g):
            return "NON-CURRENT"
        if re.search(r"current|short[\s-]*term", g):
            return "CURRENT"
        if "operating" in g:
            return "OPERATING"
        if "investing" in g:
            return "INVESTING"
        if "financing" in g:
            return "FINANCING"
        return ""
    side = {f.fid: side_of(f.group) for f in fields}
    bylabel: dict[str, dict[str, ClientField]] = {}
    byfid = {f.fid: f for f in fields}
    for f in fields:
        bylabel.setdefault(norm_label(f.name), {})[f.fid] = f
    for d in defs or []:
        f = byfid.get(d["fid"])
        if not f:
            continue
        for a in [d.get("name", "")] + list(d.get("aliases", [])):
            nl = norm_label(a)
            if nl:
                bylabel.setdefault(nl, {})[f.fid] = f
    pairs: dict[str, dict[str, str]] = {}
    for name, fs in bylabel.items():
        if len(fs) < 2:
            continue
        sides: dict[str, set[str]] = {}
        for f in fs.values():
            s = side[f.fid]
            if s:
                sides.setdefault(s, set()).add(f.fid)
        # pin only when unambiguous: every side has exactly ONE candidate fid;
        # a side with 2+ claimants means the label alone can't decide — leave
        # those lines to the definitions layer
        if len(sides) >= 2 and all(len(v) == 1 for v in sides.values()):
            pairs[name] = {s: next(iter(v)) for s, v in sides.items()}
    return pairs


def statement_of(section: str, title: str) -> str | None:
    """Which client statement a raw table belongs to.

    Most-specific match FIRST: many filings title every statement
    '... Financial Results — Balance Sheet / Cash Flows', so 'results' alone
    must never claim a table that names a more specific statement.
    """
    s = f"{section} {title}".lower()
    if "cash flow" in s:
        return "cashflow"
    if "assets and liabilities" in s or "balance sheet" in s:
        return "balance"
    if "segment" in s:
        return "segment"
    if "results" in s or "profit and loss" in s:
        return "income"
    return None


def map_quarter(tables, template, taxonomy, model: str | None = None):
    """Map one filing's raw tables to the client taxonomy.

    tables: iterable of (page, n, title, scope, section, grid).
    Merges ALL tables of the same (statement, scope) — filings routinely print
    one statement as several blocks (P&L + OCI/EPS, segment revenue + results).
    IFRS versions and unknown scopes are excluded. Returns {key: MappedStatement}.
    """
    grids: dict[tuple[str, str], list] = {}
    for _page, _n, title, scope, section, grid in tables:
        s = f"{section} {title}".lower()
        stmt = statement_of(section, title)
        if (stmt is None or "ifrs" in s or len(grid) < 3
                or scope not in ("standalone", "consolidated")):
            continue
        key = (stmt, scope)
        if key in template:
            grids.setdefault(key, []).append(grid)
    out = {}
    for key, gl in grids.items():
        width = max(len(r) for g in gl for r in g)
        merged = [r + [""] * (width - len(r)) for g in gl for r in g]
        out[key] = map_statement(merged, key[0], taxonomy, template[key], model=model)
    return out


# --------------------------------------------------------------------------- taxonomy (definitions)

def norm_label(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(r"\(refer[^)]*\)", " ", s)
    s = re.sub(r"^\(?[a-z][.)]\s*", " ", s)             # 'a)' 'b.' '(c)' enumerators
    # roman enumerators ('VI Total tax expense') — VALID numerals only, so real
    # words like 'LIC', 'CC', 'IT' are never stripped
    s = re.sub(r"^\(?(x{0,3}(ix|iv|v?i{1,3}|v|x))[\s.,):]+", " ", s)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def load_taxonomy(path: str) -> dict[str, list[dict]]:
    """{statement: [ {fid, name, concept, aliases, ...} ]}"""
    import yaml
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    out: dict[str, list[dict]] = {}
    for it in doc.get("items", []):
        out.setdefault(it["statement"], []).append(it)
    return out


# --------------------------------------------------------------------------- periods, values, units

_MONTHS = ("january|february|march|april|may|june|july|august|september|"
           "october|november|december")
_MABBR = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_DATE = re.compile(rf"({_MONTHS})\s+(\d{{1,2}}),?\s+(\d{{4}})|"
                   rf"(\d{{1,2}})[\s.-]+({_MONTHS}|{_MABBR})[\s.,-]+(\d{{2,4}})|"
                   rf"(\d{{2}})[./-](\d{{2}})[./-](\d{{4}})", re.IGNORECASE)
_M2N = {m: i + 1 for i, m in enumerate(_MONTHS.split("|"))}
_M2N.update({m: i + 1 for i, m in enumerate(_MABBR.split("|"))})


@dataclass
class Period:
    span: str
    end: str
    audited: str
    col: int
    raw: str = ""


def _parse_period(text: str, col: int) -> Period:
    t = " ".join(text.split()).lower()
    span = "?"
    if re.search(r"three\s*months|\bquarter\b|\b3\s*months", t):
        span = "3M"
    elif re.search(r"six\s*months|half\s*year|\b6\s*months", t):
        span = "6M"
    elif re.search(r"nine\s*months|\b9\s*months", t):
        span = "9M"
    elif re.search(r"year\s*ended|twelve\s*months", t):
        span = "FY"
    end = ""
    m = _DATE.search(t)
    if m:
        g = m.groups()
        if g[0]:
            end = f"{g[2]}-{_M2N[g[0].lower()]:02d}-{int(g[1]):02d}"
        elif g[3]:
            yr = int(g[5])
            yr = yr + 2000 if yr < 100 else yr
            end = f"{yr}-{_M2N[g[4].lower()]:02d}-{int(g[3]):02d}"
        else:
            end = f"{g[8]}-{int(g[7]):02d}-{int(g[6]):02d}"
    if not end:
        # Indian shorthand headers: 'Q1 FY25', 'FY 2024', 'FY25' (FY ends March 31)
        _QEND = {1: "06-30", 2: "09-30", 3: "12-31", 4: "03-31"}
        m = re.search(r"\bq([1-4])\s*fy\s*(\d{2,4})\b", t)
        if m:
            qn, yr = int(m.group(1)), int(m.group(2))
            yr = yr + 2000 if yr < 100 else yr           # FY25 -> FY ending Mar 2025
            cal = yr if qn == 4 else yr - 1              # Q1-Q3 fall in the prior calendar year
            end = f"{cal}-{_QEND[qn]}"
            span = "3M"
        else:
            m = re.search(r"\bfy\s*(\d{2,4})\b", t)
            if m:
                yr = int(m.group(1))
                yr = yr + 2000 if yr < 100 else yr
                end = f"{yr}-03-31"
                span = "FY"
    audited = ("unaudited" if "unaudited" in t else "audited" if "audited" in t else "")
    return Period(span, end, audited, col, text.strip()[:70])


def infer_spans(periods: list[Period]) -> list[Period]:
    """SEBI results layouts often print only dates. Infer spans: a duplicated
    end-date's FIRST occurrence is the 3M column, the SECOND the cumulative
    (6M for Sep, 9M for Dec, FY for Mar); a solitary March end is FY; other
    solitary ends are 3M."""
    seen: dict[str, int] = {}
    for p in periods:
        if not p.end:
            continue
        seen[p.end] = seen.get(p.end, 0) + 1
        if p.span != "?":
            continue
        month = int(p.end[5:7])
        if seen[p.end] == 1:
            p.span = "FY" if (month == 3 and sum(1 for q in periods if q.end == p.end) == 1) else "3M"
        else:
            p.span = {6: "3M", 9: "6M", 12: "9M", 3: "FY"}.get(month, "?")
    return periods


def _num(cell) -> float | None:
    s = str(cell or "").strip().strip("*^#@")
    if not s or s in "-–—" or s.lower() in ("nil", "na", "n.a.", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("₹", "").replace("`", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


_UNITS = [("crore", 1e7), ("lakh", 1e5), ("million", 1e6), ("mn", 1e6),
          ("billion", 1e9), ("'000", 1e3), ("thousand", 1e3)]


def detect_units(grid) -> tuple[str, float, str]:
    blob = " ".join(str(c) for row in grid[:6] for c in row if c).lower()
    unit, mult = "", 1.0
    for name, m in _UNITS:
        if name in blob:
            unit, mult = name, m
            break
    cur = "USD" if ("usd" in blob or "us$" in blob) else "INR"
    return unit, mult, cur


# --------------------------------------------------------------------------- definition-driven mapping

_MAP_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line": {"type": "integer"},   # the L<number> of the input line
                    "fid": {"type": "string"},     # "" when no field fits
                },
                "required": ["line", "fid"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}

_SECTION_PAT = [
    (r"non\s*-?\s*current\s+assets", "NON-CURRENT ASSETS"),
    (r"current\s+assets", "CURRENT ASSETS"),
    (r"non\s*-?\s*current\s+liabilit", "NON-CURRENT LIABILITIES"),
    (r"current\s+liabilit", "CURRENT LIABILITIES"),
    (r"^\s*equity\b|shareholders.?\s*funds", "EQUITY"),
    (r"inter.?segment\s+revenue", "INTER-SEGMENT"),
    (r"external\s+customers", "EXTERNAL-REVENUE"),
    (r"^segment\s+revenue", "SEGMENT-REVENUE"),
    (r"^segment\s+result", "SEGMENT-RESULTS"),
    (r"^segment\s+assets", "SEGMENT-ASSETS"),
    (r"^segment\s+liabilit", "SEGMENT-LIABILITIES"),
    (r"operating\s+activities", "OPERATING"),
    (r"investing\s+activities", "INVESTING"),
    (r"financing\s+activities", "FINANCING"),
]

_MAP_INSTR = """You map lines of an Indian company's financial statement to a client's field
taxonomy BY ECONOMIC MEANING using the field DEFINITIONS provided — never by name similarity.

Rules:
- Assign each report line to the ONE field whose definition it satisfies; return fid "" if no
  definition fits (do NOT force a fit).
- Respect total-vs-component guards in definitions: a printed subtotal/total line maps only to a
  field defined as that aggregate — NEVER to a component field.
- A printed TOTAL maps ONLY to the field for exactly that aggregate; if no such field exists,
  return "" (leave it unmapped). Never map a broader total into a narrower field (e.g. 'TOTAL
  LIABILITIES' must NOT go into 'Total Current Liabilities') and never map a per-segment or
  per-category subtotal (e.g. 'Total of IT Services') into an overall-total field.
- Several lines may map to the SAME fid when its definition says components are summed there.
- Ignore heading-only lines, period-header lines and blank lines (they are not in your input).
- Lines are prefixed with their statement SECTION (e.g. [CURRENT LIABILITIES], [INVESTING]) —
  use it: the same label maps to different fields in different sections.
- Refer to lines by their L<number>; every input line must appear exactly once in assignments.
- When a label appears TWICE and no section prefixes are given, Schedule III order applies:
  the FIRST occurrence is the NON-CURRENT item, the SECOND the CURRENT one.
- The aliases in definitions are ILLUSTRATIVE examples, not a match list."""


@dataclass
class MappedStatement:
    periods: list[Period]
    facts: dict[str, dict[int, float]]
    sources: dict[str, list[str]]
    unmapped: list[str]
    verification: list[str]
    n_checks: int = 0
    n_ok: int = 0
    unit: str = ""
    multiplier: float = 1.0
    currency: str = "INR"


def _label_and_vals(row):
    """Label = first SUBSTANTIVE text cell (grids may lead with Sl-No or
    enumerator cells like 'B' / 'iv.' / '(A)'); values = numeric cells after
    the label cell. Enumerator cells are kept as a prefix."""
    cand = []
    for j, c in enumerate(row):
        s = str(c or "").strip()
        if s and any(ch.isalpha() for ch in s) and _num(s) is None:
            cand.append((j, s))
    if not cand:
        return "", {}
    li, label = next(((j, s) for j, s in cand if len(s) > 4), cand[-1])
    pres = [s for j, s in cand if j < li and len(s) <= 6]
    if pres:
        label = " ".join(pres) + " " + label
    vals = {j: _num(row[j]) for j in range(li + 1, len(row)) if _num(row[j]) is not None}
    return label, vals


def parse_periods(grid, header=None) -> list[Period]:
    """Period per value column. Banner rows (a single non-empty PERIOD-WORDED
    cell, e.g. 'For the six months ended September 30,' with the years on the
    next row) apply to every column; section headings are never banners."""
    if header is None:
        header, _ = _header_and_data(grid)
    ncol = max(len(r) for r in grid)
    banner_parts = []
    for r in header:
        cells = [str(c).strip() for c in r if str(c).strip()]
        if len(cells) == 1 and re.search(r"month|quarter|year|ended|as at", cells[0], re.I):
            banner_parts.append(cells[0])
    banner = " ".join(banner_parts)
    periods = []
    for j in range(1, ncol):
        text = " ".join(str(r[j]) for r in header if j < len(r) and r[j])
        if text.strip():
            periods.append(_parse_period((banner + " " + text).strip(), j))
    return infer_spans(periods)


def _header_and_data(grid):
    first = None
    for i, row in enumerate(grid):
        label, vals = _label_and_vals(row)
        if label and vals:
            first = i
            break
    if first is None:
        first = min(3, len(grid))
    return grid[:first], grid[first:]


def map_statement(grid: list[list[str]], stmt: str, taxonomy: dict[str, list[dict]],
                  template_fields: list[ClientField], model: str | None = None) -> MappedStatement:
    """Definition-driven mapping of one raw statement grid + formula verification."""
    from src.llm import extract_json

    header, data = _header_and_data(grid)
    periods = parse_periods(grid, header)
    unit, mult, cur = detect_units(grid)

    rows = []
    section = ""
    for row in data:
        label, vals = _label_and_vals(row)
        if label and any(c.isalpha() for c in label):
            ll = re.sub(r"^\s*\d+[.)]\s*", "", label.lower())   # '2. Segment results'
            if re.search(r"comprehensive income.*attributable", ll):
                section = "TCI-ATTRIBUTION"
            elif re.search(r"profit for the (period|quarter|year).*attributable", ll):
                section = "PROFIT-ATTRIBUTION"
            elif not ll.startswith("total"):
                for pat, name in _SECTION_PAT:
                    if re.search(pat, ll):
                        section = name
                        break
            if vals:
                rows.append((label, section, vals))

    # deterministic layer first: duplicate names resolved by template structure
    valid_all = {d["fid"] for d in taxonomy.get(stmt, [])}
    pairs = derived_section_pairs(template_fields, taxonomy.get(stmt, []))
    # asset/liability kind of each fid, from its template group chain — a pinned
    # fid must agree with the report section on this axis too
    def _bs_kind(group: str) -> str:
        g = group.lower()
        if "asset" in g:
            return "ASSETS"
        if "liabilit" in g:
            return "LIABILITIES"
        return ""
    kind = {f.fid: _bs_kind(f.group) for f in template_fields}
    from collections import Counter
    label_sec_count = Counter((norm_label(lab), sec) for lab, sec, _v in rows)
    secs_present = {sec for _l, sec, _v in rows if sec}
    # a statement with separate external-customer / inter-segment revenue
    # sub-sections prints several bare totals — revenue-side pins are unsafe there
    multi_revenue = bool(secs_present & {"EXTERNAL-REVENUE", "INTER-SEGMENT"})
    pre: dict[int, str] = {}
    for i, (lab, sec, vals) in enumerate(rows):
        nl = norm_label(lab)
        pair = pairs.get(nl)
        if pair and sec == "SEGMENT-REVENUE" and multi_revenue:
            pair = None
        if pair and sec and label_sec_count[(nl, sec)] == 1:
            if "NON-CURRENT" in sec:
                side = "NON-CURRENT"
            elif "CURRENT" in sec:
                side = "CURRENT"
            elif sec in ("OPERATING", "INVESTING", "FINANCING",
                         "PROFIT-ATTRIBUTION", "TCI-ATTRIBUTION",
                         "SEGMENT-REVENUE", "SEGMENT-RESULTS",
                         "SEGMENT-ASSETS", "SEGMENT-LIABILITIES"):
                side = sec
            else:
                side = ""
            fid = pair.get(side)
            sec_kind = "ASSETS" if "ASSETS" in sec else ("LIABILITIES" if "LIABILITIES" in sec else "")
            if fid and fid in valid_all and (
                    not sec_kind or not kind.get(fid) or kind[fid] == sec_kind):
                pre[i] = fid
    todo = [(i, lab, sec, vals) for i, (lab, sec, vals) in enumerate(rows) if i not in pre]

    defs = taxonomy.get(stmt, [])
    grp = {f.fid: f.group for f in template_fields if f.group}
    def_lines = []
    for d in defs:
        al = "; ".join(d.get("aliases", [])[:5])
        g = grp.get(d["fid"], "")
        def_lines.append(f"fid={d['fid']} | {d['name']}"
                         + (f" | GROUP: {g}" if g else "")
                         + f" | {d['concept']}"
                         + (f" | e.g.: {al}" if al else ""))
    assign: dict[int, str] = {}
    if todo:
        res = extract_json(
            instructions=_MAP_INSTR,
            user_input=("FIELD DEFINITIONS:\n" + "\n".join(def_lines)
                        + "\n\nSTATEMENT LINES TO MAP:\n"
                        + "\n".join(f"L{k}: " + (f"[{sec}] " if sec else "") + lab
                                     for k, (_, lab, sec, _v) in enumerate(todo, 1))),
            schema_name="line_mapping", schema=_MAP_SCHEMA,
            model=model, max_output_tokens=8000, temperature=0.1)
        raw = {int(a["line"]): str(a["fid"]) for a in res.get("assignments", [])
               if isinstance(a.get("line"), int)}
        for k, (i, lab, sec, vals) in enumerate(todo, 1):
            if k in raw:
                assign[i] = raw[k]
    assign.update(pre)

    # focused second pass: lines the model left unmapped get one strict re-ask
    valid_pre = {d["fid"] for d in defs}
    retry = [(i, lab, sec) for i, (lab, sec, vals) in enumerate(rows)
             if vals and assign.get(i, "") not in valid_pre]
    if retry and len(retry) <= 15:
        res2 = extract_json(
            instructions=_MAP_INSTR + "\nThese lines were left unmapped in a first pass — "
            "map each to the closest fitting definition; use \"\" ONLY if truly nothing fits.",
            user_input=("FIELD DEFINITIONS:\n" + "\n".join(def_lines)
                        + "\n\nSTATEMENT LINES TO MAP:\n"
                        + "\n".join(f"L{k}: " + (f"[{sec}] " if sec else "") + lab
                                     for k, (_, lab, sec) in enumerate(retry, 1))),
            schema_name="line_mapping", schema=_MAP_SCHEMA,
            model=model, max_output_tokens=4000, temperature=0.1)
        raw2 = {int(a["line"]): str(a["fid"]) for a in res2.get("assignments", [])
                if isinstance(a.get("line"), int)}
        for k, (i, lab, sec) in enumerate(retry, 1):
            if raw2.get(k) and i not in pre:
                assign[i] = raw2[k]

    valid_fids = {d["fid"] for d in defs}
    # a printed TOTAL line subsumes its components: if a fid was assigned both
    # a total-labelled line and component lines, keep only the total(s)
    by_fid: dict[str, list[int]] = {}
    for i in range(len(rows)):
        fid = assign.get(i, "")
        if fid and fid in valid_fids:
            by_fid.setdefault(fid, []).append(i)
    for fid, idxs in by_fid.items():
        totals = [i for i in idxs if norm_label(rows[i][0]).startswith("total")]
        if totals and len(totals) < len(idxs):
            for i in idxs:
                if i not in totals:
                    assign.pop(i, None)
    facts: dict[str, dict[int, float]] = {}
    sources: dict[str, list[str]] = {}
    unmapped = []
    for i, (label, sec, vals) in enumerate(rows):
        fid = assign.get(i, "")
        if fid and fid in valid_fids:
            tgt = facts.setdefault(fid, {})
            for j, v in vals.items():
                tgt[j] = tgt.get(j, 0.0) + v
            sources.setdefault(fid, []).append(label)
        else:
            unmapped.append(label)

    # verification via the client template's own formulas
    verification = []
    n_checks = n_ok = 0
    row2fid = {f.row: f.fid for f in template_fields}
    for f in template_fields:
        if not f.formula or f.fid not in facts:
            continue
        for j in sorted(facts[f.fid].keys()):
            total, have = 0.0, 0
            for sign, r in f.formula:
                cf = row2fid.get(r)
                if cf and cf in facts and j in facts[cf]:
                    total += sign * facts[cf][j]
                    have += 1
            # a partial sum proves nothing: verify only when most formula
            # terms are actually mapped from the filing
            if have < 2 or have < 0.6 * len(f.formula):
                continue
            printed = facts[f.fid][j]
            tol = max(1.0, 0.01 * abs(printed))
            ok = abs(total - printed) <= tol
            n_checks += 1
            n_ok += ok
            if not ok:
                verification.append(f"⚠ {f.name} [{f.fid}] col{j}: printed {printed:,.2f} "
                                    f"vs sum-of-mapped {total:,.2f}")
    return MappedStatement(periods=periods, facts=facts, sources=sources,
                           unmapped=unmapped, verification=verification,
                           n_checks=n_checks, n_ok=n_ok,
                           unit=unit, multiplier=mult, currency=cur)


# --------------------------------------------------------------------------- client workbook output

def _period_label(p: Period) -> str:
    lab = f"{p.span} {p.end or p.raw}"
    if p.audited:
        lab += f" ({p.audited})"
    return lab


_SPAN_MONTHS = {"3M": 3, "6M": 6, "9M": 9, "FY": 12}


def write_client_workbook_long(company: str, mapped: dict[tuple[str, str], "MappedStatement"],
                               template: dict[tuple[str, str], list[ClientField]],
                               out_path: str) -> None:
    """LONG-format workbook: one sheet per statement x scope (e.g. 'Income
    Statement - Standalone'), one row per field x period, with Period End /
    Months / Denomination columns, plus the full Audit sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    STMT_NAME = {"income": "Income Statement", "balance": "Balance Sheet",
                 "cashflow": "Cash Flow", "segment": "Segment Finance"}
    STMT_ORDER = {"income": 0, "balance": 1, "cashflow": 2, "segment": 3}
    HDR = ["Field id", "Display Name", "Period End", "Months", "Audited",
           "Value", "Denomination", "Currency", "Method"]
    wb = Workbook()
    audit = wb.active
    audit.title = "Audit"
    audit.append(["Statement", "Scope", "Field id", "Display Name", "Method",
                  "Source report lines", "Denomination", "Currency", "Verification"])
    for c in audit[1]:
        c.font = Font(bold=True)

    keys = sorted(mapped, key=lambda k: (STMT_ORDER.get(k[0], 9), k[1] != "standalone"))
    for (stmt, scope) in keys:
        ms = mapped[(stmt, scope)]
        fields = template.get((stmt, scope))
        if not fields:
            continue
        ws = wb.create_sheet(f"{STMT_NAME.get(stmt, stmt)} - {scope.title()}"[:31])
        ws.append(HDR)
        for c in ws[1]:
            c.font = Font(bold=True)
        denom = ms.unit or "units"
        row2fid = {f.row: f.fid for f in fields}
        for f in sorted(fields, key=lambda x: x.order):
            per_vals = {}
            method = ""
            if f.fid in ms.facts:
                method = "reported" if len(ms.sources.get(f.fid, [])) == 1 else "summed"
                for p in ms.periods:
                    if p.col in ms.facts[f.fid]:
                        per_vals[p.col] = ms.facts[f.fid][p.col]
            elif f.formula:
                for p in ms.periods:
                    total, have = 0.0, 0
                    for sign, r in f.formula:
                        cf = row2fid.get(r)
                        if cf and cf in ms.facts and p.col in ms.facts[cf]:
                            total += sign * ms.facts[cf][p.col]
                            have += 1
                    if have >= 2:
                        per_vals[p.col] = round(total, 2)
                        method = "computed"
            if not per_vals:
                continue
            for p in ms.periods:
                if p.col not in per_vals:
                    continue
                # balance sheets are point-in-time ('As at ...'): no Months
                months = "" if stmt == "balance" else _SPAN_MONTHS.get(p.span, "")
                ws.append([f.fid, f.name, p.end or p.raw,
                           months, p.audited,
                           per_vals[p.col], denom, ms.currency, method])
            audit.append([stmt, scope, f.fid, f.name, method,
                          "; ".join(ms.sources.get(f.fid, [])),
                          denom, ms.currency,
                          "; ".join(v for v in ms.verification if f"[{f.fid}]" in v)[:180]])
        if ms.unmapped:
            audit.append([stmt, scope, "", "UNMAPPED LINES", "",
                          "; ".join(ms.unmapped)[:300], "", "", ""])
        for j, w in zip(range(1, 10), (10, 48, 12, 8, 11, 16, 13, 9, 10)):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    for j, w in zip(range(1, 10), (12, 12, 10, 44, 10, 70, 12, 8, 60)):
        audit.column_dimensions[get_column_letter(j)].width = w
    audit.freeze_panes = "A2"
    wb.save(out_path)


def write_client_workbook(company: str, mapped: dict[tuple[str, str], "MappedStatement"],
                          template: dict[tuple[str, str], list[ClientField]],
                          out_path: str) -> None:
    """client-template-layout workbook: template field order, one column per period,
    reported values where mapped, formula-computed where absent, plus a full
    audit sheet (fid -> source labels -> method -> verification)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    STMT_NAME = {"income": "Income Statement", "balance": "Balance Sheet",
                 "cashflow": "Cash Flow", "segment": "Segment Finance"}
    wb = Workbook()
    audit = wb.active
    audit.title = "Audit"
    audit.append(["Statement", "Scope", "Field id", "Display Name", "Method",
                  "Source report lines", "Unit", "Currency", "Verification"])
    for c in audit[1]:
        c.font = Font(bold=True)

    for (stmt, scope), ms in mapped.items():
        fields = template.get((stmt, scope))
        if not fields:
            continue
        ws = wb.create_sheet(f"{STMT_NAME.get(stmt, stmt)} - {scope.title()}"[:31])
        hdr = ["Display Name", "Field id"] + [_period_label(p) for p in ms.periods]
        ws.append(hdr)
        for c in ws[1]:
            c.font = Font(bold=True)
        row2fid = {f.row: f.fid for f in fields}
        for f in sorted(fields, key=lambda x: x.order):
            vals = []
            method = ""
            if f.fid in ms.facts:
                method = "reported" if len(ms.sources.get(f.fid, [])) == 1 else "summed"
                for p in ms.periods:
                    vals.append(ms.facts[f.fid].get(p.col))
            elif f.formula:
                got_any = False
                for p in ms.periods:
                    total, have = 0.0, 0
                    for sign, r in f.formula:
                        cf = row2fid.get(r)
                        if cf and cf in ms.facts and p.col in ms.facts[cf]:
                            total += sign * ms.facts[cf][p.col]
                            have += 1
                    vals.append(round(total, 2) if have >= 2 else None)
                    got_any = got_any or have >= 2
                method = "computed" if got_any else ""
            else:
                vals = [None] * len(ms.periods)
            if not any(v is not None for v in vals):
                continue
            ws.append([f.name, f.fid] + vals)
            if method:
                audit.append([stmt, scope, f.fid, f.name, method,
                              "; ".join(ms.sources.get(f.fid, [])),
                              ms.unit, ms.currency,
                              "; ".join(v for v in ms.verification if f"[{f.fid}]" in v)[:180]])
        ws.column_dimensions["A"].width = 52
        for j in range(3, 3 + len(ms.periods)):
            ws.column_dimensions[get_column_letter(j)].width = 20
        ws.freeze_panes = "C2"
        if ms.unmapped:
            audit.append([stmt, scope, "", "UNMAPPED LINES", "", "; ".join(ms.unmapped)[:300], "", "", ""])
    for j, w in zip(range(1, 10), (12, 12, 10, 44, 10, 70, 8, 8, 60)):
        audit.column_dimensions[get_column_letter(j)].width = w
    audit.freeze_panes = "A2"
    wb.save(out_path)
