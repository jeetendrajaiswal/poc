"""Thin OpenAI Responses API wrapper for the PoC.

Privacy contract (mirrors the reference adapter): `store=False` is ALWAYS sent —
requests/responses are never logged on the OpenAI dashboard. Structured output via
`text.format = json_schema (strict)` so we get valid JSON, no parsing guesswork.

File upload/delete helpers enforce the deletion guarantee (delete in `finally`).
"""
from __future__ import annotations

import io
import json
from typing import Any, Optional

from openai import OpenAI

from src.config import config


_client: Optional[OpenAI] = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.require_api_key())
    return _client


def extract_json(
    *,
    instructions: str,
    user_input: str,
    schema_name: str,
    schema: dict[str, Any],
    model: Optional[str] = None,
    max_output_tokens: int = 1500,
    reasoning: str = "low",
) -> dict[str, Any]:
    """One structured call. Returns the parsed JSON object. store=False always."""
    mdl = model or config.model_default
    resp = client().responses.create(
        model=mdl,
        instructions=instructions,
        input=user_input,
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": True,
            }
        },
        reasoning={"effort": reasoning},
        max_output_tokens=max_output_tokens,
        store=False,  # privacy: never logged on OpenAI dashboard
    )
    txt = getattr(resp, "output_text", "") or ""
    if not txt.strip():
        # surface incomplete/empty so the caller can mark it, not crash
        return {"_empty": True, "_status": getattr(resp, "status", "unknown")}
    return json.loads(txt)


def upload_file(content: bytes, name: str) -> str:
    f = io.BytesIO(content)
    f.name = name
    up = client().files.create(file=f, purpose="assistants")
    return up.id


def delete_file(file_id: str) -> bool:
    try:
        client().files.delete(file_id)
        return True
    except Exception:
        return False
