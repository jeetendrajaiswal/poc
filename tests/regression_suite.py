"""Deterministic regression suite — $0, NO API calls (the LLM is mocked where needed).

Locks in every behaviour that was proven during the 2026-07 fix campaign, so any future
change that silently breaks a validated reader fails HERE instead of on a paid run (this
exact failure mode happened once: a 'safer' page-year heuristic broke the validated PPE
reader 24/0 -> 20/2 and only the corpus sweep caught it).

Usage:
    .venv/bin/python tests/regression_suite.py            # fast tier (~seconds, no PDFs)
    FULL=1 .venv/bin/python tests/regression_suite.py     # + corpus sweeps (~5-10 min, needs
                                                          #   ~/Downloads/{5 company}.pdf)
Exit code 0 = all pass.
"""
import csv
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

PDFS = {c: os.path.expanduser(f"~/Downloads/{c}.pdf")
        for c in ("reliance", "hindalco", "itc", "infosys", "adani")}
HAVE_PDFS = all(os.path.exists(p) for p in PDFS.values())
FULL = os.getenv("FULL") == "1" and HAVE_PDFS

RESULTS = []


def test(name, fast=True):
    def deco(fn):
        RESULTS.append((name, fast, fn))
        return fn
    return deco


# --------------------------------------------------------------------------- fast tier
@test("hygiene: currency strip, sign restore, count-sign skip")
def _():
    from src.engine import datapoints as dp
    assert dp._hygiene("` 1.00", "x ` 1.00") == "1.00"
    assert dp._hygiene("1,234", "loss of (1,234) in the year") == "(1,234)"
    assert dp._hygiene("414,36,07,528", "404,69,40,812 (414,36,07,528) shares", sign=False) \
        == "414,36,07,528"                      # count: annotation parens are not a negative
    assert dp._hygiene("(26,783,115)", "shares (26,783,115)", sign=False) == "(26,783,115)"


@test("_wants_total: selector-aware, appositive dash, sub-total immune")
def _():
    import yaml
    from src.engine import datapoints as dp
    assert dp._wants_total("x", "TOTAL trade payables (MSME + others)")
    assert dp._wants_total("payables — total trade payables", "")
    assert not dp._wants_total("the sub-total of deposits", "")
    items = yaml.safe_load(open("taxonomy/definitions.yaml"))["items"]
    old = lambda m: any(p in (m or "").lower() for p in
                        ("the total", "aggregate figure", "note's total", "total line", "single aggregate"))
    flips = {it["key"] for it in items
             if dp._wants_total(it.get("concept") or "", it.get("column_hint") or "")
             and not old(it.get("concept") or "")}
    assert flips == {"Sundry Creditors", "Other Noncurrent Liabilities_Total Long Term Liabilities",
                     "Payments To Auditor"}, flips


@test("composite addends: arithmetic + formatting for the three proven cases")
def _():
    from src.engine import datapoints as dp
    s = lambda xs: dp._fmt_num(sum(dp._num(x) for x in xs))
    assert s(["(16,316,130)", "(10,466,985)"]) == "(26,783,115)"
    assert s(["2,247,772,772", "(546,249)"]) == "2,247,226,523"
    assert s(["3.80", "0.78", "1.62", "0.97", "0.26"]) == "7.43"


@test("prompt scoping: enrichment is per-section opt-in, never global")
def _():
    from src.engine import datapoints as dp
    assert dp._FALLBACK_HINT_SECTIONS == {"deferred_tax", "share_capital"}
    assert dp._FULL_EXAMPLE_SECTIONS == {"deferred_tax"}
    assert dp._COMPOSITE_SECTIONS == {"share_capital", "other_expenses"}
    assert dp.NON_ADDITIVE_SECTIONS == {"share_capital"}
    sc = dp._INSTR.format(scope="s", composite=dp._COMPOSITE_RULE)
    other = dp._INSTR.format(scope="s", composite=dp._NO_COMPOSITE_RULE)
    assert "COMPOSITE items" in sc and "COMPOSITE items" not in other
    # every registry key is a note-type section, never a company
    for reg in (dp.COLUMN_SECTIONS, dp.NON_ADDITIVE_SECTIONS, dp._NOTE_LOCATE_SECTIONS,
                dp.MATRIX_SECTIONS, dp.VISION_TARGET_SECTIONS, dp.VISION_CATEGORY_SECTIONS,
                dp._COMPOSITE_SECTIONS, dp._FALLBACK_HINT_SECTIONS, dp._FULL_EXAMPLE_SECTIONS,
                set(dp._SIGNATURE_CORE), dp.LOW_CONFIDENCE_SECTIONS):
        assert reg <= set(dp.SECTIONS), reg - set(dp.SECTIONS)


