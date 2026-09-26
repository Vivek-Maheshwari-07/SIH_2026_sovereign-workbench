"""
"Blueprint" look for the Workbench UI: a blue header band, white panels on a pale canvas, and
colour only for meaning (one meaning per colour). Everything is inline CSS; fonts are Windows
system fonts (Bahnschrift, Segoe UI, Consolas) with the Streamlit bundled fonts as fallback.
Nothing loads from the internet. .streamlit/config.toml repeats a few tokens for Streamlit's own
widgets; tests/track_b/test_theme.py keeps the two in step and checks the contrast pairs.
"""
from __future__ import annotations

import html

import streamlit as st

# ---------------------------------------------------------------- tokens (the only place)
HEADER = "#0B4F9C"        # top header band
HEADER_CHIP = "#1E66B8"   # chips inside the band
HEADER_SUB = "#D6E6FA"    # tagline on the band
ACTIVE = "#1673E6"        # the only interaction accent: active step, selected work order, focus, links
ACTIVE_TEXT = "#1467D0"   # same blue one step darker, for small text on the canvas (AA)
CANVAS = "#F4F8FC"        # page background
PANEL = "#FFFFFF"         # panels and cards
BORDER = "#DCE5EF"        # panel border
INK = "#1B2A3A"           # body text
INK_2 = "#4B5D70"         # secondary text
MUTED = "#5E6F82"         # muted text (#6B7C8F darkened: 4.0:1 on the canvas is below AA)
PIPE = "#9AB0C8"          # bubble outlines, dividers
TRACK = "#C3D2E3"         # pipe still to run
DIVIDER = "#E6EDF5"       # light row dividers
DONE = "#12A36B"          # finished bubbles and pipe, status lamps
DONE_TEXT = "#0E7A4E"     # green text on a light background
DELIVER = "#F57C12"       # only deliverables: the Download button
DELIVER_INK = "#2A1300"   # text on the Download button
DELIVER_TAG = "#B84E05"   # file-type tag text
DELIVER_TAG_BG = "#FFF1E4"
WARN_TEXT = "#8A5A00"     # only warnings
WARN_BG = "#FFF4DB"
ALARM = "#D93645"         # only failures and core leaks (fills, edges, white text on it)
ALARM_TEXT = "#C0283A"    # red text on white and on tinted rows (AA)
ALARM_BG = "#FDECEE"

DIN = "'Bahnschrift', 'DIN Alternate', 'Segoe UI', 'Source Sans', sans-serif"
BODY = "'Segoe UI', 'Source Sans', system-ui, sans-serif"
GUTTER = "1.75rem"

