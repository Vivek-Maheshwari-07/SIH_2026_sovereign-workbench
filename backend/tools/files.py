"""
File safety: the one place upload validation and path safety live.
backend/file_store.py delegates here instead of validating uploads itself;
the router and any future agent tool (ticket A8's read_file/write_file,
defined below) also go through safe_path().
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Optional, Union

from backend.settings import settings
from shared.contracts import ERROR_CODES

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------- constants
# This list must match shared/contracts.py's ERROR_CODES["UNSUPPORTED_FILE"]
# comment exactly — the contract is the source of truth for what's allowed.
# tif/tiff are NOT on that list; see docs/contract_change_requests.md for the
# open request to add them.
ALLOWED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md", ".py", ".csv", ".xlsx", ".docx"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
# Extensions whose real content is "plain text" (checked via the UTF-8/no-NUL sniff).
_TEXT_SUFFIXES = {".txt", ".md", ".py", ".csv"}
# Extensions that are really ZIP containers with a required inner file.
_ZIP_REQUIRED_MEMBER = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml"}

# How many bytes of a claimed text file we sniff to decide it's really text
# (no NUL bytes, decodes as UTF-8) rather than binary wearing a text-y name.
_TEXT_SNIFF_BYTES = 4096

# A sanitized filename keeps only these characters; everything else becomes "_".
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class FileSafetyError(Exception):
    """Raised for a bad upload or an unsafe path. `code` is an ERROR_CODES key."""

    def __init__(self, code: str, message: Optional[str] = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message or ERROR_CODES[code])
        self.code = code


def _workspace_root() -> Path:
    root = settings.WB_WORKSPACE_DIR
    return (root if root.is_absolute() else _REPO_ROOT / root).resolve()


def safe_path(relative: str) -> Path:
    """
    Resolves `relative` against workspace/ and refuses anything that could
    escape it: "..", absolute paths, drive letters ("C:\\..."), and UNC
    paths ("\\\\server\\share"). Always checked with Windows path semantics
    (PureWindowsPath), since this project targets Windows (AGENTS.md rule 8:
    pathlib.Path, never string-joining).
    """
    if not relative or not relative.strip():
        raise FileSafetyError("BAD_REQUEST", "path must not be empty")

    pure = PureWindowsPath(relative)
    if pure.drive or pure.root:
        # .drive catches "C:\..." and "\\server\share" (UNC); .root catches
        # any other rooted path, including a bare leading "/" or "\".
        raise FileSafetyError("BAD_REQUEST", f"path must be relative to workspace/: {relative!r}")
    if ".." in pure.parts:
        raise FileSafetyError("BAD_REQUEST", f"path must not contain '..': {relative!r}")

    workspace = _workspace_root()
    resolved = (workspace / relative).resolve()
    if not resolved.is_relative_to(workspace):
        raise FileSafetyError("BAD_REQUEST", f"path escapes workspace/: {relative!r}")

    return resolved


def sanitize_filename(filename: str) -> str:
    """Strips any path parts and replaces anything but [A-Za-z0-9._-] with '_'."""
    name = PureWindowsPath(filename).name
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.strip("._")
    return name or "upload"


# ---------------------------------------------------------------- magic-byte sniffing
def _looks_like_text(content: bytes) -> bool:
    sample = content[:_TEXT_SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _looks_like_zip_container(content: bytes, suffix: str) -> bool:
    """docx/xlsx are ZIP files; also open the zip and require the file that
    makes it a real docx/xlsx, so a random zip renamed to .docx is rejected."""
    if not content.startswith(b"PK"):
        return False
    required_member = _ZIP_REQUIRED_MEMBER[suffix]
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return required_member in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _matches_signature(content: bytes, suffix: str) -> bool:
    if suffix == ".pdf":
        return content.startswith(b"%PDF-")
    if suffix == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in (".jpg", ".jpeg"):
        return content.startswith(b"\xff\xd8\xff")
    if suffix in _TEXT_SUFFIXES:
        return _looks_like_text(content)
    if suffix in _ZIP_REQUIRED_MEMBER:
        return _looks_like_zip_container(content, suffix)
    return False


def validate_upload(filename: str, content: bytes) -> tuple[str, str]:
    """
    Validates an upload's size, extension, and real content (via magic
    bytes / a text sniff), so a renamed "virus.exe" saved as "report.pdf"
    is caught by content, not just its name.

    Returns (sanitized_filename, suffix). Raises FileSafetyError with an
    ERROR_CODES code on any failure.
    """
    max_bytes = settings.WB_MAX_UPLOAD_MB * 1024 * 1024
    if len(content) > max_bytes:
        raise FileSafetyError("FILE_TOO_LARGE")

    safe_name = sanitize_filename(filename)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise FileSafetyError("UNSUPPORTED_FILE", f"extension {suffix!r} is not allowed")

    if not _matches_signature(content, suffix):
        raise FileSafetyError(
            "UNSUPPORTED_FILE",
            f"file content does not match its extension {suffix!r} (failed the magic-byte check)",
        )

    return safe_name, suffix


# ---------------------------------------------------------------- agent tools (ticket A8)
def read_file(path: str) -> bytes:
    """Read a file inside workspace/. `path` is relative to workspace/."""
    resolved = safe_path(path)
    if not resolved.is_file():
        raise FileSafetyError("FILE_NOT_FOUND", f"no such file: {path!r}")
    return resolved.read_bytes()


def write_file(path: str, content: Union[str, bytes]) -> Path:
    """Write `content` to a file inside workspace/, creating parent dirs as needed."""
    resolved = safe_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    resolved.write_bytes(data)
    return resolved
