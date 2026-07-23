"""Positional reconciliation of an LLM-transcribed statement grid against the
PDF text layer — the single number-correction mechanism of the pipeline.

Principle: the transcription is a HYPOTHESIS; the authority for every numeric
cell is the token PRINTED AT THAT ROW AND COLUMN POSITION on the page. Never
"any token on the page sharing the digits" (digit-string collision corrupted
Infosys: the EPS value 15.96 shares digits with the amount 1,596) and never a
format prior like "amounts are 2-decimal" (false for whole-crore filings).

How a grid is anchored to its page:
  * rows  — each numeric grid row is matched to a printed line by digit
            overlap + label words, monotonic down the page;
  * columns — printed numeric tokens are RIGHT-ALIGNED in statements, so the
            matched lines' token right-edges cluster into the true printed
            columns; grid value columns map to those clusters left-to-right
            when the counts agree (the normal case), and by anchor votes
            otherwise. A vote/order contradiction is a structural flag, never
            a guess.

One pass subsumes what used to be four separate patches:
  * decimal/grouping misreads    ('77.224' → printed '77,224'; '15.96' → '1,596')
  * duplicated period columns    (Dec-25 ≡ Mar-25: the printed Dec-25 tokens differ)
  * wrong-column / shifted reads (values re-placed by printed x-position)
  * dropped values and columns   (blank cells refilled from print)

Scope: DIGITAL pages only. On scanned pages (full-page images — even with an
embedded OCR text layer, whose digits are unreliable) this module abstains
completely; those statements are covered by consensus double-reads plus the
identity suite (src/engine/identities.py) and visible review flags.

When the text layer itself is corrupted (decimal comma '296,99', missing
decimal point '4820' for 48.20 — both real in this corpus), form selection
prefers whichever of {printed form, model form} parses as a CLEAN standard
number; the residual missing-decimal class is recovered by the magnitude
check `repair_dropped_decimals` (cross-period amounts never vary ~100×).
"""
from __future__ import annotations

import re
import statistics

# ---------------------------------------------------------------- tokens

_TRAIL = "*^#@;:"           # footnote marks / stray punctuation on numbers

_GROUPED_NUMBER = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}|\d+)"
    r"(?:\.\d+)?$"
)
_SPACE_GROUPED_NUMBER = re.compile(
    r"-?(?:\d{1,3}(?:\s+\d{3})+|\d{1,2}(?:\s+\d{2})+\s+\d{3})"
    r"(?:\.\d+)?$"
)


def _digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _clean_token(w: str) -> str:
    s = str(w or "").strip().strip(_TRAIL).rstrip(",").strip()
    wrapped = s.startswith("(") and s.endswith(")")
    core = s[1:-1].strip() if wrapped else s
    punct = re.sub(r"\s*([,.])\s*", r"\1", core)
    if punct != core and _GROUPED_NUMBER.fullmatch(punct):
        core = punct
    elif _SPACE_GROUPED_NUMBER.fullmatch(core):
        core = re.sub(r"\s+", "", core)
    return f"({core})" if wrapped else core


_NUM_FORM = re.compile(r"\(?-?[\d,.]*\d\)?$")


def _is_numeric_form(w: str) -> bool:
    w = _clean_token(w)
    return bool(re.search(r"\d", w)) and bool(_NUM_FORM.match(w))


# canonical CLEAN number shapes seen in Indian filings: western grouping,
# Indian grouping (1,45,575.77), plain integers/decimals (≤2 dp)
_CLEAN_SHAPES = [
    re.compile(r"\d{1,3}(,\d{3})+(\.\d{1,2})?$"),
    re.compile(r"\d{1,2}(,\d{2})+,\d{3}(\.\d{1,2})?$"),
    re.compile(r"\d+(\.\d{1,2})?$"),
]


def parse_clean(form: str):
    """(value, negative) if the token is a clean standard number, else None.
    Unbalanced parentheses ('1)' from a split word) are NOT clean."""
    s = _clean_token(form)
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    if s.startswith("(") or s.endswith(")"):
        return None
    if s.startswith("-"):
        neg, s = True, s[1:]
    if not any(p.match(s) for p in _CLEAN_SHAPES):
        return None
    try:
        return float(s.replace(",", "")), neg
    except ValueError:
        return None


