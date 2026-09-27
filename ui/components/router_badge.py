"""
B5: router decision as a small instrument faceplate (model, task type, layer, confidence, reason),
plus "Models used": every model that actually ran in this job, from its llm_call events. The
router picks the specialist; the general model plans and calls the tools, so a coding job shows both.
"""
from __future__ import annotations

from typing import Optional

import streamlit as st

from shared.contracts import AgentEvent, EventType, RouteDecision
from ui.components.theme import esc

LAYER_TEXT = {
    "rule": "rule (keyword match)",
    "similarity": "similarity (closest example)",
    "default": "default (no rule matched)",
    "forced": "forced by the work order",
}


def models_used(events: list[AgentEvent]) -> list[tuple[str, int]]:
    """(model id, number of calls) in order of first use, from llm_call events."""
    counts: dict[str, int] = {}
    for ev in events:
        if ev.type == EventType.LLM_CALL:
            model = str((ev.data or {}).get("model_id") or "?")
            counts[model] = counts.get(model, 0) + 1
    return list(counts.items())


def used_html(used: list[tuple[str, int]]) -> str:
    if not used:
        return ""
    calls = ", ".join(f"{esc(model)} ×{n}" for model, n in used)
    return f'<tr><td>Models used</td><td><span class="wb-num">{calls}</span></td></tr>'


def badge_html(decision: Optional[RouteDecision], used: Optional[list[tuple[str, int]]] = None) -> str:
    if decision is None:
        return ('<div class="wb-rtr"><div class="wb-h">Model router</div>'
                '<div class="wb-empty">The router picks a model when the job starts.</div></div>')
    pct = round(decision.confidence * 100)
    return (
        '<div class="wb-rtr"><div class="wb-h">Model router</div>'
        f'<div class="model">{esc(decision.ollama_name)}</div>'
        '<table>'
        f'<tr><td>Task type</td><td>{esc(decision.task_type.value)}</td></tr>'
        f'<tr><td>Decided by</td><td>{esc(LAYER_TEXT.get(decision.layer, decision.layer))}</td></tr>'
        f'<tr><td>Confidence</td><td><span class="wb-num">{pct}%</span></td></tr>'
        f'{used_html(used or [])}'
        '</table>'
        f'<div class="reason">{esc(decision.reason)}</div></div>'
    )


def render(decision: Optional[RouteDecision], events: Optional[list[AgentEvent]] = None) -> None:
    st.markdown(badge_html(decision, models_used(events or [])), unsafe_allow_html=True)
