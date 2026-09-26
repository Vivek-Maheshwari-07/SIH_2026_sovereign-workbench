"""
Tests for backend.router. The cosine/registry/rule-layer/ollama-down tests
never touch Ollama. The accuracy test is skipped automatically if Ollama is
not reachable.
"""
from __future__ import annotations

from io import BytesIO

import httpx
import pytest
from PIL import Image

import backend.router as router_module
from backend.file_store import file_store
from backend.llm_client import LLMError, embed
from backend.registry import registry
from backend.router import cosine_similarity, route
from backend.settings import settings
from shared.contracts import RouteRequest, TaskType


def _save_test_image(filename: str = "photo.png") -> str:
    """Saves a tiny in-memory PNG through file_store and returns its file_id."""
    buf = BytesIO()
    Image.new("RGB", (40, 40), color="white").save(buf, format="PNG")
    return file_store.save(filename, buf.getvalue(), "image/png").file_id


def _ollama_available() -> bool:
    try:
        resp = httpx.get(settings.OLLAMA_HOST, timeout=3.0)
        return resp.status_code < 500
    except Exception:
        return False


OLLAMA_UP = _ollama_available()
_ollama_skip = pytest.mark.skipif(
    not OLLAMA_UP, reason="Ollama is not reachable at settings.OLLAMA_HOST"
)


def skip_if_no_ollama(fn):
    """Live Ollama test: marked slow (skipped by default) and skipped when Ollama is down."""
    return pytest.mark.slow(_ollama_skip(fn))

# (message, expected task type, needs an image attached)
# None of these may be an exact (or punctuation-only) copy of a yaml example
# sentence — see test_accuracy_prompts_are_not_copies_of_yaml_examples below.
ACCURACY_CASES: list[tuple[str, TaskType, bool]] = [
    # coding: some with obvious keywords (rule layer), most without (similarity layer)
    ("Write a script that parses this log file and counts errors.", TaskType.CODING, False),
    ("There's a bug in my Python function, please fix it.", TaskType.CODING, False),
    ("Add tests to check this calculation is correct.", TaskType.CODING, False),
    ("Compute the flange bolt load for these values.", TaskType.CODING, False),
    ("My program keeps crashing when I run it, please help.", TaskType.CODING, False),
    ("Calculate pipe wall thickness for 10 bar pressure and 200 mm diameter.", TaskType.CODING, False),
    ("Work out the allowable stress needed for this pipe.", TaskType.CODING, False),
    ("Compute the corrosion allowance for a 20 year design life.", TaskType.CODING, False),
    # document: most with obvious keywords, one without (similarity layer)
    ("Please summarize this inspection report for me.", TaskType.DOCUMENT, False),
    ("Could you draft an approval note covering the pipe repair work?", TaskType.DOCUMENT, False),
    ("Does our SOP cover requirements for hot work permits?", TaskType.DOCUMENT, False),
    ("Can you write a letter to the client about the delay?", TaskType.DOCUMENT, False),
    ("Pull together the key takeaways from this inspection document.", TaskType.DOCUMENT, False),
    # vision: always an image attached, so layer 1 should fire every time
    ("What kind of equipment can you identify in this drawing?", TaskType.VISION, True),
    ("Can you read the handwritten notes visible in this photo?", TaskType.VISION, True),
    ("List out every tag you can find in this P&ID.", TaskType.VISION, True),
    ("Can you identify the tag number in this picture?", TaskType.VISION, True),
    ("What does this image show?", TaskType.VISION, True),
    # general: no keywords, no attachment
    ("What is the purpose of a pressure relief valve?", TaskType.GENERAL, False),
    ("I need a checklist to prepare for a plant shutdown meeting.", TaskType.GENERAL, False),
    ("What is the boiling point of water at sea level?", TaskType.GENERAL, False),
    ("Tell me an interesting fact about steel.", TaskType.GENERAL, False),
    ("How many days are there in a leap year?", TaskType.GENERAL, False),
]

ACCURACY_PASS_BAR = 21


def _normalize(text: str) -> str:
    return text.strip().rstrip(".?!").lower()


def _best_similarity(message: str) -> tuple[float, str, str]:
    """
    Independent diagnostic helper: best cosine match for `message` against
    every yaml example, regardless of which layer route() actually used.
    """
    example_vectors = router_module._load_example_vectors()
    query_vector = embed([message])[0]
    best_score, best_type, best_text = -1.0, "", ""
    for task_type, examples in example_vectors.items():
        for text, vector in examples:
            score = cosine_similarity(query_vector, vector)
            if score > best_score:
                best_score, best_type, best_text = score, task_type, text
    return best_score, best_type, best_text


@pytest.fixture(autouse=True)
def _temp_workspace(tmp_path, monkeypatch):
    """Uploaded test images go to a temp workspace, never the real workspace/."""
    monkeypatch.setattr(settings, "WB_WORKSPACE_DIR", tmp_path / "workspace")


