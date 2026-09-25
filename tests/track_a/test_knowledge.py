"""
Tests for backend/tools/knowledge.py and the /api/kb endpoints (ticket A6).

Every test runs under a network guard: any socket connection to an address
other than 127.0.0.1 / localhost / ::1 is blocked, recorded, and fails the
test. Test SOPs are generated into a temporary folder (never data/kb/).
"""
from __future__ import annotations

import hashlib
import math
import re
import socket
from pathlib import Path

import httpx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from backend.settings import settings
from backend.tools import knowledge
from backend.tools.knowledge import (
    EmbeddingGuardError,
    GuardEmbeddingFunction,
    chunk_page_text,
    distance_to_score,
    ingest_folder,
    overlap_words,
    words_per_chunk,
)
from shared.contracts import API_PREFIX, KBSearchResponse, KBStats

# The network guard fixture lives in tests/track_a/conftest.py; every test in this module uses it.
pytestmark = pytest.mark.usefixtures("network_guard")


def test_network_guard_blocks_external_and_allows_loopback(network_guard):
    with pytest.raises(OSError, match="blocked by test guard"):
        socket.create_connection(("93.184.216.34", 443), timeout=1)
    assert network_guard == ["('93.184.216.34', 443)"]
    network_guard.clear()   # the deliberate attempt above must not fail this test's teardown
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=1)
    client.close()
    server.close()
    assert network_guard == []


# ---------------------------------------------------------------- test SOPs
# Five SOP-style documents, 2-3 pages each, with unique facts on known pages.
SOPS: dict[str, list[str]] = {
    "SOP-HSE-011_hot_work_permit.pdf": [
        "HOT WORK PERMIT PROCEDURE. Section 1: Permit issue and validity. "
        "This procedure covers welding, grinding, cutting and any work that produces sparks or flame "
        "in a process area. The area authority issues the hot work permit after walking the job site "
        "with the performing supervisor. A hot work permit is valid for 8 hours from the time of issue "
        "and cannot be extended; a new permit is required for the next shift. The permit copy must be "
        "displayed at the work location for the whole job.",
        "HOT WORK PERMIT PROCEDURE. Section 2: Precautions and fire watch. "
        "A gas test must be done before start of hot work, and the gas tester signs the permit with the "
        "reading. Combustible materials within 11 metres are removed or covered with fire blankets. "
        "A trained fire watch with a charged extinguisher stays at the site during the work and remains "
        "for 30 minutes after the work ends to check for smouldering fires.",
    ],
    "SOP-HSE-014_confined_space_entry.pdf": [
        "CONFINED SPACE ENTRY. Section 1: Roles. "
        "A confined space is a vessel, tank, column or pit with limited openings for entry and exit. "
        "Before entry the space is drained, flushed and isolated from all process lines by blinds. "
        "A standby attendant must remain outside at the entry point at all times while anyone is inside, "
        "keeps a log of entrants and never enters for rescue alone.",
        "CONFINED SPACE ENTRY. Section 2: Atmosphere testing. "
        "The atmosphere is tested at top, middle and bottom levels before entry. Entry is allowed only when "
        "oxygen is between 19.5 and 23.5 percent, flammable gas is below 10 percent of the lower explosive "
        "limit, and toxic gases are below their exposure limits. Testing is repeated every two hours.",
        "CONFINED SPACE ENTRY. Section 3: Rescue. "
        "A written rescue plan with a tripod, winch and full body harness is ready before entry. "
        "Emergency services are informed of the job location and the rescue team rehearses the plan.",
    ],
    "SOP-MNT-020_lockout_tagout.pdf": [
        "LOCKOUT TAGOUT (LOTO). Section 1: Applying locks. "
        "All energy sources of equipment under maintenance are isolated: electrical, pneumatic, hydraulic, "
        "steam and gravity. Each worker applies a personal lock to the isolation point or group lock box, "
        "and keeps the only key. A danger tag with name, date and reason is attached to every lock.",
        "LOCKOUT TAGOUT (LOTO). Section 2: Verification. "
        "After isolation, stored energy is released by bleeding pressure and discharging capacitors. "
        "Zero energy is verified by attempting to start the equipment from the local push button "
        "(try-out) before any work begins. The start switch is then returned to the off position.",
    ],
    "SOP-INS-031_hydrostatic_test.pdf": [
        "HYDROSTATIC PRESSURE TEST. Section 1: Test pressure. "
        "New and repaired pressure vessels and piping are hydrotested with clean water before service. "
        "The test pressure is 1.5 times the design pressure unless the design code gives a different factor. "
        "Pressure is raised in steps of 25 percent with a pause at each step for leak checks.",
        "HYDROSTATIC PRESSURE TEST. Section 2: Hold and acceptance. "
        "The full test pressure is held for at least 60 minutes. The test is accepted when there is no "
        "pressure drop on the calibrated gauge and no visible leaks at welds, flanges or nozzles. "
        "Water is drained through the low point vent afterwards to avoid a vacuum.",
    ],
    "SOP-HSE-017_h2s_safety.pdf": [
        "HYDROGEN SULPHIDE SAFETY. Section 1: Monitors. "
        "Hydrogen sulphide is a toxic gas that smells of rotten eggs at low levels and paralyses the sense "
        "of smell at higher levels. Every person entering a sour area wears a personal H2S monitor, "
        "and each monitor is bump tested daily before use.",
        "HYDROGEN SULPHIDE SAFETY. Section 2: Alarms. "
        "If H2S is above 10 ppm or the alarm sounds, stop work and evacuate upwind or crosswind to the "
        "muster point. Put on an escape breathing set if one is carried. Never return to rescue a "
        "collapsed person without breathing apparatus.",
    ],
}
SCANNED_SOP = "SOP-HSE-017_h2s_safety.pdf"   # written as an image-only PDF to exercise the OCR path