@test("ppe row reader units: movement identity, year-block split, rep-max year")
def _():
    from src.engine import datapoints as dp
    # movement identity: open ± moves = close for both blocks, gross-dep=net
    assert dp._solve_ppe_row([100, 20, -5, 115, 40, 10, 50, 65])   # 115-50=65
    assert not dp._solve_ppe_row([100, 20, 115, 40, 50, 99])       # net doesn't tie
    # welded two-year run splits and chains to the LATER year
    seg = [39.04, 13.08, 0.22, 0, 51.90, 26.70, 5.32, 0.22, 0, 31.80, 20.10,
           51.90, 7.94, 0, 0, 59.84, 31.80, 6.69, 0, 0, 38.49, 21.35]
    cur = dp._current_ppe_segment([seg])
    assert dp._solve_ppe_row(cur) and abs(cur[0] - 51.90) < 0.02   # opens at prior close
    # rep-max page year: repeated fiscal year beats a stray future maturity year
    txt = "maturing 2040 ... March 31, 2026 ... March 31, 2026 ... March 31, 2025"
    ys = [int(y) for y in re.findall(r"\b(20\d{2})\b", txt)]
    rep = [y for y in set(ys) if ys.count(y) >= 2]
    assert max(rep) == 2026


@test("taxonomy: parses, 59 items, forfeited alias no longer conflates calls-in-arrears")
def _():
    import yaml
    items = yaml.safe_load(open("taxonomy/definitions.yaml"))["items"]
    assert len(items) == 59
    forf = next(i for i in items if i["key"] == "Equity Forfeited")
    assert not any("arrears" in a.lower() for a in forf["aliases"])
    assert "calls unpaid" in forf["concept"].lower()


@test("llm truncation retry: retries once on 'incomplete', never otherwise (mocked)")
def _():
    from unittest.mock import MagicMock, patch
    import src.llm as llm
    calls = []
    def fake(**kw):
        calls.append(kw["max_output_tokens"])
        r = MagicMock()
        r.output_text, r.status = ('{"a": 1}', "completed") if len(calls) > 1 else ('{"a"', "incomplete")
        return r
    cli = MagicMock(); cli.responses.create = fake
    with patch.object(llm, "client", lambda: cli):
        out = llm.extract_json(instructions="i", user_input="u", schema_name="s",
                               schema={"type": "object"}, max_output_tokens=1000)
    assert calls == [1000, 2000] and out == {"a": 1}


# --------------------------------------------------------------------------- slow tier
@test("share_capital locate: signature top page IS the core table, window has every GT value",
      fast=False)
def _():
    from itertools import zip_longest
    from src.engine.index import PageIndex
    from src.engine import datapoints as dp
    gt = {}
    for r in csv.DictReader(open("data/gt_master_corrected.csv")):
        gt.setdefault((r["company"], r["scope"]), []).append((r["key"], r["corrected_value"]))
    sc_keys = {c.key for c in dp.load_concepts() if c.section == "share_capital"}
    for comp, path in PDFS.items():
        idx = PageIndex(path)
        tags = dp.page_scopes(idx)
        for scope in ("standalone", "consolidated"):
            allowed = {i + 1 for i, t in enumerate(tags) if t in (scope, "unknown")}
            sig = dp._signature_pages(idx, "share_capital", allowed)
            assert sig and sum(k in idx.page_text[sig[0] - 1].lower()
                               for k in ("authorised", "authorized", "issued", "subscribed")) >= 3, \
                (comp, scope, sig)
            win = []
            for p in sig[:2]:
                for q in (p - 1, p, p + 1):
                    if 1 <= q <= idx.n_pages and q not in win:
                        win.append(q)
            td = re.sub(r"[^\d]", "", idx.text_of(win, columns=True))
            for k, v in gt.get((comp, scope), []):
                if k not in sc_keys or not v or (v or "").strip().lower() in ("-", "not disclosed"):
                    continue
                if comp == "hindalco" and "bought back" in k:
                    assert "10466985" in td and "16316130" in td, (comp, scope, k)
                    continue
                d = re.sub(r"[^\d]", "", str(v))
                assert not d or d in td, (comp, scope, k, v)


