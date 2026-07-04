# HANDOFF — Annual Report Datapoint Extraction PoC

> For whoever (human or model) takes this over. Read this top-to-bottom first.
> Location: `/Users/jeetendrajaiswal/Desktop/self/poc`. Date of handoff: 2026-07-03.

---

## 1. What this app does (use case)

Extract a **fixed taxonomy of ~59 financial datapoints** (×2 scopes: standalone &
consolidated) from large **Indian annual reports** (Ind AS / Schedule III PDFs), by
**meaning, not keyword** — e.g. "Securities Premium" → `Share Premium`, "Sundry
Creditors" → `Trade Payables`. Output is a value + confidence per datapoint per scope.

Two things make it hard and are the whole point:
- **Semantic mapping**: every company labels the same concept differently.
- **Scope + column discipline**: standalone vs consolidated; current-year vs prior-year
  column; gross vs accumulated-depreciation; reported line vs computed total.

**Privacy is a hard requirement**: the PDF is read **locally** (`pdftotext -layout`,
PyMuPDF fallback). Only text/rendered-images are sent to the model **inline** with
`store=False`. Nothing is uploaded/persisted to OpenAI (no vector store, no files).

---

## 2. ⚠️ Read this before touching anything

1. **`src/engine/` was committed 2026-07-03 (commit `68dc470`, "new version logic")** —
   this is your baseline including the validated other_expenses fix. It was previously
   untracked; commit early and often from here.
2. **`README.md` and `PLAN.md` are STALE.** They describe an *abandoned* vector-store +
   vision approach. The live system is the **text-first `src/engine/`** engine. Trust the
   code and this doc, not those two files.
3. **Do NOT call the OpenAI API without explicit user approval.** The owner is highly
   cost-sensitive and has repeatedly demanded no unapproved runs. A full 5-company ×
   2-scope run costs real money (~$1.90/report). Design & validate deterministically
   (free) first; ask before any paid run.
4. **Single-run eval variance is ~±9 points.** Do NOT judge locate/text fixes by one paid
   accuracy run — you'll misread noise as regression (this happened, cost money). Verify
   the **deterministic** parts for free (does locate land the right page? is the value on
   the page? is text byte-identical where it should be?).
5. **Reliance PDF landmine**: two different-year `reliance.pdf` exist on disk
   (`~/Downloads/reliance.pdf` = FY2025-26, GT-aligned; `~/Downloads/nifty100/reliance.pdf`
   = FY2021-22, WRONG). Always confirm engine + GT use the same file.

---

## 3. Architecture — there are TWO layers

### Layer A — Statements/QA/KPI engine (`src/engine/`, the robust "product")
Sector-aware **locate → extract → arithmetic tie-out**. This is well-validated
(39/39 corpus, 95/95 Nifty-100 all-3-statements self-validated). Key modules:
- `index.py` — `PageIndex`: per-page text (`pdftotext -layout`, PyMuPDF fallback),
  **BM25** retrieval (no embeddings → nothing leaves host), and **two-column reflow**
  (`_dual_column_text` / `column_text` / `text_of(pages, columns=)`).
- `statements.py` — locate the BS/PL/CF and verify by **arithmetic tie-out**
  (BS: assets = equity+liab; PL: PBT−tax = PAT; CF: op+inv+fin+forex = net change).
  `candidate_pages` (deterministic locate), `validate_statement` (lru-cached, tie-out).
- `qa.py` — "ask anything" with grounding. `vision.py` — image/scanned fallback.
- `sector.py` + `config/kpis.yaml` — format detection + KPI catalog (data, not code).
- `report.py` — orchestrator; `cli.py` — CLI; `whole_doc.py` — small-doc single-call path.

### Layer B — Datapoint taxonomy extraction (`src/engine/datapoints.py`, THIS SESSION's focus)
Maps the ~59-item taxonomy onto a report. **This is the layer being actively debugged**
and scored against ground truth. Flow (`_run` / `_run_core`, per scope):

```
page_scopes(index)            # deterministic: tag pages standalone/consolidated/unknown
_statement_lines(bs), (pl)    # LLM reads BS & PL face lines (label, note_ref, value)
per section:
  _parent(section)            # value of the parent line from BS/PL (tie-out anchor)
  _note_pages / _signature_pages   # locate the note (note-ref nav; signature fallback)
  route by section type:
    VISION_CATEGORY_SECTIONS  -> _category_vision   (investments)
    MATRIX_SECTIONS           -> matrix_columns/matrix_text/matrix_vision  (ppe)
    VISION_TARGET_SECTIONS    -> _targets_vision     (borrowings)
    else -> _reconciled_section  (components sum to tied-out parent = trust)
         -> _extract_section     (BM25 best-effort fallback, grounded)
  finalize: confidence = grounded | reconciled | unverified | absent
```