def _pick_form(printed: str, model: str) -> str:
    """Digits agree, forms differ: prefer the form that parses as a CLEAN
    standard number. Both clean and same value ('124,936' vs '124936') → keep
    the model's (no churn). Both clean, different value ('2.15' — a note
    reference — vs printed '215') → the print is the authority. Only the
    model's clean ('296.99' against a corrupted text layer's '296,99') → the
    model saw the pixels, keep it."""
    p, m = _clean_token(printed), _clean_token(model)
    pv, mv = parse_clean(p), parse_clean(m)
    if pv is not None and mv is not None:
        return m if pv == mv else p
    if pv is not None:
        return p
    if mv is not None:
        return m
    return p


# ---------------------------------------------------------------- page model

def _merge_fragments(words):
    """Rejoin numeric tokens the text layer split ('11,' + '099' → '11,099',
    '(1,' + '026)' → '(1,026)') — custom fonts fragment numbers mid-group.
    Merge when two adjacent tokens are near-touching and their concatenation
    still looks like one number."""
    words = sorted(words)                      # by x0
    out = []
    for x0, x1, w in words:
        if out:
            px0, px1, pw = out[-1]
            joined = pw + w
            if (x0 - px1 <= 3.0
                    and re.search(r"[\d,.(]$", pw) and re.match(r"[\d,.)]", w)
                    and re.fullmatch(r"\(?-?[\d,.]*\d\)?\*?", joined.strip("*^#@"))):
                out[-1] = (px0, x1, joined)
                continue
        out.append((x0, x1, w))
    return out


def _visual_lines(words, tolerance: float = 3.0):
    """Cluster PDF words that share a visual baseline.

    Bold labels and regular-weight amounts in the same table row can have
    slightly different y coordinates. Fixed rounding buckets split a row when
    those coordinates happen to fall on opposite sides of a bucket boundary
    (for example 513.7 vs 515.1). Greedy center-line clustering has no such
    boundary and remains far below normal statement line spacing.
    """
    groups = []
    for y, x0, x1, word in sorted(words):
        if groups and abs(y - groups[-1][0]) <= tolerance:
            groups[-1][1].append((x0, x1, word))
            groups[-1][0] = statistics.median(
                item[0] for item in groups[-1][2] + [(y, x0, x1, word)]
            )
            groups[-1][2].append((y, x0, x1, word))
        else:
            groups.append([y, [(x0, x1, word)], [(y, x0, x1, word)]])
    return [group[1] for group in groups]


