"""
"Control room" look for the Workbench UI: ISA-101 style, neutral greys, colour only for meaning.
Everything is inline CSS; fonts are Windows system fonts (Bahnschrift, Segoe UI, Consolas)
with the Streamlit bundled fonts as fallback. Nothing loads from the internet.
"""
from __future__ import annotations

import html

import streamlit as st

# ---------------------------------------------------------------- tokens
CANVAS = "#E3E5E8"
FACE = "#F5F6F7"
INK = "#22272E"
INK_2 = "#4B545E"
LINE = "#6B7580"
RULE = "#C9CDD2"
OK = "#2E7D4F"
DONE = "#5E7F6B"
WARN = "#C98A04"
WARN_TEXT = "#8A5E00"
ALARM = "#B3261E"
ACTIVE = "#1F5FA8"

DIN = "'Bahnschrift', 'DIN Alternate', 'Segoe UI', 'Source Sans', sans-serif"
BODY = "'Segoe UI', 'Source Sans', system-ui, sans-serif"

CSS = f"""
<style>
:root {{
  --canvas:{CANVAS}; --face:{FACE}; --ink:{INK}; --ink-2:{INK_2}; --line:{LINE}; --rule:{RULE};
  --ok:{OK}; --done:{DONE}; --warn:{WARN}; --warn-text:{WARN_TEXT}; --alarm:{ALARM}; --active:{ACTIVE};
  --din:{DIN}; --body:{BODY};
}}
.block-container {{ padding-top: 1.1rem; padding-bottom: 0.6rem; max-width: 100%; }}
[data-testid="stSidebarHeader"] {{ height: 2rem; min-height: 2rem; padding-top: 0.4rem; padding-bottom: 0; }}
[data-testid="stSidebarUserContent"] {{ padding-top: 0; }}
[data-testid="stHeader"] {{ background: transparent; height: 0; }}
:focus-visible {{ outline: 2px solid var(--active) !important; outline-offset: 2px; }}

/* type scale: 12 meta, 14 body, 16 panel title, 20 value, 26 title */
h1.wb-title, .wb-title {{ font-family: var(--din) !important; font-size: 26px !important; font-weight: 600 !important;
  color: var(--ink); margin: 0 !important; padding: 0 !important; line-height: 1.15 !important; }}
.wb-sub {{ font-size: 14px; color: var(--ink-2); margin: 2px 0 0 0; }}
.wb-h {{ font-family: var(--din); font-size: 16px; font-weight: 600; color: var(--ink); margin: 0 0 6px 0; }}
.wb-meta {{ font-size: 12px; color: var(--ink-2); }}
.wb-num {{ font-family: var(--din); font-variant-numeric: tabular-nums; }}
.wb-empty {{ font-size: 14px; color: var(--ink-2); padding: 6px 0; }}

/* instrument faceplate (top bar) */
.wb-plate {{ display: flex; justify-content: flex-end; gap: 0; }}
.wb-plate > div {{ background: var(--face); border: 1px solid var(--rule); padding: 6px 12px; min-width: 132px; }}
.wb-plate > div + div {{ border-left: none; }}
.wb-plate .tag {{ font-family: var(--din); font-size: 12px; color: var(--ink-2); letter-spacing: 0.02em; }}
.wb-plate .val {{ font-family: var(--din); font-size: 20px; font-weight: 600; line-height: 1.2; }}
.wb-plate .lbl {{ font-size: 12px; color: var(--ink-2); }}

/* status lamps */
.wb-lamp {{ display: flex; align-items: baseline; gap: 8px; font-size: 13px; margin: 3px 0; color: var(--ink); }}
.wb-lamp i {{ width: 9px; height: 9px; border-radius: 50%; flex: none; display: inline-block; position: relative; top: 1px; }}
.wb-lamp .why {{ color: var(--ink-2); font-size: 12px; }}
.c-ok {{ color: var(--ok); }} .c-alarm {{ color: var(--alarm); }} .c-warn {{ color: var(--warn-text); }}
.c-active {{ color: var(--active); }} .c-dim {{ color: var(--ink-2); }}
.b-ok {{ background: var(--ok); }} .b-alarm {{ background: var(--alarm); }} .b-off {{ background: #9AA1A9; }}

/* work orders */
.wb-wo {{ font-size: 12px; color: var(--ink-2); margin: -6px 0 10px 2px; line-height: 1.35; }}
.wb-wo .io {{ font-family: var(--din); color: var(--ink); }}
[class*="st-key-wo_"] button {{ justify-content: flex-start; text-align: left; border-color: var(--rule);
  background: var(--face); border-radius: 3px; }}
[class*="st-key-wo_"] button > div {{ justify-content: flex-start; width: 100%; }}
[class*="st-key-wo_"] button p {{ font-size: 14px; text-align: left; }}
[class*="st-key-wo_"] button strong {{ font-family: var(--din); font-weight: 600; margin-right: 4px; }}

/* job header */
.wb-job {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; }}
.wb-job .id {{ font-family: var(--din); font-size: 16px; font-weight: 600; color: var(--ink); }}
.wb-job .msg {{ flex-basis: 100%; font-size: 13px; color: var(--ink-2); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }}
.wb-state {{ font-family: var(--din); font-size: 13px; font-weight: 600; padding: 1px 8px; border: 1px solid currentColor; border-radius: 2px; }}
.wb-clock {{ font-family: var(--din); font-size: 20px; font-variant-numeric: tabular-nums; color: var(--ink); }}

/* process line */
.wb-pl {{ display: flex; flex-wrap: wrap; align-items: flex-start; row-gap: 10px; padding: 8px 0 2px 0; }}
.wb-st {{ display: flex; align-items: flex-start; }}
.wb-pipe {{ width: 18px; height: 0; border-top: 3px solid var(--line); margin-top: 22px; position: relative; }}
.wb-pipe.wait {{ border-top-style: dashed; border-top-color: #A9B0B7; }}
.wb-pipe::after {{ content: ""; position: absolute; right: -1px; top: -6px; border-left: 6px solid var(--line);
  border-top: 4.5px solid transparent; border-bottom: 4.5px solid transparent; }}
.wb-pipe.wait::after {{ border-left-color: #A9B0B7; }}
.wb-st:first-child .wb-pipe {{ width: 8px; }} .wb-st:first-child .wb-pipe::after {{ display: none; }}
.wb-bub {{ width: 74px; display: flex; flex-direction: column; align-items: center; }}
.wb-bub svg {{ display: block; overflow: visible; }}
.wb-bub text {{ font-family: var(--din); font-size: 12px; font-weight: 600; }}
.wb-bub .cap {{ font-size: 11.5px; color: var(--ink-2); text-align: center; line-height: 1.25; margin-top: 3px;
  max-width: 72px; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }}
.wb-bub.active .cap {{ color: var(--active); font-weight: 600; }}
.wb-bub.failed .cap {{ color: var(--alarm); font-weight: 600; }}
.wb-bub .ring {{ fill: none; stroke: var(--active); stroke-width: 2; opacity: 0; transform-origin: 23px 23px; }}
.wb-bub.active .ring {{ animation: wb-pulse 1.8s ease-out infinite; }}
@keyframes wb-pulse {{ 0% {{ opacity: .55; transform: scale(1); }} 100% {{ opacity: 0; transform: scale(1.35); }} }}
@media (max-width: 1500px) {{ .wb-bub {{ width: 68px; }} .wb-bub .cap {{ max-width: 66px; font-size: 11px; }}
  .wb-pipe {{ width: 14px; }} }}
@media (prefers-reduced-motion: reduce) {{ .wb-bub.active .ring {{ animation: none; opacity: .5; }} }}

/* router faceplate + plan */
.wb-rtr .model {{ font-family: var(--din); font-size: 20px; font-weight: 600; color: var(--ink); line-height: 1.2; }}
.wb-rtr table {{ border-collapse: collapse; margin: 4px 0 6px 0; font-size: 13px; }}
.wb-rtr table, .wb-rtr td, .wb-rtr tr {{ border: none !important; background: none !important; }}
.wb-rtr td {{ padding: 1px 12px 1px 0; }} .wb-rtr td:first-child {{ color: var(--ink-2); }}
.wb-rtr .reason {{ font-size: 13px; color: var(--ink); border-left: 3px solid var(--rule); padding-left: 8px; }}
.wb-bar {{ display: inline-block; width: 70px; height: 6px; background: var(--rule); vertical-align: middle; margin-left: 6px; }}
.wb-bar > span {{ display: block; height: 100%; background: var(--line); }}
.wb-plan {{ list-style: none; margin: 0; padding: 0; font-size: 14px; }}
.wb-plan li {{ display: flex; gap: 8px; padding: 3px 0; color: var(--ink-2); }}
.wb-plan li .mk {{ font-family: var(--din); width: 16px; flex: none; text-align: center; }}
.wb-plan li.done {{ color: var(--ink); }} .wb-plan li.done .mk {{ color: var(--done); }}
.wb-plan li.current {{ color: var(--active); font-weight: 600; }}
.wb-plan li.failed {{ color: var(--alarm); font-weight: 600; }}
.wb-plan .tool {{ font-family: var(--din); font-size: 12px; color: var(--ink-2); font-weight: 400; }}

/* event journal */
.wb-jr {{ width: 100%; border-collapse: collapse; font-size: 13px; border: none !important; }}
.wb-jr tr {{ border: none !important; background: none; }}
.wb-jr td {{ padding: 3px 8px 3px 0; border: none !important; border-bottom: 1px solid #D5D9DE !important; vertical-align: top; }}
.wb-jr td.t {{ font-family: var(--din); font-variant-numeric: tabular-nums; color: var(--ink-2); white-space: nowrap; width: 62px; }}
.wb-jr td.k {{ font-family: var(--din); white-space: nowrap; width: 84px; color: var(--ink-2); }}
.wb-jr td.d {{ font-family: var(--din); font-variant-numeric: tabular-nums; white-space: nowrap; text-align: right; color: var(--ink-2); width: 70px; }}
.wb-jr tr.alarm td {{ background: #F6E3E1; }} .wb-jr tr.alarm td.k, .wb-jr tr.alarm td.x {{ color: var(--alarm); font-weight: 600; }}
.wb-jr tr.warn td.k, .wb-jr tr.warn td.x {{ color: var(--warn-text); }}
.wb-jr tr.log td.x {{ color: var(--ink-2); }}
.wb-jr .ok {{ color: var(--ok); font-weight: 600; }} .wb-jr .bad {{ color: var(--alarm); font-weight: 600; }}

/* result */
.wb-result {{ border-left: 4px solid var(--ok); background: var(--face); padding: 8px 12px; margin-top: 4px; }}
.wb-result.failed {{ border-left-color: var(--alarm); }} .wb-result.cancelled {{ border-left-color: var(--line); }}
.wb-result .ans {{ font-size: 14px; margin: 4px 0 0 0; color: var(--ink); line-height: 1.45; }}

/* deliverables */
.wb-file {{ display: flex; align-items: baseline; gap: 8px; }}
.wb-file .kind {{ font-family: var(--din); font-size: 12px; font-weight: 600; color: var(--ink); border: 1px solid var(--line);
  padding: 0 5px; border-radius: 2px; }}
.wb-file .name {{ font-size: 14px; color: var(--ink); overflow-wrap: anywhere; }}
.wb-footer {{ margin-top: 10px; color: var(--ink-2); font-size: 12px; text-align: right; }}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


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
