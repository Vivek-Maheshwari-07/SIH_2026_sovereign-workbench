"""Top-bar instrument faceplate: NET-001 external connections + firewall state (full page is B7)."""
from __future__ import annotations

from typing import Optional

import streamlit as st

from shared.contracts import NetworkStatus
from ui import api_client
from ui.components.theme import esc


def faceplate_html(status: Optional[NetworkStatus], error: Optional[str] = None) -> str:
    if status is None:
        return ('<div class="wb-plate"><div><div class="tag">NET-001</div><div class="val c-dim">--</div>'
                f'<div class="lbl" title="{esc(error or "")}">No network data</div></div></div>')
    count = status.external_seen_since_start
    alarm = count > 0 or status.external_count > 0
    css = "c-alarm" if alarm else "c-ok"
    now = f" (open now: {status.external_count})" if status.external_count else ""
    fw = status.firewall_outbound_blocked
    fw_text, fw_css = {True: ("blocked", "c-ok"), False: ("open", "c-warn"), None: ("unknown", "c-dim")}[fw]
    return (
        '<div class="wb-plate">'
        f'<div title="Unique external connections made by the workbench since the backend started{now}">'
        f'<div class="tag">NET-001</div><div class="val wb-num {css}">{count}</div>'
        f'<div class="lbl">External connections{esc(now)}</div></div>'
        f'<div><div class="tag">FW-001</div><div class="val {fw_css}">{fw_text.capitalize()}</div>'
        '<div class="lbl">Outbound firewall</div></div>'
        '</div>'
    )


@st.fragment(run_every=5)
def render() -> None:
    result = api_client.get_client().network_status()
    st.markdown(faceplate_html(result.data, result.error.message if result.error else None), unsafe_allow_html=True)
