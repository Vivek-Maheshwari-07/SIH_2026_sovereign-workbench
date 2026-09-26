"""
B5: deliverables tray. One entry per artifact: orange type tag, name, size, one-line summary,
the orange Download button (the only filled orange button) and a preview.
Bytes are fetched once through the API client and cached in session state (with the parsed
preview), so the 1 s refresh during a job never downloads or parses a file twice.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Optional

import streamlit as st

from shared.contracts import Artifact, ArtifactKind, ErrorInfo
from ui import api_client, messages
from ui.api_client import DownloadedFile
from ui.components.theme import esc, human_size

CACHE_KEY = "artifact_cache"
XLSX_ROWS = 50
DOCX_PARAGRAPHS = 8
TEXT_PREVIEW_CHARS = 4000


@dataclass
class CachedFile:
    file: Optional[DownloadedFile] = None
    error: Optional[ErrorInfo] = None
    preview: Any = None            # parsed preview (str, list[str] or DataFrame)
    preview_error: Optional[str] = None
    preview_ready: bool = False


# ---------------------------------------------------------------- previews (pure, testable)
def docx_paragraphs(content: bytes, limit: int = DOCX_PARAGRAPHS) -> list[str]:
    from docx import Document  # python-docx, in the lock file

    doc = Document(io.BytesIO(content))
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return paras[:limit]


def xlsx_table(content: bytes, rows: int = XLSX_ROWS):
    import pandas as pd

    return pd.read_excel(io.BytesIO(content), engine="openpyxl", nrows=rows)


def text_of(content: bytes, limit: int = TEXT_PREVIEW_CHARS) -> str:
    text = content.decode("utf-8", errors="replace")
    return text if len(text) <= limit else text[:limit] + "\n# ... (truncated in preview)"


def build_preview(kind: ArtifactKind, content: bytes) -> Any:
    if kind == ArtifactKind.DOCX:
        return docx_paragraphs(content)
    if kind == ArtifactKind.XLSX:
        return xlsx_table(content)
    if kind in (ArtifactKind.PY, ArtifactKind.TXT, ArtifactKind.MD, ArtifactKind.JSON):
        return text_of(content)
    if kind == ArtifactKind.PNG:
        return content
    return None


# ---------------------------------------------------------------- cache
def cache() -> dict[str, CachedFile]:
    return st.session_state.setdefault(CACHE_KEY, {})


def load(art: Artifact) -> CachedFile:
    entry = cache().get(art.artifact_id)
    if entry is None or (entry.file is None and entry.error is None):
        result = api_client.get_client().download_artifact(art.artifact_id)
        entry = CachedFile(file=result.data, error=result.error)
        cache()[art.artifact_id] = entry
    if entry.file is not None and not entry.preview_ready:
        try:
            entry.preview = build_preview(art.kind, entry.file.content)
        except Exception as exc:  # any parser problem -> friendly message, never a traceback
            entry.preview_error = type(exc).__name__
        entry.preview_ready = True
    return entry


def forget(artifact_id: str) -> None:
    cache().pop(artifact_id, None)


# ---------------------------------------------------------------- rendering
def show_preview(art: Artifact, entry: CachedFile) -> None:
    if entry.preview_error is not None:
        st.info(f"No preview for this file ({entry.preview_error}). The download still works.")
        return
    preview = entry.preview
    if art.kind == ArtifactKind.DOCX:
        for para in preview or []:
            st.markdown(f"<p style='font-size:14px;margin:0 0 6px 0'>{esc(para)}</p>", unsafe_allow_html=True)
    elif art.kind == ArtifactKind.XLSX:
        st.caption(f"First {min(len(preview), XLSX_ROWS)} rows")
        st.dataframe(preview, hide_index=True, width="stretch", height=260)
    elif art.kind == ArtifactKind.PY:
        st.code(preview, language="python", height=260)
    elif art.kind == ArtifactKind.PNG:
        st.image(preview)
    elif preview:
        st.code(preview, language=None, height=220)
    else:
        st.caption(art.preview or "No preview for this file type.")


def summary_line(art: Artifact) -> str:
    """One line under the file name: the backend's preview text, first line only."""
    text = (art.preview or "").strip()
    return text.splitlines()[0] if text else ""


def file_html(art: Artifact) -> str:
    summary = summary_line(art)
    sum_html = f'<span class="sum" title="{esc(summary)}">{esc(summary)}</span>' if summary else ""
    return (f'<div class="wb-file"><span class="kind">{esc(art.kind.value.upper())}</span>'
            f'<span class="name">{esc(art.filename)}</span>'
            f'<span class="wb-meta wb-num">{human_size(art.size_bytes)}</span>{sum_html}</div>')


def entry_row(art: Artifact, first: bool) -> None:
    st.markdown(file_html(art), unsafe_allow_html=True)
    entry = load(art)
    if entry.file is None:
        st.warning(messages.friendly(f"Could not fetch {art.filename} from the backend", entry.error)
                   if entry.error else f"Could not fetch {art.filename} from the backend. Press Try again.")
        st.button("Try again", key=f"retry_{art.artifact_id}", on_click=forget, args=(art.artifact_id,))
        return
    st.download_button("Download", data=entry.file.content, file_name=art.filename, mime=entry.file.media_type,
                       key=f"dl_{art.artifact_id}", on_click="ignore")
    with st.expander("Preview", expanded=first):
        show_preview(art, entry)


def tray(artifacts: list[Artifact]) -> None:
    st.markdown('<div class="wb-h tray">Deliverables</div>', unsafe_allow_html=True)
    if not artifacts:
        st.markdown('<div class="wb-empty">Files the job produces (Word notes, Excel tag lists, code) '
                    'appear here with a preview and a download button.</div>', unsafe_allow_html=True)
        return
    for i, art in enumerate(artifacts):
        if i:
            st.divider()
        entry_row(art, first=(i == 0))
