# Plan: Definition-First Annual Report Parameter Extraction (PoC)

## Context

We are building a standalone PoC (`/Users/jeetendrajaiswal/Desktop/self/poc`) that extracts a
fixed taxonomy of ~60 financial data points from large Indian corporate annual reports (Dr. Reddy's,
Adani Enterprises, Reliance, ITC, Infosys, Hindalco) with high mapping accuracy, math consistency,
and strict privacy.

The core problem is **semantic**: every company labels the same accounting concept differently
(`Securities Premium` vs `Share Premium`, `Trade Payables` vs `Sundry Creditors`). Keyword matching
fails. The agreed solution is **definition-first**: author a crisp, reviewable definition for each
data point, then use those definitions to (a) retrieve the right note from the report and (b) map the
company's reported line/value to our data point by *meaning*.

**Grounding authority stack (per item) — broader than just Schedule III + Ind AS:**
1. **Schedule III, Division II** (format + General Instructions + **2021 MCA amendment**: CWIP/payables/
   receivables ageing, promoter holding, ratios) — structure.
2. **Specific Ind AS** — 16 (PP&E), 40 (Investment Property), 109/107/32 (financial instruments, ECL —
   hedging reserve, impairment, doubtful-loan allowance), 12 (deferred tax), 2 (inventories), 19
   (employee benefits), 102 (ESOP), 110/28/111 (consolidation, parent equity, JV), 23 (borrowing
   costs), 115 (revenue/contract liabilities), 37 (provisions) — meaning.
3. **Companies Act 2013** — the items standards don't define: share-capital ladder (§2, 43–66),
   **CSR §135 + Schedule VII**, auditor remuneration (§142), buyback (§68).
4. **ICAI Guidance Notes / Schedule III General Instructions** — sub-classification granularity.
5. **SEBI LODR** — listed-company disclosures (light touch).
6. **Sector applicability** — a data point may be material for one company and absent for another
   (Infosys/IT has ~none of the manufacturing items; Hindalco/metals has Power & Fuel, Stores & Spares
   as core lines). Definitions must not assume universality.

Naming (`Sundry Creditors`, `Carriage Outwards`, `_` nesting) matches a **Capitaline-style
standardized schema**; treated as a stable standardized concept set unless the user says otherwise.

We reuse the proven patterns from the reference Django app (now copied into `poc/documents/` and
`poc/financial_reports/`) — the Responses API privacy contract, file/response cleanup, and the
vector-store flow — but strip Django and add the new layers (taxonomy definitions, standalone+
consolidated split, local math validation, error-driven self-correction, accuracy scoring).

**Decisions locked with the user:**
- Retrieval primitive = **OpenAI vector store + `file_search`** (annual reports are large). No page-number citation for now; evidence = the verbatim snippet `file_search` returns.
- Definitions: **I draft all ~60, user reviews** before any pipeline build.
- Accuracy: user has **ground-truth values** → we score extracted vs expected per company.
- Taxonomy origin unknown → author aliases defensively from Indian reporting conventions.

## Approach: validate-first, then earn complexity

**Guiding principle (killer-critic correction):** do NOT build the full 9.5 apparatus up front — that
optimizes the blueprint, not the outcome, and the score would be faith-based. Build the simplest thing,
measure it against ground truth, and add each piece of machinery only when a *measured* failure proves
it necessary. The score becomes real because it is measured, not self-assigned.

**Phase 0 — Thin-slice baseline (validate first).** One company, ~12 representative items spanning
easy → hellish (e.g. Share Premium, Sundry Creditors, PP&E gross/accum-dep, a 3-level deferred-tax
sub-line, a consolidated-only item). Simplest possible extraction: upload PDF → per-group structured
ask → JSON; **no vector store, no locators, no dual-model.** Score vs ground truth. This establishes
the empirical baseline and tells us which failures are real vs imagined.

**Phase 1 — Definitions dictionary** (for the full set; deliverable, signed off). Grounded against all
6 reports.

