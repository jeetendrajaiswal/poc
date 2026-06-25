# Annual Report Parameter Extraction (PoC)

Extract a fixed taxonomy of financial parameters from large Indian corporate annual
reports (Ind AS / Schedule III) with high mapping accuracy, scope correctness
(standalone vs consolidated), and strict privacy. Standalone Python, model `gpt-5.4`
via the OpenAI **Responses API**.

## Approach (validated empirically, not assumed)

```
PDF (local)
  → structure_map : auto-detect Standalone/Consolidated blocks from BS-statement
                    anchors (content-based, NO hardcoded pages; any company/year)
  → locate        : per-item candidate pages by alias + numeric-density, region-scoped
                    (numeric density ranks the actual table above policy/prose pages)
  → read_vision   : render located page(s) → gpt-5.4 VISION reads the table like a
                    human (text/pdfplumber both mangle dense financial tables);
                    dense column-sensitive items rendered at 400 DPI
  → verify/score  : evidence + scope-confirm + math; methodology-aware scoring that
                    adjudicates pipeline-vs-ground-truth disagreements
```

**Definition-first**: each data point carries a precise semantic definition + aliases
+ disambiguation (`taxonomy/definitions.yaml`). Mapping is by *meaning*, so
"Securities premium" → `Share Premium`, "Trade payables" → `Sundry Creditors`. The
exercise proved the distinctions that matter most: *issued vs subscribed vs paid-up vs
outstanding* shares, *gross vs accumulated-depreciation* columns, *reported line vs
computed total*.

## Privacy & cleanup (requirement satisfied by construction)

The PDF is read **locally**; only rendered page images are sent **inline** with
`store=False`. The pipeline **never uploads files or creates vector stores** on OpenAI,
so there is nothing persisted to delete — the strongest reading of "no inputs retained
on the AI platform". (`store=False` is set last on every call so it can't be overridden.)

## Results (scored vs independent careful-read ground truth)

| Company | Sector | Accuracy | Notes |
|---|---|---|---|
| ITC | FMCG | 100% (25/25) | ground truth was wrong on 6; pipeline correct |
| Infosys | IT | ~100% | 2 items correctly N/A (no Power&Fuel/Stores); GT under-found 1 |
| Hindalco | Metals | 96% (24/25) | 1 dense-table cell (since addressed via 400 DPI) |

Headline finding: **ground truth was wrong more often than the pipeline** (8 GT errors
vs 1 pipeline error across 3 sectors). Scoring therefore *adjudicates* disagreements
rather than trusting the reference. Exceeds the ≥90% target.

## Usage

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # set OPENAI_API_KEY (model gpt-5.4)

# full ~59-item taxonomy for one company (PDF at ~/Downloads/<company>.pdf)
.venv/bin/python -m src.run hindalco

# 12-item thin slice, scored against a ground-truth CSV
.venv/bin/python -m src.run hindalco --thin --score data/ground_truth.csv

# build an INDEPENDENT careful-read reference (different method → meaningful checks)
.venv/bin/python -m src.build_gt itc
```

## Layout

```
taxonomy/definitions.yaml         full ~59-item taxonomy (semantic defs + aliases + hints)
taxonomy/definitions_thin.yaml    12-item thin slice (fast iteration)
taxonomy/company_profiles.yaml    stable facts only: sector + na_items (NO page numbers)
src/config.py                     env config (store=False, model, reasoning=none)
src/structure_map.py              SA/CO auto-detection + per-page region map
src/locate.py                     region-scoped alias + numeric-density locator
src/read_vision.py                gpt-5.4 vision reader (dense=400 DPI), reports observed_scope
src/llm.py                        Responses API wrapper (store=False, structured JSON)
src/phase0.py                     extraction orchestrator
src/scoring.py                    methodology-aware scoring (match/error/divergence/N-A)
src/build_gt.py                   independent careful-read ground-truth builder
src/run.py                        CLI entry point
```

## Settings that were measured, not assumed
- **`reasoning=none`** — A/B showed none == low == accuracy for value extraction; faster/cheaper.
- **Vision over text/pdfplumber** — both linearize/split dense financial tables; vision reads cells.
- **DPI 300 default, 400 for dense tables** — 300 stably misread one PP&E cell (1,467→1,487).
- **Self-consistency** — only helps *random* misreads; the stubborn cases were *systematic*
  (fixed by DPI) or ground-truth errors. Kept tier-able, off by default for cost.

## Cost (gpt-5.4: $2.50/1M in, $15/1M out)
~$0.5/company (thin slice) · ~$2.5/company (full taxonomy), N=1. All cost is page-image
input tokens. Lever (not yet applied): batch items sharing a page to hit the 10× cached-input rate.

## Limitations / future work
- Dense-table cells remain the hardest read; a deterministic pdfplumber cross-check could
  cross-validate the vision number where the table is cleanly parseable.
- Cross-year consistency check (prior-year reports in `~/Downloads/prev/`) is designed
  (see PLAN.md) but not yet wired.
- Profiles cover the 6 test companies; unknown companies fall back to full auto-detection.
```
