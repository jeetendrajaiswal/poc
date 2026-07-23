# Root-cause findings & proposed design — quarterly extraction pipeline

*2026-07-22. All analysis offline (code, `output/qtr_raw/*.pkl`, delivered `.xlsx`, source PDFs). No paid calls made.*

---

## 1. Corpus survey (82 PDFs: 78 results-only + 3 full Infosys + 1 Birlasoft)

| Dimension | Distribution |
|---|---|
| Page count | 5–68 (results-only); full Infosys filings 198–379 |
| Text quality | ~60 clean digital; ~15 mixed (some scan pages: coforge, hm, ltm, techm q3/q4, intellect q3, mphasis q2, latent_view q1, persistent q1, infosys q1); 4 majority-scan (coforge q1/q2/q4, latent_view q1). Atishay is digital-with-dirty-OCR-layer (embedded OCR text, 65–91% grounding) |
| Denomination | crores: infosys, hcl, tcs, tata_tech (integer amounts, **no decimals**); lakhs: atishay, genesis, hm, mastek, newgen (2 dp); millions: wipro, techm, ltm, persistent, kpit, coforge, oracle, birlasoft, latent_view, intellect (2 dp or integer); **~10 filings where `filing_unit` finds nothing** (tataelxsi, mphasis, kpit q1/q3, intellect q1/q2) |
| Grouping | Mostly western 1,234,567; Indian 1,23,456 appears in genesis, newgen, tataelxsi, hm, infosys full |
| GAAP variants | Ind AS + IFRS twins: infosys (INR + US$), wipro (all 4 quarters) |
| Exceptional items | ~30 filings |
| Period columns | 4–7; Q2/Q4 filings add 6M/FY columns; BS/CF only in Q2/Q4 for most companies (Q1/Q3 "missing" BS/CF sheets are usually legitimate) |
| Layout traps | two-column label-block/value-block text layers (genesis — OCR'd); transposed segment tables (infosys q1–q3: columns are segments, not periods); bare-year CF headers (`2026 | 2025`, infosys all quarters); date forms without separators (`31March 2026`, hcl) |

## 2. Quantified failure incidence

**Raw layer** (47 pickles; `verify_raw` suites + `_dup_columns` on every grid):
- 15/47 raws have ≥1 identity failure left after repair (atishay×2, coforge×4, infosys q4, latent_view×2, persistent×2, techm q4, wipro×4-IFRS).
- Duplicate columns: infosys q3 (CF), infosys q4 (consolidated results **and** segment), techm q2 (consolidated results) — all still in the shipped raws, ⚠-flag only in the raw title.
- Digit grounding < 70%: coforge q1/q2/q3 (8–14%), techm q4 (13%), ltm q1 (59%), atishay q4 (65%), persistent q1 (69%) — scan-heavy filings where the text layer can't ground anything.

**Delivered layer** (46 workbooks in `output/client/`, checked with the template's own formulas on non-`computed` fields + the TA==TEL cross-identity):
- **38/46 workbooks fail ≥1 printed identity** (528 individual failures; a large share are "reported total present but components under-mapped" — not wrong values, but they blind the formula verification).
- **11 sheets delivered EMPTY while the raw pickle contains the statement**: Infosys cash flow ×8 (every quarter, both scopes), Infosys income ×2 (q1 standalone, q3 consolidated), HM q3 consolidated BS. Silent data loss, zero flags.
- Provably wrong shipped values (spot-confirmed against PDFs): Wipro Q2/Q4 standalone+consolidated BS (assets ≠ printed totals), Genesis Q4 BS (does not balance; from Downloads delivery), Infosys Q4 Travelling Cost 15.96, TechM Q3 consolidated OCI, HCL Q4 (Downloads copies) 13775 = 38,775.

## 3. Root causes (each reproduced from real data)

### RC1 — One unverified transcription channel
`quarterly_statement_tables` asks a vision-capable LLM to emit markdown; that answer *is* the data. Decimal corruption, column duplication, wrong-column reads, label/value shifts, OCR digit errors are all the same underlying event — **the model misplacing or reforming a token while transcribing a wide table** — and the pipeline patches each *symptom signature* separately afterwards.

### RC2 — The correction layer's "authority" is wrong: digit-string collision + a false 2-dp prior
Reproduction (Infosys Q4 standalone, `output/qtr_raw/infosys_q4FY2026.pkl`):
raw row = `['Travel expenses', '401', '380', '413', '15.96', '1,467']`. Page 32 of the full PDF prints **both** `1,596` (Travel FY26, the correct value) and `15.96` (an EPS line) — identical digit string `1596`. `_reconcile_grid` (filing_chat.py:251) collects *all page tokens sharing the digit string* and adopts the unique "canonically 2-dp" form → it selects/keeps `15.96`. **The repair itself corrupts integer-denominated filings** (crores companies print integers; their EPS rows print 2-dp tokens that collide by digits with ×100-larger amounts). The same prior makes it powerless on Wipro (`77.224` is 3-dp → not "canonical" → no candidate → corrupted form kept).
The real authority is **positional**: the token printed at that row/column position in the text layer — never "any token on the page with the same digits". The building blocks already exist (`_page_word_lines`, `_column_centers`) but are only used for sparse-row realignment.

### RC3 — Verification can't see placement errors, and its verdicts don't gate delivery
- Tie-outs and digit-grounding are **placement-blind**: a duplicated column is internally consistent and every value "appears in the PDF", so `tie_out_results` and `verify()` both pass (infosys q4 OCI block: Dec-25 ≡ Mar-25 for 8 rows; techm q2: an invented 7th column ≡ the FY column).
- `_dup_columns` (the patch) detects some of this, triggers **one blind re-ask with the same failure mode**, then puts ⚠ in the raw table *title* — which `map_quarter` drops. The flagged Infosys grid was mapped and delivered with no mark.
- Nothing re-checks the **mapped/delivered** numbers: Genesis shipped with `Total Assets (computed) = 115,541.51` vs `Total Equity & Liabilities = 101,198.69` on the same sheet — the raw grid's printed totals balanced, the delivered sheet doesn't, no check runs there. (PDF p6 truth: TNCA 45,034.74, TCA 56,163.95, TA 1,01,198.69 — the extraction shifted the asset-side values one row against their labels; classic two-column OCR text layer.)

### RC4 — Period semantics parsed from the grid alone, failure = silent empty sheet
Infosys CF header rows are literally `['Particulars', '2026', '2025']`; the phrase "for the year ended March 31," is a page heading the extraction never captures → `parse_periods` = [] → `_periods_fallback` (needs a banner in the grid) declines → **every Infosys cash-flow sheet delivered empty, all four quarters, no flag**. Same class: `31March 2026` (no space) fails `_DATE` → HCL periods have no end date; transposed segment tables yield "periods" that are segment names. The job *knows* it is Q4 FY2026 (the filename/form fields) — that context is never used.

### RC5 — Mapping may sum any N lines into a field; no arithmetic gate before write
HCL `[13775] = 38,775 = 17,599 + 21,076`: the grand-total line (no dedicated field) bled into the subtotal field. The COMPUTED-SUBTOTAL guard (client_map.py:837) now blocks that specific shape (confirmed: current `output/client/HCL_Q4FY2026.xlsx` = 17,599; Downloads copies predate it). But the general invariant — *a formula-field takes exactly one printed line, and the mapped statement must satisfy its cross-field identities before write* — is only reported in the Audit sheet, never enforced or surfaced on the statement sheets.

## 4. Patch verdicts

| Patch | Verdict |
|---|---|
| `_reconcile_grid` 2-dp rule (filing_chat.py:251) | **Actively harmful on integer filings** (created 15.96). Replace with positional reconciliation (M1) |
| `_repair_dropped_decimals` | Sound magnitude prior, narrow. Fold into M1's per-statement number-format model |
| `_realign_sparse_rows` | The right idea (positional authority) applied only to short rows. Generalize to every row/column = M1 |
| `_dup_columns` + re-ask + title ⚠ | Right detector, wrong integration: results-only, blind re-ask, flag dropped at mapping. Subsumed by M1 (column-level positional check) + M2 (flag propagation) |
| COMPUTED-SUBTOTAL guard (client_map.py:837) | Correct invariant; keep, but decide by arithmetic (which candidate satisfies the formula/identities), not label similarity |
| `_infer_scope` | Keep — genuine root fix for OCR'd scope words |
| `_periods_fallback` | Symptom patch; declines the majority case (Infosys). Replace with M3 |
| CF `PBT+adjustments=OPBWC` check (verify_raw) | Keep — becomes part of the shared identity suite |
| `_sized_comment` | Cosmetic, keep |

## 5. Proposed design — three mechanisms replace the patch pile

### M1 — Positional source reconciliation (deterministic, offline)
Build the printed table from the text layer once per statement page: rows by y-cluster, column x-centers from full rows (`_page_word_lines`/`_column_centers` already do this). Align each extracted grid row to its printed line (label words + digit overlap — the `_realign_sparse_rows` matcher, applied to *all* rows). Then **every cell is confirmed or corrected against the token at its (row, column) position**:
- wrong form, same digits (`77.224`/`77,224`, `15.96`/`1,596`) → printed form wins;
- value from the wrong column (duplicated/shifted columns) → detected because the printed token at that x-position differs → correct from source;
- number parsing uses a per-statement format model (grouping style, decimal places) inferred from the *source page*, not per-cell guesses — `detect_number_format`/`_num_repair` fold in here;
- cells that cannot be aligned are *marked unverified*, never silently kept.
Scanned pages (no trustworthy text): this mechanism abstains; the existing consensus double-read + identity suite + cell flags is the path (already built). Genesis-style OCR layers still work: the value sequence in the text layer is ordered, and total-anchored alignment catches the one-row shift.

### M2 — One identity suite, run at every layer, flags that cannot be dropped
Single shared suite (results, BS, CF identities incl. OCI-components, expense-components, PBT+adjustments; plus segment sums), executed:
1. post-extraction per grid (as today),
2. **post-mapping per MappedStatement** — template formulas + the cross-identities the template can't express (TA==TEL, PBT−tax chain, NP+OCI=TCI, op+inv+fin(+fx)=Δcash, beg+Δ=end),
3. **post-write on the workbook itself** (read back the file; prototype exists in scratchpad, found the Genesis/Wipro/empty-sheet cases).
Failure at any layer → targeted re-read of that statement from pixels (existing `repair_raw` machinery) → if still failing, a ⚠ that **propagates**: RawTable → MappedStatement → statement sheet header cell + Review sheet. Delete the current one-shot "re-ask with a scolding prompt". `verified/unverified` becomes a field carried through the dataflow, not a substring in a title.

### M3 — Period resolution with document + job context; fail loud, never empty
Periods resolved from (in order): column header text → page banner/statement heading (capture the heading line with each table at extraction) → **job metadata** (Q4 FY2026 is known from the upload form — a CF with bare `2026|2025` in a Q4 filing is FY-ended Mar-2026/2025) → cross-statement consistency (results columns constrain BS/CF end dates). Date regex fixed for `31March`-style strings; transposed segment tables detected (header cells are non-period words) and pivoted or excluded explicitly. If a statement's periods still can't be resolved: **write the sheet with the raw column labels and a ⚠ Review entry** — an empty sheet is never a legal outcome when the raw has the statement.

**Cost:** M1 and M3 are free/offline. M2's only paid step is the existing per-statement vision re-read, triggered only on failure (~$0.01–0.03/page, same as today's repair loop).

## 6. Validation harness (deliverable 4)
- `scripts/verify_delivered.py` (promote the scratchpad prototype): template-formula + cross-identity checks on every workbook in `output/client/`, with the empty-sheet-vs-raw cross-reference. Run before/after any change.
- Regression fixtures: the four named reproductions (infosys 15.96 + dup OCI block, techm q2 7th column, hcl 13775, genesis shift) as offline unit tests over the cached pickles/xlsx.
- Full-corpus re-extraction is the only paid validation — to be asked for separately with a cost estimate once the offline fixes land.

---

## 7. IMPLEMENTED (2026-07-22) — status

The design above is now in the tree. What exists:

| Piece | Where |
|---|---|
| M1 positional reconciliation | `src/engine/source_align.py` — coverage-ranked source-page spans, right-edge column clustering, anchor-voted/order-checked column mapping, conservative mode (form-fix/fill only) whenever the page or mapping is not fully authoritative, font-based OCR-layer detection (`untrusted_text_pages`: GlyphLessFont/HiddenHorzOCR ⇒ abstain), `repair_dropped_decimals` kept for source-absent decimals |
| One identity suite | `src/engine/identities.py` (results incl. OCI-components with subtotal/attribution-aware interpretation, balance, cash flow incl. PBT+adjustments, duplicate-column structural check) — used by filing_chat, verify_raw, repair_raw, verify_delivered |
| Extraction integration | `src/engine/filing_chat.py` — reconcile → identity check → ONE informed re-ask → ⚠ title; the 4 old patches (`_reconcile_grid`, `_repair_dropped_decimals`, `_realign_sparse_rows`, `_dup_columns`) removed/absorbed; period-banner capture |
| M3 period resolution | `src/engine/client_map.py::resolve_bare_periods` — cross-statement evidence (bare-year CF columns take the filing's longest span for that year), job-metadata hint (Q/FY from the upload form), placeholder-periods + loud flag when unresolvable (empty sheets abolished); `_DATE` accepts '31March 2026' and 'Jun 30, 2025' |
| M2 flags that can't be dropped | `MappedStatement.flags` accumulates extraction ⚠ + mapping failures; `verify_mapped` adds TA==TEL; `annotate_flags` stamps orange tabs + Review rows on the wide deliverable; `scripts/verify_delivered.py::verify_workbook/annotate_findings` re-checks the WRITTEN file (template formulas on non-computed fields, TA==TEL, empty-sheet-vs-raw) and stamps findings; all wired in `src/webapp.py::_run_tables_job` |
| Guard upgrade | computed-subtotal guard now picks the printed total ARITHMETICALLY (candidate must equal the mapped components' sum), label similarity only as tie-break |
| Harness | `scripts/regression_offline.py` (15 assertions, all passing) + `scripts/verify_delivered.py --all` |

Offline validation results:
- corpus-wide reconciliation: raw identity failures 53 → 49, **zero statements worsened**; Infosys 15.96→1,596 and the mis-copied OCI cell fixed; genuine wrong-column swaps corrected on latent_view/persistent/tcs/techm (confirmed against printed x-positions);
- structural corruption (infosys q4 dup columns, techm q2, infosys q3 CF) detected on every statement type and routed to repair/flag;
- post-write audit of the 46 legacy workbooks: 38 flagged / 8 clean — matching the manual analysis.

Not yet validated end-to-end (paid): a fresh extract→map→write run through the new pipeline. Recommended first runs: infosys_q4 (full PDF), techm_q2, genesis_q4, atishay_q4 — ask before spending.

### 7b. Second pass (same day) — paid-work economics + hardening

Principle enforced: **the file-upload Q&A already reads the pages provider-side; our own paid vision/double-read spend happens ONLY when verification fails AND the text layer cannot settle it.**

- **Targeted double-read** — the old rule ("any filing with ≥3 scan pages → full second extraction") is gone. `source_align.has_text_authority` decides per statement; only statements with NO trusted text source (scans) are cross-read, and a fully-digital filing pays for zero double-reads. Comparison logic extracted from the 80-line webapp blob into `client_map.compare_reads` (arithmetic-provable disagreements are auto-resolved, only unprovable ones flag).
- **Deterministic-first repair** — `repair_raw.repair` now runs the FREE positional pass before any vision call (persisted immediately, so a vision crash cannot lose it), and SKIPS vision when the text layer had full authority yet the statement still fails (pixels would say the same thing) — flag instead of spend.
- **Cross-quarter consistency wired into the job** — `verify_raw.cross_quarter_flags` (free): repeated period columns must match the company's other cached filings; span-ambiguous columns are never compared (no false flags); mismatches become statement flags + Review rows. Independently re-confirms the known Infosys defects.
- **Deleted**: `_inject_period_banner` + `best_page_span` (near-zero hit rate; mapping-layer resolution covers it), the webapp `_nscan` block.
- **Hardened**: span-tournament refuses value rewrites on identity-less grids from uncertain spans; 2-digit FY hints normalized; Review-sheet rows deduped across layers.
- Harness now 20 assertions (`scripts/regression_offline.py`), all green; corpus raw identity failures 38 → 34 under reconciliation with zero statements worsened (baseline dropped from 53 → 38 by removing OCI-check false positives, not by loosening: Persistent's genuine wrong-column comparative still fails).

### 7c. Third pass — the process itself: prompts, truncation, completeness, observability

- **Extraction got its own system prompt** (`filing_chat.EXTRACT_PROMPT`). The Q&A prompt it used to share contained "*Prefer consolidated figures. Use standalone only if consolidated is not available*" — actively fighting the STANDALONE question — plus chat rules that are noise for transcription. The new prompt targets the observed failure modes directly: *never invent a period column that is not printed in the table you are reading* (the fabricated Infosys Dec-column), never blend twin printings, include the denomination line + period banner (feeds unit and period parsing for free).
- **Truncation is no longer silent.** `ask_text` now exposes `resp.status` (`with_status=True`); a 6,000-token answer holding two ~50-row statements could previously lose its tail with no trace. Now: escalate to 12,000 → if a combined standalone+consolidated question still overflows, ask each scope separately (the proven `quarterly_tables` split) → anything still truncated carries "⚠ response truncated — rows may be missing".
- **Completeness tripwire** (`filing_chat.unextracted_statements`, free): a statement kind whose heading is printed on a number-heavy text page but for which NO table was extracted (a wrong "NOT PRESENT") is logged, written to the `.review` sidecar, and listed on the workbook's Review sheet. Closes the last silent-loss channel the empty-sheet check could not see (raw missing ⇒ nothing to compare).
- **Full observability** — `output/logs/<raw_name>.log` per job (`webapp._job_logger`): pages kept by trim, per-statement extraction outcomes (source pages, corrections, conservative mode, identity results, truncation), repair decisions (free-fix vs vision vs skipped-because-text-is-authoritative), double-read decisions (which statements and why / why skipped), cross-quarter flags, unit + denomination conflicts, per-statement mapping stats + every flag, post-write findings, timings. All `except Exception: pass` blocks around pipeline stages now log the traceback. The log ships to S3 with the artifacts.
- **Unit-conflict flag**: statement-level `detect_units` vs filing-level `company_unit` disagreement (singular/plural-normalized) flags the statement — a wrong denomination scales every value.
- Harness: 25 assertions, all green.