QUESTIONS: list[tuple[str, str, int]] = [
    ("How long can a hot work permit be used before it expires?", "SOP-HSE-011_hot_work_permit.pdf", 1),
    ("How long does the fire watcher have to stay once welding has finished?", "SOP-HSE-011_hot_work_permit.pdf", 2),
    ("What oxygen level is acceptable inside a tank before a person goes in?", "SOP-HSE-014_confined_space_entry.pdf", 2),
    ("Who needs to stay at the manhole while somebody works inside a vessel?", "SOP-HSE-014_confined_space_entry.pdf", 1),
    ("Can one padlock cover the whole maintenance crew when isolating a machine?", "SOP-MNT-020_lockout_tagout.pdf", 1),
    ("How do we confirm the machine is really dead after isolation?", "SOP-MNT-020_lockout_tagout.pdf", 2),
    ("What pressure should a hydrotest use compared with the design value?", "SOP-INS-031_hydrostatic_test.pdf", 1),
    ("For how long is the pressure kept during a water pressure test?", "SOP-INS-031_hydrostatic_test.pdf", 2),
    ("Which way should people run when the sour gas alarm goes off?", "SOP-HSE-017_h2s_safety.pdf", 2),
    ("How often must personal gas detectors for rotten egg gas be checked?", "SOP-HSE-017_h2s_safety.pdf", 1),
]


def _write_text_pdf(path: Path, pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 540, 780), text, fontsize=12)
    doc.save(str(path))
    doc.close()


def _write_scanned_pdf(path: Path, pages: list[str]) -> None:
    """Render each page to a bitmap and store only the image: no text layer, so OCR is needed."""
    text_doc = pymupdf.open()
    for text in pages:
        page = text_doc.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 540, 780), text, fontsize=14)
    scanned = pymupdf.open()
    for page in text_doc:
        pix = page.get_pixmap(dpi=200)
        out = scanned.new_page(width=page.rect.width, height=page.rect.height)
        out.insert_image(out.rect, stream=pix.tobytes("png"))
    scanned.save(str(path))
    scanned.close()
    text_doc.close()


