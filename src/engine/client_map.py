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
    if "segment" in s:            # before balance: 'Segment Assets and Liabilities'
        return "segment"          # names the more specific statement
    if "assets and liabilities" in s or "balance sheet" in s:
        return "balance"
    if "results" in s or "profit and loss" in s:
        return "income"
    return None


def map_quarter(tables, template, taxonomy, model: str | None = None,
                default_unit: str = ""):
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
        ms = map_statement(merged, key[0], taxonomy, template[key], model=model)
        if default_unit and not ms.unit:
            ms.unit = default_unit        # units line is printed above the
        if key[0] == "cashflow":          # table, not inside the grid
            reconcile_cashflow_opening(ms, template[key])
        out[key] = ms
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
- Lines under a 'Components of cash and cash equivalents' block (balances with banks,
  'on current account', EEFC accounts, deposits with original maturity < 3 months, cash in
  hand, cheques on hand, remittances in transit) are the BREAKDOWN of closing cash, which is
  already mapped — return "" for every one of them.
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
    for hrow in header:
        hl = re.sub(r"^\s*\d+[.)]\s*", "", str(hrow[0] or "").lower())
        for pat, name in _SECTION_PAT:
            if re.search(pat, hl):
                section = name
                break
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
        elif len(idxs) >= 3:
            # arithmetic total-detection: if ONE line equals the sum of all the
            # others (every column), it IS their printed total — keep only it
            # (e.g. 'Cash at the end' followed by its components breakdown)
            def _vec(i, cols):
                return [rows[i][2].get(c) for c in cols]
            cols = sorted({c for i in idxs for c in rows[i][2]})
            hits = []
            for i in idxs:
                rest = [j for j in idxs if j != i]
                ok = True
                for c in cols:
                    tot = rows[i][2].get(c)
                    ssum = sum(rows[j][2].get(c, 0.0) for j in rest)
                    if tot is None or abs(ssum - tot) > max(0.02, 0.005 * abs(tot)):
                        ok = False
                        break
                if ok:
                    hits.append(i)
            if len(hits) == 1:
                for j in idxs:
                    if j != hits[0]:
                        assign.pop(j, None)
    # duplicate-total pruning: filings print the same figure twice (TCI in
    # the body AND as the 'attributable to' block total) or as a running
    # subtotal chain ('PBT before exceptional items' followed by 'PBT').
    # If one fid holds two such rows it double-counts. Purely arithmetic:
    #   identical value vectors            -> same printed fact repeated
    #   B - A == sum(rows strictly between)-> B is a later running subtotal
    # keep the row whose label best matches the client field name.
    from itertools import combinations as _combos
    _fname = {tf.fid: norm_label(tf.name) for tf in template_fields}
    _byfid: dict[str, list[int]] = {}
    for _i in range(len(rows)):
        _fd = assign.get(_i)
        if _fd and _fd in valid_fids and rows[_i][2]:
            _byfid.setdefault(_fd, []).append(_i)
    for _fd, _idxs in _byfid.items():
        if len(_idxs) < 2:
            continue
        _drop: set = set()
        for _a, _b in _combos(sorted(_idxs), 2):
            if _a in _drop or _b in _drop:
                continue
            _va, _vb = rows[_a][2], rows[_b][2]
            _cc = sorted(set(_va) & set(_vb))
            if len(_cc) < 2:
                continue
            _same = all(abs(_va[c] - _vb[c]) <= 0.02 for c in _cc)
            _chain = False
            if not _same:
                _mids = [rows[_m][2] for _m in range(_a + 1, _b)]
                _chain = all(abs((_vb[c] - _va[c])
                                 - sum(_mv.get(c, 0.0) for _mv in _mids)) <= 0.02
                             for c in _cc)
            if not (_same or _chain):
                continue
            def _sim(i):
                _lt = set(norm_label(rows[i][0]).split())
                _nt = set(_fname.get(_fd, "").split())
                return (len(_lt & _nt) / max(1, len(_lt | _nt)), i)
            _keep = max((_a, _b), key=_sim)
            _drop.add(_a if _keep == _b else _b)
        for _i in _drop:
            assign.pop(_i, None)
    facts: dict[str, dict[int, float]] = {}
    sources: dict[str, list[str]] = {}
    sources_vals: dict[str, dict[int, list]] = {}
    unmapped = []
    unmapped_vals: list = []
    for i, (label, sec, vals) in enumerate(rows):
        fid = assign.get(i, "")
        if fid and fid in valid_fids:
            tgt = facts.setdefault(fid, {})
            sv = sources_vals.setdefault(fid, {})
            for j, v in vals.items():
                tgt[j] = tgt.get(j, 0.0) + v
                sv.setdefault(j, []).append((label, v))
            sources.setdefault(fid, []).append(label)
        else:
            unmapped.append(label)
            unmapped_vals.append((label, dict(vals)))

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
    ms = MappedStatement(periods=periods, facts=facts, sources=sources,
                         unmapped=unmapped, verification=verification,
                         n_checks=n_checks, n_ok=n_ok,
                         unit=unit, multiplier=mult, currency=cur)
    ms.sources_vals = sources_vals            # exact per-line provenance
    ms.unmapped_vals = unmapped_vals
    return ms