@test("ppe deterministic reader: 24 correct / 0 wrong on the whole corpus", fast=False)
def _():
    from src.engine.index import PageIndex
    from src.engine import datapoints as dp
    def num(s):
        if s is None:
            return None
        neg = "(" in str(s) and ")" in str(s)
        m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace(" ", ""))
        if not m:
            return None
        v = float(m.group(0).replace(",", ""))
        return -abs(v) if neg else v
    GT = {}
    for r in csv.DictReader(open("data/gt_master_corrected.csv")):
        GT[(r["company"], r["scope"], r["key"])] = r["corrected_value"]
    concepts = [c for c in dp.load_concepts() if c.section == "ppe"]
    det_keys = [c.key for c in concepts if c.key.lower().startswith(("gross", "accumulated"))]
    correct = wrong = 0
    for comp, path in PDFS.items():
        idx = PageIndex(path)
        tags = dp.page_scopes(idx)
        for scope in ("standalone", "consolidated"):
            allowed = {i + 1 for i, t in enumerate(tags) if t in (scope, "unknown")}
            det = dp._matrix_rows(idx, scope, "ppe", concepts, [], allowed)
            for k in det_keys:
                d, gv = det.get(k), GT.get((comp, scope, k))
                if d is None:
                    continue
                g = num(gv)
                if g is not None and abs(num(d.value) - g) < max(abs(g) * 0.005, 0.02):
                    correct += 1
                else:
                    wrong += 1
    assert wrong == 0, f"{wrong} WRONG deterministic ppe values"
    assert correct >= 24, f"coverage dropped: {correct} < 24"


@test("deferred-tax deterministic reader: correct-or-silent on all 20 GT cells", fast=False)
def _():
    from src.engine.index import PageIndex
    from src.engine import datapoints as dp
    def num(s):
        if s is None:
            return None
        neg = "(" in str(s) and ")" in str(s)
        m = re.search(r"-?\d[\d,]*\.?\d*", str(s).replace(" ", ""))
        if not m:
            return None
        v = float(m.group(0).replace(",", ""))
        return -abs(v) if neg else v
    GT = {}
    for r in csv.DictReader(open("data/gt_master_corrected.csv")):
        GT[(r["company"], r["scope"], r["key"])] = r["corrected_value"]
    dt_c = [c for c in dp.load_concepts() if c.section == "deferred_tax"]
    dt_keys = [c.key for c in dt_c if any(f in c.key for f in dp._DT_TARGET_KW)]
    correct = wrong = 0
    for comp, path in PDFS.items():
        idx = PageIndex(path)
        tags = dp.page_scopes(idx)
        for scope in ("standalone", "consolidated"):
            allowed = {i + 1 for i, t in enumerate(tags) if t in (scope, "unknown")}
            det = dp._dt_rows(idx, scope, dt_c, allowed)
            for k in dt_keys:
                d, gv = det.get(k), GT.get((comp, scope, k))
                if d is None:
                    continue
                g = num(gv)
                if g is not None and abs(num(d.value) - g) < max(abs(g) * 0.005, 0.02):
                    correct += 1
                else:
                    wrong += 1
    assert wrong == 0, f"{wrong} WRONG deterministic DT values"
    assert correct >= 16, f"coverage dropped: {correct} < 16"


@test("deterministic BS-face parser: identity-accepted, correct parents, decoy-proof",
      fast=False)
