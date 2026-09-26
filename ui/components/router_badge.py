"""B5: router decision as a small instrument faceplate (model, task type, layer, confidence, reason)."""
from __future__ import annotations

from typing import Optional

import streamlit as st

from shared.contracts import RouteDecision
from ui.components.theme import esc

LAYER_TEXT = {
    "rule": "rule (keyword match)",
    "similarity": "similarity (closest example)",
    "default": "default (no rule matched)",
    "forced": "forced by the work order",
}


def badge_html(decision: Optional[RouteDecision]) -> str:
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
        '</table>'
        f'<div class="reason">{esc(decision.reason)}</div></div>'
    )


def render(decision: Optional[RouteDecision]) -> None:
    st.markdown(badge_html(decision), unsafe_allow_html=True)