# --------------------------------------------------------------------------- client workbook output

def _period_label(p: Period) -> str:
    lab = f"{p.span} {p.end or p.raw}"
    if p.audited:
        lab += f" ({p.audited})"
    return lab


_SPAN_MONTHS = {"3M": 3, "6M": 6, "9M": 9, "FY": 12}


def attach_provenance(ms: "MappedStatement", grid) -> None:
    """Rebuild per-period (report line, value) provenance for every mapped fid
    and every unmapped line from the raw grid — deterministic."""
    from collections import defaultdict
    rows_by_label = defaultdict(list)
    for row in grid:
        label, vals = _label_and_vals(row)
        if label and any(c.isalpha() for c in label):
            rows_by_label[norm_label(label)].append((label, vals))
    if not getattr(ms, "unmapped_vals", None):
        uv = []
        for lab in ms.unmapped:
            for orig, vals in rows_by_label.get(norm_label(lab), []):
                uv.append((orig, dict(vals)))
                break
        ms.unmapped_vals = uv
    if getattr(ms, "sources_vals", None):
        return                                  # exact provenance already recorded
    out: dict = {}
    for fid, labels in ms.sources.items():
        per: dict = defaultdict(list)
        seen = set()
        for lab in labels:
            nl = norm_label(lab)
            if nl in seen:
                continue
            seen.add(nl)
            for orig, vals in rows_by_label.get(nl, []):
                for col, v in vals.items():
                    per[col].append((orig, v))
        # duplicate-label rows can over-list: keep only a combination whose sum
        # matches the mapped value (try: all, keep-one, drop-one, keep-a-pair);
        # if nothing reconciles, say so rather than mislead
        from itertools import combinations
        for col, items in list(per.items()):
            fact = ms.facts.get(fid, {}).get(col)
            if fact is None or abs(sum(v for _l, v in items) - fact) <= 0.01:
                continue
            candidates = ([[it] for it in items]
                          + [items[:k] + items[k + 1:] for k in range(len(items))]
                          + ([list(c) for c in combinations(items, 2)] if len(items) <= 6 else []))
            for cand in candidates:
                if cand and abs(sum(v for _l, v in cand) - fact) <= 0.01:
                    per[col] = cand
                    break
            else:
                per[col] = [("(same-named report lines; exact split ambiguous)", fact)]
        out[fid] = dict(per)
    ms.sources_vals = out