def page_word_lines(pdf_path: str) -> list:
    """Per page: printed visual lines as [(x_right, word), ...] sorted by x,
    lines in top-to-bottom order. RIGHT edges, because statement numbers are
    right-aligned — '(1,246)' and '532' in one column share a right edge, not
    a centre. Coordinates are the truth even when the text layer's extraction
    order is scrambled (label-block/value-block OCR layouts)."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    out = []
    for pg in doc:
        words = []
        for x0, y0, x1, y1, w, *_ in pg.get_text("words"):
            words.append(((y0 + y1) / 2, x0, x1, w))
        page = []
        for v in _visual_lines(words):
            page.append([(x1, w) for _x0, x1, w in _merge_fragments(v)])
        out.append(page)
    doc.close()
    return out


# ---------------------------------------------------------------- text-layer trust

_OCR_FONT = re.compile(r"glyphless|ocr|invisible", re.IGNORECASE)
_REAL_FONT = re.compile(r"helvetica|times|arial|calibri|cambria|georgia|verdana|"
                        r"courier|garamond|book|frutiger|univers|futura|roboto|"
                        r"lato|open.?sans|source|noto|segoe|tahoma|trebuchet|"
                        r"myriad|minion|palatino|century|franklin|gill|optima",
                        re.IGNORECASE)


def untrusted_text_pages(pdf_path: str) -> set[int]:
    """1-based pages whose text layer must NOT be used as authority.

    A page's text is untrusted when it is an OCR overlay on a scan — detected
    by the OCR engines' marker fonts (Tesseract 'GlyphLessFont', ABBYY
    'HiddenHorzOCR', …), or by a full-page image whose text carries no
    recognizable real font (scanner-generated CID stubs). A DIGITAL page that
    merely has a full-page background image (letterheads, watermarks) keeps
    real fonts and stays trusted — the naive image-area heuristic used for
    vision routing misclassifies those."""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    out = set()
    for i in range(len(doc)):
        page = doc[i]
        fonts = " ".join(str(f[3]) for f in page.get_fonts())
        if _OCR_FONT.search(fonts):
            out.add(i + 1)
            continue
        text_len = len(page.get_text().strip())
        if text_len < 40:
            out.add(i + 1)
            continue
        parea = abs(page.rect)
        fullpage_img = False
        for img in page.get_images(full=True):
            try:
                if abs(page.get_image_bbox(img)) > 0.8 * parea:
                    fullpage_img = True
                    break
            except Exception:
                pass
        if fullpage_img and not _REAL_FONT.search(fonts):
            out.add(i + 1)
    doc.close()
    return out


# ---------------------------------------------------------------- alignment

class _Line:
    __slots__ = ("nums", "digs", "words")

    def __init__(self, toks):
        self.nums = []                    # (x_right, form) — numeric tokens
        words = []
        for x, w in toks:
            wc = _clean_token(w)
            if _is_numeric_form(wc):
                self.nums.append((x, wc))
            else:
                words.append(w)
        self.digs = {_digits(f) for _x, f in self.nums if len(_digits(f)) >= 3}
        self.words = set(re.findall(r"[a-z]{3,}", " ".join(words).lower()))


def _grid_row_features(row):
    """(label words, digit strings ≥3 digits, value cells {padded col: form})."""
    label_cells, vals = [], {}
    for j, c in enumerate(row):
        s = str(c or "").strip()
        if not s:
            continue
        if _is_numeric_form(s):
            vals[j] = _clean_token(s)
        elif any(ch.isalpha() for ch in s):
            label_cells.append(s)
    words = set(re.findall(r"[a-z]{3,}", " ".join(label_cells).lower()))
    digs = {_digits(v) for v in vals.values() if len(_digits(v)) >= 3}
    return words, digs, vals


def _match_rows(grid, lines):
    """Best printed line per numeric grid row, monotonic down the page.

    Score = digit-overlap ratio (weight 2) + label-word Jaccard. Digits pin a
    row even when the label is garbled; the label pins it even when the VALUES
    were transcribed wrong (exactly the case being repaired). A weighted
    longest-nondecreasing-subsequence over line order drops any assignment
    that would scramble the reading order."""
    cand = []
    for ri, row in enumerate(grid):
        words, digs, vals = _grid_row_features(row)
        if not vals:
            continue
        best, bscore = None, 0.0
        for li, L in enumerate(lines):
            if not L.nums:
                continue
            ds = len(digs & L.digs) / len(digs) if digs else 0.0
            wj = (len(words & L.words) / len(words | L.words)
                  if words and L.words else 0.0)
            score = 2.0 * ds + wj
            if score > bscore:
                bscore, best = score, li
        if best is None:
            continue
        L = lines[best]
        ds_abs = len(digs & L.digs)
        wj = (len(words & L.words) / len(words | L.words)
              if words and L.words else 0.0)
        # accept on real evidence only: half the digits found, or a clearly
        # matching label (≥2 shared words and majority Jaccard)
        if ds_abs >= max(1, len(digs) // 2) or (len(words & L.words) >= 2 and wj >= 0.5):
            cand.append((ri, best, bscore))
    n = len(cand)
    if not n:
        return {}
    wsum = [c[2] for c in cand]
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if cand[j][1] <= cand[i][1] and wsum[j] + cand[i][2] > wsum[i]:
                wsum[i] = wsum[j] + cand[i][2]
                prev[i] = j
    i = max(range(n), key=lambda k: wsum[k])
    keep = {}
    while i != -1:
        keep[cand[i][0]] = cand[i][1]
        i = prev[i]
    return keep


def _cluster_columns(lines, matched_lis):
    """1-D cluster the matched lines' numeric-token right-edges into printed
    columns. Returns [(center, tokens_count, big_count)] sorted by x."""
    xs = []
    for li in matched_lis:
        for x, f in lines[li].nums:
            xs.append((x, f))
    if not xs:
        return []
    xs.sort()
    clusters = [[xs[0]]]
    for x, f in xs[1:]:
        if x - clusters[-1][-1][0] > 18.0:      # > ~a digit-group width apart
            clusters.append([])
        clusters[-1].append((x, f))
    out = []
    for cl in clusters:
        n_big = sum(1 for _x, f in cl
                    if len(_digits(f)) >= 4 or "," in f)
        out.append((statistics.median(x for x, _f in cl), len(cl), n_big))
    return out


# ---------------------------------------------------------------- reconcile

# below this share of the grid's numbers printed on the chosen page span, the
# span is a different layout of the statement, not the full source
_FULL_AUTHORITY_COVERAGE = 0.80

def reconcile(grid, lines_raw, coverage: float = 1.0, log=None):
    """Verify/correct every numeric cell of `grid` against the printed page
    lines. Returns (new_grid, report). Abstains (grid unchanged,
    report['abstained']=True) whenever it cannot anchor the grid to the page —
    it must never guess.

    coverage: share of the grid's numbers the chosen page span prints (from
    candidate_spans). Below _FULL_AUTHORITY_COVERAGE the page cannot be the
    complete source (e.g. a 4-column reprint of a 5-column statement), so the
    pass runs CONSERVATIVELY: form fixes and blank fills only, never a
    digits-differ rewrite."""
    report = {"abstained": False, "corrections": [], "filled": 0,
              "unmatched_rows": 0, "numeric_rows": 0,
              "structure_mismatch": False, "unverified_cols": [],
              "conservative": False}
    lines = [_Line(ln) for ln in lines_raw]
    matches = _match_rows(grid, lines)
    numeric_rows = [ri for ri, row in enumerate(grid) if _grid_row_features(row)[2]]
    report["numeric_rows"] = len(numeric_rows)
    report["unmatched_rows"] = len([r for r in numeric_rows if r not in matches])
    if len(matches) < 3 or len(matches) < 0.3 * max(1, len(numeric_rows)):
        report["abstained"] = True
        return grid, report

    # ---- printed columns (clusters of right-aligned token edges)
    clusters = _cluster_columns(lines, set(matches.values()))
    min_rows = max(3, len(matches) // 4)

    # ---- anchor votes: grid column -> printed cluster, from cells whose
    # digits are found verbatim in their matched line (the grid is mostly
    # right — its correct cells locate the columns; wrong cells get outvoted)
    def _cl_of(x):
        best = min(range(len(clusters)), key=lambda k: abs(clusters[k][0] - x))
        return best if abs(clusters[best][0] - x) <= 14.0 else None

    votes: dict[int, dict[int, int]] = {}
    for ri, li in matches.items():
        L = lines[li]
        byd: dict[str, list] = {}
        for x, f in L.nums:
            byd.setdefault(_digits(f), []).append(x)
        for j, form in _grid_row_features(grid[ri])[2].items():
            d = _digits(form)
            if len(d) >= 3 and len(byd.get(d, [])) == 1:
                k = _cl_of(byd[d][0])
                if k is not None:
                    votes.setdefault(j, {})[k] = votes.get(j, {}).get(k, 0) + 1
    vote_map = {}
    for j, vc in votes.items():
        k, n = max(vc.items(), key=lambda it: it[1])
        if n >= 3 and n >= 0.6 * sum(vc.values()):
            vote_map[j] = k

    # value clusters: printed columns that carry real amounts — voted by the
    # grid, or dominated by big (≥4-digit / grouped) tokens. This drops
    # note-reference and serial-number columns.
    value_ks = sorted({k for k in vote_map.values()}
                      | {k for k, (cx, n, nbig) in enumerate(clusters)
                         if n >= min_rows and nbig >= 0.5 * n})
    grid_cols = sorted({j for ri in numeric_rows
                        for j in _grid_row_features(grid[ri])[2]
                        if sum(1 for r2 in numeric_rows
                               if j in _grid_row_features(grid[r2])[2]) >= 2})

    # ---- column mapping. Left-to-right order is the ground truth of a table;
    # when the counts agree, map by order and use the votes as a CHECK. When
    # they disagree, trust only strictly-increasing voted columns and flag.
    col_map: dict[int, int] = {}
    if len(grid_cols) == len(value_ks):
        col_map = dict(zip(grid_cols, value_ks))
        disagreement = sum(1 for j in grid_cols
                           if j in vote_map and vote_map[j] != col_map[j])
        if disagreement:
            report["structure_mismatch"] = True   # shifted/duplicated columns:
    else:                                         # order mapping REPAIRS them
        report["structure_mismatch"] = True
        last = -1
        for j in grid_cols:                       # strictly-increasing votes only
            k = vote_map.get(j)
            if k is not None and k > last:
                col_map[j] = k
                last = k
        report["unverified_cols"] = [j for j in grid_cols if j not in col_map]
    if len(col_map) < 1:
        report["abstained"] = True
        return grid, report

    # ---- authority level. A page that does not print ~all of the grid's
    # numbers is a DIFFERENT layout of the statement (fewer period columns) —
    # its positions must not overwrite values it never printed. Same when the
    # column mapping itself is in doubt: fix forms, fill blanks, flag — the
    # identity suite + repair loop own the rest.
    conservative = coverage < _FULL_AUTHORITY_COVERAGE or bool(report["unverified_cols"])
    report["conservative"] = conservative

    # ---- per-cell verification/correction
    out = [list(r) for r in grid]
    for ri, li in matches.items():
        L = lines[li]
        placed: dict[int, tuple] = {}
        for x, f in L.nums:
            k = _cl_of(x)
            if k is None:
                continue
            if k not in placed or abs(clusters[k][0] - x) < abs(clusters[k][0] - placed[k][0]):
                placed[k] = (x, f)
        vals = _grid_row_features(out[ri])[2]
        for j, k in col_map.items():
            p = placed.get(k)
            g = vals.get(j)
            if p is None:
                # Print has no token in this column. Normally we leave the cell
                # (a genuine nil, or a token the clustering missed). But under
                # FULL authority, if the grid's value here is the SAME digit
                # string the page prints in ANOTHER value column of this row,
                # it is a value duplicated into the wrong column (the model
                # copied a sparse row's single figure across) — null it so the
                # row's arithmetic is not thrown off by a phantom entry.
                if not conservative and g is not None:
                    gd = _digits(g)
                    dup = any(kk != k and _digits(pp[1]) == gd
                              for kk, pp in placed.items() if kk in value_ks)
                    if gd and len(gd) >= 3 and dup:
                        out[ri][j] = "-"
                        report["corrections"].append((ri, j, g, "-"))
                continue                    # print has nothing here: leave the cell
            pf = p[1]
            if g is None:
                cell = str(out[ri][j] or "").strip() if j < len(out[ri]) else ""
                if (not cell or cell in "-–—") and not conservative:
                    while len(out[ri]) <= j:
                        out[ri].append("")
                    out[ri][j] = pf         # model dropped a printed value
                    report["filled"] += 1
                continue
            if _digits(pf) == _digits(g):
                new = _pick_form(pf, g)
                if new != g:
                    report["corrections"].append((ri, j, g, new))
                    out[ri][j] = new
            elif not conservative:
                # The text token's digits may themselves be corrupt (custom
                # fonts can drop a leading parenthesized digit). Apply the
                # same clean-form arbitration used above: a malformed print
                # token must not overwrite a clean model reading.
                new = _pick_form(pf, g)
                if new != g:
                    report["corrections"].append((ri, j, g, new))
                    out[ri][j] = new
    return out, report


# ---------------------------------------------------------------- entry point

def candidate_spans(grid, page_forms, k: int = 3) -> list[tuple[list[int], float]]:
    """Top-k page spans by coverage of the grid's distinct digit strings
    (single pages and adjacent pairs only — a table never spans more, and a
    large filing prints the same statement twice with DIFFERENT layouts, so
    far-apart pages must never be pooled)."""
    gd = {_digits(c) for row in grid for c in row if re.search(r"\d", str(c))}
    gd = {d for d in gd if len(d) >= 3}
    if not gd:
        return []
    n = len(page_forms)
    scored = []
    for p in range(n):
        for span in ([p], [p, p + 1] if p + 1 < n else None):
            if span is None:
                continue
            got = set()
            for q in span:
                got |= gd & set(page_forms[q])
            if len(got) >= 3:
                scored.append((len(got) / len(gd), span))
    scored.sort(key=lambda t: (-t[0], t[1][0], len(t[1])))
    # keep overlapping spans (a table crossing a page break needs [p, p+1]
    # even when [p] alone scores highest); skip only exact duplicates
    out, seen = [], set()
    for cov, span in scored:
        key = tuple(span)
        if key in seen:
            continue
        seen.add(key)
        out.append((span, cov))
        if len(out) >= 2 * k:
            break
    return out


def has_text_authority(grid, page_forms, untrusted_pages) -> bool:
    """True when some TRUSTED page span prints (nearly) all of this grid's
    numbers — i.e. positional reconciliation can verify it. Statements without
    text authority (scans) are the only ones that justify paying for a second
    independent read."""
    for span, cov in candidate_spans(grid, page_forms):
        if cov >= _FULL_AUTHORITY_COVERAGE and not all(
                (p + 1) in untrusted_pages for p in span):
            return True
    return False


def reconcile_with_source(grid, section, title, page_forms, page_lines,
                          scan_pages=None, log=None):
    """Reconcile `grid` against its best source pages: try the top candidate
    spans and keep the outcome with the fewest failing identities (ties: most
    verified rows). Guard rails:

      * only statements WITH a verifying identity suite are corrected —
        an unverifiable "correction" is a guess, so segment tables (repeated
        sub-blocks, blank row labels, digit collisions) get detection only;
      * spans on scanned pages are skipped — their text layer is OCR noise;
      * a candidate that fails MORE identities than the input grid is never
        accepted — the input is a candidate too.

    Returns (grid, report | None)."""
    from src.engine import identities
    if identities.suite_for(section, title) is None:
        return grid, None
    scan_pages = scan_pages or set()
    baseline = len(identities.failing(section, title, grid))
    best = None
    for span, cov in candidate_spans(grid, page_forms):
        if all((p + 1) in scan_pages for p in span):
            continue
        pool = [ln for p in span for ln in page_lines[p] if (p + 1) not in scan_pages]
        if not pool:
            continue
        g2, rep = reconcile(grid, pool, coverage=cov)
        if rep["abstained"]:
            continue
        checks = identities.run_checks(section, title, g2)
        nfail = sum(1 for _n, ok in checks if not ok)
        if nfail > baseline:
            continue
        # a grid with NO checkable identity rows gives the tournament nothing
        # to referee — accept value rewrites there only from a span that is
        # near-certainly the source (a wrong twin page could rewrite silently)
        if not checks and (rep["corrections"] or rep["filled"]) and cov < 0.9:
            continue
        key = (nfail, rep["unmatched_rows"], -len(rep["corrections"]) - rep["filled"])
        if best is None or key < best[0]:
            rep["pages"] = [p + 1 for p in span]
            rep["coverage"] = cov
            best = (key, g2, rep)
    if best is None:
        return grid, None
    return best[1], best[2]


# ---------------------------------------------------------------- residual source corruption

def _is_2dp(form) -> bool:
    s = str(form).strip().strip("()").replace(",", "").replace(" ", "")
    return bool(re.fullmatch(r"\d+\.\d{2}", s))


def _cell_value(form):
    s = str(form or "").strip()
    if not re.fullmatch(r"[()\d,.\s₹-]+", s) or not re.search(r"\d", s):
        return None, False
    neg = s.startswith("(") and s.endswith(")")
    s2 = s.strip("()").replace(",", "").replace(" ", "").replace("₹", "")
    try:
        v = float(s2)
    except ValueError:
        return None, False
    return (-v if neg else v), neg


def _is_bare_int(form):
    s = str(form).strip().strip("()").replace(",", "").replace(" ", "").replace("₹", "")
    return bool(re.fullmatch(r"\d{3,}", s))


def repair_dropped_decimals(grid):
    """Recover decimals ABSENT from the source text itself (custom fonts can
    print '48.20' whose text layer says '4820' — positional reconciliation
    then faithfully reproduces the corruption). Cross-period magnitude is the
    only remaining signal: one line item never varies ~100× across the
    quarter/half-year/year columns. A bare integer ≥20× its row's 2-dp peers
    whose ÷100 lands back in their range is a dropped decimal. Requires ≥2
    clean 2-dp peers, so integer-denominated filings (whole crores) and
    header/year rows are never touched."""
    out = [list(r) for r in grid]
    for ri, row in enumerate(out):
        cells = []
        for ci, c in enumerate(row):
            v, neg = _cell_value(c)
            if v is not None:
                cells.append((ci, v, str(c).strip(), neg))
        peers = [abs(v) for _ci, v, form, _neg in cells if _is_2dp(form) and abs(v) > 0]
        if len(peers) < 2:
            continue
        med = statistics.median(peers)
        if med <= 0:
            continue
        for ci, v, form, neg in cells:
            if _is_bare_int(form) and abs(v) >= 20 * med:
                corr = v / 100.0
                if med / 5 <= abs(corr) <= med * 5:
                    out[ri][ci] = f"({abs(corr):.2f})" if neg else f"{corr:.2f}"
    return out


# ---- glyph-level text-layer corruption on boxed/bold subtotal cells --------
# Some digital PDFs render the totals band (the ruled/bold rows) with a font
# whose glyphs mis-map in the text layer even though the PAGE is perfectly
# legible: the opening parenthesis extracts as the digit '1' — '(726.99)'
# becomes '(1726.99)' or '1726.99)' — and thousands-commas extract as spaces —
# '1,645.32' -> '1 645.32', '(2,416.55)' -> '(12 416.55)'.  Positional
# reconciliation cannot fix this because the pixels AGREE with the corrupt
# text, and the file still reports high text-authority coverage.  What breaks
# is the arithmetic: the statement's own printed cross-identities no longer
# tie.  Those identities are exactly the constraint that pins the true value,
# so the repair is accepted ONLY when it makes the failing checks tie out.

_SPACE_SEP = re.compile(r"(?<=\d) (?=\d{3}(?:\D|$))")      # thousands space
_PAREN_GLYPH = re.compile(r"\(?1(\d{3,}(?:\.\d+)?)\)")     # '(' rendered as '1'
_COMMA_DEC = re.compile(r",(\d{1,2})(\)?)$")               # decimal '.' -> ','


def _fix_spaces(cell):
    """'1 645.32' -> '1645.32', '(12 416.55)' -> '(12416.55)'."""
    return _SPACE_SEP.sub("", str(cell))


_DOT_AS_THOUSANDS = re.compile(r"\.(\d{3})(?=\D|$)")     # '.' before exactly 3 digits
_CLEAN_NUM = re.compile(r"\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?")


def _repair_bracket_glyph(cell):
    """Un-mangle a boxed NEGATIVE whose parenthesis rendered as a stray '1' and
    whose thousands comma rendered as a decimal point — the same font artifact,
    e.g. text layer '121.914)' for the printed '(21,914)'.  Fires ONLY on this
    exact, self-proving signature (no identity gate needed):
      * an UNBALANCED bracket — a lone '(' or ')' cannot occur in a clean number,
        so the value was parenthesised (negative);
      * a '.' before EXACTLY three digits — a thousands separator, never a
        decimal (statement amounts carry <=2 decimals);
      * a stray '1' at the mis-rendered-bracket end, which is dropped.
    All three must hold, so a genuine value is never altered. Corruptions that
    do NOT match (a dropped digit, a lone bracket without the dot artifact) are
    left untouched for the identity checks — this never GUESSES a magnitude."""
    s = str(cell).strip()
    open_b, close_b = s.startswith("("), s.endswith(")")
    core = (s[1:-1] if open_b and close_b
            else s[1:] if open_b else s[:-1] if close_b else s)
    fixed = _DOT_AS_THOUSANDS.sub(r",\1", core)
    if fixed == core:                            # no comma-rendered-as-dot: not this glyph
        return s
    if open_b ^ close_b:                         # lone bracket: the pair became a stray '1'
        if close_b and fixed.startswith("1"):
            fixed = fixed[1:]                    # leading '1' was the lost '('
        elif open_b and fixed.endswith("1"):
            fixed = fixed[:-1]                   # trailing '1' was the lost ')'
        else:
            return s                             # dropped digit, not a stray '1' -> skip
        neg = True
    else:
        neg = open_b and close_b                 # balanced parens -> negative
    return f"({fixed})" if (neg and _CLEAN_NUM.fullmatch(fixed)) \
        else (fixed if _CLEAN_NUM.fullmatch(fixed) else s)


def repair_bracket_glyphs(grid):
    """Apply `_repair_bracket_glyph` to every cell. A no-op on clean cells (they
    have balanced or no brackets), so it is safe to run UNCONDITIONALLY on every
    statement — it corrects the boxed-total paren/comma artifact even when the
    printed subtotals were read correctly and no identity fails to trigger the
    identity-gated repair (the Wipro consolidated cash-flow case)."""
    return [[_repair_bracket_glyph(c) for c in row] for row in grid]


def _fix_comma_decimal(cell):
    """A decimal point rendered as a comma on a boxed cell: '1,833.09' extracts
    as '1,833,09'.  The signature is a trailing comma-group of ONE or TWO
    digits (a real thousands group is always three), so only that final comma
    is turned back into a point: '1,833,09' -> '1,833.09', '(12,34)' -> '(12.34)'.
    Genuine grouped integers ('1,833', '10,834') are untouched."""
    return _COMMA_DEC.sub(lambda m: "." + m.group(1) + m.group(2), str(cell).strip())


def _fix_paren_glyph(cell):
    """A parenthesised magnitude written WITHOUT a thousands separator and led
    by a spurious '1' (the mis-rendered '('): '(1726.99)' -> '(726.99)'.  A
    genuine >=1000 negative prints its comma ('(1,726.99)'), so a comma-less
    leading '1' is the corruption signature; the identity gate below is the
    real guard against mutating a legitimate value."""
    s = str(cell).strip()
    m = _PAREN_GLYPH.fullmatch(s)
    return f"({m.group(1)})" if m else s


def repair_glyph_by_identity(grid, section, title):
    """Recover the glyph corruption above, gated on the statement's identities.

    Returns (repaired_grid, checks_fixed).  A candidate repair is adopted only
    when it makes at least one previously-failing (or inactive-because-a-value
    would-not-parse) identity newly TIE, and breaks NO previously-passing
    identity.  That is the guard against a value-mutating fix shipping a wrong
    number: the mutated cells must be corroborated by an arithmetic identity of
    the statement itself — here op+inv+fin=net across every period column, a
    multi-equation confirmation a coincidental mutation cannot satisfy.  Any
    residual failure (a DIFFERENT corruption the glyph fix doesn't address)
    stays visibly flagged; we ship what is provable, never a guess."""
    from src.engine.identities import run_checks     # local: avoid import cycle

    def _status(g):
        return {name: ok for name, ok in run_checks(section, title, g)}

    base = _status(grid)
    if not base or all(base.values()):
        return grid, 0

    g_space = [[_fix_spaces(c) for c in row] for row in grid]
    g_both = [[_fix_comma_decimal(_fix_paren_glyph(c)) for c in row] for row in g_space]
    best, best_fixed = grid, 0
    for cand in (g_both, g_space):
        st = _status(cand)
        broke = any(base.get(n) and not ok for n, ok in st.items())
        newly = sum(1 for n, ok in st.items() if ok and not base.get(n))
        if not broke and newly > best_fixed:
            best, best_fixed = cand, newly
    return best, best_fixed