**Phase 2 — Add machinery incrementally, each justified by a Phase-0/scoring gap:** local structure-map
+ multi-locator → only if simple locate misses; dual-model/adversarial → only if wrong-column/year
errors show up; coverage ledger, caching, regression → as the set scales. Vector store is included
because the user prefers it, but Phase 0 will reveal whether it beats local retrieval or is dead weight.

No pipeline code beyond Phase 0 is finalized until definitions are agreed and the baseline is measured.

---

## Phase 1 — Definitions Dictionary

Single source of truth: `poc/taxonomy/definitions.yaml` (human-reviewable; loaded into Pydantic at
runtime). One record per data point with these fields:

| Field | Purpose |
|---|---|
| `key` | Exact data-point name from the user's list (e.g. `Gross Carrying Value_Furnitures & Fixtures`) |
| `hierarchy_path` | The `_`-split path (parent → child → grandchild) |
| `group` | Disclosure cluster / note it belongs to (see grouping below) |
| `definition` | 1–2 sentence crisp meaning, grounded in Schedule III / Ind AS |
| `aliases` | As-reported label variants Indian companies use |
| `location_anchor` | Which statement + note it is disclosed in (drives the vector query) |
| `statement_scope` | `standalone` / `consolidated` / `both` |
| `value_type` | `monetary` / `count` / `per_share` |
| `sign_convention` | parentheses = negative; contra items (e.g. `Less: Doubtful Advances`) |
| `disambiguation` | Rules to avoid wrong mapping (gross vs net column; sub-line vs total) |
| `math_links` | Related keys + identity (e.g. `net = gross − accumulated_depreciation`) |
| `kind` | `reported` (a printed line) vs `derived` (a subtotal we must sum from components — e.g. `Total Power & Fuel`, `Total Long Term Liabilities`, `Total Changes in Inventories`) |

**Run-level spec (not per-record), because they silently break accuracy:**
- **Reporting period** — **latest FY present in each report** (the current-year column, never the
  prior-year comparative). Ground truth must be for that same year.
- **Scale/sign normalization** — reports use ₹ lakhs / crores / millions inconsistently; capture each
  company's reporting scale, normalize to a canonical unit before scoring; parentheses = negative;
  `"Not disclosed"` ≠ 0 ≠ blank.