def reconcile_cashflow_opening(ms: "MappedStatement",
                               template_fields: list[ClientField]) -> int:
    """Closing-identity completion for cash flows: if closing != beginning +
    net(+fx) and a subset of unmapped lines sums EXACTLY to the residual in
    every period, fold those lines into the beginning-cash field (they are
    printed opening-balance adjustments like 'Cash acquired on acquisition' or
    'Less: Bank overdraft'). Deterministic and arithmetic-gated. Returns the
    number of lines folded; folded rows are flagged 'adjusted' in the output."""
    from itertools import combinations

    def find(*pats):
        for f in template_fields:
            n = f.name.lower()
            if all(re.search(p, n) for p in pats):
                return f.fid
        return None

    BEG = find(r"cash and cash equivalents at the beginning")
    END = find(r"cash and cash equivalents at the end")
    FX = find(r"effect of exchange fluctuation")
    NET = find(r"net increase.*cash and cash equivalents")
    ADJ = find(r"^other adjustments$")
    f = ms.facts
    if not BEG or BEG not in f or not END or END not in f or not ADJ:
        return 0
    cols = sorted(set(f[BEG]) & set(f[END]))
    net = f.get(NET) or {}
    fx = f.get(FX) or {}
    resid = {}
    for c in cols:
        n = net.get(c)
        if n is None:
            continue
        resid[c] = round(f[END][c] - (f[BEG][c] + n + fx.get(c, 0.0)
                                      + f.get(ADJ, {}).get(c, 0.0)), 2)
    if not resid or all(abs(v) <= 0.02 for v in resid.values()):
        return 0
    uv = getattr(ms, "unmapped_vals", None) or []
    # candidates must come from the reconciliation zone: opening-balance
    # adjustments are printed BEFORE the closing-cash line. Lines from the
    # 'Components of cash and cash equivalents' breakdown (below closing) are
    # parts of the closing balance, never adjustments — without this cut the
    # subset search can grab one to absorb the filing's own rounding noise.
    end_zone = re.compile(r"cash and cash equivalents at the end|"
                          r"components of cash", re.IGNORECASE)
    zone_end = next((i for i, (lab, _v) in enumerate(uv) if end_zone.search(lab)),
                    len(uv))
    cand = [(i, lab, vals) for i, (lab, vals) in enumerate(uv[:zone_end])
            if any(c in vals for c in resid)]
    for size in range(1, min(4, len(cand)) + 1):
        for combo in combinations(cand, size):
            ok = all(abs(sum(vals.get(c, 0.0) for _i, _l, vals in combo) - r) <= max(0.02, 0.002 * abs(f[END][c]))
                     for c, r in resid.items())
            if not ok:
                continue
            sv = getattr(ms, "sources_vals", None) or {}
            svb = sv.setdefault(ADJ, {})
            for i, lab, vals in combo:
                for c, v in vals.items():
                    f.setdefault(ADJ, {})[c] = round(f.get(ADJ, {}).get(c, 0.0) + v, 2)
                    svb.setdefault(c, []).append((lab + " [adjusted]", v))
                ms.sources.setdefault(ADJ, []).append(lab)
                ms.verification.append(
                    f"~ reconciliation [{ADJ} Other Adjustments]: '{lab}' folded so that "
                    "beginning + net (+fx) + adjustments = closing ✓")
            drop = {i for i, _l, _v in combo}
            ms.unmapped_vals = [x for i, x in enumerate(uv) if i not in drop]
            ms.unmapped = [l for l in ms.unmapped
                           if l not in {lab for _i, lab, _v in combo}]
            return size
    return 0


