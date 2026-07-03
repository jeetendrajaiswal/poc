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
  Cause = `_statement_lines` (the LLM balance-sheet read) dropped the equity-share-capital
  note-ref line → note-locate collapsed. This is the deeper fragility; **it is now the #1
  lever.**
- `other_expenses` 27 → **17 (−10)** — the fix, confirmed.
- `other_nc_liabilities` 12 → 5, `loans_advances` 10 → 6, `deferred_tax` 11 → 10,
  `ppe` 11 → 12, `finance_costs` 1 → 4 — **all UNTOUCHED by code**; these swings are
  pure run-to-run variance (proof that single-run deltas are noisy; only measure
  deterministic parts, or run N times and average).

**#1 next lever = kill the share_capital stochastic collapse.** Make its locate
deterministic so a bad `_statement_lines` draw can't zero out the whole note (e.g. always
run `_signature_pages` for share_capital, not only as a fallback; or make `_note_pages`
independent of the stochastic BS read). This also de-risks every other BS-anchored section.

---

## 6. Suggested next steps (in order)

> Done already: `src/engine/` committed (commit `68dc470`); one full paid run executed
> 2026-07-03 → `error_report.xlsx` refreshed (104 errors). See §5a.

1. **#1 lever — kill the share_capital stochastic whole-note collapse** (32 errors,
   mostly all-`None` collapses). Root cause is the LLM balance-sheet read
   (`_statement_lines`) dropping the equity-share-capital note-ref line, which zeroes out
   note-locate. Make share_capital's locate **deterministic** so a bad draw can't collapse
   it: e.g. run `_signature_pages` for share_capital ALWAYS (not only when `_note_pages`
   is empty), and/or add a deterministic BS-face reader for the share-capital total. This
   also de-risks every BS-anchored section. **First, quantify variance**: re-run 2–3× to
   see how much share_capital swings before/after any change (single runs are noisy).
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
