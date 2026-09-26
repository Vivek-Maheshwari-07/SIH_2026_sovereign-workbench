"""
B7: network page. The big NET-001 faceplate is the sovereign proof (CORE external connections
since the backend started); platform, blocked attempts, other apps and probes are shown openly but
smaller, each with one sentence. Live table of current connections, and the "Try to reach Google"
probe. Refreshes every 2 s (fragment).
"""
from __future__ import annotations

from typing import Literal, Optional

import streamlit as st

from shared.contracts import Connection, NetworkStatus, ProbeRequest, ProbeResult
from ui import api_client, messages
from ui.components.theme import esc

REFRESH_S = 2.0
PROBE_KEY = "probe_result"
Filter = Literal["Core", "Platform", "All"]
FILTERS: list[str] = ["All", "Core", "Platform"]


# ---------------------------------------------------------------- pure html builders
def _since(status: NetworkStatus) -> str:
    return status.since.astimezone().strftime("%H:%M:%S") if status.since else "backend start"


def headline_html(status: NetworkStatus) -> str:
    count = status.external_seen_since_start
    css = "c-alarm" if count > 0 else "c-ok"
    fw = status.firewall_outbound_blocked
    fw_text, fw_css, fw_note = {
        True: ("Blocked", "c-ok", "Windows Firewall blocks outbound traffic for this demo."),
        False: ("Open", "c-warn", "Outbound traffic is allowed. Run scripts/firewall_block.ps1 before the demo."),
        None: ("Unknown", "c-dim", "The firewall state could not be read."),
    }[fw]
    if count == 0:
        verdict = "No workbench process has reached the internet."
    else:
        where = "red rows below and " if status.external_count else ""
        verdict = (f"{count} connection(s) from workbench processes left this machine. "
                   f"See the {where}core leak records on the Audit screen.")
    return (
        '<div class="wb-net-head">'
        f'<div class="big"><div class="tag">NET-001</div><div class="val wb-num {css}">{count}</div>'
        f'<div class="lbl">Core external connections since {esc(_since(status))}</div></div>'
        f'<div class="side"><div class="tag">FW-001</div><div class="val2 {fw_css}">{fw_text}</div>'
        f'<div class="lbl">Outbound firewall</div><div class="note">{esc(fw_note)}</div></div>'
        '</div>'
        f'<div class="wb-net-verdict {css}">{esc(verdict)}</div>'
        '<div class="wb-net-note">Core means the backend and every process it starts, the Ollama model server '
        'and this screen. That is the software doing the work, so 0 here is the proof that it ran offline.</div>'
    )


def readings(status: NetworkStatus) -> list[tuple[str, str, int, str, str]]:
    """(tag, label, value, css, sentence) for the secondary readings."""
    platform = status.platform_seen_since_start or 0
    return [
        ("NET-002", "Platform", platform, "c-warn" if platform else "c-dim",
         "Docker Desktop / WSL / Ollama tray app phoning home (update checks). Not workbench code, "
         "so it is counted here, openly, and never in NET-001."),
        ("NET-003", "Blocked attempts", status.attempts_since_start or 0, "c-dim",
         "Connections that were tried but never opened, usually because the firewall stopped them."),
        ("NET-004", "Other apps", status.other_apps_since_start or 0, "c-dim",
         "Browsers, updaters and other programs on this laptop. Shown for honesty, not part of the workbench."),
        ("NET-005", "Probes", status.probe_since_start or 0, "c-dim",
         "Connections made on purpose by the Try to reach Google button below."),
    ]


def readings_html(status: NetworkStatus) -> str:
    cells = "".join(
        f'<div class="cell"><div class="tag">{tag}</div><div class="v wb-num {css}">{value}</div>'
        f'<div class="lbl">{esc(label)}</div><div class="note">{esc(text)}</div></div>'
        for tag, label, value, css, text in readings(status))
    return f'<div class="wb-readings">{cells}</div>'


def row_class(conn: Connection) -> str:
    if conn.component == "core":
        return "core"
    if conn.component == "platform":
        return "platform"
    return "probe" if conn.origin == "probe" else "other"


def filter_connections(conns: list[Connection], which: Optional[str]) -> list[Connection]:
    if which == "Core":
        return [c for c in conns if c.component == "core"]
    if which == "Platform":
        return [c for c in conns if c.component == "platform"]
    return list(conns)