def classify_unmapped(ms: "MappedStatement",
                      template_fields: list[ClientField]) -> list[tuple[str, str]]:
    """Deterministically classify every unmapped line by its ARITHMETIC
    relationship to the mapped fields — nothing is guessed:

      aggregate : the line equals a +/- combination of mapped fields (verified)
      component : the line belongs to a consecutive group that sums exactly to
                  a mapped field's value (verified — already included there)
      info      : neither — genuinely informational, no client field

    Returns [(class, comment)] aligned with ms.unmapped_vals."""
    from itertools import combinations
    names = {f.fid: f.name for f in template_fields}
    uv = getattr(ms, "unmapped_vals", None) or []
    facts = ms.facts

    def vec(d, cols):
        return tuple(round(d.get(c, 0.0), 2) for c in cols)

    def close(a, b):
        return all(abs(x - y) <= max(0.02, 0.005 * abs(y)) for x, y in zip(a, b))

    results: list = [None] * len(uv)

    # --- component groups: consecutive unmapped lines summing to a mapped fid
    for size in range(len(uv), 1, -1):
        for start in range(0, len(uv) - size + 1):
            idxs = list(range(start, start + size))
            if any(results[i] for i in idxs):
                continue
            cols = sorted({c for i in idxs for c in uv[i][1]})
            if not cols:
                continue
            total = {c: sum(uv[i][1].get(c, 0.0) for i in idxs) for c in cols}
            for fid, fv in facts.items():
                if all(c in fv for c in cols) and close(vec(total, cols), vec(fv, cols)):
                    for i in idxs:
                        results[i] = ("component",
                                      f"Already included in '{names.get(fid, fid)}' [{fid}] — "
                                      f"the group of {size} lines sums exactly to its value ✓")
                    break

    # --- aggregates: line = +/- combination of mapped fields
    fids = [f for f in facts if facts[f]]
    for i, (lab, vals) in enumerate(uv):
        if results[i] or not vals:
            continue
        cols = sorted(vals)
        cand = [f for f in fids if all(c in facts[f] for c in cols)]
        tv = vec(vals, cols)
        hit = None
        for f in cand:
            if close(vec(facts[f], cols), tv):
                hit = f"equals '{names.get(f, f)}' [{f}] ✓"
                break
        if hit is None:                        # sums first (clearer than differences)
            for f1, f2 in combinations(cand, 2):
                s = tuple(round(facts[f1][c] + facts[f2][c], 2) for c in cols)
                if close(s, tv):
                    hit = (f"= '{names.get(f1, f1)}' [{f1}] + '{names.get(f2, f2)}' [{f2}] ✓")
                    break
        if hit is None:
            for f1, f2 in combinations(cand, 2):
                d1 = tuple(round(facts[f1][c] - facts[f2][c], 2) for c in cols)
                d2 = tuple(round(facts[f2][c] - facts[f1][c], 2) for c in cols)
                if close(d1, tv):
                    hit = (f"= '{names.get(f1, f1)}' [{f1}] − '{names.get(f2, f2)}' [{f2}] ✓")
                elif close(d2, tv):
                    hit = (f"= '{names.get(f2, f2)}' [{f2}] − '{names.get(f1, f1)}' [{f1}] ✓")
                if hit:
                    break
        if hit:
            results[i] = ("aggregate",
                          f"No dedicated client field, but fully represented: {hit} "
                          "(shown for verification)")
    for i in range(len(uv)):
        if not results[i]:
            results[i] = ("info", "No matching client field — informational disclosure line; "
                                  "value not contained in any mapped field")
    return results


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
    # per-share figures are rupees, never the statement denomination
    # (filings print 'Rs. in lakhs EXCEPT EPS')
    _EPS = re.compile(r"\bEPS\b|earnings per (equity )?share", re.IGNORECASE)
    STMT_ORDER = {"income": 0, "balance": 1, "cashflow": 2, "segment": 3}
    HDR = ["Field id", "Display Name", "Period End", "Months", "Audited",
           "Value", "Denomination", "Currency", "Method", "Sub-items (report lines / calculation)"]
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
        byfid = {f.fid: f for f in fields}
        prov = getattr(ms, "sources_vals", None) or {}
        for f in sorted(fields, key=lambda x: x.order):
            per_vals = {}
            per_sub = {}
            method = ""
            if f.fid in ms.facts:
                method = "reported" if len(ms.sources.get(f.fid, [])) == 1 else "summed"
                if any("[adjusted]" in lab for items in prov.get(f.fid, {}).values()
                       for lab, _v in items):
                    method = "adjusted"
                for p in ms.periods:
                    if p.col in ms.facts[f.fid]:
                        per_vals[p.col] = ms.facts[f.fid][p.col]
                        items = prov.get(f.fid, {}).get(p.col, [])
                        per_sub[p.col] = "  +  ".join(f"{lab} = {v:,.2f}".rstrip("0").rstrip(".")
                                                      for lab, v in items)
            elif f.formula:
                for p in ms.periods:
                    total, have = 0.0, 0
                    parts = []
                    for sign, r in f.formula:
                        cf = row2fid.get(r)
                        if cf and cf in ms.facts and p.col in ms.facts[cf]:
                            total += sign * ms.facts[cf][p.col]
                            have += 1
                            parts.append(f"{'+' if sign > 0 else '-'} {byfid[cf].name} "
                                         f"[{cf}] = {ms.facts[cf][p.col]:,.2f}".rstrip("0").rstrip("."))
                    if have >= 2:
                        per_vals[p.col] = round(total, 2)
                        per_sub[p.col] = "  ".join(parts)
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
                           per_vals[p.col],
                           "rupees" if _EPS.search(f.name) else denom,
                           ms.currency, method,
                           per_sub.get(p.col, "")])
            audit.append([stmt, scope, f.fid, f.name, method,
                          "; ".join(ms.sources.get(f.fid, [])),
                          denom, ms.currency,
                          "; ".join(v for v in ms.verification if f"[{f.fid}]" in v)[:180]])
        if ms.unmapped:
            audit.append([stmt, scope, "", "UNMAPPED LINES", "",
                          "; ".join(ms.unmapped)[:300], "", "", ""])
        for j, w in zip(range(1, 11), (10, 48, 12, 8, 11, 16, 13, 9, 10, 80)):
            ws.column_dimensions[get_column_letter(j)].width = w
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    # dedicated sheet for report lines with NO client field — visible, with values
    un = wb.create_sheet("Unmapped")
    un.append(["Statement", "Scope", "Report line (as printed)", "Period End", "Months",
               "Value", "Denomination", "Reason"])
    for c in un[1]:
        c.font = Font(bold=True)
    # filings routinely reprint the same line (IFRS mirror, revenue block +
    # segment-revenues block): one identical (line, period, value) fact is
    # listed ONCE, keeping the most informative reason (verified ✓ over info)
    entries: dict = {}
    order: list = []
    RANK = {"component": 0, "aggregate": 0, "info": 1}
    for (stmt, scope) in keys:
        ms = mapped[(stmt, scope)]
        denom = ms.unit or "units"
        col2p = {p.col: p for p in ms.periods}
        klass = classify_unmapped(ms, template.get((stmt, scope), []))
        for k_i, (lab, vals) in enumerate(getattr(ms, "unmapped_vals", None) or []):
            cls, reason = klass[k_i]
            if stmt == "segment" and cls == "component":
                reason += " (per-segment member; template has no per-segment fields)"
            for col, v in vals.items():
                p = col2p.get(col)
                if p is None:
                    continue
                months = "" if stmt == "balance" else _SPAN_MONTHS.get(p.span, "")
                key = (stmt, scope, lab, p.end or p.raw, months, v)
                if key not in entries:
                    order.append(key)
                    entries[key] = (RANK.get(cls, 1), reason, denom)
                elif RANK.get(cls, 1) < entries[key][0]:
                    entries[key] = (RANK.get(cls, 1), reason, denom)
    for (stmt, scope, lab, pend, months, v) in order:
        _rk, reason, denom = entries[(stmt, scope, lab, pend, months, v)]
        un.append([STMT_NAME.get(stmt, stmt), scope.title(), lab,
                   pend, months, v, denom, reason])
    for j, w in zip(range(1, 9), (16, 12, 52, 12, 8, 16, 13, 72)):
        un.column_dimensions[get_column_letter(j)].width = w
    un.freeze_panes = "A2"
    un.auto_filter.ref = un.dimensions

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
    # per-share figures are rupees, never the statement denomination
    # (filings print 'Rs. in lakhs EXCEPT EPS')
    _EPS = re.compile(r"\bEPS\b|earnings per (equity )?share", re.IGNORECASE)
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
                if any("[adjusted]" in lab for items in prov.get(f.fid, {}).values()
                       for lab, _v in items):
                    method = "adjusted"
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
