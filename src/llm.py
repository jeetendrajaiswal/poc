"""Thin OpenAI Responses API wrapper for the PoC.

Privacy contract (mirrors the reference adapter): `store=False` is ALWAYS sent —
requests/responses are never logged on the OpenAI dashboard. Structured output via
`text.format = json_schema (strict)` so we get valid JSON, no parsing guesswork.

File upload/delete helpers enforce the deletion guarantee (delete in `finally`).
"""
from __future__ import annotations

import contextlib
import io
import json
from typing import Any, Iterator, Optional

from openai import OpenAI

from src.config import config


_client: Optional[OpenAI] = None


def client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.require_api_key())
    return _client


# cumulative token usage for the current process — lets callers report real cost
USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}

# $/1M tokens (uncached input, output) by model prefix
_PRICES = [("gpt-5-mini", (0.25, 2.00)), ("gpt-5", (1.25, 10.00))]


def _record_usage(resp) -> None:
    u = getattr(resp, "usage", None)
    if u is None:
        return
    USAGE["calls"] += 1
    USAGE["input_tokens"] += getattr(u, "input_tokens", 0) or 0
    USAGE["output_tokens"] += getattr(u, "output_tokens", 0) or 0


def usage_cost(model: Optional[str] = None) -> float:
    """USD cost of USAGE at the given (or default) model's price."""
    mdl = model or config.model_default
    for prefix, (pin, pout) in _PRICES:
        if mdl.startswith(prefix):
            return (USAGE["input_tokens"] * pin + USAGE["output_tokens"] * pout) / 1e6
    return 0.0


def reset_usage() -> None:
    USAGE.update(calls=0, input_tokens=0, output_tokens=0)


def extract_json(
    *,
    instructions: str,
    user_input: str,
    schema_name: str,
    schema: dict[str, Any],
    model: Optional[str] = None,
    max_output_tokens: int = 1500,
    reasoning: str = "low",
    images_b64: Optional[list[str]] = None,
    file_ids: Optional[list[str]] = None,
    temperature: Optional[float] = None,
) -> dict[str, Any]:
    """One structured call. Returns the parsed JSON object. store=False always.

    Pass `images_b64` (base64-encoded PNGs) to send page images alongside the text
    — used by the vision fallback for scanned/image-only PDFs. Pass `file_ids`
    (uploaded PDF ids) to let the model read the file natively — used by the
    small-scanned-doc path; upload/delete via `ephemeral_file`.
    """
    mdl = model or config.model_default
    if images_b64 or file_ids:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user_input}]
        for fid in (file_ids or []):
            content.append({"type": "input_file", "file_id": fid})
        for b64 in (images_b64 or []):
            content.append({"type": "input_image",
                            "image_url": f"data:image/png;base64,{b64}"})
        api_input: Any = [{"role": "user", "content": content}]
    else:
        api_input = user_input
    # A truncated response ('incomplete': hit max_output_tokens) is DETECTABLE, and returning
    # _empty for it silently collapses the caller's whole section to absent (2026-07-04 run:
    # infosys cons share_capital landed on exactly 2000 output tokens -> 12 misses; two other
    # calls truncated the same way). So retry ONCE with double the budget before surfacing.
    budgets = [max_output_tokens, min(max_output_tokens * 2, 8000)]
    resp = None
    for budget in budgets:
        kwargs = dict(
            model=mdl,
            instructions=instructions,
            input=api_input,
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
            reasoning={"effort": reasoning},
            max_output_tokens=budget,
            store=False,  # privacy: never logged on OpenAI dashboard
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            resp = client().responses.create(**kwargs)
        except Exception as e:
            # some reasoning models reject sampling params — drop and retry
            if temperature is not None and "temperature" in str(e):
                kwargs.pop("temperature", None)
                resp = client().responses.create(**kwargs)
            else:
                raise
        _record_usage(resp)
        txt = getattr(resp, "output_text", "") or ""
        if txt.strip():
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                pass                                     # truncated mid-JSON -> maybe retry
        if getattr(resp, "status", "") != "incomplete":
            break                                        # empty for another reason: no point retrying
    return {"_empty": True, "_status": getattr(resp, "status", "unknown")}


def ask_text(
    *,
    instructions: str,
    question: str,
    file_ids: Optional[list[str]] = None,
    model: Optional[str] = None,
    max_output_tokens: int = 1500,
    reasoning: str = "low",
    temperature: Optional[float] = 0.1,
) -> str:
    """Free-text (markdown) answer about attached file(s) — the frp chat shape:
    system prompt + file attachment message(s) + the question. store=False."""
    mdl = model or config.model_default
    api_input: list[dict[str, Any]] = []
    for i, fid in enumerate(file_ids or []):
        api_input.append({"role": "user", "content": f"This is Document {i + 1}"})
        api_input.append({"role": "user",
                          "content": [{"type": "input_file", "file_id": fid}]})
    api_input.append({"role": "user", "content": question})
    kwargs: dict[str, Any] = dict(
        model=mdl,
        instructions=instructions,
        input=api_input,
        reasoning={"effort": reasoning},
        max_output_tokens=max_output_tokens,
        store=False,  # privacy: never logged on OpenAI dashboard
    )
    if temperature is not None:
        kwargs["temperature"] = temperature
    try:
        resp = client().responses.create(**kwargs)
    except Exception as e:
        if temperature is not None and "temperature" in str(e):
            kwargs.pop("temperature", None)
            resp = client().responses.create(**kwargs)
        else:
            raise
    _record_usage(resp)
    return getattr(resp, "output_text", "") or ""


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


@contextlib.contextmanager
def ephemeral_file(content: bytes, name: str) -> Iterator[str]:
    """Upload a file, yield ITS id, and delete ONLY that file in finally — even on error.

    Privacy: we only ever delete the resource WE created here (tracked by the returned id);
    other files on the account are never touched. Use:  with ephemeral_file(b, "x.pdf") as fid: ...
    """
    fid = upload_file(content, name)
    try:
        yield fid
    finally:
        delete_file(fid)


@contextlib.contextmanager
def ephemeral_vector_store(name: str, file_ids: Optional[list[str]] = None) -> Iterator[str]:
    """Create a vector store, yield ITS id, and delete ONLY that store in finally — even on error.

    Deletes solely the store created here (by its returned id); nothing else on the account.
    """
    vs = client().vector_stores.create(name=name, file_ids=file_ids or [])
    try:
        yield vs.id
    finally:
        try:
            client().vector_stores.delete(vs.id)
        except Exception:
            pass
