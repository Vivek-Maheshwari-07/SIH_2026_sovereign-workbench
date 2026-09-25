"""
Ollama LLM client for Track A. Every model call in the backend goes through
one of the three functions here, so timeouts, retries, audit logging and
error codes stay consistent (AGENTS.md rule 6: config only via settings).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

import httpx
import ollama
import yaml
from pydantic import BaseModel, ValidationError

from backend.audit import write_audit_record
from backend.settings import settings
from shared.contracts import ERROR_CODES

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Matches `<tool_call>{"name": ..., "arguments": {...}}</tool_call>` written as plain text.
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)

ImageInput = Union[str, bytes, Path]


class LLMError(Exception):
    """Raised for any LLM-call failure. `code` is a key from shared.contracts.ERROR_CODES."""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message or ERROR_CODES[code])
        self.code = code


@dataclass
class ChatResult:
    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tokens_out: Optional[int] = None     # Ollama's eval_count; None when Ollama does not report it


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


def _client() -> ollama.Client:
    return ollama.Client(host=settings.OLLAMA_HOST, timeout=settings.WB_LLM_TIMEOUT_S)


def _options() -> dict[str, Any]:
    return {"num_ctx": settings.WB_NUM_CTX, "temperature": settings.WB_TEMPERATURE}


def _load_embed_model_name() -> str:
    models_path = _resolve(settings.WB_MODELS_FILE)
    data = yaml.safe_load(models_path.read_text(encoding="utf-8"))
    for entry in data.get("models", []):
        if entry.get("embedding"):
            return entry["ollama_name"]
    raise LLMError("MODEL_UNAVAILABLE", f"No embedding model configured in {models_path}")


def _tokens_out(response: Any) -> Optional[int]:
    """Output token count Ollama reported (eval_count), or None if it did not report one."""
    count = getattr(response, "eval_count", None)
    return count if isinstance(count, int) else None


def parse_fallback_tool_call(text: str) -> Optional[dict[str, Any]]:
    """
    Detect a tool call the model wrote as plain text instead of a real tool
    call: either `<tool_call>{"name": ..., "arguments": {...}}</tool_call>`
    or a bare `{"name": ..., "arguments": {...}}` JSON object.

    Returns {"name": str, "arguments": dict} or None if nothing was found.
    This function does not touch Ollama, so it can be unit-tested alone.
    """
    if not text:
        return None

    candidates: list[str] = []
    match = _TOOL_CALL_TAG_RE.search(text)
    if match:
        candidates.append(match.group(1))

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("name"), str)
            and isinstance(parsed.get("arguments"), dict)
        ):
            return {"name": parsed["name"], "arguments": parsed["arguments"]}

    return None


def _call_with_retry(fn: Callable[[], Any], *, purpose: str, model_id: str) -> tuple[Any, int]:
    attempts = settings.WB_LLM_RETRIES + 1
    last_exc: Optional[Exception] = None
    last_code = "MODEL_UNAVAILABLE"

    for attempt in range(attempts):
        start = time.monotonic()
        try:
            result = fn()
        except httpx.TimeoutException as exc:
            last_exc, last_code = exc, "MODEL_TIMEOUT"
        except httpx.ConnectError as exc:
            last_exc, last_code = exc, "MODEL_UNAVAILABLE"
        except ollama.ResponseError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            write_audit_record(
                kind="llm",
                name=model_id,
                target=settings.OLLAMA_HOST,
                duration_ms=duration_ms,
                ok=False,
                detail={"purpose": purpose, "error": str(exc)},
            )
            raise LLMError("MODEL_UNAVAILABLE", f"Ollama error for {model_id}: {exc}") from exc
        else:
            duration_ms = int((time.monotonic() - start) * 1000)
            return result, duration_ms

        duration_ms = int((time.monotonic() - start) * 1000)
        write_audit_record(
            kind="llm",
            name=model_id,
            target=settings.OLLAMA_HOST,
            duration_ms=duration_ms,
            ok=False,
            detail={"purpose": purpose, "error": str(last_exc), "attempt": attempt + 1},
        )

    raise LLMError(
        last_code,
        f"{model_id} unreachable at {settings.OLLAMA_HOST} after {attempts} attempt(s): {last_exc}",
    )


def chat(
    model_id: str,
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    images: Optional[list[ImageInput]] = None,
    *,
    purpose: str = "chat",
) -> ChatResult:
    """
    Send a chat turn to `model_id`. Returns the text reply and any tool
    calls (real ones from Ollama, or ones recovered by the fallback parser
    if the model wrote them as plain text instead).

    `images` are attached to the last message, which must have role "user".
    """
    ollama_messages = [dict(m) for m in messages]
    if images:
        if not ollama_messages or ollama_messages[-1].get("role") != "user":
            raise LLMError("BAD_REQUEST", "images must be attached to a trailing user message")
        ollama_messages[-1] = {**ollama_messages[-1], "images": list(images)}

    client = _client()

    def _do_call():
        return client.chat(
            model=model_id,
            messages=ollama_messages,
            tools=tools,
            think=settings.WB_THINK,
            options=_options(),
        )

    response, duration_ms = _call_with_retry(_do_call, purpose=purpose, model_id=model_id)

    message = response.message
    text = message.content or ""
    tool_calls = [
        {"name": tc.function.name, "arguments": dict(tc.function.arguments or {})}
        for tc in (message.tool_calls or [])
    ]
    if not tool_calls:
        fallback = parse_fallback_tool_call(text)
        if fallback:
            tool_calls = [fallback]

    write_audit_record(
        kind="llm",
        name=model_id,
        target=settings.OLLAMA_HOST,
        duration_ms=duration_ms,
        ok=True,
        detail={"purpose": purpose, "tokens_out": response.eval_count},
    )

    return ChatResult(text=text, tool_calls=tool_calls, tokens_out=_tokens_out(response))


def chat_json(
    model_id: str,
    messages: list[dict[str, Any]],
    schema: type[BaseModel],
    *,
    purpose: str = "chat_json",
) -> BaseModel:
    """
    Like chat(), but forces the reply into `schema`'s JSON schema (via
    Ollama's `format` parameter) and validates it with the Pydantic model.
    On a validation failure, the error is fed back to the model once for a
    single retry; a second failure raises LLMError(BAD_MODEL_OUTPUT).
    """
    conversation = [dict(m) for m in messages]
    client = _client()
    json_schema = schema.model_json_schema()

    def _do_call():
        return client.chat(
            model=model_id,
            messages=conversation,
            format=json_schema,
            think=settings.WB_THINK,
            options=_options(),
        )

    last_error: Optional[Exception] = None
    for attempt in range(2):
        response, duration_ms = _call_with_retry(_do_call, purpose=purpose, model_id=model_id)
        content = response.message.content or ""
        try:
            result = schema.model_validate_json(content)
        except ValidationError as exc:
            last_error = exc
            write_audit_record(
                kind="llm",
                name=model_id,
                target=settings.OLLAMA_HOST,
                duration_ms=duration_ms,
                ok=False,
                detail={"purpose": purpose, "error": "schema validation failed", "attempt": attempt + 1},
            )
            conversation.append({"role": "assistant", "content": content})
            conversation.append(
                {
                    "role": "user",
                    "content": (
                        "Your last reply did not match the required JSON schema. "
                        f"Validation error: {exc}. Reply again with valid JSON only, "
                        "no extra text."
                    ),
                }
            )
            continue

        write_audit_record(
            kind="llm",
            name=model_id,
            target=settings.OLLAMA_HOST,
            duration_ms=duration_ms,
            ok=True,
            detail={"purpose": purpose, "tokens_out": response.eval_count},
        )
        return result

    raise LLMError(
        "BAD_MODEL_OUTPUT",
        f"{model_id} did not produce valid {schema.__name__} JSON after retry: {last_error}",
    )


def embed(texts: list[str], *, purpose: str = "embed") -> list[list[float]]:
    """Embed `texts` with the embedding model from config/models.yaml (bge-m3)."""
    model_name = _load_embed_model_name()
    client = _client()

    def _do_call():
        return client.embed(model=model_name, input=texts)

    response, duration_ms = _call_with_retry(_do_call, purpose=purpose, model_id=model_name)

    write_audit_record(
        kind="llm",
        name=model_name,
        target=settings.OLLAMA_HOST,
        duration_ms=duration_ms,
        ok=True,
        detail={"purpose": purpose, "tokens_out": response.eval_count, "count": len(texts)},
    )

    return [list(vector) for vector in response.embeddings]
