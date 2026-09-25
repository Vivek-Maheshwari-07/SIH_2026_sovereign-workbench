"""
Minimal file store for Track A (ticket A4 will harden this: dedup, cleanup,
bigger file-type sniffing). Saves uploads under workspace/<file_id>/<name>
and keeps an in-memory file_id -> FileRef + saved path registry, protected
by a lock.
"""
from __future__ import annotations

import mimetypes
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pymupdf

from backend.settings import settings
from shared.contracts import ERROR_CODES, FileRef

_REPO_ROOT = Path(__file__).resolve().parent.parent

SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md", ".py", ".csv", ".xlsx", ".docx"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
_PDF_PAGES_TO_CHECK = 3


class FileStoreError(Exception):
    """Raised for a bad upload. `code` is a key from shared.contracts.ERROR_CODES."""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message or ERROR_CODES[code])
        self.code = code


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


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
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise FileStoreError("UNSUPPORTED_FILE")

        max_bytes = settings.WB_MAX_UPLOAD_MB * 1024 * 1024
        if len(content) > max_bytes:
            raise FileStoreError("FILE_TOO_LARGE")

        file_id = new_file_id()
        dest_dir = _resolve(settings.WB_WORKSPACE_DIR) / file_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename
        dest_path.write_bytes(content)

        mime_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        is_image = suffix in _IMAGE_SUFFIXES
        pages: Optional[int] = None
        has_text_layer: Optional[bool] = None
        if suffix == ".pdf":
            pages, has_text_layer = _inspect_pdf(dest_path)

        ref = FileRef(
            file_id=file_id,
            filename=filename,
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
