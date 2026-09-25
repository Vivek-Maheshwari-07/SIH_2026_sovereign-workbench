"""
Minimal file store for Track A: an in-memory file_id -> FileRef + saved-path
registry. Upload validation (size, allowed type, magic bytes, filename
sanitization) all lives in backend.tools.files (ticket A4) — this module
just persists whatever that module already approved.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pymupdf

from backend.settings import settings
from backend.tools.files import IMAGE_SUFFIXES, validate_upload
from shared.contracts import FileRef

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PDF_PAGES_TO_CHECK = 3

_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".py": "text/x-python",
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


def _guess_mime(suffix: str) -> str:
    return _MIME_TYPES.get(suffix, "application/octet-stream")


def new_file_id() -> str:
    return "f_" + secrets.token_hex(6)


def _inspect_pdf(path: Path) -> tuple[Optional[int], Optional[bool]]:
    """Returns (page_count, has_text_layer). has_text_layer False = scanned."""
    try:
        doc = pymupdf.open(path)
    except Exception:
        return None, None
    try:
        page_count = doc.page_count
        pages_to_check = [doc[i] for i in range(min(_PDF_PAGES_TO_CHECK, page_count))]
        has_text = any(page.get_text().strip() for page in pages_to_check)
        return page_count, has_text
    finally:
        doc.close()


@dataclass
class _StoredFile:
    ref: FileRef
    path: Path


class FileStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: dict[str, _StoredFile] = {}

    def save(self, filename: str, content: bytes, content_type: Optional[str] = None) -> FileRef:
        safe_name, suffix = validate_upload(filename, content)  # raises FileSafetyError

        file_id = new_file_id()
        dest_dir = _resolve(settings.WB_WORKSPACE_DIR) / file_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / safe_name
        dest_path.write_bytes(content)

        mime_type = content_type or _guess_mime(suffix)
        is_image = suffix in IMAGE_SUFFIXES
        pages: Optional[int] = None
        has_text_layer: Optional[bool] = None
        if suffix == ".pdf":
            pages, has_text_layer = _inspect_pdf(dest_path)

        ref = FileRef(
            file_id=file_id,
            filename=safe_name,
            mime_type=mime_type,
            size_bytes=len(content),
            is_image=is_image,
            pages=pages,
            has_text_layer=has_text_layer,
        )

        with self._lock:
            self._files[file_id] = _StoredFile(ref=ref, path=dest_path)
        return ref

    def get_ref(self, file_id: str) -> Optional[FileRef]:
        with self._lock:
            stored = self._files.get(file_id)
            return stored.ref if stored else None

    def get_path(self, file_id: str) -> Optional[Path]:
        with self._lock:
            stored = self._files.get(file_id)
            return stored.path if stored else None


file_store = FileStore()