def write_sops(folder: Path, scanned: bool = True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, pages in SOPS.items():
        if scanned and name == SCANNED_SOP:
            _write_scanned_pdf(folder / name, pages)
        else:
            _write_text_pdf(folder / name, pages)


TOTAL_PAGES = sum(len(p) for p in SOPS.values())
tesseract_ok = Path(settings.TESSERACT_CMD).exists()
needs_tesseract = pytest.mark.skipif(not tesseract_ok, reason="Tesseract not installed (needed for the scanned SOP)")


# ---------------------------------------------------------------- fixtures
def fake_embed(texts, *, purpose="embed"):
    """Deterministic offline stand-in for bge-m3: hashed bag of words, unit length."""
    vectors = []
    for text in texts:
        vec = [0.0] * 256
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 256] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        vectors.append([v / norm for v in vec])
    return vectors


@pytest.fixture
def kb(tmp_path, monkeypatch):
    """Temporary KB folder + Chroma dir + extraction cache; knowledge module points at them."""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    monkeypatch.setattr(settings, "WB_KB_DIR", kb_dir)
    monkeypatch.setattr(settings, "WB_CHROMA_DIR", tmp_path / "chroma")
    monkeypatch.setattr(settings, "WB_CACHE_DIR", tmp_path / "cache")
    knowledge.reset_client()
    yield kb_dir
    knowledge.reset_client()


@pytest.fixture
def mock_embed(monkeypatch):
    calls: list[int] = []

    def counting_embed(texts, *, purpose="embed"):
        calls.append(len(texts))
        return fake_embed(texts)

    monkeypatch.setattr(knowledge, "embed", counting_embed)
    return calls


def _quiet(_msg: str) -> None:
    pass


def _chunks_for(source: str) -> list[dict]:
    got = knowledge.get_collection().get(where={"source": source}, include=["metadatas"])
    return got["metadatas"]


# ---------------------------------------------------------------- chunking
def test_chunk_sizes_follow_token_approximation():
    assert words_per_chunk() == int(800 / 1.3) == 615
    assert overlap_words() == int(100 / 1.3) == 76


def test_chunking_overlap_and_sizes():
    words = [f"w{i}" for i in range(1500)]
    chunks = chunk_page_text(" ".join(words))
    size, overlap = words_per_chunk(), overlap_words()
    step = size - overlap
    assert [len(c.split()) for c in chunks] == [615, 615, 1500 - 2 * step]
    first, second = chunks[0].split(), chunks[1].split()
    assert first[-overlap:] == second[:overlap]             # neighbours share exactly `overlap` words
    assert second[0] == f"w{step}"
    assert chunks[-1].split()[-1] == "w1499"                  # nothing lost at the end


def test_chunking_short_and_empty_pages():
    assert chunk_page_text("just a few words") == ["just a few words"]
    assert chunk_page_text("   \n ") == []
    assert len(chunk_page_text(" ".join(["x"] * 615))) == 1  # exactly one full chunk, no tiny tail
    with pytest.raises(ValueError):
        chunk_page_text("a b c", size=5, overlap=5)


def test_distance_to_score_range():
    assert distance_to_score(0.0) == 1.0
    assert distance_to_score(0.25) == pytest.approx(0.75)
    assert distance_to_score(1.7) == 0.0


@needs_tesseract
def test_ingest_gives_correct_pages_and_metadata(kb, mock_embed):
    write_sops(kb)
    report = ingest_folder(progress=_quiet)

    assert (report.files, report.pages, report.chunks) == (5, TOTAL_PAGES, TOTAL_PAGES)
    assert report.failed_files == []
    for name, pages in SOPS.items():
        metas = sorted(_chunks_for(name), key=lambda m: m["page"])
        assert [m["page"] for m in metas] == list(range(1, len(pages) + 1))
        assert all(m["chunk_index"] == 0 and len(m["file_hash"]) == 64 for m in metas)
    # the scanned SOP went through OCR and its text is searchable
    ocr_hits = knowledge.search("evacuate upwind 10 ppm", top_k=1)
    assert ocr_hits[0].source == SCANNED_SOP and ocr_hits[0].page == 2
    assert "upwind" in ocr_hits[0].text.lower()


