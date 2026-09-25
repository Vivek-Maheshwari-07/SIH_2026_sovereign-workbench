"""
Knowledge base (ticket A6): SOP / manual chunks in a local ChromaDB, searched
with our own bge-m3 embeddings from llm_client.embed().

SOVEREIGN RULES
- The Chroma client is created with anonymized_telemetry=False.
- Chroma's default embedding function (it downloads an ONNX model from the
  internet) must never run. We always pass embeddings ourselves, for both
  upsert and query, and the collection carries a guard embedding function
  that raises if Chroma ever tries to embed anything on its own.

Chunking is page by page so every chunk keeps an exact page number (SOP
references need it). See chunk_page_text().
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import register_embedding_function

from backend.llm_client import embed
from backend.registry import registry
from backend.settings import settings
from backend.tools.documents import extract
from shared.contracts import KBHit, KBStats

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ---- named constants (no .env key exists for these)
COLLECTION_NAME = "wb_kb"
CHUNK_TOKENS = 800                 # target chunk size
OVERLAP_TOKENS = 100               # tokens shared by neighbouring chunks on the same page
TOKENS_PER_WORD = 1.3              # approximation; no tokenizer package
EMBED_BATCH = 16                   # chunks per embed() call
SUPPORTED_SUFFIXES = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".txt", ".md", ".py", ".csv", ".docx", ".xlsx"})
MAX_TOP_K = 10                     # KBSearchRequest allows 1..10

_lock = threading.Lock()
_client: Optional[Any] = None
_collection: Optional[Any] = None


# ------------------------------------------------------------------ embedding guard
class EmbeddingGuardError(RuntimeError):
    """Chroma tried to embed text itself; that would download a model from the internet."""


@register_embedding_function
class GuardEmbeddingFunction(EmbeddingFunction[Documents]):
    """
    Collection embedding function that always raises. Registered by name so
    that when Chroma reopens the collection from disk it rebuilds THIS guard
    from the stored config, instead of falling back to its default model.
    """

    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:
        raise EmbeddingGuardError(
            "Chroma tried to compute embeddings itself. Pass embeddings from llm_client.embed() instead."
        )

    @staticmethod
    def name() -> str:
        return "wb-guard-no-embed"

    def get_config(self) -> dict[str, Any]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "GuardEmbeddingFunction":
        return GuardEmbeddingFunction()

    def default_space(self) -> str:
        return "cosine"


# ------------------------------------------------------------------ client / collection
def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _REPO_ROOT / path


def get_collection():
    """One shared PersistentClient + collection, created lazily on first use."""
    global _client, _collection
    with _lock:
        if _collection is None:
            path = _resolve(settings.WB_CHROMA_DIR)
            path.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(path=str(path), settings=ChromaSettings(anonymized_telemetry=False))
            _collection = _client.get_or_create_collection(
                COLLECTION_NAME,
                embedding_function=GuardEmbeddingFunction(),
                configuration={"hnsw": {"space": "cosine"}},
            )
        return _collection


def reset_client() -> None:
    """Forget the cached client (tests point WB_CHROMA_DIR somewhere else)."""
    global _client, _collection
    with _lock:
        _client = None
        _collection = None


# ------------------------------------------------------------------ search / stats
def distance_to_score(distance: float) -> float:
    """Cosine distance (0 = same direction, 2 = opposite) -> similarity clamped to 0..1."""
    return max(0.0, min(1.0, 1.0 - float(distance)))


def search(query: str, top_k: int = 4) -> list[KBHit]:
    """Top-k chunks for `query`, best first. Empty query or empty KB -> []."""
    if not query or not query.strip():
        return []
    collection = get_collection()
    count = collection.count()
    if count == 0:
        return []
    n_results = max(1, min(int(top_k), MAX_TOP_K, count))
    query_vector = embed([query], purpose="kb_search")[0]
    result = collection.query(
        query_embeddings=[query_vector],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )
    hits: list[KBHit] = []
    for text, meta, distance in zip(result["documents"][0], result["metadatas"][0], result["distances"][0]):
        meta = meta or {}
        page = meta.get("page")
        hits.append(
            KBHit(
                text=text or "",
                source=str(meta.get("source", "unknown")),
                page=int(page) if isinstance(page, int) and page > 0 else None,
                score=distance_to_score(distance),
            )
        )
    return hits


def _all_metadatas(collection) -> list[dict[str, Any]]:
    if collection.count() == 0:
        return []
    return [m or {} for m in collection.get(include=["metadatas"])["metadatas"]]


def stats() -> KBStats:
    collection = get_collection()
    metas = _all_metadatas(collection)
    return KBStats(
        documents=len({m.get("source") for m in metas if m.get("source")}),
        chunks=len(metas),
        embed_model=registry.embedding_model().ollama_name,
    )


# ------------------------------------------------------------------ chunking
def words_per_chunk() -> int:
    return int(CHUNK_TOKENS / TOKENS_PER_WORD)       # 800 / 1.3 -> 615 words


def overlap_words() -> int:
    return int(OVERLAP_TOKENS / TOKENS_PER_WORD)     # 100 / 1.3 -> 76 words


def chunk_page_text(text: str, size: Optional[int] = None, overlap: Optional[int] = None) -> list[str]:
    """
    Split ONE page into word windows of `size` words; each window starts
    `size - overlap` words after the previous one, so neighbours share
    `overlap` words. A page shorter than `size` is a single chunk.
    """
    size = size or words_per_chunk()
    overlap = overlap if overlap is not None else overlap_words()
    if overlap >= size:
        raise ValueError("overlap must be smaller than chunk size")
    words = text.split()
    if not words:
        return []
    step = size - overlap
    chunks: list[str] = []
    for start in range(0, len(words), step):
        chunks.append(" ".join(words[start:start + size]))
        if start + size >= len(words):
            break
    return chunks


def chunk_id(file_hash: str, page: int, index: int) -> str:
    return hashlib.sha256(f"{file_hash}:{page}:{index}".encode("utf-8")).hexdigest()[:32]


def embed_text(source: str, page: int, text: str) -> str:
    """Text actually embedded: a short source header helps retrieval by document topic."""
    title = Path(source).stem.replace("_", " ").replace("-", " ")
    return f"{title} (page {page})\n{text}"


# ------------------------------------------------------------------ ingest
@dataclass
class _Chunk:
    id: str
    text: str
    page: int
    index: int


@dataclass
class IngestReport:
    files: int = 0
    pages: int = 0
    chunks: int = 0
    embedded: int = 0                  # chunks embedded in this run (unchanged files are skipped)
    skipped_files: int = 0
    failed_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    embed_seconds: float = 0.0
    seconds: float = 0.0

    @property
    def ms_per_chunk(self) -> Optional[float]:
        return (self.embed_seconds * 1000 / self.embedded) if self.embedded else None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kb_files(kb_dir: Path) -> list[Path]:
    if not kb_dir.exists():
        return []
    return sorted(p for p in kb_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)


def _existing_by_source(collection) -> dict[str, dict[str, Any]]:
    """{source: {"hashes": set, "count": int, "total": expected chunk count}} from stored metadata."""
    info: dict[str, dict[str, Any]] = {}
    for meta in _all_metadatas(collection):
        source = meta.get("source")
        if not source:
            continue
        entry = info.setdefault(source, {"hashes": set(), "count": 0, "total": meta.get("chunks_total")})
        entry["hashes"].add(meta.get("file_hash"))
        entry["count"] += 1
    return info


def _file_chunks(path: Path, file_hash: str) -> tuple[int, list[_Chunk]]:
    pages = extract(path).pages
    chunks: list[_Chunk] = []
    for page in pages:
        for index, text in enumerate(chunk_page_text(page.text)):
            chunks.append(_Chunk(id=chunk_id(file_hash, page.page, index), text=text, page=page.page, index=index))
    return len(pages), chunks


def _upsert_file(
    collection, path: Path, file_hash: str, chunks: list[_Chunk], file_no: int, file_total: int,
    progress: Callable[[str], None], batch_size: int, report: IngestReport,
) -> None:
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        t0 = time.monotonic()
        vectors = embed([embed_text(path.name, c.page, c.text) for c in batch], purpose="kb_ingest")
        report.embed_seconds += time.monotonic() - t0
        collection.upsert(
            ids=[c.id for c in batch],
            embeddings=vectors,
            documents=[c.text for c in batch],
            metadatas=[
                {"source": path.name, "page": c.page, "chunk_index": c.index,
                 "file_hash": file_hash, "chunks_total": len(chunks)}
                for c in batch
            ],
        )
        report.embedded += len(batch)
        progress(f"file {file_no}/{file_total}, chunk {start + len(batch)}/{len(chunks)}")


def ingest_folder(
    kb_dir: Optional[Path] = None,
    progress: Callable[[str], None] = print,
    batch_size: int = EMBED_BATCH,
) -> IngestReport:
    """
    Bring the collection in line with the files in `kb_dir` (default
    WB_KB_DIR): new/changed files are (re)embedded, unchanged files are
    skipped, files no longer in the folder lose their chunks. One bad file
    only produces a warning.
    """
    started = time.monotonic()
    kb_dir = _resolve(kb_dir or settings.WB_KB_DIR)
    collection = get_collection()
    report = IngestReport()
    files = _kb_files(kb_dir)
    existing = _existing_by_source(collection)

    present = {p.name for p in files}
    for source in sorted(set(existing) - present):
        collection.delete(where={"source": source})
        report.removed_files.append(source)
        progress(f"removed chunks of deleted file {source}")

    for number, path in enumerate(files, start=1):
        try:
            file_hash = file_sha256(path)
            old = existing.get(path.name)
            if old and old["hashes"] == {file_hash} and old["count"] == old["total"]:
                report.files += 1
                report.skipped_files += 1
                report.chunks += old["count"]
                report.pages += len({m.get("page") for m in _all_metadatas_for(collection, path.name)})
                progress(f"file {number}/{len(files)}: {path.name} unchanged, skipped")
                continue
            page_count, chunks = _file_chunks(path, file_hash)   # extract first: a broken file keeps its old chunks
            if not chunks:
                progress(f"WARNING: file {number}/{len(files)}: {path.name} has no text, skipped")
                report.failed_files.append(path.name)
                continue
            if old:
                collection.delete(where={"source": path.name})
                progress(f"file {number}/{len(files)}: {path.name} changed, old chunks removed")
            _upsert_file(collection, path, file_hash, chunks, number, len(files), progress, batch_size, report)
            report.files += 1
            report.pages += page_count
            report.chunks += len(chunks)
        except Exception as exc:  # one bad file must not stop the others
            report.failed_files.append(path.name)
            progress(f"WARNING: file {number}/{len(files)}: {path.name} failed: {exc}")

    report.seconds = time.monotonic() - started
    return report


def _all_metadatas_for(collection, source: str) -> list[dict[str, Any]]:
    got = collection.get(where={"source": source}, include=["metadatas"])
    return [m or {} for m in got["metadatas"]]
