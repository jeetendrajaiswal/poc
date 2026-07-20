# Annual-Report Extraction Engine — Deep Analysis & Implementation Reference

> **Audience:** a model/engineer (Fable) about to build *new* functionality on top of this
> codebase. This document is the ground truth for **how the system actually works today**,
> **why it is built the way it is**, and **where the seams are** for new work.
> It is derived from reading the live code (`src/engine/`), the taxonomy, the eval harness,
> and the project handoff — not from the stale `README.md`/`PLAN.md` (which describe an
> abandoned vector-store + vision design; ignore them).
>
> **Repo:** `/Users/jeetendrajaiswal/Desktop/self/poc` · **Live model:** `gpt-5.4-mini`
> (default) / `gpt-5.4` (large, rarely used) · **Corpus accuracy:** ~91.5% (49 errors /
> 579 GT rows on the last full paid run), trend 82% → 91.5% over the project.

---

## 0. TL;DR — the one-paragraph mental model

The system extracts a **fixed taxonomy of 59 financial datapoints** (× 2 scopes: standalone
and consolidated) from large **Indian annual report PDFs** (Ind AS / Schedule III, 200–700
pages), **by meaning, not keyword**. The whole design philosophy is: **"locate proposes,
verify disposes."** Almost every failure is a *locate* failure (wrong page or garbled text),
almost never an *extraction* failure. So the engine spends its intelligence on (a) finding the
right page deterministically and for free, and (b) **verifying** every value it extracts by an
independent check — an accounting identity that must tie out, or a grounding check that the
digits are actually printed on the page. A value that can't be verified is downgraded, never
trusted. The LLM is used narrowly (transcribe the numbers off a located page); everything that
*can* be done deterministically (locating, arithmetic, reflowing two-column text, summing
components) is done in code, because code is free, private, and has **zero run-to-run
variance**. The steady march from 82% → 91.5% was almost entirely **replacing stochastic LLM
steps with deterministic readers** guarded by arithmetic.

---

## 1. What the system does (the product)

Given a PDF annual report, produce, **per scope**:

- **Primary statements** — Balance Sheet, P&L, Cash Flow — each *validated by arithmetic
  tie-out* (well-solved; not the active problem).
- **59 taxonomy datapoints** — note-level line items like *Share Premium*, *Gross Carrying
  Value of Office Equipment*, *Interest Expense on Borrowings*, *CSR Expenditure*,
  *Deferred Tax Asset on Leave Encashment*, etc. Each returns `{value, confidence, evidence,
  pages}`. (This is the layer under active development.)
- **Ask-anything Q&A** and **sector KPIs** (a secondary product surface).

Two things make it genuinely hard, and they are the entire point:

1. **Semantic mapping** — every company labels the same concept differently
   ("Securities Premium" = "Share Premium"; "Sundry Creditors" = "Trade Payables";
   "Separation and retirement benefits" = "Leave Encashment"). String/alias matching does
   not scale, so mapping is done **by meaning** (the model reads the note and maps each target
   to a line using its economic *definition*, not its label).
2. **Scope + column discipline** — standalone vs consolidated (each has its *own* full set of
   statements and notes); current-year vs prior-year column; gross vs accumulated-depreciation;
   the reported line vs a computed total; count vs amount.

### Privacy is a hard requirement
The PDF is read **locally** (`pdftotext -layout`, PyMuPDF fallback). Only text / rendered
page-images are sent to the model **inline** with `store=False`. **Nothing is uploaded or
persisted** — no vector store, no file uploads (except the one narrow small-scanned-doc path,
which uploads then deletes in a `finally`). Retrieval is **BM25**, not embeddings, precisely so
nothing leaves the host. Any new feature must preserve this contract.

---

## 2. Architecture — two layers over one shared page index

