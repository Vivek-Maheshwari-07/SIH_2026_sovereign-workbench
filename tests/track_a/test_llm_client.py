from io import BytesIO
from pathlib import Path

import httpx
import pytest
import yaml
from PIL import Image, ImageDraw

from types import SimpleNamespace

from backend import llm_client
from backend.llm_client import chat, chat_json, embed, parse_fallback_tool_call
from backend.settings import settings
from shared.contracts import ApprovalNote

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_MODELS = yaml.safe_load((_REPO_ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))


def _model_name(model_id: str) -> str:
    for entry in _MODELS["models"]:
        if entry["id"] == model_id:
            return entry["ollama_name"]
    raise KeyError(f"no model with id {model_id!r} in config/models.yaml")


def _ollama_available() -> bool:
    try:
        resp = httpx.get(settings.OLLAMA_HOST, timeout=3.0)
        return resp.status_code < 500
    except Exception:
        return False


OLLAMA_UP = _ollama_available()
skip_if_no_ollama = pytest.mark.skipif(
    not OLLAMA_UP, reason="Ollama is not reachable at settings.OLLAMA_HOST"
)


# ---------------------------------------------------------------- fallback parser (no Ollama)
def test_fallback_parser_extracts_tagged_tool_call():
    text = (
        "Sure, let me look that up.\n"
        '<tool_call>{"name": "search_kb", "arguments": {"query": "hot work permit"}}</tool_call>'
    )
    result = parse_fallback_tool_call(text)
    assert result == {"name": "search_kb", "arguments": {"query": "hot work permit"}}


def test_fallback_parser_extracts_bare_json_tool_call():
    text = '{"name": "get_weather", "arguments": {"city": "Vadodara"}}'
    result = parse_fallback_tool_call(text)
    assert result == {"name": "get_weather", "arguments": {"city": "Vadodara"}}


def test_fallback_parser_returns_none_for_plain_text():
    assert parse_fallback_tool_call("The wall thickness is 6.1 mm, nominal 8 mm.") is None


def test_fallback_parser_returns_none_for_empty_text():
    assert parse_fallback_tool_call("") is None


# ---------------------------------------------------------------- tokens_out (no Ollama)
class _FakeOllamaClient:
    def __init__(self, response):
        self.response = response

    def chat(self, **kwargs):
        return self.response


def _fake_response(eval_count):
    message = SimpleNamespace(content="ready", tool_calls=None)
    return SimpleNamespace(message=message, eval_count=eval_count)


def test_chat_returns_eval_count_as_tokens_out(monkeypatch):
    monkeypatch.setattr(llm_client, "_client", lambda: _FakeOllamaClient(_fake_response(42)))
    result = chat("any-model", [{"role": "user", "content": "hi"}])
    assert result.text == "ready"
    assert result.tokens_out == 42


def test_chat_tokens_out_is_none_when_ollama_omits_it(monkeypatch):
    monkeypatch.setattr(llm_client, "_client", lambda: _FakeOllamaClient(_fake_response(None)))
    result = chat("any-model", [{"role": "user", "content": "hi"}])
    assert result.tokens_out is None


# ---------------------------------------------------------------- live tests (need Ollama)
@skip_if_no_ollama
def test_chat_returns_non_empty_text():
    model = _model_name("general")
    result = chat(model, [{"role": "user", "content": "Reply with exactly one word: ready"}])
    assert result.text.strip() != ""
    assert isinstance(result.tokens_out, int) and result.tokens_out > 0


@skip_if_no_ollama
def test_chat_json_returns_valid_approval_note():
    model = _model_name("general")
    messages = [
        {
            "role": "user",
            "content": (
                "Draft an approval note for a corroded pipe support found during a routine "
                "inspection. Invent a plausible ref_no and today's date. Respond with JSON "
                "matching the required schema only."
            ),
        }
    ]
    note = chat_json(model, messages, ApprovalNote)
    assert isinstance(note, ApprovalNote)
    assert len(note.findings) >= 1


@skip_if_no_ollama
def test_chat_with_image_returns_non_empty_text():
    model = _model_name("general")

    img = Image.new("RGB", (240, 100), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 35), "TAG P-101", fill="black")
    buf = BytesIO()
    img.save(buf, format="PNG")
    image_bytes = buf.getvalue()

    result = chat(
        model,
        [{"role": "user", "content": "What tag or text do you see written in this image?"}],
        images=[image_bytes],
    )
    assert result.text.strip() != ""


@skip_if_no_ollama
def test_embed_returns_nonempty_vector():
    vectors = embed(["hot work permit"])
    assert len(vectors) == 1
    assert len(vectors[0]) > 0