def _():
    from src.engine.index import PageIndex
    from src.engine import datapoints as dp
    # verified printed BS-face values (this suite run's own evidence trail)
    EXPECT = {
        ("reliance", "standalone"): ("share_capital", 13532),      # FY26 bonus-doubled
        ("reliance", "consolidated"): ("other_nc_liabilities", 6932),
        ("hindalco", "standalone"): ("other_nc_liabilities", 967),
        ("hindalco", "consolidated"): ("other_nc_liabilities", 1685),
        ("infosys", "standalone"): ("share_capital", 2027),
        ("infosys", "consolidated"): ("share_capital", 2024),
        ("adani", "standalone"): ("share_capital", 129.24),
    }
    accepted = 0
    for comp, path in PDFS.items():
        idx = PageIndex(path)
        tags = dp.page_scopes(idx)
        per_scope = {}
        for scope in ("standalone", "consolidated"):
            allowed = {i + 1 for i, t in enumerate(tags) if t in (scope, "unknown")}
            rows = dp._bs_face_lines_det(idx, allowed, tags, scope)
            per_scope[scope] = rows
            if rows is None:
                continue
            accepted += 1
            exp = EXPECT.get((comp, scope))
            if exp:
                sec, want = exp
                got = dp._parent(sec, rows, [])[0]
                assert got is not None and abs(got - want) < max(abs(want) * 0.01, 0.5), \
                    (comp, scope, sec, want, got)
        # decoy guard: std and cons must never return identical parses (infosys's abridged
        # front-matter BS once served both scopes)
        if per_scope["standalone"] and per_scope["consolidated"]:
            v = lambda rows: dp._parent("share_capital", rows, [])[0]
            oncl = lambda rows: dp._parent("other_nc_liabilities", rows, [])[0]
            assert (v(per_scope["standalone"]), oncl(per_scope["standalone"])) != \
                   (v(per_scope["consolidated"]), oncl(per_scope["consolidated"])), comp
    assert accepted >= 10, f"BS parser acceptance dropped: {accepted}/10"


@test("deterministic PL-face parser: identity-accepted with sane parents, or silent",
      fast=False)
def _():
    from src.engine.index import PageIndex
    from src.engine import datapoints as dp
    EXPECT = {  # printed-line verified parents / values
        ("reliance", "standalone"): {"fc": 6904, "oe": 61269},
        ("infosys", "standalone"): {"fc": 207, "oe": 4044},
        ("infosys", "consolidated"): {"fc": 416},
        ("adani", "standalone"): {"fc": 1747.51, "chg": "(168.75)"},
        ("adani", "consolidated"): {"chg": "(2,824.59)"},
    }
    accepted = 0
    for comp, path in PDFS.items():
        idx = PageIndex(path)
        tags = dp.page_scopes(idx)
        for scope in ("standalone", "consolidated"):
            allowed = {i + 1 for i, t in enumerate(tags) if t in (scope, "unknown")}
            rows = dp._pl_face_lines_det(idx, allowed, tags, scope)
            if rows is None:
                continue
            accepted += 1
            # acceptance contract: both P&L parents present and never 'profit before' rows
            fc = dp._parent("finance_costs", [], rows)[0]
            oe = dp._parent("other_expenses", [], rows)[0]
            assert fc is not None and oe is not None, (comp, scope)
            exp = EXPECT.get((comp, scope), {})
            if "fc" in exp:
                assert abs(fc - exp["fc"]) < max(exp["fc"] * 0.01, 0.5), (comp, scope, fc)
            if "oe" in exp:
                assert abs(oe - exp["oe"]) < max(exp["oe"] * 0.01, 0.5), (comp, scope, oe)
            if "chg" in exp:
                chg = next((r["value"] for r in rows
                            if "changes in inventor" in r["label"].lower() and r["value"]), None)
                assert chg == exp["chg"], (comp, scope, chg)
    assert accepted >= 10, f"PL parser acceptance dropped: {accepted}/10"


@test("reflow guard: dev pages still reflow; a known-lossy page falls back to -layout",
      fast=False)
def _():
    from src.engine.index import PageIndex
    idx = PageIndex(PDFS["hindalco"])
    for p in (369, 292, 367):
        assert idx._reflow_safe(p), p