def _process_name(conn: Connection) -> str:
    name = conn.process or "?"
    return name.rsplit(" [", 1)[0] if name.endswith("]") else name  # drop the "[ours: core]" suffix


def _who(conn: Connection) -> str:
    if conn.origin == "ours":
        return f"ours: {conn.component or '?'}"
    return {"other_app": "other app", "probe": "probe"}.get(conn.origin or "", conn.origin or "?")


def connections_html(conns: list[Connection]) -> str:
    rows = []
    for c in conns:
        seen = c.first_seen.astimezone().strftime("%H:%M:%S") if c.first_seen else ""
        rows.append(
            f'<tr class="{row_class(c)}"><td>{esc(_process_name(c))}'
            f'<span class="wb-meta"> {esc(c.pid) if c.pid is not None else ""}</span></td>'
            f'<td class="mono">{esc(c.remote)}</td><td>{esc(c.status)}</td><td>{esc(c.group or "")}</td>'
            f'<td class="who">{esc(_who(c))}</td><td class="t">{seen}</td></tr>')
    head = ('<tr><th>Process</th><th>Remote address</th><th>State</th><th>Group</th><th>Who</th>'
            '<th>First seen</th></tr>')
    return f'<table class="wb-conn">{head}{"".join(rows)}</table>'


def probe_html(result: ProbeResult) -> str:
    if result.reachable:
        return (f'<div class="wb-probe alarm"><div class="val2 c-alarm">Reachable ({result.duration_ms} ms)</div>'
                f'<div class="note">{esc(result.target)} answered. This laptop can reach the internet: '
                'run scripts/firewall_block.ps1 and switch Wi-Fi off before the demo.</div></div>')
    why = f" ({esc(result.error)})" if result.error else ""
    return (f'<div class="wb-probe ok"><div class="val2 c-ok">Blocked - not reachable ({result.duration_ms} ms)</div>'
            f'<div class="note">{esc(result.target)} could not be reached{why}.</div></div>')


# ---------------------------------------------------------------- rendering
def run_probe() -> None:
    result = api_client.get_client().network_probe(ProbeRequest())
    st.session_state[PROBE_KEY] = result


def probe_block() -> None:
    left, right = st.columns([1, 2.4], vertical_alignment="center")
    left.button("Try to reach Google", key="probe", on_click=run_probe, width="stretch")
    right.caption("Opens one TCP connection to `google.com` port 443 and closes it at once. "
                  "No data is sent. The attempt itself is counted under NET-005.")
    result = st.session_state.get(PROBE_KEY)
    if result is None:
        return
    if result.data is None:
        messages.show("The probe could not run", result.error, result.failure)
    else:
        st.markdown(probe_html(result.data), unsafe_allow_html=True)


def live_panel() -> None:
    result = api_client.get_client().network_status()
    if result.data is None:
        messages.show("Network readings are not available", result.error, result.failure, warn=True)
        return
    status = result.data
    st.markdown(headline_html(status), unsafe_allow_html=True)
    st.markdown(readings_html(status), unsafe_allow_html=True)
    if status.monitor_error:
        st.warning(f"The network monitor reported a problem: {status.monitor_error}. "
                   "Readings may be incomplete; run the backend as the same user as before.")
    probe_block()

    head, pick = st.columns([2, 1.2], vertical_alignment="bottom")
    head.markdown(f'<div class="wb-h">Current connections <span class="wb-meta">'
                  f'{len(status.connections)} open, loopback hidden</span></div>', unsafe_allow_html=True)
    which = pick.segmented_control("Show", FILTERS, default="All", key="conn_filter", label_visibility="collapsed")
    rows = filter_connections(status.connections, which)
    with st.container(height=300, border=False, key="conn_table", autoscroll=False):
        if rows:
            st.markdown(connections_html(rows), unsafe_allow_html=True)
        else:
            empty = {"Core": "No core connections open right now. That is the expected state.",
                     "Platform": "No platform connections open right now."}.get(which or "", "No external connections open.")
            st.markdown(f'<div class="wb-empty">{empty}</div>', unsafe_allow_html=True)
    st.caption(f"Updated every {REFRESH_S:g} s. Red rows are workbench (core), amber rows are platform, "
               "grey rows are other programs.")


def render() -> None:
    st.fragment(live_panel, run_every=REFRESH_S)()
