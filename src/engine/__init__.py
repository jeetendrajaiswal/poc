"""Robust, sector-agnostic extraction engine (architecture v3.2).

Public surface:
    from src.engine import Report                 # one report, everything
    from src.engine import validate_report, validate_statement   # statements only

The engine follows one principle proven across 39 Indian annual reports / 4
regulatory formats:  *locate proposes generously, arithmetic disposes.*
Failures are locate-recall, never extraction — once a statement page is found,
its accounting identity (BS sides balance; PBT-tax=PAT; op+inv+fin=net cash)
validates the extraction with zero ground truth. Free-form answers are validated
analogously: every answer must quote the page it came from (citation grounding).

Layers:
    statements.py  primary statements: locate -> extract -> arithmetic tie-out
    index.py       per-page text + BM25 retrieval (no embeddings, nothing leaves host)
    qa.py          ask-anything: retrieve -> extract+cite -> ground
    vision.py      fallback for scanned/image-only PDFs
    sector.py      format detection + KPI catalog (config/kpis.yaml)
    report.py      Report orchestrator (auto-routes text vs vision)
    cli.py         command-line entry point
"""
from src.engine.qa import Answer, ask  # noqa: F401
from src.engine.report import Report  # noqa: F401
from src.engine.statements import (  # noqa: F401
    ReportResult,
    StatementResult,
    validate_report,
    validate_statement,
)