```
                        ┌─────────────────────────────────────────────┐
                        │  PageIndex  (src/engine/index.py)             │
                        │  • per-page text: pdftotext -layout → PyMuPDF │
                        │  • BM25 lexical retrieval (no embeddings)     │
                        │  • two-column coordinate REFLOW (+ safety)    │
                        │  ONE extraction, cached, shared by everything │
                        └───────────────┬─────────────────────────────┘
                                        │
              ┌─────────────────────────┴───────────────────────────┐
              │                                                       │
   LAYER A: Statements / QA / KPI                LAYER B: Datapoint taxonomy extraction
   (the robust, validated "product")             (datapoints.py — the active work)
   • statements.py  locate→extract→tie-out       • page_scopes()  tag pages std/cons
   • qa.py          ask-anything + grounding      • deterministic BS/PL face parsers
   • vision.py      scanned/image fallback        • per-section routing (7 read strategies)
   • sector.py      format detect + KPI catalog   • arithmetic reconciliation + grounding
   • whole_doc.py   small-doc single-call path    • rescue pass for absents
   • report.py      orchestrator / entry point    • deterministic PPE / deferred-tax readers
```

**`Report`** (`report.py`) is the single public entry point:

```python
r = Report("annual_report.pdf")
r.format          # 'bank' | 'nbfc' | 'insurer' | 'manufacturer'
r.statements()    # {bs, pl, cf} validated by tie-out
r.ask("...")      # citation-grounded answer
r.kpis()          # sector-appropriate KPIs
r.datapoints("standalone")  # the 59-item taxonomy for one scope
```

### The routing that happens automatically in `Report`
- **Small report** (≤ 50 pages AND text fits in ~100k-token budget, e.g. a quarterly
  results filing) → **whole-document path** (`whole_doc.py`): the entire document goes to the
  model in one call, no page location. A *scanned* small report is uploaded, read natively,
  and deleted.
- **Big report** (annual) → the **locate → extract → verify** pipeline.
- **Scanned / image-only big report** → the **vision** fallback (`vision.py`): render pages,
  classify roles, read the same figures, run the same tie-out.

---

## 3. The `PageIndex` — the shared substrate (`src/engine/index.py`)

Everything reads through one `PageIndex`. Understand this class first; new features almost
always consume it.

- **`page_text`** (cached list of per-page strings) — extracted once via
  `pdftotext -layout` (preserves column alignment), falling back to PyMuPDF `get_text()`
  when poppler returns < 20 chars (image pages). This single extraction is shared across
  statements, QA, KPI, and both scopes of the datapoint layer.
- **`search(query, k, expansion)`** — BM25 over per-page content tokens (stopworded, k1=1.5,
  b=0.75). Returns 1-based page numbers, best first. `expansion` adds synonym terms. This is
  the retrieval workhorse; it's free and local.
- **`column_text`** (cached) — the **two-column coordinate reflow**. This is one of the most
  important ideas in the codebase. `pdftotext -layout` linearises by x-position, so a page
  with **two notes printed side-by-side** ("two-up") gets its columns *interleaved* into
  garbage (a Share Capital table welded onto held-for-sale prose). `_dual_column_text()`
  reads true word coordinates, detects a genuine central **empty vertical gutter** with a real
  text block on *both* sides, and re-renders each half whole (labels *with* their values). The
  subtle correctness point: a normal `Label | Note | Value | Value` financial table also has a
  wide whitespace gap before the numbers, but the number side is *not its own text block*, so
  it is **not** split — the table stays intact. Only genuine side-by-side notes split.
- **`_reflow_safe(p)`** — a per-page guard: accept the reflow **only when it preserves the
  `-layout` text's full numeric-token multiset**. The dev corpus never lost tokens, but the
  95-PDF held-out sweep found ~9% of two-up pages *lose* numeric tokens in reflow (gutter
  mis-detection). This guard turns that failure into a plain `-layout` read by construction.
- **`text_of(pages, columns=False)`** — concatenate labelled page text; `columns=True` uses
  the reflow *only where `_reflow_safe`*, else `-layout`.

> **Takeaway for new work:** if a new note type is being garbled, first ask *"is this a two-up
> page?"* — the reflow (`columns=True`) probably fixes it. Add the section to
> `COLUMN_SECTIONS` in `datapoints.py`.

---

## 4. Layer A: Primary statements (`src/engine/statements.py`)

This layer is **solid and well-validated** (39 reports, 117 statements self-validated). It is
the template the whole project imitates. The pattern is three steps:

1. **LOCATE** (`candidate_pages`) — generous candidate pages by content fingerprint UNION
   despaced statement titles. **Recall matters, precision does not** — false candidates are
   cheap because step 3 rejects them. Fingerprints per kind:
   - `bs`: `totalassets` + one of {equity&liabilities, capital&liabilities, ...}; two-page
     BS spillover; IRDAI "sources/application of funds"; bank Form A; despaced title.
   - `pl`: revenue marker + profit marker (EPS footer is the most rephrase-robust); section
     title.
   - `cf`: "operating activities".
   - Ordering: **TITLE-anchored pages first**, then the back-60% financials region, then page
     order.
   - **`pages_filter`** restricts candidates to a scope's page block *before* the cap — this
     is essential: the cap otherwise fills with the standalone block's earlier pages and the
     consolidated statement gets zero candidates (a real, fixed bug — the standalone page was
     silently serving both scopes).
2. **EXTRACT** (`_extract_once` / `_extract_with_reask`) — the mini model transcribes only the
   few figures each statement's *identity* needs. A **completeness re-ask** recovers a
   transiently-missed total once before giving up on a candidate.
3. **VERIFY** (`tie_out`) — the accounting identity must hold to tolerance. **The first
   candidate that ties wins; that tie IS the correctness proof.**
   - BS: `assets == equity + liabilities`
   - PL: `PBT − tax == PAT` (primary), else `total income − total expenses == PBT` (looser)
   - CF: `op + inv + fin + forex == net change`, else `opening + net change == closing`

`validate_statement(path, kind, pages_filter)` is **`lru_cache`d** — the locate→tie-out loop is
the expensive multi-call step and is re-needed by the statement layer *and* both datapoint
scopes.

**Format detection** (`sector.py`): a cheap text fingerprint over the BS candidate pages →
`bank` (Capital & Liabilities / Form A) | `insurer` (Policyholders' / IRDAI) | `nbfc` (Financial
Assets + Debt Securities / Div III) | `manufacturer` (default, Div II). KPI catalog is **data**
(`config/kpis.yaml`), so analysts extend KPIs without code changes.

---

## 5. Layer B: Datapoint extraction (`src/engine/datapoints.py`) — the core

This is a ~2,400-line file and the heart of the active work. It maps the 59-item taxonomy onto
a report. The whole flow is `extract_datapoints(index, scope)`. Read this section carefully —
it is where any new datapoint work happens.

### 5.1 The insight the whole file is built on
> **Group each target by the Ind AS / Schedule III NOTE it structurally lives in.** These note
> categories (Other Equity, PP&E schedule, Borrowings, Share Capital, Deferred Tax …) are
> **mandated by the framework** — they are structural, not company-specific. Locate the *note*
> by its structure (BM25 over framework vocabulary), then extract **every target in the note in
> ONE grounded pass**, mapping each target to a line **by meaning**. Seeing the whole note at
> once is what lets the model pick the right column/line and *refuse look-alikes* — the two
> failure modes of naive per-item Q&A.

### 5.2 The end-to-end flow of `extract_datapoints(index, scope)`

```
1. concepts = load_concepts()              # 59 Concept objects from taxonomy/definitions.yaml
   group concepts by .section              # 16 sections (SECTIONS dict)

2. tags = page_scopes(index)               # tag every page standalone/consolidated/unknown
   allowed = pages tagged {scope, unknown} # the scope's page block (whole doc if scope absent)

3. bs_lines = _statement_lines(index, scope, "bs", allowed)   # face lines: label, note_ref, value
   pl_lines = _statement_lines(index, scope, "pl", allowed)   # (deterministic parser first)

4. for each section (in parallel threads):  run(section, concepts_in_section)
```

Where `run(section, cs)` = `_run_core` + three post-passes (rescue, deferred-tax override,
pl-changes override, low-confidence capping).

`_run_core` **routes each section to one of seven read strategies** by section type:

