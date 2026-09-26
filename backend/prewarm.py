"""
Prewarm (ticket A10): load the models and our own caches before the demo, so
the first scenario does not pay model loads or cold caches.

Items run in a fixed order and each one is timed. A failed item is reported
in PrewarmResult.failed; nothing here ever raises.

Order matters when Ollama keeps at most 2 models in RAM
(OLLAMA_MAX_LOADED_MODELS=2): Ollama evicts the least recently used model, so
the model loaded LAST survives. The general model (Scenario A and C, and the
agent) is loaded last, the embed model just before it (Scenario A's KB search),
so the coder is the one evicted; Scenario B then reloads it once (~5 s).
With Ollama's default limit (3 on CPU) all three stay loaded (~6.7 GB RAM).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from backend import llm_client, router
from backend.registry import registry
from backend.tools import knowledge
from backend.tools.sandbox import sandbox_available
from shared.contracts import PrewarmResult, TaskType

logger = logging.getLogger("backend.prewarm")

KB_WARM_QUERY = "inspection repair criteria"   # any query works; it loads the Chroma index from disk
ERROR_MAX_CHARS = 200


@dataclass
class PrewarmItem:
    name: str
    ok: bool
    duration_ms: int
    detail: str = ""

    def label(self) -> str:
        return f"{self.name} ({self.duration_ms} ms)" if self.ok else f"{self.name}: {self.detail}"


def _timed(name: str, fn: Callable[[], str]) -> PrewarmItem:
    start = time.monotonic()
    try:
        ok, detail = True, fn()
    except Exception as exc:  # a failed item is reported, never raised
        ok, detail = False, f"{type(exc).__name__}: {exc}"[:ERROR_MAX_CHARS]
    return PrewarmItem(name, ok, int((time.monotonic() - start) * 1000), detail)


def _loader(model_name: str, embedding: bool = False) -> Callable[[], str]:
    def load() -> str:
        llm_client.load_model(model_name, embedding=embedding)
        return "loaded"
    return load


def _raiser(exc: Exception) -> Callable[[], str]:
    def fail() -> str:
        raise exc
    return fail


def _warm_router_examples() -> str:
    router._load_example_vectors()
    return "router example embeddings cached"


def _warm_kb() -> str:
    hits = knowledge.search(KB_WARM_QUERY, 1)
    return f"collection open, {len(hits)} hit"


def _check_sandbox() -> str:
    if not sandbox_available():
        raise RuntimeError("Docker is not reachable or the sandbox image is missing")
    return "sandbox image present"


def prewarm_steps() -> list[tuple[str, Callable[[], str]]]:
    """(item name, function) in load order; the general model is last so it stays loaded."""
    coder = registry.model_for_task(TaskType.CODING)
    general = registry.model_for_task(TaskType.DOCUMENT)
    embed = registry.embedding_model()
    return [
        (f"model {coder.id} ({coder.ollama_name})", _loader(coder.ollama_name)),
        (f"model {embed.id} ({embed.ollama_name})", _loader(embed.ollama_name, embedding=True)),
        ("router examples", _warm_router_examples),
        ("kb collection", _warm_kb),
        ("sandbox image", _check_sandbox),
        (f"model {general.id} ({general.ollama_name})", _loader(general.ollama_name)),
    ]


def _loaded() -> list[str]:
    try:
        return llm_client.loaded_models()
    except Exception:
        return []


def run_prewarm() -> tuple[PrewarmResult, list[PrewarmItem], list[str]]:
    """Returns (contract result, per-item details, models loaded in Ollama afterwards)."""
    start = time.monotonic()
    try:
        steps = prewarm_steps()
    except Exception as exc:  # broken models.yaml: report it, do not crash
        steps = [("model registry", _raiser(exc))]
    items = [_timed(name, fn) for name, fn in steps]
    loaded = _loaded()
    result = PrewarmResult(warmed=[i.label() for i in items if i.ok],
                           failed=[i.label() for i in items if not i.ok],
                           duration_ms=int((time.monotonic() - start) * 1000))
    logger.info("Prewarm done in %d ms; loaded models: %s; items: %s", result.duration_ms, loaded,
                [i.label() for i in items])
    return result, items, loaded