@pytest.fixture(autouse=True)
def _reset_example_cache():
    router_module._example_vectors_cache = None
    yield
    router_module._example_vectors_cache = None


# ---------------------------------------------------------------- cosine similarity (no Ollama)
def test_cosine_similarity_identical_vectors():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_opposite_vectors():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_is_scale_invariant():
    assert cosine_similarity([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)


def test_cosine_similarity_zero_vector_returns_zero_not_error():
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_similarity_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


# ---------------------------------------------------------------- registry (no Ollama)
def test_registry_loads_yaml_and_finds_three_models():
    registry.reload()
    ids = {m.id for m in registry.all_models()}
    assert ids == {"general", "coder", "embed"}


def test_registry_model_for_task_and_embedding_model():
    registry.reload()
    assert registry.model_for_task(TaskType.CODING).id == "coder"
    assert registry.model_for_task(TaskType.VISION).id == "general"
    assert registry.embedding_model().id == "embed"


def test_accuracy_prompts_are_not_copies_of_yaml_examples():
    registry.reload()
    yaml_examples = {
        _normalize(example) for examples in registry.examples().values() for example in examples
    }
    for message, _, _ in ACCURACY_CASES:
        assert _normalize(message) not in yaml_examples, f"prompt is a copy of a yaml example: {message!r}"


# ---------------------------------------------------------------- rule layer (no Ollama)
def test_rule_layer_image_attachment_routes_to_vision():
    file_id = _save_test_image()

    decision = route(RouteRequest(message="What is this?", file_ids=[file_id]))

    assert decision.task_type == TaskType.VISION
    assert decision.layer == "rule"
    assert decision.model_id == registry.model_for_task(TaskType.VISION).id


def test_rule_layer_coding_keyword_routes_to_coder():
    decision = route(RouteRequest(message="There's a bug in my python function, please fix it."))

    assert decision.task_type == TaskType.CODING
    assert decision.layer == "rule"
    assert decision.model_id == "coder"
    assert "python" in decision.reason


# ---------------------------------------------------------------- Ollama down -> default (mocked)
def test_ollama_down_falls_back_to_default_with_clear_reason(monkeypatch):
    def _raise_unavailable(texts, **kwargs):
        raise LLMError("MODEL_UNAVAILABLE")

    monkeypatch.setattr(router_module, "embed", _raise_unavailable)

    decision = route(RouteRequest(message="Explain what a pressure relief valve does"))

    assert decision.layer == "default"
    assert decision.task_type == registry.default_task_type()
    assert "Ollama unavailable" in decision.reason


# ---------------------------------------------------------------- accuracy (needs Ollama)
@skip_if_no_ollama
def test_router_accuracy_23_prompts():
    file_id = _save_test_image("pid_snippet.png")

    rows: list[dict] = []
    correct = 0
    for message, expected, needs_image in ACCURACY_CASES:
        file_ids = [file_id] if needs_image else []
        decision = route(RouteRequest(message=message, file_ids=file_ids))
        ok = decision.task_type == expected
        correct += int(ok)
        rows.append(
            {
                "message": message,
                "expected": expected.value,
                "got": decision.task_type.value,
                "layer": decision.layer,
                "reason": decision.reason,
                "ok": ok,
            }
        )

    header = f"{'':<4}{'PROMPT':<58}{'EXPECTED':<10}{'GOT':<10}{'LAYER':<11}REASON"
    lines = [header, "-" * len(header)]
    for row in rows:
        mark = "OK" if row["ok"] else "XX"
        prompt_display = row["message"] if len(row["message"]) <= 56 else row["message"][:53] + "..."
        lines.append(
            f"[{mark}]{prompt_display:<58}{row['expected']:<10}{row['got']:<10}{row['layer']:<11}{row['reason']}"
        )
    lines.append(f"\n{correct}/{len(ACCURACY_CASES)} correct")
    table = "\n".join(lines)
    print("\n" + table)

    # Diagnostic: independent similarity score for every coding/general prompt,
    # regardless of which layer route() actually used to decide.
    print("\nSimilarity scores for coding & general prompts (best match, independent of routing layer):")
    for message, expected, _ in ACCURACY_CASES:
        if expected not in (TaskType.CODING, TaskType.GENERAL):
            continue
        score, best_type, best_text = _best_similarity(message)
        print(f"  {score:.2f}  ({best_type:<8}) '{best_text}'  <-  \"{message}\"")

    default_rows = [row for row in rows if row["layer"] == "default"]
    print(f"\n{len(default_rows)} prompt(s) went to the DEFAULT layer:")
    for row in default_rows:
        print(f"  - \"{row['message']}\"  ->  {row['reason']}")

    assert correct >= ACCURACY_PASS_BAR, f"only {correct}/{len(ACCURACY_CASES)} correct:\n{table}"
