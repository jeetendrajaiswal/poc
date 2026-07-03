"""Centralized configuration — standalone port of the reference `openai_config`.

The reference (`financial_reports/openai_config.py`) read settings from Django.
This PoC reads the same knobs from environment variables (.env) instead, so the
project runs with no web framework. The privacy default is identical: store=False.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    """Read a string env var, tolerating an inline '# comment' and stray whitespace."""
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.split("#", 1)[0].strip()   # drop inline comment, trim
    return raw or default


@dataclass(frozen=True)
class Config:
    # --- Auth ---
    api_key: str = os.getenv("OPENAI_API_KEY", "")

    # --- Models ---
    # Default: cost-effective model for per-section extraction.
    model_default: str = _env_str("OPENAI_MODEL_DEFAULT", "gpt-5-mini-2025-08-07")
    # Large: reserved for the harder synthesis / self-correction passes.
    model_large: str = _env_str("OPENAI_MODEL_LARGE", "gpt-5.2-2025-12-11")

    # --- Privacy (matches reference: privacy by default) ---
    # When False, "store" is sent False on every /v1/responses call, so nothing
    # is logged on the OpenAI dashboard. Keep False unless polling is required.
    store_responses: bool = _env_bool("OPENAI_STORE_RESPONSES", False)

    # --- Limits ---
    max_tokens_default: int = int(os.getenv("OPENAI_MAX_TOKENS_DEFAULT", "4000"))
    max_correction_rounds: int = int(os.getenv("MAX_CORRECTION_ROUNDS", "2"))
    # Reasoning effort for gpt-5.4: none | low | medium | high | xhigh.
    # A/B showed none == low (80%) for value extraction — default to none (faster/cheaper).
    reasoning_effort: str = _env_str("REASONING_EFFORT", "none")
    # Self-consistency: read each value N times, take the agreeing answer. Fixes
    # unstable perception errors (misread digits). 1 = off (default): text-first reads
    # exact digits from layout text, so reads are stable — the run-to-run flips were a
    # vision-era problem. Bump via SELF_CONSISTENCY_N for extra stability on hard tables.
    self_consistency_n: int = int(os.getenv("SELF_CONSISTENCY_N", "1"))

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        return self.api_key


config = Config()
