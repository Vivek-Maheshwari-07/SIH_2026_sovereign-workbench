"""
Ingest every document in data/kb/ (WB_KB_DIR) into the local knowledge base.

    python scripts/ingest.py

Safe to run again: unchanged files are skipped, changed files replace their
old chunks, files removed from the folder lose their chunks. Needs Ollama
(bge-m3 embeddings) on 127.0.0.1; nothing else leaves the machine.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.settings import settings  # noqa: E402
from backend.tools.knowledge import ingest_folder, stats  # noqa: E402


def main() -> int:
    print(f"Ingesting {settings.WB_KB_DIR} ...")
    report = ingest_folder()
    kb = stats()
    print()
    print(f"files:  {report.files} ({report.skipped_files} unchanged, {len(report.failed_files)} failed)")
    print(f"pages:  {report.pages}")
    print(f"chunks: {report.chunks} ({report.embedded} embedded this run)")
    if report.removed_files:
        print(f"removed: {', '.join(report.removed_files)}")
    if report.ms_per_chunk is not None:
        print(f"embedding: {report.ms_per_chunk:.0f} ms per chunk")
    print(f"time:   {report.seconds:.1f} s")
    print(f"knowledge base now: {kb.documents} documents, {kb.chunks} chunks ({kb.embed_model})")
    if report.failed_files:
        print(f"WARNING: failed files: {', '.join(report.failed_files)}")
    return 1 if report.failed_files and report.files == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