def test_ingest_prints_progress_in_batches(kb, mock_embed):
    long_page = " ".join(f"word{i}" for i in range(20 * 540))   # ~20 chunks on one page
    _write_text_pdf(kb / "long.pdf", ["short page one"])
    (kb / "notes.txt").write_text(long_page, encoding="utf-8")
    messages: list[str] = []
    report = ingest_folder(progress=messages.append, batch_size=16)
    assert report.chunks == 1 + len(chunk_page_text(long_page))
    assert "file 2/2, chunk 16/" in " ".join(messages)
    assert max(mock_embed) <= 16


# ---------------------------------------------------------------- idempotency / updates
@needs_tesseract
def test_ingest_twice_no_duplicates(kb, mock_embed):
    write_sops(kb)
    first = ingest_folder(progress=_quiet)
    count = knowledge.get_collection().count()
    calls_after_first = len(mock_embed)

    second = ingest_folder(progress=_quiet)

    assert knowledge.get_collection().count() == count == first.chunks == second.chunks
    assert second.skipped_files == 5 and second.embedded == 0
    assert len(mock_embed) == calls_after_first               # nothing re-embedded


def test_changed_file_replaces_old_chunks(kb, mock_embed):
    path = kb / "SOP-X.pdf"
    _write_text_pdf(path, ["Old procedure page one about pumps.", "Old page two about valves."])
    ingest_folder(progress=_quiet)
    old_ids = set(knowledge.get_collection().get(where={"source": "SOP-X.pdf"})["ids"])

    _write_text_pdf(path, ["New procedure revision B about compressors only."])
    report = ingest_folder(progress=_quiet)

    metas = _chunks_for("SOP-X.pdf")
    new_ids = set(knowledge.get_collection().get(where={"source": "SOP-X.pdf"})["ids"])
    assert len(metas) == 1 and metas[0]["page"] == 1
    assert old_ids.isdisjoint(new_ids)
    assert report.embedded == 1
    assert "compressors" in knowledge.search("compressors", top_k=1)[0].text


def test_removed_file_loses_chunks(kb, mock_embed):
    _write_text_pdf(kb / "keep.pdf", ["Keep this procedure on boilers."])
    _write_text_pdf(kb / "drop.pdf", ["Drop this procedure on cranes."])
    ingest_folder(progress=_quiet)
    assert knowledge.stats().documents == 2

    (kb / "drop.pdf").unlink()
    report = ingest_folder(progress=_quiet)

    assert report.removed_files == ["drop.pdf"]
    assert _chunks_for("drop.pdf") == []
    assert knowledge.stats().documents == 1 and knowledge.stats().chunks == 1


def test_bad_file_does_not_stop_others(kb, mock_embed):
    (kb / "broken.pdf").write_bytes(b"%PDF-1.4 this is not really a pdf")
    _write_text_pdf(kb / "good.pdf", ["A good procedure on scaffolding inspection."])
    messages: list[str] = []
    report = ingest_folder(progress=messages.append)
    assert report.failed_files == ["broken.pdf"]
    assert report.files == 1 and knowledge.stats().chunks == 1
    assert any(m.startswith("WARNING") and "broken.pdf" in m for m in messages)


# ---------------------------------------------------------------- empty KB / guard
def test_empty_kb_search_and_stats(kb, mock_embed):
    assert knowledge.search("anything at all", top_k=4) == []
    assert knowledge.search("   ") == []
    kb_stats = knowledge.stats()
    assert (kb_stats.documents, kb_stats.chunks) == (0, 0)
    assert mock_embed == []                                    # no embedding needed for an empty KB
    report = ingest_folder(progress=_quiet)                    # empty folder: nothing to do, no crash
    assert (report.files, report.chunks) == (0, 0)


def test_missing_kb_folder_is_empty_not_a_crash(kb, mock_embed, tmp_path):
    report = ingest_folder(kb_dir=tmp_path / "does-not-exist", progress=_quiet)
    assert report.files == 0 and knowledge.stats().chunks == 0


def test_guard_embedding_function_raises():
    with pytest.raises(EmbeddingGuardError):
        GuardEmbeddingFunction()(["some text"])


