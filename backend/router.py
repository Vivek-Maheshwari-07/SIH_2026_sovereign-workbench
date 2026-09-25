"""
3-layer task router: hard rules -> embedding similarity -> default.

Model names always come from backend.registry, never hard-coded here
(AGENTS.md rule 6). File attachments are looked up through backend.file_store
(ticket A3), which resolves a file_id to a real FileRef (is_image /
has_text_layer).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from backend.file_store import file_store
from backend.llm_client import LLMError, embed
from backend.registry import registry
from shared.contracts import RouteDecision, RouteRequest, TaskType

# Lazily built and cached in memory on first use:
# {task_type_str: [(example_text, vector), ...]}
_example_vectors_cache: Optional[dict[str, list[tuple[str, list[float]]]]] = None


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vectors must have the same length, got {len(a)} and {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _inspect_attachments(file_ids: list[str]) -> tuple[bool, bool]:
    """Returns (has_image, has_scanned_pdf) for the given file_ids, via file_store."""
    has_image = False
    has_scanned_pdf = False
    for file_id in file_ids:
        ref = file_store.get_ref(file_id)
        if ref is None:
            continue
        if ref.is_image:
            has_image = True
        elif ref.has_text_layer is False:
            has_scanned_pdf = True
    return has_image, has_scanned_pdf


def _decision_for(task_type: TaskType, reason: str, layer: str, confidence: float) -> RouteDecision:
    model = registry.model_for_task(task_type)
    return RouteDecision(
        task_type=task_type,
        model_id=model.id,
        ollama_name=model.ollama_name,
        reason=reason,
        layer=layer,  # type: ignore[arg-type]
        confidence=max(0.0, min(1.0, confidence)),
    )


def _rule_layer(request: RouteRequest) -> Optional[RouteDecision]:
    has_image, has_scanned_pdf = _inspect_attachments(request.file_ids)
    message_lower = request.message.lower()

    for rule in registry.rules():
        task_type = TaskType(rule["task_type"])
        rule_reason = rule.get("reason", f"rule matched for {task_type.value}")

        if rule.get("if_image_attached") and has_image:
            return _decision_for(task_type, f"Rule: {rule_reason}", "rule", 1.0)

        if rule.get("if_pdf_without_text_layer") and has_scanned_pdf:
            return _decision_for(task_type, f"Rule: {rule_reason}", "rule", 1.0)

        keywords = rule.get("if_keywords_any")
        if keywords:
            hit = next((kw for kw in keywords if kw.lower() in message_lower), None)
            if hit:
                return _decision_for(task_type, f"Rule: {rule_reason} (matched '{hit}')", "rule", 1.0)

    return None


def _load_example_vectors() -> dict[str, list[tuple[str, list[float]]]]:
    global _example_vectors_cache
    if _example_vectors_cache is not None:
        return _example_vectors_cache

    examples = registry.examples()
    flat_texts: list[str] = []
    flat_task_types: list[str] = []
    for task_type, texts in examples.items():
        for text in texts:
            flat_texts.append(text)
            flat_task_types.append(task_type)

    vectors = embed(flat_texts)

    cache: dict[str, list[tuple[str, list[float]]]] = {}
    for task_type, text, vector in zip(flat_task_types, flat_texts, vectors):
        cache.setdefault(task_type, []).append((text, vector))

    _example_vectors_cache = cache
    return cache


@dataclass
class _SimilarityOutcome:
    decision: Optional[RouteDecision]
    skipped_reason: Optional[str] = None
    best_score: Optional[float] = None


def _similarity_layer(request: RouteRequest) -> _SimilarityOutcome:
    try:
        example_vectors = _load_example_vectors()
        query_vector = embed([request.message])[0]
    except LLMError as exc:
        return _SimilarityOutcome(decision=None, skipped_reason=f"Ollama unavailable ({exc.code})")

    threshold = registry.similarity_threshold()
    best_score = -1.0
    best_task_type: Optional[str] = None
    best_text: Optional[str] = None

    for task_type, examples in example_vectors.items():
        for text, vector in examples:
            score = cosine_similarity(query_vector, vector)
            if score > best_score:
                best_score, best_task_type, best_text = score, task_type, text

    if best_task_type is None:
        return _SimilarityOutcome(decision=None)

    if best_score >= threshold:
        task_type = TaskType(best_task_type)
        reason = f"Similarity: {best_score:.2f} to {best_task_type} example '{best_text}'"
        decision = _decision_for(task_type, reason, "similarity", best_score)
        return _SimilarityOutcome(decision=decision, best_score=best_score)

    return _SimilarityOutcome(decision=None, best_score=best_score)


def route(request: RouteRequest) -> RouteDecision:
    """
    Decide which task type (and therefore which model) should handle a
    request: hard rules first, then embedding similarity, then a default.
    """
    decision = _rule_layer(request)
    if decision is not None:
        return decision

    outcome = _similarity_layer(request)
    if outcome.decision is not None:
        return outcome.decision

    threshold = registry.similarity_threshold()
    default_type = registry.default_task_type()

    if outcome.skipped_reason:
        reason = f"Default: {outcome.skipped_reason}, skipped similarity layer"
        confidence = 0.0
    elif outcome.best_score is not None:
        reason = f"Default: no rule matched, best similarity {outcome.best_score:.2f} < {threshold:.2f}"
        confidence = max(outcome.best_score, 0.0)
    else:
        reason = "Default: no rule matched"
        confidence = 0.0

    return _decision_for(default_type, reason, "default", confidence)
