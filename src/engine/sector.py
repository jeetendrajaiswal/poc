"""Format detection + the sector KPI catalog.

The four Indian regulatory statement formats determine both how the statements
are read and which KPIs make sense ("bucket similar companies"). Detection is a
cheap, deterministic fingerprint over the report's text; the KPI catalog is data
(config/kpis.yaml), so analysts extend it without touching code.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache

import yaml

FORMATS = ("bank", "nbfc", "insurer", "manufacturer")
_KPI_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "config", "kpis.yaml")


def _norm(s: str) -> str:
    # strip whitespace AND hyphens so 'Non-current Assets' -> 'noncurrentassets';
    # '&' -> 'and' so a bank's 'CAPITAL & LIABILITIES' still hits the Form-A fingerprint
    # (PNB/Bank of Baroda print the ampersand form and were misdetected as manufacturer)
    return re.sub(r"[\s\-]+", "", s.lower().replace("&", "and"))


def detect_format(bs_page_texts: list[str]) -> str:
    """Classify the report into one of the four regulatory formats from the text
    of the BALANCE SHEET candidate pages (the cleanest signal — notes elsewhere
    mention every term, and a 2-page BS may split its hallmarks). We test the
    UNION of those pages, most-specific first; manufacturer is the default.

      bank        -> 'Capital and Liabilities' header (Form A)
      insurer     -> "Policyholders'" funds (IRDAI)
      nbfc        -> Financial Assets + Debt Securities (Schedule III Div III)
      manufacturer-> default (Non-current/Current split, Schedule III Div II)
    """
    blob = " ".join(_norm(t) for t in bs_page_texts)
    if "capitalandliabilities" in blob:
        return "bank"
    if "policyholders" in blob:
        return "insurer"
    if "financialassets" in blob and "debtsecurities" in blob:
        return "nbfc"
    return "manufacturer"


@lru_cache(maxsize=1)
def _catalog() -> dict:
    with open(os.path.normpath(_KPI_PATH)) as f:
        return yaml.safe_load(f)


def kpi_catalog(fmt: str) -> list[dict]:
    """Return the list of KPI specs ({key, q, also}) for a format."""
    return _catalog().get(fmt, {}).get("kpis", [])


def format_label(fmt: str) -> str:
    return _catalog().get(fmt, {}).get("label", fmt)