**Phase 1 must be grounded against all 6 real reports** (Reliance/Adani conglomerates, ITC FMCG,
Dr. Reddy's pharma, Hindalco metals, Infosys IT) so aliases/location-anchors cover every disclosure
style before definitions freeze — not overfit to two. All 6 PDFs are therefore a Phase-1 input.

The ~60 keys cluster into ~12 disclosure groups (Equity Share Capital, Reserves, Non-current
Investments, PP&E gross/accum-dep/CWIP/investment-property, Borrowings, Trade Payables, Loans &
Advances / Other Current Assets, Other Liabilities, Deferred Tax, P&L Expense schedule, Finance
Costs, Changes in Inventories, Consolidated-only equity/JV items). Grouping is what makes Phase 2
token-efficient: one retrieval+extraction per group, not 60 separate calls.

Workflow: I author `definitions.yaml` (all ~60) → user reviews/corrects the ambiguous items
(3-level deferred-tax, borrowings sub-lines, contra items) → freeze.

---

## Phase 2 — Standalone Vector Pipeline

Project layout (Django stripped; config via `.env`):

```
poc/
  requirements.txt, .env.example, .gitignore   [done]
  src/
    config.py            [done] env-based config; store_responses=False default
    responses_adapter.py  port of ResponsesAPIAdapter — store set LAST, delete_response()
    vector_store.py       port of VectorStoreService — upload, create, poll, query, cleanup
    structure_map.py      deterministic local map: notes index (Note N→pages), per-page text index, SA/CO region boundaries
    locators.py           multi-locator union: note-index + alias/keyword scan + vector + vision → candidate regions
    pdf_reader.py         modality-adaptive read of the located note: text-layer detect → local text, OR render page→image for multimodal vision read (adds `PyMuPDF`)
    verify.py             3-tier checks + adversarial refutation + dual-model read (fire on non-clean items only)
    ledger.py             coverage ledger (justified terminal state per item×{SA,CO}) + result cache (pdf-hash, def-version)
    taxonomy.py           Pydantic models + loader for definitions.yaml
    extraction.py         definition-driven file_search per group, x standalone & consolidated
    validation.py         accounting-identity math checks from math_links
    self_correction.py    feed failed checks back → bounded re-extraction rounds
    scoring.py            extracted vs ground-truth → per-company accuracy
    run.py                CLI: <pdf> [--company X] → verified JSON + score report
  taxonomy/definitions.yaml
  data/    (input PDFs)        output/  (JSON results)
```

**Reused reference code (port, don't reinvent):**
- Responses privacy contract — `poc/financial_reports/openai_responses_helper.py` +
  `…/llm/frp/backend/smart_query/planner/responses_api_adapter.py` (`store` set after kwargs;
  `DELETE /v1/responses/{id}`; 429/5xx backoff).
- Vector store lifecycle — `poc/documents/vector_store_service.py` (`get_or_create`, poll
  `file_counts.completed`, `query_with_vector_store` with `include=["file_search_call.results"]`,
  `cleanup_vector_store`).
- Prompt conventions (parentheses=negative, ₹ Cr/Lakhs, FY Apr–Mar, exact-line-item discipline) —
  `poc/documents/ai_summary_annual_service.py`, `poc/documents/ai_summary_prompts.py`.

**Locate-by-concept, not by label (the answer to "every company names things differently"):** we
never query the company's label. Two-stage **locate-then-read**:
- **Stage A — find the section** by *concept + Schedule III structure* (statement → note category →
  parent total), which the company cannot rename. Query is built from the definition + aliases (semantic
  embedding match), e.g. "note reconciling gross carrying amount, accumulated depreciation and net block
  of tangible assets by asset class" → retrieves the PP&E note under any heading. Raise
  `max_num_results`, `include=["file_search_call.results"]`.
- **Stage B — read the located note from the LOCAL PDF (modality-adaptive, not file_search chunks)**:
  file_search returns mangled chunks, not the whole table. Since we already hold the PDF (it is our
  input), we take the note/section Stage A identified and read it from the local file. **Per located
  region, detect modality (pymupdf: real text layer vs image-only):**
  - **text layer** → pull that section's faithful text locally (scoped to the located region — NOT a
    heuristic whole-doc page-slicer); model maps reported line → our `key` over the intact table.
  - **image / scanned** → **render the located page(s) to images and send as `input_image` to the
    multimodal model** (gpt-5/gpt-5.2 vision) to read the table pixels directly. Better than Tesseract
    on dense tables, zero extra system deps. Traditional OCR (Tesseract/ocrmypdf) only as optional last
    resort if vision is unavailable.
  - **whole-doc scanned** (negligible text layer overall, detected upfront) → file_search locate may
    fail; degrade to a vision page-sweep over candidate sections and flag higher cost + uncertainty.
    (Rare for the 6 named large-caps — all file typeset digital ARs.)
  Mapping uses the definition + aliases + disambiguation as the rubric. **Vector for recall; local text
  OR page-image vision for precision.**

Rationale: this keeps the vector store as the spine (semantic locate, label-independent) while removing
the table-fidelity ceiling that pure-chunk reading imposes. Privacy posture is unchanged (the upload for
locate already happens; the local read adds no exposure). Caveat: locate is still probabilistic — odd
nestings can miss, which evidence-grounding + alias-expansion (from the 2 grounding reports) +
ground-truth scoring catch and quantify.

**Flow per company PDF:**
1. Upload PDF → create vector store → poll until ready (reuse poll loop / size-based timeouts).
2. For each definition group: build a `file_search` query from the group's `definition` +
   `location_anchor` + `aliases` (semantic, not bare label) → retrieve the note's table text.
3. Structured extraction (strict Pydantic JSON) maps reported line → our `key`, returns
   `{value, unit, evidence_quote, statement, found, confidence}` for **standalone and consolidated**
   separately, for the **target FY only**. `evidence_quote` is the citation; `page_number` is captured
   best-effort only (no effort spent on accuracy/offsets — de-prioritized). Missing → `"Not disclosed"`,
   never invented. `derived` items are summed from extracted components.
4. **Verification, three tiers (page-citation tier removed — de-prioritized):**
   - **Cross-year consistency (primary, applies to all ~60 — uses `Downloads/prev/`):** the prior-year
     comparative column in *this* year's report must equal the current-year value in *last* year's
     report. Two independent sources agreeing = strong correctness signal; this catches the dominant
     wrong-cell / wrong-column / wrong-year errors that evidence-grounding cannot. Mismatch → flag as
     extraction-error OR genuine restatement (the latter not counted as a miss). Label- and
     math-independent, so it covers items no accounting identity reaches.
   - **Evidence-grounding (applies to all ~60):** the quote must contain a label matching the concept's
     definition/aliases — guards against a right-number/wrong-concept mapping.
   - **Math validation (where identities exist):** share-capital ladder (Authorised≥Issued≥Subscribed≥
     Paid-up); Paid-up≈shares×face value; PP&E net=gross−accum-dep (extract net too, purely to
     validate); `derived` subtotals = Σ components.
5. **Self-correction — tiered (not blanket, to control cost):** single-shot extraction first. Escalate
   ONLY for items that fail evidence/math or return low confidence:
   - **N-sample self-consistency** — re-extract the item N times (independent calls), take the agreeing
     value; combats the forced `temperature=1.0` on gpt-5 models (the adapter omits temp for gpt-5).
   - **Scope-confirm** — an explicit check that the picked cell is the right *statement* (standalone vs
     consolidated), *FY* (latest, not comparative), and *column* (gross vs net) — the dominant error
     modes, which evidence-grounding alone does NOT catch.
   Bounded by `MAX_CORRECTION_ROUNDS`; feed the failure log back into the targeted re-read.
6. **Cleanup (always, in `finally`)**: delete vector store + uploaded file + any response IDs.
7. Write verified JSON to `output/<company>.json`.

**Privacy (with honest disclosure):** every Responses call uses `store=False` (no dashboard logging).
**But** building a vector store *physically uploads the PDF to OpenAI* and persists it until cleanup —
`store=False` does not cover this, and OpenAI retains files for abuse-monitoring unless the *account*
has Zero-Data-Retention. Mitigation: bulletproof cleanup (delete in `finally` + an orphan sweeper that
lists & deletes any leftover files/vector stores), and document the trade-off. If the strict reading
("PDF never leaves our control") is mandatory, vector store is not viable and we must revisit.

**Resource deletion guarantee (hard requirement — user-mandated):**
- Every uploaded file → `client.files.delete(file_id)` in a `finally` block — runs on success AND on
  any exception/crash path. No code path leaves a file on OpenAI. (Mirrors reference
  `ai_summary_service._cleanup_openai_file`.)
- Vector store (if used) → `client.vector_stores.delete()` + its file, also in `finally`. (Mirrors
  reference `cleanup_vector_store`.)
- Response IDs → `store=False` means nothing is retained; if `store_override=True` is ever used,
  `delete_openai_response()` → `DELETE /v1/responses/{id}` purges it.
- Orphan sweeper → at batch start/end, `files.list()` / `vector_stores.list()` and delete anything
  matching our run prefix that a hard crash (kill -9) could have orphaned.
- Residual (honest): OpenAI may retain inputs up to ~30 days for abuse monitoring unless the account
  has ZDR — outside our code's control; everything we CAN delete, we delete.

---

## Second-layer hardening (deeper scenarios)

- **Standalone vs Consolidated co-location** — both note sets live in one PDF. Detect the two
  document regions up front (locate the "Standalone Financial Statements" and "Consolidated Financial
  Statements" boundaries), tag every page with its region, and **scope each locate + read to the
  correct region**. This is the mechanism behind the scope-confirm check, not just a verification.
- **Note-number two-hop indirection** — ~40 of 60 items are note sub-lines reached via a face
  reference ("Other Equity — Note 14"). Resolve `face line → note number → note table`: locate the
  face reference, capture the note number, then read that note's table for the sub-line.
- **`reported` vs `derived` policy** — the `Total_` prefix is a trap: `Total Power & Fuel`, `Total
  Changes in Inventories` are usually single printed P&L lines, not sums. Rule: prefer the printed line
  if one matches the concept; sum components only when no printed total exists. Set `kind` per item in
  definitions accordingly.
- **Sign / unit / class rules in each definition** — contra items (`Less: Doubtful Advances`,
  `Allowance for…`) carry an explicit sign convention; share-count items carry a count-unit (absolute
  vs lakhs/crores of shares); equity-share items must isolate the equity class (exclude preference /
  other classes).
- **Locate robustness** — locate is non-deterministic and is the recall bottleneck: retry with query
  reformulation (alias-broadening) before giving up. If the **vector store build fails/times out** on a
  very large AR, fall back to direct file-input reading / vision page-sweep so the company does not fail
  silently.
- **Definition-iteration loop (formalized)** — run → score → triage mismatches into "definition gap"
  vs "extraction error" → revise `definitions.yaml` → re-run. Most accuracy gain comes from this loop;
  it is the core Phase-1↔Phase-2 feedback, not a one-shot author-then-build.

## Bulletproofing to ≥9.5 (drive residual risks to near-zero / bounded-auditable)

1. **Multi-locator backbone (kills locate-recall risk).** Build a deterministic local structure map
   first — notes index (Note N → page range), per-page text index, SA/CO region boundaries. Each
   concept then has FOUR independent locators: note-index lookup, local alias/keyword scan (BM25/regex),
   vector `file_search`, vision sweep. **Union the candidates**; a miss requires all four to fail.
2. **Coverage ledger (no silent gaps).** Every item × {SA, CO} ends in a justified terminal state:
   `found(value, page, evidence, checks✓)` or `not_disclosed(note-index✓, alias-scan✓, vector✓,
   vision✓ → provably absent)`. "Not disclosed" accepted only after ALL locators are exhausted.
3. **Adversarial refutation + dual-model read (on non-clean items only).** A skeptic pass tries to
   refute the pick (right region/column/FY?), default-reject on doubt; a second model re-reads the same
   region; disagreement → flag. Targets the dominant wrong-region/column/year error mode.
4. **Calibrated confidence + auto-accept/flag triage.** Triple-verified high-confidence values
   auto-accept; uncertain remainder routes to a review queue with evidence pre-attached. 9.5+ = bounded,
   auditable, near-zero silent failure — NOT "model always right".
5. **Golden regression set + result cache.** Freeze ground truth as a regression test; cache
   located-region + value keyed by (pdf-hash, definition-version) → reproducible, diffable runs despite
   forced `temperature=1.0`.

Heavy tiers (dual-model, adversarial, self-consistency) fire ONLY on non-clean items, so clean
digital-text disclosures (the majority for these large-caps) cost one pass.

**Irreducible ceiling (~0.4 below 10):** truly scanned/garbled pages, genuinely ambiguous disclosures,
and possible errors / vendor-reclassification in the ground truth itself. Documented, not hidden.

**Honest scoring caveat (killer-critic):** the above describes a strong *design* (~9 on paper). It is
NOT a proven outcome — nothing has run on a real PDF, and empirical priors for granular AR note
sub-lines are ~60–80% first-pass. The ≥90% "applicable items" target is partly gameable (deciding
applicability is as hard as extraction) and some items (`Equity Forfeited`, JV depreciation share,
3-level deferred-tax) may simply not be disclosed at that granularity — a disclosure-reality ceiling no
architecture can lift. Build the machinery only as Phase-0 measurement justifies it; report measured
accuracy with applicability decisions shown, so the metric can't silently inflate.

## Cost / latency envelope (rough, PoC scale)
Per company: 1 upload + vector-store build (minutes, size-dependent) + ~12 group locates × 2 regions +
full-note reads + self-consistency (N≈3) only on failed items + vision calls only on image pages.
Order-of-magnitude: tens of model calls/company, low-single-digit USD/company on mini-tier for text;
vision/large-tier escalations add to that. 6 companies = a short batch, not real-time. Documented so
cost is a known quantity, not a surprise.

## Acceptance criteria & tolerance policy
- **Tolerance**: exact match for share counts; ±rounding tolerance for monetary (report rounding to ₹
  cr/lakh) after unit normalization; `"Not disclosed"` must match on both sides ("correctly absent").
- **Success target (set): ≥ 90% match on applicable items** per company (applicable = the item is
  disclosed for that company / sector), excluding flagged methodology divergences. Below 90% → iterate
  definitions and re-run.

## Accuracy measurement (ground truth)

User provides `data/ground_truth.csv` (or xlsx) with columns:
`company, key, expected_value (or "Not disclosed"), statement_scope`.
`scoring.py` compares extracted vs expected with numeric tolerance (unit-scale + rounding aware), and —
because ground-truth provenance is unknown — **classifies each divergence**, not just match/miss:
- **match** (within tolerance),
- **true error** (no solid evidence, or fails math),
- **likely methodology/reclassification divergence** (our value has solid evidence + passes math but
  still differs — e.g. a standardized vendor reclassified the line). Reported separately so the
  headline accuracy number stays interpretable.
Output: per-company precision, the divergence-classified mismatch list (extracted, expected, evidence,
math status), and counts of "correctly absent" (Not disclosed on both sides) so sector-inapplicable
items don't distort the score.

## Real-file findings (probed 2026-06-17, all 6 in ~/Downloads)
- All 6 are **clean text-layer PDFs**, unencrypted, 187–428 pp (reddy 256, adani 396, reliance 187,
  itc 428, infosys 383, hindalco 413). **Scanned/OCR machinery is unnecessary for this set** — local
  text read is fully viable; vision fallback is a dormant safety net only.
- **SA/CO ordering varies**: Infosys/Adani = consolidated-first, Reliance = standalone-first. Region
  detection must handle both orders (cannot assume SA precedes CO).
- **Sector applicability confirmed empirically**: `Carriage Outwards` and `Stores & Spares` are absent
  in Infosys (IT), present in ITC. Scoring MUST be on applicable items only.
- **Naive keyword locate is fragile**: a plain `Authorised capital` regex matched nothing in any file
  (table-cell layout / line breaks / spelling) — confirms locate needs alias + structural robustness,
  and validates the multi-locator design over single keyword scan.
- Env: `.venv` created, libs installed (openai, pydantic, PyMuPDF, pdfplumber, pyyaml, rapidfuzz,
  python-dotenv, requests). **No OPENAI_API_KEY yet** → local backbone runnable now; LLM layer pending key.

## Inputs (provided / still needed)
- **Current-year PDFs** for all 6 → present in `~/Downloads/` (reddy, adani, reliance, itc, infosys,
  hindalco). A Phase-1 grounding input, not just a Phase-2 test input.
- **Prior-year PDFs** for the 6 → present in `~/Downloads/prev/` — used for the **cross-year
  consistency check** (second source). (To inspect after coding is approved.)
- `ground_truth.csv` with expected values (latest FY) — **still needed** to score.
- `OPENAI_API_KEY` in `.env` — **still needed** to run any extraction.

## Verification
1. `pip install -r requirements.txt`; copy `.env.example`→`.env`, set `OPENAI_API_KEY`.
2. Review/freeze `taxonomy/definitions.yaml` (Phase 1 gate).
3. `python -m src.run data/<company>.pdf --company "Hindalco"` → produces `output/hindalco.json`
   with value + evidence + math-check status per data point.
4. Confirm console shows `Store=False` (privacy) and cleanup logs (vector store + file deleted).
5. `python -m src.scoring` → per-company accuracy vs `ground_truth.csv`.
6. Spot-check a few mismatches against the PDF to validate definitions vs mapping failures.