Key constants in `datapoints.py`:
- `COLUMN_SECTIONS = {"share_capital", "other_expenses"}` — read the note with
  **column reflow** (two-up notes). Used in `_extract_section` (~L275) and
  `_reconciled_section` (~L584 via `_reconcile_reflow`).
- `_NOTE_LOCATE_SECTIONS = {"share_capital"}` — deterministic note-ref/signature LOCATE
  fallback (BS-anchored only; kept off P&L sections).
- `_reconcile_reflow = section in COLUMN_SECTIONS and section not in _NOTE_LOCATE_SECTIONS`
  — so share_capital's reconcile read stays `-layout` (byte-identical), only
  other_expenses reflows in reconcile. This isolation is deliberate.
- `MATRIX_SECTIONS={"ppe"}`, `VISION_TARGET_SECTIONS={"borrowings"}`,
  `VISION_CATEGORY_SECTIONS={"investments"}`.
- `NON_ADDITIVE_SECTIONS = {"share_capital"}` — notes whose lines are NOT additive
  components (Authorised/Issued/Subscribed are stages): reconciliation is structurally
  meaningless for them and mis-selects look-alike pages that DO tie out (SOCIE, prose),
  so they skip `_reconciled_section` entirely. See §5b.
- `_PARENT` — per-section parent line (statement + label keywords) for tie-out.

**Central insight**: failures are almost always **locate** (wrong page / garbled text),
rarely extraction. "Locate proposes, verify disposes." The dominant garble cause is
**two-column (two-up) page layout** — `pdftotext -layout` interleaves side-by-side
columns; the fix is the coordinate **column reflow**.

---

## 4. Config / model / how to run

- `src/config.py` — reads `.env`. Knobs: `OPENAI_MODEL_DEFAULT`, `OPENAI_MODEL_LARGE`,
  `REASONING_EFFORT` (default `none`), `SELF_CONSISTENCY_N` (default 1),
  `OPENAI_STORE_RESPONSES` (default False = privacy), token limits.
  - Note: code defaults name `gpt-5-mini-2025-08-07` / `gpt-5.2-2025-12-11`, but `.env`
    overrides to the gpt-5.4 family per project intent. **Check `.env` for the live model.**
- `src/llm.py` — `extract_json(...)`: OpenAI **Responses API**, strict JSON schema,
  `store=False` set last (privacy), retry handling; supports `images_b64` / `file_ids`.
- **Entry points**:
  - `build_error_report.py` — the eval harness THIS session used: runs the engine on
    5 companies (`reliance, hindalco, itc, infosys, adani`) from `~/Downloads/<c>.pdf` ×
    2 scopes, diffs vs `data/gt_master_corrected.csv`, writes **`error_report.xlsx`**
    (Summary + per-company sheets). ⚠️ **Runs the API — needs approval.**
  - `src/engine/cli.py`, `src/report.py` — the Layer-A product CLI.
  - `debug_run.py`, `t_final.py` — scratch scripts.
- **Python env**: use `.venv/bin/python` (has openai, pydantic, PyMuPDF, openpyxl, etc.).
- **Ground truth**: `data/gt_master_corrected.csv` (columns:
  `company,scope,key,original_value,corrected_value,status,evidence`;
  `status` = `verified_ok` / `CORRECTED`; `"Not disclosed"` = correctly absent).

---

## 5. Current state

- **Layer A (statements/QA/KPI)**: solid, self-validated at scale. Not the active problem.
- **Layer B (datapoints)**: being improved section-by-section against `gt_master_corrected`.
- **`error_report.xlsx` is STALE** — it's the 114-error run from *before* the
  share_capital simplification and the other_expenses fix. **Regenerate it** (one approved
  run) to see true current numbers.

### Fixes done this session
1. **share_capital** — two-column reflow read + note-focused/deterministic-signature
   locate. (In the code now.) BUT see §5a: it **stochastically collapses whole notes**
   run-to-run — the "9/11, 10/10" was a good draw, not a stable floor.