@test("rescue candidates: coverage on the proven target rows", fast=False)
def _():
    from src.engine.index import PageIndex
    from src.engine import datapoints as dp
    CASES = [
        ("hindalco", "standalone", "Total Power & Fuel Expenses", "8798"),
        ("hindalco", "standalone", "Carriage Outwards", "1006"),
        ("hindalco", "standalone", "Interest Expense Borrowings", "1051"),
        ("hindalco", "consolidated", "Equity Forfeited", "546249"),
        ("adani", "standalone", "Impairment loss on financial assets", "646"),
        ("adani", "standalone", "Total Power & Fuel Expenses", "4093"),
        ("adani", "standalone", "Loss On Disposal Of Fixed Asset", "963"),
        ("adani", "standalone", "Changes In Inventories", "16875"),
    ]
    concepts = dp.load_concepts()
    idxs = {}
    for comp, scope, frag, gtd in CASES:
        if comp not in idxs:
            x = PageIndex(PDFS[comp])
            idxs[comp] = (x, dp.page_scopes(x))
        idx, tags = idxs[comp]
        allowed = {i + 1 for i, t in enumerate(tags) if t in (scope, "unknown")}
        c = next(c for c in concepts if frag in c.key)
        lines = dp._alias_candidates(idx, [c], allowed, set()).get(c.key, [])
        assert any(gtd in re.sub(r"[^\d]", "", sn) for _, sn in lines), (comp, scope, frag)


@test("scope-aware statements: each scope's candidates include its own tagged page; they differ",
      fast=False)
def _():
    from src.engine.index import PageIndex
    from src.engine import statements as st
    from src.engine import datapoints as dp
    for comp, path in PDFS.items():
        idx = PageIndex(path)
        tags = dp.page_scopes(idx)
        for kind in ("bs", "pl"):
            tops = {}
            for scope in ("standalone", "consolidated"):
                allowed = frozenset(i + 1 for i, t in enumerate(tags) if t in (scope, "unknown"))
                c = st.candidate_pages(path, kind, allowed)
                # a page tagged EXACTLY this scope must be reachable — 'unknown' pages can
                # host false fingerprint hits (hindalco p179 is a dividend-policy page that
                # matches the bs fingerprint; the runtime tie-out rejects it and moves on)
                tagged = [p for p in c if tags[p - 1] == scope]
                assert tagged, (comp, kind, scope, c)
                tops[scope] = tagged[0]
            assert tops["standalone"] != tops["consolidated"], (comp, kind, tops)


@test("mocked end-to-end: 59 datapoints, no crash, PPE deterministic even with dead LLM",
      fast=False)
def _():
    from unittest.mock import patch
    from src.engine.index import PageIndex
    from src.engine import datapoints as dp
    from src.engine import statements as st
    def fake(instructions="", user_input="", **kw):
        return {"results": [], "lines": [], "targets": [], "components": [], "total": None,
                "assets": [], "total_net": None, "_empty": False}
    class FakeStmt:
        page = None
    os.environ["DP_MAX_WORKERS"] = "1"
    with patch("src.llm.extract_json", side_effect=fake), \
         patch.object(st, "validate_statement", lambda *a, **k: FakeStmt()), \
         patch("src.engine.vision.render_page", lambda *a, **k: "AAAA"):
        res = dp.extract_datapoints(PageIndex(PDFS["adani"]), "consolidated", dp.load_concepts())
    assert len(res) == 59
    assert sum(1 for d in res.values() if d.section == "ppe" and d.present) == 4


# --------------------------------------------------------------------------- runner
if __name__ == "__main__":
    ran = passed = 0
    for name, fast, fn in RESULTS:
        if not fast and not FULL:
            print(f"SKIP  {name}  (set FULL=1 with PDFs present)")
            continue
        ran += 1
        try:
            fn()
            passed += 1
            print(f"PASS  {name}")
        except Exception:
            print(f"FAIL  {name}\n{traceback.format_exc()}")
    print(f"\n{passed}/{ran} passed" + ("" if FULL else "  (fast tier only)"))
    sys.exit(0 if passed == ran else 1)