| Strategy | Sections | How it reads |
|---|---|---|
| **`_category_vision`** | `investments` (`VISION_CATEGORY_SECTIONS`) | dense class-listing note → **vision**; model lists member holdings, **code sums** them |
| **matrix readers** | `ppe` (`MATRIX_SECTIONS`) | 4-stage cascade: deterministic per-column → deterministic per-row (movement identity) → model text read → vision |
| **`_targets_vision`** | `borrowings` (`VISION_TARGET_SECTIONS`) | garbled specific-cell note → **vision** cell read |
| **`_reconciled_section`** | most BS/PL note sections | try candidate pages; accept the page whose components **sum to the tied-out parent** — deterministic trust |
| **`_extract_section`** | fallback for the above; primary for non-additive | grounded best-effort read of located/BM25 pages |
| **non-additive skip** | `share_capital` (`NON_ADDITIVE_SECTIONS`) | **skips reconciliation entirely** (see 5.5); goes straight to grounded extract with signature-located pages |
| **`_dt_rows`** override | `deferred_tax` | deterministic movement-matrix reader, arithmetic-validated (post-pass) |

### 5.3 `page_scopes()` — scope tagging (critical, and hard)
Indian reports contain a **full standalone block AND a full consolidated block**, each with its
own notes. Restricting extraction to the right block removes cross-scope contamination. Tagging
forward-fills from **top-region anchors** (first ~6 non-empty lines only — matching the whole
page let prose mentions flip whole blocks). Anchors are **tiered**: a statement TITLE
("Standalone Balance Sheet as at …") outranks a section-nav phrase; titles must start their
column segment (defeats sidebar cross-refs); tier-2 section phrases must occupy nearly their own
line (defeats nav ribbons that list every section on every page); a bare title with no
"consolidated" in the top region anchors *standalone* (many reports title standalone statements
plainly). This function was the single cause of all 14 held-out locate gaps before it was
hardened; nifty100 locate went 176 → **189/190** by fixing it. **Scope tags feed every locate
downstream**, so a change here shifts every section.

### 5.4 Verification is the product, not extraction
Every value carries a **confidence** ∈ `{reconciled, grounded, unverified, absent}`:
- **`reconciled`** — the note's components summed to the tied-out parent statement line
  (strongest — an arithmetic proof).
- **`grounded`** — the value's digit-string is actually printed on the read page
  (anti-hallucination backstop; `_digits(val) in page_digits`).
- **`unverified`** — present but neither reconciled nor grounded (a consumer should re-check).
- **`absent`** — not found (correct when GT says "Not disclosed").

The **grounding check** (`len(vd) >= 3 and vd in page_digits`) is the single most reused guard
in the file — it kills vision hallucinations (a value not in the page's extractable text is a
fabrication, because shredding destroys *geometry*, never *digits*), and it validates every
text read.

### 5.5 Non-additive notes — why `share_capital` is special
Share Capital lines (Authorised 250 / Issued 225 / Subscribed & Paid-up 225) are **successive
stages of the same capital, not addends** — the true note can *never* reconcile. Worse, *wrong*
pages DO tie out arithmetically and win: the Statement of Changes in Equity (opening+changes=
closing), rights-issue prose ("increased from 115.42 to 129.27"), a buyback sub-table. Each then
returns the whole note as absent (the "whole-note collapse" bug — 24 misses in one run). Fix:
`NON_ADDITIVE_SECTIONS = {share_capital}` **skip reconciliation entirely** and go straight to a
**signature-located, grounded, column-reflowed** extract. Verification for them is *groundedness*,
not arithmetic. This is a **property of the Schedule III note TYPE**, so it generalises to every
Indian company.

### 5.6 Deterministic readers — the winning pattern
The biggest accuracy gains came from replacing stochastic LLM reads with **deterministic readers
guarded by an arithmetic identity, silent when ambiguous**:

- **`_bs_face_lines_det` / `_pl_face_lines_det`** — parse the statement face mechanically
  (label | note | CY | PY). Accepted **only** when the identity closes (BS: assets == E&L;
  PL: TI−TE == a printed "Profit before …" line, with consolidated JV-share variant, or
  PBT−tax == PAT). When accepted, parents + note refs are stable run-to-run and the ~2 LLM
  calls/scope are skipped. Decoy discipline: scope-tagged candidates only (infosys prints an
  abridged summary BS in front matter that passes the identity with the *other* scope's figures).
- **`_matrix_columns`** — asset-per-column PPE schedules: read closing gross/dep rows straight
  from de-rotated layout text, map columns by trusted header geometry (`_ppe_layout`). No model.
- **`_matrix_rows`** — asset-per-row PPE: accept a row **only** when the Ind AS 16 movement
  identity closes (`gross_open ± moves = gross_close`; same for dep; `gross − dep = net`).
  Handles clean rows, prose-welded rows, and rotated/shredded grids (PyMuPDF y-cluster rebuild).
  Corpus: **24/24 correct, 0 wrong**; PPE errors went 12 → 0.
- **`_dt_rows`** — deferred-tax movement matrix (`opening ± movements = closing`, 0.1% tol) +
  a 2-column component-sum mode; silent on multi-orientation ambiguity. **16 correct / 0 wrong.**

The meta-rule, learned the hard way: **a "safer" heuristic can silently break a validated
reader — re-run the full deterministic sweep after any change to one** (a modal-year tweak once
regressed the PPE reader; only the sweep caught it).

### 5.7 The rescue pass (`_rescue_absents`)
The largest residual failure shape is *"the value IS printed in-scope but the section read
returned absent"* (refused synonym, two-up garble, or the value lives in a *neighbouring* note —
P&L face, Other Income, related-party). The rescue: (1) deterministically scan the scope's pages
(reflowed) for table-row lines carrying **all** stemmed content words of one of the target's
taxonomy aliases, ranked by alias specificity; (2) **one** strict re-ask per section quoting only
those lines; a returned value must be **byte-grounded** in the quoted lines; **fills absents
only**, never touches a present value. This took hindalco 16→10 and adani 13→5 errors.

### 5.8 Composite datapoints
Some GT values are the **sum of several printed lines** with no single line to cite (treasury
shares held via two trusts; auditor remuneration as five fee lines; a subtotal minus a "Less:"
line). The schema has an `addends` field: the model returns the printed components, **code does
the arithmetic** (and code always wins over any model-computed value); grounding requires *every*
addend's digits on the page. Opted in per section (`_COMPOSITE_SECTIONS`).