2. **other_expenses** — ✅ **DONE and VALIDATED in a paid run (2026-07-03): 27 → 17
   errors (−10).** Root cause: the Other Expenses note is printed two-up beside an unrelated
   note (EPS / Finance costs / Employee costs), so `-layout` interleaves them and the line
   items (CSR, Donations, Professional Fees, Power & fuel, Auditor) can't be parsed. Fix:
   added `other_expenses` to `COLUMN_SECTIONS` (reflow read in both reconcile & extract),
   kept locate on baseline BM25 (signature-locate *mislands* it). **Isolation proven for
   free**: share_capital unchanged; every other section unchanged; ITC other_expenses is
   single-column so reflow is a no-op (byte-identical); 39 split candidate pages had zero
   numeric-token loss; row integrity verified on reliance/adani/hindalco note pages.

### Investigated & deliberately NOT fixed
- **other_nc_liabilities** — analyzed deeply, **rejected** as not cleanly fixable:
  heterogeneous value sources (infosys `Total` isn't the BS line), GT inconsistency
  (hindalco cons `Other`=40 sub-line vs the definition's total), and unreliable BS-face
  locate (infosys picks up an abridged highlights BS → false positives). A deterministic
  reader would fix reliance+adani (~+5) but regress infosys. Not worth it. No code changed.

### 5a. Latest paid run — 2026-07-03 (`error_report.xlsx`, 602 calls, ~$3.86)
**Total 104 errors** (was 114 stale). Per company: reliance 12, hindalco 32, itc 10,
infosys 24, adani 26.

Per-section, NEW vs old-stale:
- `share_capital` 16 → **32 (+16)** — ⚠️ NOT a regression from any code change (path is
  byte-identical). It's **3 whole-note stochastic collapses this run**: adani-cons (6
  misses), hindalco-std (10), infosys-cons (8) all returned `None` for the ENTIRE note.
  ~~Cause = `_statement_lines` dropped the note-ref line~~ **← this diagnosis was WRONG.
  2026-07-03 (later session) proved the real cause deterministically — see §5b — and fixed
  it: `_reconciled_section` was SELECTING arithmetically-tying WRONG pages (SOCIE /
  rights-issue prose / buyback sub-table) because the share-capital note is NON-ADDITIVE
  (Authorised/Issued/Subscribed are stages, not addends → the TRUE note can never
  reconcile), and the reconciled-but-empty result short-circuited the good fallback read.**
- `other_expenses` 27 → **17 (−10)** — the fix, confirmed.
- `other_nc_liabilities` 12 → 5, `loans_advances` 10 → 6, `deferred_tax` 11 → 10,
  `ppe` 11 → 12, `finance_costs` 1 → 4 — **all UNTOUCHED by code**; these swings are
  pure run-to-run variance (proof that single-run deltas are noisy; only measure
  deterministic parts, or run N times and average).

**#1 next lever = kill the share_capital stochastic collapse.** → **DONE, see §5b.**

### 5b. share_capital collapse — ROOT CAUSE FOUND & FIXED (2026-07-03, later session; $0 spent)

Diagnosed entirely deterministically from the PDFs (no API calls). The three collapses all
had pages recorded that only `_reconciled_section` windows produce ([345]; [315]; [324,325]),
and none of those windows contain the note's core table:

- **hindalco-std read p345** = the *Statement of Changes in Equity*, not the note (true note
  = p369). SOCIE trivially reconciles (opening+changes=closing == BS parent) → wins.
- **adani-cons read p315** = the note's "(Contd.)" page; its rights-issue PROSE ties out
  (115.42 + 13.85 = 129.27 == BS parent) → wins. True core table = p314.
- **infosys-cons read p324-325** = a shares-movement/buyback sub-table (opening − buyback =
  closing) → wins; also produced the wrong-but-"reconciled" buyback 10,00,00,000. True = p322.

**Why the TRUE pages lose even on a perfect run:** the share-capital note is NON-ADDITIVE —
Authorised (250) / Issued (225) / Subscribed & Paid-up (225) are *stages*, not components, so
components-sum-to-total==parent NEVER holds on the real note. Reconciliation, for this note
type, is a wrong-page selector. Proven: with the CORRECT note ref (hindalco "10"), `ranked` =
[369, 370, 345, …]; windows [369]/[369,370] fail tie-out, window [345] (SOCIE) passes with ~0
targets → returned, short-circuiting the good `_extract_section` reflow read. So the earlier
"#1 lever" framing (statement-read dropping the ref) was only a co-factor, not the cause.

**Fix (in `datapoints.py`, all changes gated to share_capital only):**
1. `NON_ADDITIVE_SECTIONS = {"share_capital"}` — these sections SKIP `_reconciled_section`
   entirely and go straight to the deterministically-located, grounded, column-reflow
   `_extract_section` read (the exact path that produced the validated 9/11 & 10/10 draws).
2. `_signature_pages` now ALWAYS runs for `_NOTE_LOCATE_SECTIONS` (not only when
   `_note_pages` is empty) and is interleaved signature-first with the ref pages — so locate
   no longer depends on the stochastic `_statement_lines` draw at all.

3. `_SIGNATURE_CORE` — per-section CORE term groups for `_signature_pages` (share_capital:
   authorised/authorized + issued + subscribed). Pages are ranked by core-group co-occurrence
   BEFORE raw term count: raw counts let wordy look-alikes (Directors' Report, Corporate
   Governance, Shareholder Information pages) outrank the real note; only the note's core
   table prints all three stages together. Pure re-ranking — degrades to the old order when
   no core groups hit.

**Validation (free, deterministic): 20/20 scenarios pass** — for all 5 companies × 2 scopes
× {no-refs bad draw, true refs}, the `_extract_section` window (top-2 npages ±1) contains the
true note core page AND the digits of every GT share_capital value, and with the core ranking
`sig[0]` IS the true core-table page in all 10 combos (hindalco's treasury count 26,783,115
is printed nowhere — it's the sum of two disclosed lines 10,466,985 + 16,316,130, both inside
the window; the engine's earlier 10,466,985 answer was arguably GT-debatable). Isolation:
NON_ADDITIVE_SECTIONS / _NOTE_LOCATE_SECTIONS / _SIGNATURE_CORE all contain only
share_capital → every other section byte-identical. A mocked-LLM end-to-end smoke test
confirms the path makes exactly ONE extraction call per scope reading the right window even
when statement locate fails completely.

**Targeted test harness: `t_share_capital.py`** (⚠️ paid, needs approval; ~60-70 small calls
per pass, `--n 3` for variance). Judge by "collapsed scopes = NONE" (deterministic), not the
raw error count (single-run noise ±).

Expected impact: kills the 3 whole-note collapse modes (24 of the 32 share_capital errors,
+ the false "reconciled" infosys buyback). Extraction itself is still an LLM read — confirm
with ONE approved paid run before trusting the number.

---

### 5c. 2026-07-04 — holistic fix set from full-corpus forensics ($0 spent, all deterministic)

All 104 errors were traced to printed lines via 4 parallel forensic passes (other_expenses,
ppe, deferred_tax, long tail). Root-cause distribution: two-up interleave ~17, PPE
vision-path failures 12 (all traced to the Σnet==parent gate rejecting mixed
PPE/ROU/intangible schedules → vision fallback hallucinated/mislanded), model refusal of
legitimate synonym labels ~11, share_capital reconcile mis-selection 24-27 (see §5b),
GT/taxonomy defects ~15-17, cross-note sourcing gaps ~7, deterministic engine bugs 1.

**Code changes (src/engine/datapoints.py), each validated free:**
1. `COLUMN_SECTIONS` += deferred_tax, loans_advances, finance_costs, other_cur_assets,
   other_cur_liabilities, trade_payables. Proof: 308 candidate pages swept, 197 genuinely
   two-up, ZERO numeric-token loss layout→reflow; single-column pages byte-identical.
2. `_matrix_rows` — NEW deterministic per-row PPE reader (runs after `_matrix_columns`,
   before `_matrix_text`; its values override model output, never vice versa). Accepts a row
   ONLY if the Ind AS 16 movement identity closes (gross_open±mov=gross_close, same for dep,
   gross−dep=net). Handles clean layout rows, prose-welded rows (numbers before the asset
   label are dropped), rotated/shredded grids (PyMuPDF y-cluster rebuild + ±2-line label
   window + scored candidates + numeric two-year-block splitting with year chaining).
   Proof: 24/24 correct on the corpus (every GT gross/accum-dep value it returns), 16
   silent (itc/infosys fall through to their existing readers), 0 wrong. Works with the
   LLM completely dead (mocked smoke test).
3. `_matrix_vision` grounding guard — a vision value whose digit-string is absent from the
   page's extractable text is a fabrication (killed 13,287/61,473 printed NOWHERE, and a
   6→8 misread that a page-level tie-out had blessed). Skipped on image-only pages.
4. `_wants_total` now scans the SELECTOR too (adani Sundry Creditors 4,474.96 was
   force-demoted: its total-ness is stated in column_hint 'TOTAL trade payables').
   Sweep: exactly 3 items flip, all genuinely total-type.
5. Un-truncated concept definitions in ALL prompts ([:120]/[:140]/[:180]/[:320] → [:400]) —
   the cap was cutting off guards like 'NEVER return the TOTAL' (hindalco Other-CWIP FP).
6. ALL taxonomy aliases now passed as examples (was [:3]; adani's leave-encashment label
   'Employee Benefits Liability' IS alias #5 — the cap caused refusals).
7. `_SECTION_HINT` threaded into `_extract_section` (was reconcile-only) + new deferred_tax
   hint (movement-matrix closing column, DTA-vs-DTL orientation, umbrella labels).
8. `_targets_vision` symmetric window [pg−1,pg,pg+1] (adani borrowings note starts one page
   before the top candidate).
9. `_equity_method_pages` — consolidated investments windows now include the Ind AS 28
   equity-method note (consolidated JV stakes NEVER sit in the FV-investments note the
   locator reads; hindalco p263 / itc p272 / adani p283 verified in the new window).

**GT/taxonomy defects found (fix data, not code — ~15 rows):** infosys other_equity GT took
PRIOR-year SOCIE balances 3× (cons premium 1,091→280; std hedging (18)→(19); std premium
1,054→243); infosys cons 'equity attributable' 92,852 printed verbatim p287 but GT says Not
disclosed; infosys cons advances-to-suppliers 474 printed p311; infosys std power&fuel 196
printed p257; hindalco std deferred-tax accdep (5,751) printed p367; infosys deferred-tax
std/cons internally inconsistent (std 234 accepted, cons 133 'Not disclosed'); finance_costs
cross-company inconsistency (identical 'interest on financial liabilities' wording accepted
for hindalco 1,051, rejected for itc 12.91/adani 1,625.51); itc deposits 6.26 vs the
definition-matching 25.24; JV-vs-associates boundary (hindalco 165 / adani 6,705.74 include
associates, taxonomy excludes them); computed-sum GTs never printed as one number (itc
auditor 7.43 = 5 lines; itc cons CSR 475.33 cross-note; hindalco treasury 26,783,115 =
10,466,985+16,316,130).

**Known NOT fixed by design:** other_expenses values living OUTSIDE the note (P&L face/CF/
Other Income/related-party, 4 rows — cross-statement sourcing, same shape as the rejected
other_nc_liabilities work); other_nc_liabilities (5, per §5); reliance cons finance_costs
locate-miss (1); infosys std SOCIE wide-table column grab (1). The alias-anchored
rescue-re-ask pass (other_expenses agent's proposal, prototype validated: candidate set
contains the GT line for all 8 refusal rows AND all 36 correct items) is designed but NOT
implemented — it adds one cheap API call per section and needs owner approval.

### 5d. 2026-07-04 full paid run — 104 → 72 errors (557 calls, $3.45) + post-run tuning

Run with the §5b+§5c fix set, fully logged under `logs/run_20260704_023955/` (llm_calls.jsonl
= every call with company/scope/section/pages/tokens/cost/status; datapoints_*.json = EVERY
datapoint incl. correct ones; errors.json + summary.json). `build_error_report.py` now always
produces these logs. Precise old-vs-new diff: **61 fixed, 29 regressed (net −32)**.

Confirmed wins: **share_capital 32→8 with ZERO whole-note collapses** (residuals = the known
GT quirks: hindalco treasury sum ×2, infosys 'Not disclosed' FPs ×2, + hindalco extraction
subtleties); **ppe 12→0** (the deterministic `_matrix_rows`/`_matrix_columns` pair covered
everything); **deferred_tax 10→5**; trade_payables 1→0; borrowings 1→0.

Regression found and REVERTED: threading `_SECTION_HINT` + full aliases into EVERY section's
prompts took **other_expenses 17→25** (the other_expenses hint adds refusal pressure — whole
blocks of previously-correct fallback items went absent; full aliases also shifted BM25
ex_terms ranking and the investments class boundaries, 3 investments FPs). Both are now scoped
to deferred_tax ONLY (`_FALLBACK_HINT_SECTIONS` / `_FULL_EXAMPLE_SECTIONS` + `_examples()`),
restoring the exact baseline prompt everywhere else. LESSON: prompt enrichment must be opted
in per section with run evidence, never globally. Also fixed post-run: `_hygiene(...,
sign=False)` for COUNT-type datapoints — `_reconcile_sign` turned infosys's opening share
count negative because infosys prints 'current (prior)' pairs and the prior-year count
appears only parenthesized on the line.

Of the remaining 72: ~15-17 are the GT/taxonomy defects listed in §5c (fix the CSV, not
code), ~10 are single-run variance flips (itc '–' FP, deferred_tax itc-std flip, etc.),
~7 cross-note sourcing (other_expenses values on P&L face/CF), 5 other_nc (known), rest
extraction subtleties. Next paid run should re-check other_expenses lands back at ≤17.

### 5e. 2026-07-04 (later) — share_capital residuals root-caused; COMPOSITE datapoints capability

The 8 post-§5d share_capital errors were re-examined against the printed notes (no labels
like "GT quirk" without evidence). Findings (hindalco p369 std / p292 cons, identical
structure both scopes):
- The capital block prints **UNLABELED bare-number subtotal rows**: `…Subscribed & Paid-up
  2,247,772,772 / 225` → `Less: forfeited (546,249)` → **`2,247,226,523 / 225`** (paid-up
  after forfeiture — GT's count) → `Less: Treasury Shares (b)(i) (16,316,130), (b)(ii)
  (10,466,985)` → **`2,220,443,408 / 222`** (final = the BS carrying amount — GT's value;
  hindalco's BS line IS 222). The model cited the labeled gross line (225) because nothing
  told it bare-number subtotal rows are valid answers.
- The treasury count GT **(26,783,115)** is a genuine COMPOSITE — the sum of the two trust
  lines, never printed as one number. Same shape as itc's auditor total 7.43 (five fee lines).
- infosys std "Not disclosed" FPs ×2 are **GT defects**: both counts printed on p243
  (`4,05,55,91,723 (4,15,32,63,455)`). → add to the §5c GT list.

**Fixes (generic capabilities, not patches):**
1. **COMPOSITE datapoints** — `_SCHEMA` gains `addends`; the model returns the printed
   components (parens = negative), CODE does the arithmetic (`_fmt_num`), grounding requires
   EVERY addend's digits on the read pages (a fabricated component keeps the value but kills
   `grounded`). Unit + mocked-e2e validated: trusts sum to (26,783,115) grounded on the real
   hindalco page; subscribed−forfeited = 2,247,226,523; auditor fees = 7.43.
2. **share_capital structural hint** (per-section opt-in mechanism, §5d lesson applied — purely
   additive structure, no refusal language): describes the canonical capital block incl. the
   unlabeled subtotal/final rows and COUNT-vs-AMOUNT column pairing.
3. **Taxonomy contract sharpened** (data): Equity Paid Up = final total after ALL deductions
   (= BS carrying amount); Equity Forfeited = the printed forfeited face-value figure even when
   the crore column rounds to '-'; treasury = sum of trust lines.
These are validated deterministically where possible; the model-behavior part needs the next
paid run (expect share_capital ≤ 3: the two infosys rows are GT-side).

### 5f. 2026-07-04 adversarial self-review — 6 defects found & fixed, dead code removed

A devil's-advocate pass over all changes found and fixed ($0, all re-validated):
1. `_wants_total` dash-regex falsely exempted 'sub-total …' phrasings → dash must be a
   standalone appositive. Sweep still flips exactly the 3 genuine total-type items.
2. The COMPOSITE `addends` instruction was GLOBAL — violating the §5d per-section-opt-in
   lesson. Now `_COMPOSITE_SECTIONS = {share_capital, other_expenses}` (the two with known
   composite GTs); other sections get a one-line 'addends=null'.
3. Model-computed value could win over code-summed addends → code arithmetic now ALWAYS wins
   (addends 2..12 accepted regardless of the model's value field).
4. `_matrix_rows` page-year used plain max — a single stray future year (lease maturing 2040)
   could get sibling pages' correct values dropped. ⚠️ First attempt (modal year) BROKE the
   validated corpus (adani p304's prior year is the modal token on the rotated grid → 24/0
   became 20/2) — caught by re-running the sweep. Final: **rep-max** (latest year appearing
   ≥2×; fallback max) = matches max on every corpus schedule page AND survives the stray-year
   case. Sweep back to 24 correct / 0 wrong. META-LESSON: re-run the full deterministic sweep
   after ANY change to a validated reader — a 'safer' heuristic can be a regression.
5. Dead complexity removed: `_reconcile_reflow`'s share_capital carve-out (unreachable since
   NON_ADDITIVE skips reconcile) → plain `section in COLUMN_SECTIONS`.
6. Hot-loop `import itertools` in `_block_closes` hoisted to module level.

### 5g. 2026-07-04 (later still) — GT corrections, scope-aware statements, held-out nifty100 validation

1. **GT corrected (10 rows, `data/gt_master_corrected.csv`; backup `.bak-20260704`)** — only
   rows with printed-line evidence: infosys prior-year SOCIE values ×3 (premium 1,091→280
   cons / 1,054→243 std — the FY26 buyback debited securities premium, GT had taken the
   OPENING balances; hedging (18)→(19)); infosys 'Not disclosed' rows that are printed
   verbatim ×5 (equity-attributable 92,852 p287; advances-to-vendors 474 p311; power&fuel
   196/223 — row split from scope=both; share counts p243 ×2); hindalco std DT-accdep
   (5,751) p367; infosys cons DT-accdep 133 (consistency with accepted std 234). The
   POLICY-ambiguous rows (itc/adani finance-costs wording, itc deposits 6.26, JV-vs-
   associates) were NOT touched — they need an owner decision, not silent rewrites.
2. **Scope-aware statement locate** — `candidate_pages`/`validate_statement` accept a
   `pages_filter` (the scope's page block), applied BEFORE the candidate cap. Discovery: the
   cap fills with the standalone block's earlier pages, so the consolidated P&L had ZERO
   candidates on ALL 5 dev companies — the standalone page silently served both scopes
   (latent cross-scope corruption of note refs/parents). Validated: std/cons statements now
   separate for bs AND pl on all 5; degrade path = old behaviour when a scope block has no
   candidate.
3. **Held-out nifty100 sweep (95 PDFs, $0, deterministic)** — scope tagging median 87%;
   share_capital signature top page = core table 86/95 std, 90/95 cons; PPE `_matrix_rows`
   proves 4/4 targets on ~1/3 of companies, 0 exceptions, never-wrong-by-design. The 14
   signature misses are ALL format gaps (banks/insurance/NBFC never print the
   authorised/issued/subscribed trio — no page to find), zero locate gaps; ranking degrades
   to the old order there. Future work: sector-specific SECTIONS vocabulary (bank Schedule 1
   'Capital', IRDAI share-capital schedule).
4. **Reflow numeric-token guard (`PageIndex._reflow_safe`)** — the sweep found ~9% of two-up
   pages in the wild LOSE numeric tokens in reflow (gutter mis-detection; 0% on the dev
   corpus — held-out validation caught it). `text_of(columns=True)` now uses the reflow only
   when it preserves the -layout token multiset, per page — the failure mode becomes a plain
   -layout read by construction. Validated both directions (dev pages still reflow;
   ASIANPAINT loss page falls back).

### 5h. 2026-07-04 run #2 (65 errors, $3.16, logs/run_20260704_034458/) + the truncation cliff

Run with §5e-§5g fixes + corrected GT: **65 errors** (was 72, was 104). ppe 0 ✓, deferred_tax
3 ✓, other_expenses 25→18 (the §5d revert confirmed), consolidated_equity/other_equity/
other_cur_assets largely cleared by GT corrections. BUT share_capital printed 17 — of which
**12 were ONE truncated call**: infosys cons share_capital landed on exactly
max_output_tokens=2000 → status=incomplete → `extract_json` returned `_empty` → whole note
absent. Two other calls truncated the same way this run (infosys other_equity note, adani
ppe_matrix). Truncation was a SILENT WHOLE-SECTION collapse mode.

**Fix (generic, one place): `llm.extract_json` retries ONCE with double budget (cap 8000)
when the response status is 'incomplete'** — truncation is detectable, so it must never
surface as all-absent. Unit-tested (retries only on incomplete). **Micro-confirmed paid
($0.04): the exact failing call now returns 12/13 correct** — including previously-wrong
buyback 86,50,911 and forfeited 1,500.

Also from this run's forensics: adani forfeited FP (0.03) traced to the taxonomy alias
'Less: Calls in arrears / forfeited' — calls-unpaid is NOT forfeiture; alias replaced +
concept says so. hindalco std forfeited GT '-' contradicted its own cons GT (546,249) for
the SAME printed line → std GT corrected. Known remaining share_capital residual: hindalco
paid-up count both scopes (model takes the final-after-treasury row 2,220,443,408 instead of
the after-forfeiture subtotal 2,247,226,523 despite the hint) — 2 errors.

Projected clean state after these fixes: ~50 errors, of which the §5c policy-ambiguous GT
rows and the known cross-note sourcing gaps are the bulk. Next full run should confirm.

## 6. Suggested next steps (in order)

> Done already: `src/engine/` committed (commit `68dc470`); one full paid run executed
> 2026-07-03 → `error_report.xlsx` refreshed (104 errors). See §5a.

1. ~~#1 lever — share_capital collapse~~ **DONE (see §5b) — fixed & validated
   deterministically; needs one approved paid run to confirm the score.** When running it,
   remember single-run variance is ±9: judge share_capital by "did any whole-note collapse
   happen" (a deterministic yes/no), not by the raw total.
1b. **Known residual share_capital risks** (cheap to check on the next run):
   - adani-cons signature top-2 = [155, 314]: p155 is the Directors'-Report highlights page
     (prose with the note's vocabulary). The true note p314 is in the same read window, so
     all values are present, but if the model grabs prose numbers, add a "notes to the
     financial statements" header preference or numeric-table-density tiebreak to
     `_signature_pages`.
   - hindalco treasury-shares GT (26,783,115) is a SUM of two disclosed lines
     (10,466,985 + 16,316,130 on p369/p292) — the model must add them; consider whether GT
     or the taxonomy selector should change instead.
   - `_statement_lines` still extracts BOTH scopes' parents from whichever single BS page
     `validate_statement` tied out first (no scope arg!) — cross-scope note refs are a
     latent fragility for every OTHER BS-anchored section, now harmless for share_capital.
2. Longer-tail, harder targets (no clean two-column win left) — only after #1:
   - `deferred_tax` (10): wide movement-matrix + two-column + net/gross ambiguity — hardest.
   - `loans_advances` (6): specificity (model returns broad totals for narrow sub-lines).
   - `other_nc_liabilities` (5): investigated & rejected — see §5. Don't redo without new idea.
3. Method that works here: **diagnose from the actual PDF text for free** (dump the note
   page both `-layout` and reflow, check where the GT value truly lives) → find the ONE
   root cause → design an **isolated** fix → **prove isolation deterministically** → only
   then spend on a validation run.

---

## 7. Files map (quick)

- **LIVE engine**: `src/engine/{index,statements,datapoints,qa,vision,sector,report,cli,whole_doc}.py`
- **Config/LLM**: `src/config.py`, `src/llm.py`, `.env` (secrets + live model)
- **Taxonomy**: `taxonomy/definitions.yaml` (the ~59 datapoints; each has key, scope,
  concept, aliases, selector, examples, location_hint). `config/kpis.yaml` (KPI catalog).
- **Ground truth**: `data/gt_master_corrected.csv` (+ per-company `gt_*_corrected.csv`).
- **Eval**: `build_error_report.py` → `error_report.xlsx`.
- **LEGACY / superseded (old vision+vector pipeline, safe to ignore)**: top-level
  `src/*.py` (`locate.py`, `read_vision.py`, `structure_map.py`, `section_batch.py`,
  `phase0.py`, `scoring.py`, `validate.py`, `cross_year.py`, `export_wide.py`, etc.),
  plus `README.md` and `PLAN.md`.
- **Memory (background, point-in-time)**:
  `~/.claude/projects/-Users-jeetendrajaiswal-Desktop-self-poc/memory/` — see
  `architecture-pivot-v2.md`, `two-column-interleaving-fix.md`,
  `duplicate-reliance-pdf-landmine.md`, `vision-not-the-lever-locate-is.md`,
  `extraction-cost-architecture.md`.

---

## 8. Cost (measured)

- Annual report, per-item path (~114 reads): **~$1.90** (Tata Steel $1.47, ITC $1.88,
  Reliance $2.36). Input tokens ≈ 94% of cost; scales with note density, not page count.
- Small quarterly, whole-doc single call: **~$0.11**.
- `gpt-5.4-mini` was **−17 pts** accuracy vs the full model — the reliability lever is
  N=3 consensus (~3× cost), not a cheaper model.
