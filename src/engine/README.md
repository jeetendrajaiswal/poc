# Extraction engine (`src/engine`)

Robust, sector-agnostic extraction for Indian annual / quarterly reports (PDF only).
Built mini-first; nothing is uploaded to OpenAI and `store=False` on every call.

## One principle

**Locate proposes generously, arithmetic disposes.** We over-generate candidate
pages, extract with the mini model, and keep only what an accounting identity
proves correct — no ground truth, no per-company code.

| Statement | Self-validating identity |
|-----------|--------------------------|
| Balance Sheet | assets total = equity + liabilities total |
| Profit & Loss | profit before tax − tax = profit for the year |
| Cash Flow | operating + investing + financing (+ forex) = net change in cash |

Free-form answers are validated the same way: every answer must quote the page it
came from, and that quote (or the value's digits) is checked against the page text.

## Measured on 39 reports / 4 sectors

- **39/39** reports validate all three primary statements (38 via text, 1 via vision).
- **27/27** correct format detection (bank / NBFC / insurer / manufacturer).
- ~$0.014 / report for the statement layer; units (crore/lakh/million/billion/thousand)
  and scope (standalone/consolidated) auto-detected.

## Usage

```python
from src.engine import Report

r = Report("annual_report.pdf")
r.format                      # 'bank' | 'nbfc' | 'insurer' | 'manufacturer'
r.statements()                # {bs,pl,cf} StatementResult, each .validated by tie-out
r.fully_validated             # all three tie out
r.ask("What is the dividend per share?")   # citation-grounded Answer (anything in the report)
r.kpis()                      # sector-appropriate KPIs from config/kpis.yaml
```

CLI:

```bash
python -m src.engine.cli REPORT.pdf --statements
python -m src.engine.cli REPORT.pdf --ask "What are the contingent liabilities?"
python -m src.engine.cli REPORT.pdf --kpis
python -m src.engine.cli REPORT.pdf --all
```

## Layers

| File | Role |
|------|------|
| `statements.py` | primary statements: locate → extract → arithmetic tie-out |
| `index.py` | per-page text + BM25 retrieval (no embeddings — nothing leaves the host) |
| `qa.py` | ask-anything: retrieve → extract + cite → ground |
| `vision.py` | fallback for scanned / image-only PDFs (render → classify → extract → same tie-out) |
| `sector.py` + `config/kpis.yaml` | format detection + KPI catalog (KPIs are data, not code) |
| `report.py` | `Report` orchestrator (auto-routes text vs vision) |
| `cli.py` | command-line entry point |

## Extending

- **New KPI / sector metric** → add a line to `config/kpis.yaml`. No code change.
- **New question type** → just call `r.ask(...)`; retrieval + grounding are generic.