def test_collection_never_embeds_by_itself(kb, mock_embed):
    collection = knowledge.get_collection()
    with pytest.raises(Exception, match="Chroma tried to compute embeddings"):
        collection.add(ids=["x"], documents=["no embeddings given"])
    with pytest.raises(Exception, match="Chroma tried to compute embeddings"):
        collection.query(query_texts=["no embeddings given"], n_results=1)
    assert collection.configuration["hnsw"]["space"] == "cosine"


def test_guard_survives_reopening_from_disk(kb, mock_embed):
    knowledge.get_collection()
    knowledge.reset_client()
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    client = chromadb.PersistentClient(path=str(settings.WB_CHROMA_DIR), settings=ChromaSettings(anonymized_telemetry=False))
    reopened = client.get_collection(knowledge.COLLECTION_NAME)     # no embedding_function passed
    with pytest.raises(Exception, match="Chroma tried to compute embeddings"):
        reopened.query(query_texts=["x"], n_results=1)


# ---------------------------------------------------------------- API
def test_api_kb_stats_and_search(kb, mock_embed):
    _write_text_pdf(kb / "SOP-A.pdf", ["Crane lifting plan must be signed by the lifting supervisor."])
    ingest_folder(progress=_quiet)
    from backend.main import app

    with TestClient(app) as client:
        stats = KBStats.model_validate(client.get(f"{API_PREFIX}/kb/stats").json())
        assert (stats.documents, stats.chunks) == (1, 1)
        resp = client.post(f"{API_PREFIX}/kb/search", json={"query": "lifting supervisor crane", "top_k": 3})
        assert resp.status_code == 200
        hits = KBSearchResponse.model_validate(resp.json()).hits
        assert hits[0].source == "SOP-A.pdf" and hits[0].page == 1 and 0 < hits[0].score <= 1
        assert client.get(f"{API_PREFIX}/health").json()["kb_chunks"] == 1


def test_api_kb_search_model_down_is_api_error(kb, monkeypatch):
    from backend.llm_client import LLMError
    from backend.main import app

    monkeypatch.setattr(knowledge, "embed", fake_embed)
    _write_text_pdf(kb / "SOP-A.pdf", ["Some text."])
    ingest_folder(progress=_quiet)

    def down(texts, *, purpose="embed"):
        raise LLMError("MODEL_UNAVAILABLE", "ollama down")

    monkeypatch.setattr(knowledge, "embed", down)
    with TestClient(app) as client:
        resp = client.post(f"{API_PREFIX}/kb/search", json={"query": "text"})
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "MODEL_UNAVAILABLE"


# ---------------------------------------------------------------- live accuracy (bge-m3)
def _ollama_up() -> bool:
    try:
        return httpx.get(f"{settings.OLLAMA_HOST}/api/tags", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.slow
@needs_tesseract
@pytest.mark.skipif(not _ollama_up(), reason="Ollama not reachable")
def test_live_retrieval_accuracy(kb, network_guard):
    write_sops(kb)
    report = ingest_folder(progress=_quiet)
    assert report.chunks == TOTAL_PAGES

    rows, found = [], 0
    for question, source, page in QUESTIONS:
        hits = knowledge.search(question, top_k=4)
        rank = next((i + 1 for i, h in enumerate(hits) if h.source == source and h.page == page), None)
        found += rank is not None
        rows.append((question, f"{source.split('_', 1)[1].removesuffix('.pdf')} p{page}", rank, hits[0].score))

    print(f"\ningest: {report.chunks} chunks, {report.embed_seconds:.1f} s embedding "
          f"({report.ms_per_chunk:.0f} ms/chunk), {report.seconds:.1f} s total incl. OCR")
    print(f"{'question':<78} {'expected':<26} {'rank':>4} {'top score':>9}")
    for question, expected, rank, top in rows:
        print(f"{question:<78} {expected:<26} {rank if rank else '-':>4} {top:>9.3f}")
    print(f"found in top 4: {found}/10; external connection attempts: {len(network_guard)}")
    assert found >= 8