CSS = f"""
<style>
:root {{
  --header:{HEADER}; --chip:{HEADER_CHIP}; --header-sub:{HEADER_SUB}; --active:{ACTIVE}; --active-text:{ACTIVE_TEXT};
  --canvas:{CANVAS}; --panel:{PANEL}; --border:{BORDER}; --ink:{INK}; --ink-2:{INK_2}; --muted:{MUTED};
  --pipe:{PIPE}; --track:{TRACK}; --divider:{DIVIDER}; --done:{DONE}; --done-text:{DONE_TEXT};
  --deliver:{DELIVER}; --deliver-ink:{DELIVER_INK}; --tag:{DELIVER_TAG}; --tag-bg:{DELIVER_TAG_BG};
  --warn-text:{WARN_TEXT}; --warn-bg:{WARN_BG}; --alarm:{ALARM}; --alarm-text:{ALARM_TEXT}; --alarm-bg:{ALARM_BG};
  --din:{DIN}; --body:{BODY}; --shadow: 0 1px 2px rgba(11, 79, 156, 0.07);
}}
.block-container {{ padding: 0 {GUTTER} 0.6rem {GUTTER}; max-width: 100%; }}
[data-testid="stSidebarHeader"] {{ height: 2rem; min-height: 2rem; padding-top: 0.4rem; padding-bottom: 0; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 0; }}
[data-testid="stHeader"] {{ background: transparent; height: 0; }}
/* style-only markdown blocks take no room (the rules still apply), so the band starts at the very top */
[data-testid="stElementContainer"]:has(> .stMarkdown style:only-child) {{ display: none; }}
:focus-visible {{ outline: 2px solid var(--active) !important; outline-offset: 2px; }}
a {{ color: var(--active-text); }}

/* type scale: 13 meta, 15 body, 17 panel title, 22 value, 26 title */
.wb-h {{ font-family: var(--din); font-size: 17px; font-weight: 600; color: var(--ink); margin: 0 0 6px 0; }}
.wb-meta {{ font-size: 13px; color: var(--muted); }}
.wb-num {{ font-family: var(--din); font-variant-numeric: tabular-nums; }}
.wb-empty {{ font-size: 15px; color: var(--ink-2); padding: 6px 0; }}

/* header band */
.wb-band {{ background: var(--header); margin: 0 -{GUTTER} 10px -{GUTTER}; padding: 14px {GUTTER};
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 24px; }}
h1.wb-title, .wb-title {{ font-family: var(--din) !important; font-size: 26px !important; font-weight: 600 !important;
  color: #FFFFFF !important; margin: 0 !important; padding: 0 !important; line-height: 1.15 !important; }}
.wb-sub {{ font-size: 15px; color: var(--header-sub); margin: 3px 0 0 0 !important; }}
.wb-chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.wb-chip {{ font-family: var(--din); font-size: 15px; font-weight: 600; color: #FFFFFF; background: var(--chip);
  border-radius: 999px; padding: 5px 14px; white-space: nowrap; }}
.wb-chip b {{ font-weight: 700; }}
.wb-chip.seal {{ background: #FFFFFF; }} .wb-chip.seal.ok {{ color: var(--done-text); }}
.wb-chip.seal.warn {{ color: var(--warn-text); }}
.wb-chip.alarm {{ background: var(--alarm); }}

/* status lamps */
.wb-lamp {{ display: flex; align-items: baseline; gap: 8px; font-size: 15px; margin: 4px 0; color: var(--ink); }}
.wb-lamp i {{ width: 10px; height: 10px; border-radius: 50%; flex: none; display: inline-block; position: relative; top: 1px; }}
.wb-lamp .why {{ color: var(--muted); font-size: 13px; }}
.wb-lamp .fix {{ display: block; color: var(--alarm-text); font-size: 13px; font-weight: 600; }}
.c-ok {{ color: var(--done-text); }} .c-alarm {{ color: var(--alarm-text); }} .c-warn {{ color: var(--warn-text); }}
.c-active {{ color: var(--active-text); }} .c-dim {{ color: var(--muted); }}
.b-ok {{ background: var(--done); }} .b-alarm {{ background: var(--alarm); }} .b-off {{ background: var(--pipe); }}

/* work orders: one white panel per order; the selected one gets a blue left edge */
[class*="st-key-wocard_"] {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 2px 12px 10px 12px; gap: 0; box-shadow: var(--shadow); }}
[class*="st-key-wocard_"] [data-testid="stMarkdownContainer"] {{ margin-bottom: 0; }}
[class*="st-key-wo_"] button {{ justify-content: flex-start; text-align: left; border: none; background: none;
  padding: 6px 0 2px 0; min-height: 0; }}
[class*="st-key-wo_"] button:hover {{ color: var(--active-text); background: none; }}
[class*="st-key-wo_"] button > div {{ justify-content: flex-start; width: 100%; }}
[class*="st-key-wo_"] button p {{ font-size: 15px; font-weight: 600; text-align: left; }}
[class*="st-key-wo_"] button strong {{ font-family: var(--din); font-weight: 600; margin-right: 4px; }}
.wb-wo {{ font-size: 13px; color: var(--ink-2); line-height: 1.4; }}
.wb-wo .io {{ font-family: var(--din); color: var(--ink); }}
.wb-tag {{ font-family: var(--din); font-size: 13px; font-weight: 600; color: #FFFFFF; background: var(--active);
  border-radius: 999px; padding: 1px 9px; display: inline-block; margin: 4px 0 2px 0; }}

/* job header */
.wb-job {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; }}
.wb-job .id {{ font-family: var(--din); font-size: 17px; font-weight: 600; color: var(--ink); }}
.wb-job .msg {{ flex-basis: 100%; font-size: 15px; color: var(--ink-2); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }}
.wb-state {{ font-family: var(--din); font-size: 14px; font-weight: 600; padding: 1px 10px; border: 1.5px solid currentColor;
  border-radius: 999px; background: var(--panel); }}
.wb-clock {{ font-family: var(--din); font-size: 22px; font-variant-numeric: tabular-nums; color: var(--ink); }}

/* process line: green = finished, blue = running now, grey outline = still to run */
.wb-pl {{ display: flex; flex-wrap: wrap; align-items: flex-start; row-gap: 12px; padding: 10px 0 4px 0; }}
.wb-st {{ display: flex; align-items: flex-start; }}
.wb-pipe {{ width: 20px; height: 4px; background: var(--done); margin-top: 21px; border-radius: 2px; }}
.wb-pipe.wait {{ background: var(--track); }}
.wb-st:first-child .wb-pipe {{ display: none; }}
.wb-bub {{ width: 84px; display: flex; flex-direction: column; align-items: center; }}
.wb-bub svg {{ display: block; overflow: visible; }}
.wb-bub text {{ font-family: var(--din); font-size: 18px; font-weight: 700; }}
.wb-bub .cap {{ font-size: 13px; color: var(--ink-2); text-align: center; line-height: 1.25; margin-top: 4px;
  max-width: 82px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
.wb-bub.done .cap {{ color: var(--ink); }}
.wb-bub.active .cap {{ color: var(--active-text); font-weight: 600; }}
.wb-bub.failed .cap {{ color: var(--alarm-text); font-weight: 600; }}
.wb-bub .ring {{ fill: none; stroke: var(--active); stroke-width: 3; opacity: 0; transform-origin: 23px 23px; }}
.wb-bub.active .ring {{ animation: wb-pulse 1.8s ease-out infinite; }}
@keyframes wb-pulse {{ 0% {{ opacity: .5; transform: scale(1); }} 100% {{ opacity: 0; transform: scale(1.4); }} }}
@media (max-width: 1500px) {{ .wb-bub {{ width: 76px; }} .wb-bub .cap {{ max-width: 74px; }} .wb-pipe {{ width: 14px; }} }}
@media (prefers-reduced-motion: reduce) {{ .wb-bub.active .ring {{ animation: none; opacity: .35; transform: scale(1.2); }} }}

/* router + plan panels */
.wb-rtr, .wb-planbox {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px;
  box-shadow: var(--shadow); }}
.wb-rtr .model {{ font-family: var(--din); font-size: 22px; font-weight: 600; color: var(--ink); line-height: 1.2; }}
.wb-rtr table {{ border-collapse: collapse; margin: 4px 0 6px 0; font-size: 14px; }}
.wb-rtr table, .wb-rtr td, .wb-rtr tr {{ border: none !important; background: none !important; }}
.wb-rtr td {{ padding: 1px 12px 1px 0; }} .wb-rtr td:first-child {{ color: var(--muted); }}
.wb-rtr .reason {{ font-size: 14px; color: var(--ink-2); border-left: 3px solid var(--border); padding-left: 8px; }}
.wb-plan {{ list-style: none; margin: 0; padding: 0; font-size: 15px; }}
.wb-plan li {{ display: flex; gap: 8px; padding: 3px 0; color: var(--ink-2); }}
.wb-plan li .mk {{ font-family: var(--din); width: 16px; flex: none; text-align: center; }}
.wb-plan li.done {{ color: var(--ink); }} .wb-plan li.done .mk {{ color: var(--done-text); }}
.wb-plan li.current {{ color: var(--active-text); font-weight: 600; }}
.wb-plan li.failed {{ color: var(--alarm-text); font-weight: 600; }}
.wb-plan .tool {{ font-family: var(--din); font-size: 13px; color: var(--muted); font-weight: 400; }}

/* event journal */
.st-key-journal {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 2px 12px; }}
.wb-jr {{ width: 100%; border-collapse: collapse; font-size: 14px; border: none !important; }}
.wb-jr tr {{ border: none !important; background: none !important; }}
.wb-jr td {{ padding: 4px 10px 4px 0; border: none !important; border-bottom: 1px solid var(--divider) !important;
  vertical-align: top; color: var(--ink); }}
.wb-jr td.t {{ font-family: var(--din); font-variant-numeric: tabular-nums; color: var(--muted); white-space: nowrap; width: 66px; }}
.wb-jr td.k {{ font-family: var(--din); white-space: nowrap; width: 84px; color: var(--ink-2); }}
.wb-jr td.d {{ font-family: var(--din); font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right;
  color: var(--muted); width: 70px; }}
.wb-jr tr.alarm td.k, .wb-jr tr.alarm td.x {{ color: var(--alarm-text); font-weight: 600; }}
.wb-jr tr.warn td.k, .wb-jr tr.warn td.x {{ color: var(--warn-text); }}
.wb-jr tr.log td.x {{ color: var(--ink-2); }}
.wb-jr .ok {{ color: var(--done-text); font-weight: 600; }} .wb-jr .bad {{ color: var(--alarm-text); font-weight: 600; }}

/* result */
.wb-result {{ background: var(--panel); border: 1px solid var(--border); border-left: 4px solid var(--done);
  border-radius: 0 8px 8px 0; padding: 10px 14px; margin-top: 10px; box-shadow: var(--shadow); }}
.wb-result.failed {{ border-left-color: var(--alarm); }} .wb-result.cancelled {{ border-left-color: var(--pipe); }}
.wb-result .ans {{ font-size: 15px; margin: 4px 0 0 0; color: var(--ink); line-height: 1.45; }}

/* idle screen: how a job runs + last job */
.wb-how {{ display: flex; flex-wrap: wrap; gap: 0; margin: 6px 0 4px 0; }}
.wb-how .s {{ flex: 1 1 180px; display: flex; gap: 10px; align-items: flex-start; padding: 6px 14px 6px 0; }}
.wb-how .n {{ font-family: var(--din); font-weight: 600; font-size: 14px; width: 28px; height: 28px; flex: none;
  border: 2px solid var(--pipe); background: var(--panel); border-radius: 50%; display: flex; align-items: center;
  justify-content: center; color: var(--ink-2); }}
.wb-how .t {{ font-size: 15px; color: var(--ink); font-weight: 600; }}
.wb-how .d {{ font-size: 14px; color: var(--ink-2); line-height: 1.4; }}
.wb-last {{ background: var(--panel); border: 1px solid var(--border); border-left: 4px solid var(--pipe);
  border-radius: 0 8px 8px 0; padding: 10px 14px; box-shadow: var(--shadow); }}
.wb-last.succeeded {{ border-left-color: var(--done); }} .wb-last.failed {{ border-left-color: var(--alarm); }}
.wb-last .row {{ display: flex; flex-wrap: wrap; gap: 4px 14px; align-items: baseline; }}
.wb-last .files {{ font-size: 14px; color: var(--ink-2); margin-top: 3px; }}
.wb-last .ans {{ font-size: 15px; color: var(--ink); margin-top: 4px; }}

/* banners */
.wb-banner {{ border-left: 6px solid var(--alarm); background: var(--alarm-bg); border-radius: 0 8px 8px 0;
  padding: 12px 16px; margin: 8px 0; }}
.wb-banner .h {{ font-family: var(--din); font-size: 20px; font-weight: 600; color: var(--alarm-text); }}
.wb-banner p {{ margin: 4px 0 0 0; font-size: 15px; color: var(--ink); }}
.wb-banner.warn {{ border-left-color: var(--warn-text); background: var(--warn-bg); }}
.wb-banner.warn .h {{ color: var(--warn-text); }}

/* network page */
.wb-net-head {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 4px; }}
.wb-net-head > div {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 18px;
  box-shadow: var(--shadow); }}
.wb-net-head .big {{ min-width: 300px; }}
.wb-net-head .side {{ min-width: 240px; max-width: 380px; }}
.wb-net-head .tag, .wb-readings .tag {{ font-family: var(--din); font-size: 13px; font-weight: 600; color: var(--muted); }}
.wb-net-head .val {{ font-family: var(--din); font-size: 64px; font-weight: 600; line-height: 1; margin: 4px 0; }}
.wb-net-head .val2, .wb-probe .val2 {{ font-family: var(--din); font-size: 26px; font-weight: 600; line-height: 1.2; margin: 6px 0 4px 0; }}
.wb-net-head .lbl, .wb-readings .lbl {{ font-size: 15px; color: var(--ink); }}
.wb-net-head .note, .wb-readings .note, .wb-probe .note {{ font-size: 14px; color: var(--ink-2); line-height: 1.4; margin-top: 3px; }}
.wb-net-verdict {{ font-size: 16px; font-weight: 600; margin: 10px 0 0 0; }}
.wb-net-note {{ font-size: 14px; color: var(--ink-2); margin: 2px 0 0 0; max-width: 900px; }}
.wb-readings {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; margin: 12px 0; }}
.wb-readings .cell {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; }}
.wb-readings .v {{ font-family: var(--din); font-size: 22px; font-weight: 600; line-height: 1.2; }}
.wb-probe {{ background: var(--panel); border: 1px solid var(--border); border-left: 4px solid var(--done);
  border-radius: 0 8px 8px 0; padding: 6px 12px; margin: 6px 0 10px 0; }}
.wb-probe.alarm {{ border-left-color: var(--alarm); }}
.st-key-conn_table, .st-key-audit_table {{ background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }}
.wb-conn {{ width: 100%; border-collapse: collapse; font-size: 14px; border: none !important; }}
.wb-conn th {{ text-align: left; font-weight: 600; color: var(--ink-2); font-size: 13px; padding: 6px 8px 6px 0;
  border: none !important; border-bottom: 1px solid var(--pipe) !important; background: var(--panel) !important; }}
.wb-conn tr {{ border: none !important; background: none; }}
.wb-conn td {{ padding: 4px 8px 4px 0; border: none !important; border-bottom: 1px solid var(--divider) !important;
  vertical-align: top; color: var(--ink); }}
.wb-conn td.mono {{ font-family: var(--din); font-size: 14px; overflow-wrap: anywhere; }}
.wb-conn td.t, .wb-conn td.d {{ font-family: var(--din); font-variant-numeric: tabular-nums; white-space: nowrap; color: var(--muted); }}
.wb-conn tr.core td, .wb-conn tr.leak td {{ background: var(--alarm-bg); color: var(--alarm-text); }}
.wb-conn tr.core td:first-child, .wb-conn tr.leak td:first-child {{ box-shadow: inset 4px 0 0 var(--alarm); }}
.wb-conn tr.platform td {{ background: var(--warn-bg); color: var(--warn-text); }}
.wb-conn tr.platform td:first-child {{ box-shadow: inset 4px 0 0 var(--warn-text); }}
.wb-conn tr.other td, .wb-conn tr.probe td {{ color: var(--muted); }}
.wb-conn tr.fail td {{ color: var(--alarm-text); }}
.wb-conn td:first-child, .wb-conn th:first-child {{ padding-left: 10px; }}
.wb-audit td.task {{ font-family: var(--din); font-size: 13px; white-space: nowrap; color: var(--muted); }}

/* deliverables: orange only for files you can take away */
.wb-file {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; }}
.wb-file .kind {{ font-family: var(--din); font-size: 13px; font-weight: 700; color: var(--tag); background: var(--tag-bg);
  border: 1px solid var(--deliver); padding: 0 8px; border-radius: 999px; letter-spacing: 0.03em; }}
.wb-file .name {{ font-size: 15px; font-weight: 600; color: var(--ink); overflow-wrap: anywhere; }}
.wb-file .sum {{ flex-basis: 100%; font-size: 14px; color: var(--ink-2); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }}
[class*="st-key-dl_"] button {{ background: var(--deliver) !important; border: 1px solid var(--deliver) !important;
  color: var(--deliver-ink) !important; }}
[class*="st-key-dl_"] button p {{ font-weight: 700; }}
[class*="st-key-dl_"] button:hover {{ filter: brightness(1.06); }}
.wb-footer {{ margin-top: 14px; color: var(--muted); font-size: 13px; }}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def selected_css(key: str) -> str:
    """Blue left edge for the selected / running work order panel (square corners on that edge)."""
    return (f'<style>.st-key-wocard_{key} {{ border-left: 3px solid {ACTIVE} !important; '
            'border-radius: 0 8px 8px 0 !important; }</style>')


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def fmt_seconds(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    minutes, sec = divmod(seconds, 60)
    return f"{minutes:02d}:{sec:02d}"


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
