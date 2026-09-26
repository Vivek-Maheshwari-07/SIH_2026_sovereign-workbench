"""
Top header band: title, one line of what the workbench does and three status chips
(air-gap seal, NET-001, outbound firewall). Replaces the old NET-001 / FW-001 faceplate.
The chips refresh every 5 s (fragment); the full network page is B7.
"""
from __future__ import annotations

from typing import Optional

import streamlit as st

from shared.contracts import NetworkStatus
from ui import api_client
from ui.components.theme import esc

REFRESH_S = 5
TITLE = "Sovereign AI Workbench"
TAGLINE = "Approval notes, calculation code and P&ID tag lists, drafted by local AI models."
NET_TIP = ("Core external connections since the backend started: the backend, its child processes, "
           "the Ollama model server and this UI. Details on the Network screen.")


def seal_state(status: Optional[NetworkStatus]) -> tuple[bool, str]:
    """(sealed, reason). Sealed only when NET-001 is 0 and the firewall blocks outbound traffic."""
    if status is None:
        return False, "no network data"
    count = status.external_seen_since_start
    if count > 0:
        return False, f"NET-001 is {count}"
    if status.external_count > 0:
        return False, f"{status.external_count} open now"
    fw = status.firewall_outbound_blocked
    if fw is None:
        return False, "firewall state unknown"
    if not fw:
        return False, "firewall open"
    return True, ""


def chips_html(status: Optional[NetworkStatus], error: Optional[str] = None) -> str:
    sealed, reason = seal_state(status)
    seal = ('<span class="wb-chip seal ok">Air-gap sealed</span>' if sealed
            else f'<span class="wb-chip seal warn">Not sealed: {esc(reason)}</span>')
    if status is None:
        return (f'<div class="wb-chips" title="{esc(error or "")}">{seal}'
                '<span class="wb-chip">NET-001 <b class="wb-num">--</b></span></div>')
    count = status.external_seen_since_start
    alarm = count > 0 or status.external_count > 0
    now = f", {status.external_count} open now" if status.external_count else ""
    fw = {True: "blocked", False: "open", None: "unknown"}[status.firewall_outbound_blocked]
    return (f'<div class="wb-chips">{seal}'
            f'<span class="wb-chip{" alarm" if alarm else ""}" title="{esc(NET_TIP)}">'
            f'NET-001 <b class="wb-num">{count}</b>{esc(now)}</span>'
            f'<span class="wb-chip">Firewall {fw}</span></div>')


def band_html(chips: str = "") -> str:
    return (f'<div class="wb-band"><div><h1 class="wb-title">{TITLE}</h1><p class="wb-sub">{esc(TAGLINE)}</p></div>'
            f'{chips}</div>')


@st.fragment(run_every=REFRESH_S)
def render() -> None:
    result = api_client.get_client().network_status()
    chips = chips_html(result.data, result.error.message if result.error else None)
    st.markdown(band_html(chips), unsafe_allow_html=True)
