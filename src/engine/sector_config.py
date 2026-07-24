"""Sector-scoped client configuration.

Adding a sector is a configuration operation: create
``config/<id>/taxonomy.yaml`` and its referenced field/structure/identity
files, plus ``config/<id>/extraction.yaml``. Pipeline code must not branch on
sector or company names.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import yaml


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG_DIR = os.path.join(ROOT, "config")
DEFAULTS_PATH = os.path.join(CONFIG_DIR, "default.yaml")
_SECTOR_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class SectorConfig:
    sector_id: str
    name: str
    taxonomy_path: str
    extraction_path: str
    extraction_policy: dict


def _read_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        value = yaml.safe_load(fh) or {}
    if not isinstance(value, dict):
        raise ValueError(f"configuration must be a YAML object: {path}")
    return value


def default_sector_id() -> str:
    """Return the configured default sector, optionally overridden at deploy."""
    configured = str(_read_yaml(DEFAULTS_PATH).get("default_sector", "")).strip()
    sector_id = os.getenv("REPORT_SECTOR", configured).strip().lower()
    if not _SECTOR_ID.fullmatch(sector_id):
        raise ValueError(f"invalid sector id: {sector_id!r}")
    return sector_id


def available_sector_ids() -> list[str]:
    out = []
    for name in os.listdir(CONFIG_DIR):
        if (_SECTOR_ID.fullmatch(name)
                and os.path.isfile(
                    os.path.join(CONFIG_DIR, name, "taxonomy.yaml"))
                and os.path.isfile(
                    os.path.join(CONFIG_DIR, name, "extraction.yaml"))):
            out.append(name)
    return sorted(out)


def load_sector_config(sector_id: str | None = None) -> SectorConfig:
    sid = (sector_id or default_sector_id()).strip().lower()
    if not _SECTOR_ID.fullmatch(sid):
        raise ValueError(f"invalid sector id: {sid!r}")
    taxonomy_path = os.path.join(CONFIG_DIR, sid, "taxonomy.yaml")
    extraction_path = os.path.join(CONFIG_DIR, sid, "extraction.yaml")
    doc = _read_yaml(taxonomy_path)
    declared = str(doc.get("sector", "")).strip().lower()
    if declared != sid:
        raise ValueError(
            f"taxonomy sector {declared!r} does not match directory {sid!r}")
    extraction = _read_yaml(extraction_path)
    rules = extraction.get("statement_exclusions")
    if not isinstance(rules, list) or not rules:
        raise ValueError(
            "extraction.statement_exclusions must be a non-empty list")
    compiled_rules = []
    seen_ids = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("statement exclusion must be an object")
        rule_id = str(rule.get("id", "")).strip()
        description = str(rule.get("description", "")).strip()
        patterns = rule.get("header_patterns")
        if not rule_id or rule_id in seen_ids or not description:
            raise ValueError(
                "statement exclusions require unique IDs and descriptions")
        if (not isinstance(patterns, list) or not patterns
                or any(not str(pattern).strip() for pattern in patterns)):
            raise ValueError(
                f"statement exclusion {rule_id!r} requires header_patterns")
        clean_patterns = [str(pattern).strip() for pattern in patterns]
        try:
            for pattern in clean_patterns:
                re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(
                f"invalid extraction pattern in {rule_id!r}") from exc
        seen_ids.add(rule_id)
        compiled_rules.append({
            "id": rule_id,
            "description": description,
            "header_patterns": clean_patterns,
        })
    return SectorConfig(
        sector_id=sid,
        name=str(doc.get("name") or sid.replace("_", " ").title()),
        taxonomy_path=taxonomy_path,
        extraction_path=extraction_path,
        extraction_policy={"statement_exclusions": compiled_rules},
    )


def load_sector_assets(sector_id: str | None = None):
    """Load and compile a sector's structural and semantic configuration."""
    from src.engine.client_map import (
        load_taxonomy,
        template_from_taxonomy,
    )

    cfg = load_sector_config(sector_id)
    taxonomy = load_taxonomy(cfg.taxonomy_path)
    template = template_from_taxonomy(taxonomy)
    return cfg, template, taxonomy
