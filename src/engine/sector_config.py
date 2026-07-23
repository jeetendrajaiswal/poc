"""Sector-scoped client configuration.

Adding a sector is a configuration operation: create
``config/<id>/taxonomy.yaml``. Pipeline code must not branch on sector or
company names.
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
                and os.path.isfile(os.path.join(CONFIG_DIR, name, "taxonomy.yaml"))):
            out.append(name)
    return sorted(out)


def load_sector_config(sector_id: str | None = None) -> SectorConfig:
    sid = (sector_id or default_sector_id()).strip().lower()
    if not _SECTOR_ID.fullmatch(sid):
        raise ValueError(f"invalid sector id: {sid!r}")
    taxonomy_path = os.path.join(CONFIG_DIR, sid, "taxonomy.yaml")
    doc = _read_yaml(taxonomy_path)
    declared = str(doc.get("sector", "")).strip().lower()
    if declared != sid:
        raise ValueError(
            f"taxonomy sector {declared!r} does not match directory {sid!r}")
    return SectorConfig(
        sector_id=sid,
        name=str(doc.get("name") or sid.replace("_", " ").title()),
        taxonomy_path=taxonomy_path,
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
