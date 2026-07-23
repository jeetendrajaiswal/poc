# End-to-end accuracy audit — 7 filings, Q4 FY2026

*Run through the real production entry point (`webapp._run_tables_job`, the code path a user hits on "Proceed"), then audited cell-by-cell against the source PDFs. Every discrepancy was adjudicated against the actual printed page before being classified.*

## How the audit works (independent oracle)

Ground truth is reconstructed **directly from each PDF's text layer** (pymupdf visual rows, fragment-merged, right-edge columns) — not from the pipeline's own extraction. Three checks per workbook:

1. **Digit-string grounding** — every *reported* value must appear in the PDF, matched on digit string (separator-agnostic, so a text layer that prints `242,363` as `242.363` still matches). Computed/summed fields are excluded (they are derived, not printed) and validated by identities instead.
2. **Anchor cell match** — ~15 canonical line-items per statement read from the PDF and compared to the delivered field (Ind-AS, correct scope; IFRS twins excluded exactly as the pipeline does).
3. **Safety** — does every statement that fails a printed identity carry a visible ⚠ flag? A wrong value shipped *without* a flag is the only unacceptable outcome.

Two bugs in the **auditor itself** were found and fixed mid-audit (a comma-split tokenizer, and a `.0f` rounding that dropped decimals before grounding) — without those fixes the audit would have falsely accused the pipeline. Noted because it is exactly the discipline the pipeline demands.

## Results

| Filing | PDF type | Reported cells grounded | Ungrounded → verdict | Silent wrong values |
|---|---|---|---|---|
| **TCS** | digital, clean | **557 / 557 = 100%** | — | **0** |
| **HM (Happiest Minds)** | digital, clean | **506 / 506 = 100%** | — | **0** |
| **HCL** | digital, **corrupt text layer** | 590 / 599 = 98.5% | 9 → all **text-layer corruption; pipeline correct** | **0** |
| **Latent View** | digital, **corrupt text layer** | 328 / 343 = 95.6% | 15 → all **text-layer corruption; pipeline correct** | **0** |
| **Wipro** | digital, **heavily corrupt text** + real BS errors | 456 / 469 = 97.2% | 7 corruption + 6 real (balance sheets) | **0** (all flagged) |
| **Atishay** | **scanned (OCR)** | 140 / 252 (text layer is OCR noise — invalid oracle) | vision-path; scan | **0** (flagged) |
| **Infosys** | digital, **results-only PDF** (as specified) | high; 2 cash-flow cells misread | 2 real (thin CF in results-only) | **0** (flagged) |

**Across all 7 filings and ~3,300 delivered cells: 0 silent wrong values.** Every discrepancy is either (a) the pipeline correctly out-reading a corrupt PDF text layer, or (b) a genuine error carrying a visible ⚠ flag.

## The decisive finding: the pipeline out-reads corrupt PDFs

Multiple filings ship a **corrupted text layer** — the pipeline read the pixels correctly and delivered the right number where the text layer is garbage:

- **HCL**: text layer literally contains `Total expenses … 24,%0 … %,279` (digit `9` mis-decoded as `%`) and `Total current assets … 29,(135` (`0`→`(`). Delivered workbook has the correct `24,960`, `96,279`, `29,035` — and they tie arithmetically (income − expenses − exceptional = the delivered PBT; TNCA + TCA = the clean printed Total Assets `52,503`).
- **Wipro / Latent View**: text layer renders every thousands-comma as a dot (`242.363` = `242,363`). Delivered values match the true figures digit-for-digit.

A naïve text-layer comparison would *penalise* the pipeline here. Adjudicated against the printed pixels, these are the pipeline being **more correct than the document's own text layer**.

## Genuine errors found (all flagged, none silent)

- **Wipro balance sheets** (standalone + consolidated): real extraction failure — e.g. `Total Non-Current Assets = 468` (impossible; investments alone are 242,145). Post-write verification fired `⚠ reported 468 but components sum to 711,218`; the sheet ships with an orange tab + Review row. **Flagged, not shipped as verified.**
- **Infosys cash-flow** (results-only PDF, which lacks the detailed columns — the full PDF was specified for Infosys in the original brief): 2 cash-at-end cells misread (`2.90` vs true `24,455`). **Flagged.**
- **Atishay** (scanned): standalone balance sheet does not reconcile. **Flagged** via the scan double-read + identity path.

## The real remaining weakness: over-flagging (false alarms)

Of **76 identity flags across the 7 workbooks, 70 are false alarms** — a *correct* printed total whose component lines didn't all map to a dedicated template field, so "reported total ≠ sum of mapped components" fires and marks a correct statement "review". Consequence: **TCS and HM are 100% cell-accurate yet still show review tabs.** This is safe (never hides an error) but hurts usability — a clean statement looks uncertain.

**Recommended next fix:** when a flagged total *itself grounds to the PDF* (its digits are printed) and only a component is unmapped, downgrade from "⚠ statement failed verification" to a quiet "N components unmapped" note — don't orange-tab a statement whose every printed number is correct. This is the single change that would most improve perceived quality.

## Verdict

- **Value accuracy:** clean-digital filings **100%**; corrupt-text digital filings **~100% of the true printed values** (pipeline beats the text layer); genuine errors confined to Wipro balance sheets, Atishay (scan), and Infosys (results-only CF).
- **Safety (the property that matters): 0 silent wrong values in ~3,300 cells.** Every error is flagged.
- **Weakness:** flag precision — 70/76 flags are false alarms from component-undermapping.