### 5.9 Value hygiene (deterministic, model-agnostic)
`_hygiene()` strips currency/footnote noise (`₹`, `` ` ``, `Rs`, `*`) and restores dropped
negative parentheses (`_reconcile_sign`), with `sign=False` for **count** datapoints (parenthesised
counts are "current (prior)" typography, not negatives). `_fmt_num()` matches printed precision
(Nestle prints `651.4`, not `651.40` — emitting the wrong precision broke the grounding recheck).

---

## 6. The LLM interface (`src/llm.py`) and config (`src/config.py`)

- **`extract_json(...)`** — one structured call via the OpenAI **Responses API**, with a
  **strict JSON schema** (`text.format = json_schema, strict=True`), so output is always valid
  JSON — no parsing guesswork. `store=False` always (privacy). Supports `images_b64` (vision)
  and `file_ids` (native PDF read). **Key robustness feature:** on a truncated response
  (`status == 'incomplete'`, i.e. hit `max_output_tokens`) it **retries once with double the
  budget (cap 8000)** — truncation was a silent whole-section collapse mode (one call landing on
  exactly 2000 tokens lost 12 datapoints).
- **`ephemeral_file` / `ephemeral_vector_store`** — upload → yield id → **delete in `finally`**
  (even on error); only ever deletes the resource it created.
- **Config knobs** (from `.env`): `OPENAI_MODEL_DEFAULT` (live: `gpt-5.4-mini`),
  `OPENAI_MODEL_LARGE` (`gpt-5.4`), `REASONING_EFFORT` (`none` — A/B showed none==low for value
  extraction), `SELF_CONSISTENCY_N` (1; the reliability lever for hard tables is N=3 consensus,
  not a bigger model), `OPENAI_STORE_RESPONSES` (False). Note: `config.py`'s *code defaults*
  name older models; **`.env` is the source of truth for the live model.**

---

## 7. The taxonomy (`taxonomy/definitions.yaml`) — the output contract

59 items. Each item:

```yaml
- key: Share Premium               # the output identifier (also carries scope/orientation)
  scope: both                      # standalone | consolidated | both
  value_type: monetary             # monetary | count
  concept: >                       # the ECONOMIC DEFINITION — this is what the model maps by
    Premium received over face value ... Return the CLOSING balance ..., NOT the opening ...
  aliases:                         # ILLUSTRATIVE labels to calibrate the model (NOT a match-list)
    - Securities premium
    - Securities Premium Account
  column_hint: unquoted only; ...  # the SELECTOR (which column/row: gross vs dep, CY vs PY, ...)
  location_hint: Other Equity ...  # routes the key to a SECTION (see _ROUTES)
```

- **`concept`** is the load-bearing field — the model maps by meaning using this text. It is
  passed **untruncated** (`[:400]`) in prompts (truncation once cut off a "NEVER return the
  TOTAL" guard and caused a false positive).
- **`aliases`** are examples only, all passed (the old `[:3]` cap caused refusals — e.g. adani's
  "Employee Benefits Liability" is alias #5 for leave-encashment DTA).
- **`_route(key, location_hint)`** maps each key to one of 16 `SECTIONS` (checked
  specific-before-general). `_PARENT` maps each section to its BS/PL parent line for tie-out.

**The taxonomy is data, not code.** Adding a datapoint = adding a YAML item + (if a new note
type) a `SECTIONS` entry, a `SECTION_LABEL`, a `_ROUTES` rule, and optionally a `_PARENT`.

The 16 sections: `other_equity, investment_property, investments, ppe, trade_payables,
borrowings, other_nc_liabilities, other_cur_liabilities, loans_advances, other_cur_assets,
share_capital, finance_costs, other_expenses, deferred_tax, pl_changes_inventory,
consolidated_equity`.

---

## 8. Evaluation harness & ground truth

- **`build_error_report.py`** — the eval harness. Runs the engine on 5 companies
  (`reliance, hindalco, itc, infosys, adani` from `~/Downloads/<c>.pdf`) × 2 scopes, diffs vs
  `data/gt_master_corrected.csv`, and writes **`error_report.xlsx`** (Summary + per-company
  sheets) plus full run logs under `logs/run_<ts>/`:
  - `llm_calls.jsonl` — every API call (company/scope/section/pages/tokens/cost/status)
  - `datapoints_<comp>_<scope>.json` — **every** datapoint incl. correct ones (forensics
    without re-running)
  - `errors.json`, `summary.json`
- **⚠️ This harness calls the paid API.** The owner is highly cost-sensitive and requires
  explicit approval before *any* paid run (~$3.20 per full 5-company × 2-scope run).
- **Ground truth**: `data/gt_master_corrected.csv` (`company, scope, key, original_value,
  corrected_value, status, evidence`). `"Not disclosed"` = correctly absent.
  Held-out set: `data/gt_holdout_nifty.csv` (96 rows, 10 unseen companies incl. a bank) — the
  true generalisation test, **not yet run paid**.
- **`tests/regression_suite.py`** — self-contained, **no API calls**. Fast tier (seconds):
  hygiene, `_wants_total` sweep, composite arithmetic, prompt-scoping invariants, PPE row-reader
  units, taxonomy integrity, truncation-retry mock. `FULL=1` adds corpus sweeps (PPE 24/0,
  deferred-tax, share_capital locate 10/10, reflow guard, mocked end-to-end). **Run the fast
  tier after every edit; FULL before any paid run.**

### The eval methodology that matters
- **Single-run eval variance is ~±9 points.** Do NOT judge a locate/text fix by one paid run —
  you'll misread noise as regression (this happened and cost money).
- **Validate the deterministic parts for free**: does locate land the right page? Is the value
  on the page? Is the reflowed text byte-preserving? Judge collapses by a *deterministic* yes/no
  ("did any whole-note collapse happen"), not the raw error count.
- **Method that works here:** dump the note page both `-layout` and reflow for free → find the
  ONE root cause → design an *isolated* fix → **prove isolation deterministically** → only then
  spend on a validation run.

---

## 9. Cost & performance

- **Annual report, full per-datapoint path:** ~$1.90–$3.20 per report (5-company run ~$3.20).
  Input tokens ≈ 94% of cost; scales with **note density**, not page count.
- **Small quarterly (whole-doc single call):** ~$0.11.
- `gpt-5.4-mini` is **−17 pts** accuracy vs the full model — but the reliability lever is
  **N=3 self-consistency** (~3× cost), *not* a bigger model. The bigger long-term lever is
  **more deterministic readers** (free, zero variance).
- Sections run in **parallel threads** (`DP_MAX_WORKERS`, default 6; set to 1 to debug serially).

---

## 10. Known limitations & open problems (where new work is most valuable)

These are documented in `HANDOFF.md` §5 with full forensics. In rough priority order:

1. **Bilingual banks (Form A) statement parsing** — BoB / CANBK / ICICI / PNB stay silent on
   the deterministic parsers (bilingual labels). 5/9 nifty banks accepted; 4 fall back to LLM.
   The 10 banks + TRENT are the known **format boundary** — the next major format-work frontier.
2. **Wide-SOCIE (`other_equity`) deterministic reader** — same matrix pattern as PPE/deferred-tax;
   would kill the largest remaining LLM-noise source (infosys's 14-column SOCIE grid).
3. **Cross-note sourcing** (~7 rows) — some `other_expenses` values live on the P&L face / CF /
   Other Income / related-party note, outside the section's read window (same shape as the
   rejected `other_nc_liabilities` work).
4. **Held-out paid eval** against `gt_holdout_nifty.csv` (~$7) — the true generalisation number
   is still unmeasured.
5. **GT/taxonomy policy defects** (~15 rows) — several remaining "errors" are GT inconsistencies
   or policy-ambiguous rows (JV-vs-associates boundary, finance-cost wording, deposits) needing an
   *owner decision*, not code.
6. **`investments`** is capped at `unverified` (`LOW_CONFIDENCE_SECTIONS`) — class boundaries are
   intractable for both mini and full models.
7. **Latent fragility:** `_statement_lines` for a BS-anchored section can still draw both scopes'
   parents from whichever BS page tied out first if scope tagging fails — now harmless for
   share_capital but a general risk.

---

## 11. Extension guide — how to add new functionality cleanly

The codebase has hard-won conventions. Follow them or you will regress something.

### 11.1 Add a new datapoint
1. Add a YAML item to `taxonomy/definitions.yaml` with a precise `concept` (definition), a
   `column_hint` (selector), representative `aliases`, and a `location_hint`.
2. Ensure `_route()` sends its key to the right `SECTIONS` bucket (add a `_ROUTES` rule if the
   location_hint keyword isn't matched). If it's a brand-new note type, also add `SECTIONS`,
   `SECTION_LABEL`, and (for reconciliation) `_PARENT` entries.
3. Run the **fast** regression tier; add a corpus row to GT; validate deterministically before
   spending on a paid run.

### 11.2 Add a new note SECTION or read strategy
- Decide the read strategy: is the note **additive** (reconciles to a parent)? →
  `_reconciled_section`. **Non-additive stages**? → `NON_ADDITIVE_SECTIONS`. A **wide matrix**
  with an arithmetic identity? → build a deterministic reader (copy `_matrix_rows` / `_dt_rows`).
  **Garbled dense cells**? → vision (`_targets_vision`). **Two-up**? → add to `COLUMN_SECTIONS`.
- **Prefer a deterministic, identity-validated, silent-when-ambiguous reader** over an LLM read
  wherever an accounting identity exists. This is the pattern that moved the needle every time.

### 11.3 Prompt enrichment — the cardinal rule
**Never enrich prompts globally.** Threading hints/full-aliases into every section once regressed
`other_expenses` 17→25 (added refusal pressure). Enrichment is **opt-in per section on run
evidence** (`_FALLBACK_HINT_SECTIONS`, `_FULL_EXAMPLE_SECTIONS`, `_SECTION_HINT`,
`_COMPOSITE_SECTIONS`). Keep every other section's prompt byte-identical to the validated baseline.

### 11.4 Invariants any change must preserve
- **Privacy:** local read only; `store=False`; no uploads except the ephemeral delete-in-finally
  path; BM25 not embeddings.
- **Miss beats wrong:** a wrong value is worse than "absent." Every reader goes silent /
  returns None when it cannot verify.
- **Code arithmetic wins** over any model-computed value (composites, deterministic readers).
- **Determinism where possible:** anything that can be done in code (locate, sum, reflow, parse
  a face) should be — for cost, privacy, and zero variance.
- **Isolation, proven for free:** scope every fix to the section it fixes; prove other sections
  are byte-identical before paying for a run.

### 11.5 Ideas that were tried and REJECTED (don't redo without a new idea)
- **Vision routing as the accuracy lever** — rejected; misses were *locate* failures, not
  perception. The lever is locate/reflow, not the model's eyes.
- **`other_nc_liabilities` deterministic reader** — heterogeneous value sources + GT
  inconsistency; a fix helps 2 companies and regresses a third.
- **Global prompt enrichment** — regressed other_expenses (see 11.3).
- **Name-based residual guards** (keying off the KEY string) — fought the definition and demoted
  correct answers. The *definition* is authoritative, never the key's wording.

---

## 12. File map (quick reference)

| File | Role |
|---|---|
| `src/engine/report.py` | Orchestrator / single entry point; small/scanned/big routing |
| `src/engine/index.py` | `PageIndex`: page text, BM25, two-column reflow + safety guard |
| `src/engine/statements.py` | Layer A: locate→extract→arithmetic tie-out for BS/PL/CF |
| `src/engine/datapoints.py` | Layer B: the 59-item taxonomy extraction (the core, ~2400 lines) |
| `src/engine/sector.py` | Format detection (4 formats) + KPI catalog loader |
| `src/engine/qa.py` | Ask-anything: BM25 retrieve → answer → ground quote |
| `src/engine/vision.py` | Scanned/image-only fallback (render → classify → read → tie-out) |
| `src/engine/whole_doc.py` | Small-doc single-call path (quarterly filings) |
| `src/engine/cli.py` | CLI: `--statements --ask --kpis --datapoints --all` |
| `src/llm.py` | Responses-API wrapper: strict JSON, store=False, truncation retry, ephemeral files |
| `src/config.py` | `.env`-driven config (models, reasoning, privacy) |
| `taxonomy/definitions.yaml` | The 59-datapoint output contract (data) |
| `config/kpis.yaml` | Sector KPI catalog (data) |
| `build_error_report.py` | Eval harness → `error_report.xlsx` + run logs (**paid**) |
| `tests/regression_suite.py` | Free deterministic regression tests (fast + FULL tiers) |
| `data/gt_master_corrected.csv` | Ground truth (5 dev companies × 2 scopes) |
| `data/gt_holdout_nifty.csv` | Held-out GT (10 unseen companies) — not yet run paid |
| `HANDOFF.md` | The definitive prose history & forensics (read §5 for every fix) |
| `README.md`, `PLAN.md`, top-level `src/*.py` | **STALE / legacy** — abandoned vision+vector design; ignore |

---

## 13. Suggested directions for new implementation (for Fable)

Ranked by leverage, given everything above:

1. **Deterministic wide-SOCIE reader for `other_equity`** — the single largest remaining source
   of LLM run-to-run noise. It's the same "matrix + arithmetic identity, silent when ambiguous"
   pattern as `_matrix_rows`/`_dt_rows`, which have a perfect track record. High-confidence win.
2. **Bank (Form A) & bilingual statement parsing** — extend `_bs_face_lines_det`/`_pl_face_lines_det`
   to the 4 bilingual banks and TRENT. Unlocks a whole company class; format-boundary work.
3. **Cross-note sourcing** — a bounded, deterministic "look in neighbouring notes" step for the
   handful of `other_expenses` values that live on the P&L face / Other Income (generalise the
   rescue pass, keep it grounded).
4. **Confidence-driven consumer surface** — the engine already emits calibrated confidence
   (`reconciled > grounded > unverified > absent`). A new product feature could route only
   `unverified` cells to N=3 consensus or human review, spending compute exactly where it's
   uncertain.
5. **Held-out generalisation harness** — wire `gt_holdout_nifty.csv` into a paid eval and a
   free deterministic pre-check (locate coverage, grounding coverage) so generalisation is
   measured continuously, not once.

Whatever you build: **stay deterministic where an identity exists, verify everything, keep fixes
isolated and prove isolation for free, and never trust a single paid run.** That discipline is
the reason this engine went from 82% to 91.5% — and it's how it gets to 95%+.
