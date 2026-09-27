"""
The fixed tool set the agent model can call (ticket A8).

Big results (document text, KB hits, drafted notes) live in a per-task
Scratchpad; the model only ever sees a short summary plus an id such as
"doc_1" or "kb_2", cut to TOOL_RESULT_MAX_CHARS, so the context window
(WB_NUM_CTX) never overflows.

Every tool call emits tool_call + tool_result events, and every file produced
emits an artifact event (keys as in shared.contracts).
"""
from __future__ import annotations

import ast
import csv
import io
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from backend import llm_client
from backend.audit import write_audit_record
from backend.file_store import file_store
from backend.flows import code_flow
from backend.registry import registry
from backend.router import TABLE_SUFFIXES
from backend.settings import settings
from backend.tools import findings, knowledge, office
from backend.tools.documents import (
    PID_FAST_METHODS,
    PID_PROMPT_EXAMPLES,
    PID_TILE_NAMES,
    DocumentExtractionError,
    PageResult,
    extract,
    pid_path_note,
    vision_png,
)
from backend.tools.sandbox import SandboxError, run_in_sandbox
from shared.contracts import (
    ApprovalNote,
    Artifact,
    CodeResult,
    ArtifactKind,
    ErrorInfo,
    EventType,
    KBHit,
    PidTag,
    PidTagList,
    Severity,
    TaskType,
)

# ---- named constants (no .env key exists for these)
TOOL_RESULT_MAX_CHARS = 1500        # longest tool result text sent back to the model
DOC_EXCERPT_CHARS = 700             # document excerpt shown to the model by read_document
KB_SNIPPET_CHARS = 200              # per-hit snippet shown by search_knowledge
MIN_KB_SCORE = 0.57                 # hits below this are never shown or cited (a CUI query hit unrelated hot-work pages at 0.544)
DOC_PROMPT_MAX_CHARS = 14000        # document text given to draft_approval_note (fits WB_NUM_CTX 8192)
MAX_TOP_K = 10
MAX_FILLED_SOP_REFS = 3             # retrieved passages cited when the model cites none
ANSWER_TOP_K = 4                    # KB hits searched by answer_question / create_document
ANSWER_MAX_PASSAGES = 3             # SOP passages given to them (each ~600 tokens of prompt on CPU)
ANSWER_SOURCE_CHARS = 2500          # per SOP passage
ANSWER_DOC_CHARS = 9000             # attached-document text, shared between the documents
ANSWER_TIMEOUT_S = 300              # these calls have long prompts; still capped by the task deadline
TABLE_INPUT, TABLE_OUTPUT, TABLE_SCRIPT = "input.csv", "output.csv", "analysis.py"
TABLE_MAX_ROWS = 20000              # rows read from a CSV/Excel file by analyze_table
TABLE_PREVIEW_ROWS = 5              # rows shown to the coder model
TABLE_ERROR_CHARS = 1200            # tail of a failed run fed back to the coder model
MAX_KB_QUERIES = findings.MAX_KB_QUERIES   # search_knowledge with `queries`: most searches per call
# The approval note must come out the same every run (severities were High in some runs and Medium
# in others at WB_TEMPERATURE 0.2). Greedy decoding + a fixed seed, for draft_approval_note only.
NOTE_TEMPERATURE = 0.0
NOTE_SEED = 42
NOTE_OPTIONS = {"temperature": NOTE_TEMPERATURE, "seed": NOTE_SEED}
# With 3 SOP passages the note prompt is ~2,400 tokens; a first (uncached) call measured 203-215 s on
# the demo laptop, over WB_LLM_TIMEOUT_S (180 s). That timed out, and the retry wasted ~180 s per run.
# The note call alone gets this longer timeout (never shorter than WB_LLM_TIMEOUT_S).
NOTE_TIMEOUT_S = 420
AUDIT_ARGS_CHARS = 300              # tool arguments kept in the audit record
# Seen live: after the Excel file was saved the 4B model wandered off into run_code_task and timed out.
NEXT_FINISH = "The file is ready. Next step: call finish with a short answer naming the file."

# Tag letter prefix -> (equipment type to use, words that count as agreeing with it).
# Common ISA / plant conventions; extend here. Unknown prefixes keep the model's answer.
# Instrument types name what is measured (wording as in demo/expected.md: "Flow transmitter", ...).
# GENERIC_INSTRUMENT_WORDS alone ("Instrument", "Transmitter") agree but say too little: the
# specific type from the prefix replaces them (see check_tag_types).
_INSTRUMENT = ("instrument",)
GENERIC_INSTRUMENT_WORDS = frozenset({"instrument", "transmitter", "indicator", "gauge", "controller"})
MEASURED_VARIABLES = frozenset({"flow", "pressure", "level", "temperature"})
TAG_TYPE_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "P": ("Pump", ("pump",)),
    "V": ("Vessel", ("vessel", "drum", "separator", "receiver")),
    "T": ("Tank", ("tank",)),
    "E": ("Heat exchanger", ("exchanger", "cooler", "heater", "condenser", "reboiler")),
    "C": ("Column", ("column", "tower")),
    "K": ("Compressor", ("compressor",)),
    "FT": ("Flow transmitter", ("flow", "transmitter") + _INSTRUMENT),
    "FI": ("Flow indicator", ("flow", "indicator", "gauge") + _INSTRUMENT),
    "FIC": ("Flow indicating controller", ("flow", "controller") + _INSTRUMENT),
    "FV": ("Flow control valve", ("valve",)),
    "PT": ("Pressure transmitter", ("pressure", "transmitter") + _INSTRUMENT),
    "PI": ("Pressure indicator", ("pressure", "indicator", "gauge") + _INSTRUMENT),
    "PIC": ("Pressure indicating controller", ("pressure", "controller") + _INSTRUMENT),
    "PV": ("Pressure control valve", ("valve",)),
    "LT": ("Level transmitter", ("level", "transmitter") + _INSTRUMENT),
    "LI": ("Level indicator", ("level", "indicator", "gauge") + _INSTRUMENT),
    "LIC": ("Level indicating controller", ("level", "controller") + _INSTRUMENT),
    "TT": ("Temperature transmitter", ("temperature", "transmitter") + _INSTRUMENT),
    "TI": ("Temperature indicator", ("temperature", "indicator", "gauge") + _INSTRUMENT),
    "TIC": ("Temperature indicating controller", ("temperature", "controller") + _INSTRUMENT),
    "PSV": ("Pressure safety valve", ("safety", "relief", "psv")),
    "PRV": ("Pressure relief valve", ("relief", "safety", "prv")),
    "LV": ("Level control valve", ("valve",)),
    "TV": ("Temperature control valve", ("valve",)),
    "XV": ("Shutdown valve", ("valve",)),
    "SDV": ("Shutdown valve", ("valve",)),
    "HV": ("Hand valve", ("valve",)),
}
_TAG_PREFIX_RE = re.compile(r"\s*([A-Za-z]+)")

EmitFn = Callable[..., None]


class ToolError(Exception):
    """A tool failed in an expected way; the message goes back to the model."""

    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ scratchpad
@dataclass
class DocEntry:
    doc_id: str
    file_id: str
    filename: str
    kind: str
    pages: list[PageResult]

    def full_text(self) -> str:
        return "\n\n".join(f"--- Page {p.page} ---\n{p.text}" for p in self.pages)

    def page_numbers(self) -> set[int]:
        return {p.page for p in self.pages}


@dataclass
class Scratchpad:
    docs: dict[str, DocEntry] = field(default_factory=dict)
    kb_hits: dict[str, KBHit] = field(default_factory=dict)
    notes: dict[str, ApprovalNote] = field(default_factory=dict)
    tag_lists: dict[str, PidTagList] = field(default_factory=dict)
    code_results: dict[str, CodeResult] = field(default_factory=dict)
    answers: dict[str, str] = field(default_factory=dict)      # ans_1: answer_question / inspect_image text
    _counters: dict[str, int] = field(default_factory=dict)

    def new_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}_{self._counters[prefix]}"


@dataclass
class ToolContext:
    task_id: str
    emit: EmitFn                                   # TaskHandle.emit(event_type, title, data, step=...)
    add_artifact: Callable[[Artifact], None]
    scratchpad: Scratchpad = field(default_factory=Scratchpad)
    step: Optional[int] = None
    task_message: str = ""                         # the user's original request (run_code_task passes it on)
    artifacts: list[Artifact] = field(default_factory=list)

    def event(self, event_type: EventType, title: str, data: dict[str, Any]) -> None:
        self.emit(event_type, title, data, step=self.step)

    def warn(self, text: str) -> None:
        self.event(EventType.LOG, "Warning", {"level": "warn", "text": text})

    def artifact(self, artifact: Artifact) -> None:
        self.artifacts.append(artifact)
        self.add_artifact(artifact)
        self.event(EventType.ARTIFACT, artifact.filename, {"artifact": artifact.model_dump(mode="json")})


@dataclass
class ToolOutcome:
    ok: bool
    summary: str
    finish_answer: Optional[str] = None
    error_code: Optional[str] = None               # ERROR_CODES key when ok is False


# ------------------------------------------------------------------ helpers
def trim(text: str, limit: int = TOOL_RESULT_MAX_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 15].rstrip() + " ...[truncated]"


def _one_line(text: str, limit: int) -> str:
    return trim(" ".join(text.split()), limit)


def _llm_json(ctx: ToolContext, schema, messages: list[dict[str, Any]], purpose: str,
              options: Optional[dict[str, Any]] = None, timeout_s: Optional[float] = None):
    model = registry.model_for_task(TaskType.DOCUMENT)
    result = llm_client.chat_json_meta(model.ollama_name, messages, schema, purpose=purpose, options=options,
                                       timeout_s=timeout_s)
    ctx.event(EventType.LLM_CALL, f"{model.id}: {purpose}", {
        "model_id": model.id, "purpose": purpose, "duration_ms": result.duration_ms, "tokens_out": result.tokens_out,
    })
    return result.value


def _doc(ctx: ToolContext, doc_id: str) -> DocEntry:
    entry = ctx.scratchpad.docs.get(doc_id)
    if entry is None:
        known = ", ".join(ctx.scratchpad.docs) or "none yet (call read_document first)"
        raise ToolError(f"unknown doc_id {doc_id!r}; known documents: {known}")
    return entry


# ------------------------------------------------------------------ tools
def read_document(ctx: ToolContext, file_id: str, kind: str = "auto") -> ToolOutcome:
    ref = file_store.get_ref(file_id)
    if ref is None:
        raise ToolError(f"unknown file_id {file_id!r}; use one of the file_ids listed in the task")
    pages = extract(file_id, kind=kind).pages
    doc_id = ctx.scratchpad.new_id("doc")
    entry = DocEntry(doc_id=doc_id, file_id=file_id, filename=ref.filename, kind=kind, pages=pages)
    ctx.scratchpad.docs[doc_id] = entry
    note = pid_path_note(pages) if kind == "pid" else None
    if note:
        ctx.event(EventType.LOG, "P&ID read path", {"level": "info", "text": note})
    text = "\n".join(p.text for p in pages)
    methods = ", ".join(sorted({p.method for p in pages}))
    unit = "tiles" if kind == "pid" else "pages"
    failed = [p.tile or str(p.page) for p in pages if p.error]
    summary = (f"{doc_id}: {ref.filename}, {len(pages)} {unit}, {len(text.split())} words (read by {methods})."
               + (f" Failed {unit}: {', '.join(failed)}." if failed else "")
               + f"\nExcerpt: {_one_line(text, DOC_EXCERPT_CHARS)}")
    if not text.strip():
        return ToolOutcome(ok=False, summary=summary + "\nNo text could be read from this file.",
                           error_code="UNSUPPORTED_FILE")
    return ToolOutcome(ok=True, summary=summary)


def _search_many(ctx: ToolContext, queries: list[str], top_k: int) -> ToolOutcome:
    """Search each query, merge unique hits (best score kept), keep only hits >= MIN_KB_SCORE."""
    merged: dict[tuple[str, Optional[int], str], KBHit] = {}
    report = []
    for number, query in enumerate(queries, start=1):
        hits = knowledge.search(query, top_k)
        best = hits[0] if hits else None
        passed = sum(h.score >= MIN_KB_SCORE for h in hits)
        report.append(f"q{number} best {best.score:.3f} {best.source} p.{best.page} ({passed} >= {MIN_KB_SCORE:.2f}): "
                      f"{_one_line(query, 90)}" if best else f"q{number} no hits: {_one_line(query, 90)}")
        for hit in hits:
            key = (hit.source, hit.page, hit.text)
            if hit.score >= MIN_KB_SCORE and (key not in merged or hit.score > merged[key].score):
                merged[key] = hit
    ctx.event(EventType.LOG, "Knowledge base queries", {"level": "info", "text": "\n".join(report)})
    if not merged:
        return ToolOutcome(ok=True, summary=(
            f"No relevant SOP passages found for {len(queries)} queries; minimum score is {MIN_KB_SCORE:.2f}. "
            "Do not cite any SOP for this."))
    lines = []
    for hit in sorted(merged.values(), key=lambda h: h.score, reverse=True):
        kb_id = ctx.scratchpad.new_id("kb")
        ctx.scratchpad.kb_hits[kb_id] = hit
        page = f" p.{hit.page}" if hit.page else ""
        lines.append(f"{kb_id}: {hit.source}{page} (score {hit.score:.2f}): {_one_line(hit.text, KB_SNIPPET_CHARS // 2)}")
    return ToolOutcome(ok=True, summary=f"{len(merged)} relevant passage(s) from {len(queries)} queries:\n"
                                        + "\n".join(lines))


def search_knowledge(ctx: ToolContext, query: str, top_k: int = 4,
                     queries: Optional[list[str]] = None) -> ToolOutcome:
    top_k = max(1, min(int(top_k), MAX_TOP_K))
    all_queries = list(dict.fromkeys(q.strip() for q in [query, *(queries or [])] if isinstance(q, str) and q.strip()))
    if len(all_queries) > 1:
        return _search_many(ctx, all_queries[:MAX_KB_QUERIES], top_k)
    hits = knowledge.search(query, top_k)
    relevant = [h for h in hits if h.score >= MIN_KB_SCORE]
    if not relevant:
        best = f" (best score {hits[0].score:.2f})" if hits else ""
        return ToolOutcome(ok=True, summary=(
            f"No relevant SOP passages found for {query!r}{best}; minimum score is {MIN_KB_SCORE:.2f}. "
            "Do not cite any SOP for this."))
    lines = []
    for hit in relevant:
        kb_id = ctx.scratchpad.new_id("kb")
        ctx.scratchpad.kb_hits[kb_id] = hit
        page = f" p.{hit.page}" if hit.page else ""
        lines.append(f"{kb_id}: {hit.source}{page} (score {hit.score:.2f}): {_one_line(hit.text, KB_SNIPPET_CHARS)}")
    ignored = len(hits) - len(relevant)
    tail = f"\n({ignored} weaker hit(s) below {MIN_KB_SCORE:.2f} ignored.)" if ignored else ""
    return ToolOutcome(ok=True, summary="\n".join(lines) + tail)


_NOTE_SYSTEM = """You draft an inspection approval note as JSON for human review.
Use ONLY facts from the inspection document. Rules:
- ref_no and date: write empty strings; the system fills them.
- findings: one per row of the report's findings table, in the same order; do not merge or split rows.
  item: the row's item text. observation: the row's observation with its numbers.
  severity: copy it EXACTLY as written in that row's Severity column (low, medium, high or critical);
  never raise or lower it. Only if the report states no severity, judge it yourself.
  source_page is the page number (from the '--- Page N ---' markers) where the finding is written.
- sop_references: for every SOP passage below that supports a finding or the recommendation
  (for example repair criteria or severity rules), cite it exactly as written in its [brackets],
  e.g. "SOP-X.pdf, p.2". Cite nothing that is not listed below. If no passages are given, use [].
- cost_implication: copy an amount only if the document states it; otherwise null. Never invent money.
- recommendation: one or two sentences."""


def _check_sop_references(ctx: ToolContext, note: ApprovalNote, hits: list[KBHit]) -> list[str]:
    by_file: dict[str, list[KBHit]] = {}
    for hit in hits:
        by_file.setdefault(hit.source.lower(), []).append(hit)
        by_file.setdefault(hit.source.rsplit(".", 1)[0].lower(), []).append(hit)
    kept: list[str] = []
    for ref in note.sop_references:
        document, page = office.split_sop_reference(ref)
        matches = by_file.get(document.strip().lower())
        if not matches:
            ctx.warn(f"Dropped SOP reference {ref!r}: it is not one of the retrieved SOP passages.")
            continue
        pages = [h.page for h in matches if h.page]
        if page != "-" and pages and int(page) not in pages:
            ctx.warn(f"SOP reference {ref!r}: page {page} was not retrieved; using p.{pages[0]}.")
            page = str(pages[0])
        elif page == "-" and pages:
            page = str(pages[0])
        entry = f"{matches[0].source}, p.{page}" if page != "-" else matches[0].source
        if entry not in kept:
            kept.append(entry)
    return kept


def check_against_report(ctx: ToolContext, note: ApprovalNote, text: str) -> None:
    """
    The report states each finding's item and severity; the model must not change them. For every
    finding that matches a row of the report's findings table: use the row's severity (warn on change)
    and the row's item text (so the Word table is tidy; the model's observation is kept).
    Also warn about report rows that no finding refers to.
    """
    rows = findings.parse_finding_rows(text)
    if not rows:
        return
    used: set[int] = set()
    for finding in note.findings:
        row = findings.match_row(finding.item, rows) or findings.match_row(finding.observation, rows)
        if row is None:
            continue
        used.add(id(row))
        if row.severity != finding.severity:
            ctx.warn(f"Finding {finding.item!r}: model said {finding.severity.value}, the report says "
                     f"{row.severity.value}; using the report's severity.")
            finding.severity = row.severity
        finding.item = findings.row_item(row, finding.observation) or finding.item
    for row in rows:
        if id(row) not in used:
            ctx.warn(f"Report finding not in the note: {_one_line(row.text, 80)!r} ({row.severity.value}).")


def _check_cost(ctx: ToolContext, note: ApprovalNote, source_text: str) -> None:
    if note.cost_implication and office.cost_text(note.cost_implication, source_text) == office.COST_PLACEHOLDER:
        ctx.warn(f"Cost {_one_line(note.cost_implication, 160)!r} has an amount that is not in the report; "
                 f"replaced with {office.COST_PLACEHOLDER!r}.")


def _check_source_pages(ctx: ToolContext, note: ApprovalNote, doc: DocEntry) -> None:
    valid = doc.page_numbers()
    for finding in note.findings:
        if finding.source_page is not None and finding.source_page not in valid:
            ctx.warn(f"Finding {finding.item!r}: page {finding.source_page} does not exist in {doc.filename} "
                     f"(pages {min(valid)}-{max(valid)}); source page cleared.")
            finding.source_page = None


def draft_approval_note(ctx: ToolContext, doc_id: str, kb_ref_ids: Optional[list[str]] = None) -> ToolOutcome:
    doc = _doc(ctx, doc_id)
    kb_ref_ids = list(kb_ref_ids or [])
    unknown = [k for k in kb_ref_ids if k not in ctx.scratchpad.kb_hits]
    if unknown:
        ctx.warn(f"Ignored unknown knowledge ids: {', '.join(unknown)}")
    hits = [ctx.scratchpad.kb_hits[k] for k in kb_ref_ids if k in ctx.scratchpad.kb_hits]
    sop_text = "\n\n".join(f"[{h.source}, p.{h.page}]\n{h.text}" for h in hits) or "(none)"
    messages = [
        {"role": "system", "content": _NOTE_SYSTEM},
        {"role": "user", "content": f"Inspection document ({doc.filename}):\n{doc.full_text()[:DOC_PROMPT_MAX_CHARS]}"
                                    f"\n\nSOP passages you may cite:\n{sop_text}"},
    ]
    note: ApprovalNote = _llm_json(ctx, ApprovalNote, messages, "draft_approval_note", options=NOTE_OPTIONS,
                                   timeout_s=max(NOTE_TIMEOUT_S, settings.WB_LLM_TIMEOUT_S))
    check_against_report(ctx, note, doc.full_text())
    _check_cost(ctx, note, doc.full_text())
    note.sop_references = _check_sop_references(ctx, note, hits)
    sop_auto = False
    if hits and not note.sop_references:
        # Small models often leave the list empty; cite the passages that were chosen as relevant.
        chosen = sorted(hits, key=lambda h: h.score, reverse=True)[:MAX_FILLED_SOP_REFS]
        note.sop_references = list(dict.fromkeys(f"{h.source}, p.{h.page}" if h.page else h.source for h in chosen))
        sop_auto = True
        ctx.event(EventType.LOG, "SOP references filled", {"level": "info", "text": (
            "The model cited no SOP; cited the retrieved passages instead: " + "; ".join(note.sop_references))})
    _check_source_pages(ctx, note, doc)

    artifact = office.make_word(note, source_text=doc.full_text(), task_id=ctx.task_id, sop_auto=sop_auto)
    ctx.artifact(artifact)
    note_id = ctx.scratchpad.new_id("note")
    ctx.scratchpad.notes[note_id] = note
    high = sum(f.severity in (Severity.HIGH, Severity.CRITICAL) for f in note.findings)
    ref_no = artifact.filename.split("_", 1)[0]
    return ToolOutcome(ok=True, summary=(
        f"{note_id}: approval note {ref_no} saved as {artifact.filename}: {len(note.findings)} findings "
        f"({high} high/critical), {len(note.sop_references)} SOP reference(s). "
        f"Recommendation: {_one_line(note.recommendation, 200)} {NEXT_FINISH}"))


_PID_SYSTEM = """You list every equipment and instrument tag seen on a P&ID drawing, as JSON.
The drawing was read in tiles; each tile's text is marked '--- Tile N (name) ---'.
For each tag give: tag (exactly as written, e.g. P-101A), equipment_type (Pump, Valve, Vessel,
Tank, Instrument, Heat exchanger, Line or Other), a short description if the text gives one, and tile = the tile number N.
List a tag once per tile where it appears. Do not invent tags that are not in the text."""


def tag_prefix(tag: str) -> str:
    """Letter prefix of a tag: 'FIC-101' -> 'FIC', 'p-101a' -> 'P'."""
    match = _TAG_PREFIX_RE.match(tag or "")
    return match.group(1).upper() if match else ""


def check_tag_types(ctx: ToolContext, tag_list: PidTagList) -> None:
    """
    Check the model's equipment type against the tag's letter prefix (TAG_TYPE_RULES):
      - agrees with a specific word (e.g. "Flow transmitter", "Mass flow meter" for FT): kept;
      - only generic instrument words ("Instrument", "Transmitter"): replaced by the specific
        prefix type ("Flow transmitter"), silently, since it is a refinement, not a disagreement;
      - clearly disagrees (no accepted word, or another measured variable such as "Pressure
        transmitter" on an LT tag): replaced by the prefix type, with a warning.
    """
    for tag in tag_list.tags:
        rule = TAG_TYPE_RULES.get(tag_prefix(tag.tag))
        if rule is None:
            continue                                     # unknown prefix: keep the model's answer
        canonical, accepted = rule
        given = (tag.equipment_type or "").lower()
        specific = [w for w in accepted if w not in GENERIC_INSTRUMENT_WORDS]
        other_variables = [v for v in MEASURED_VARIABLES - set(accepted) if v in given] \
            if MEASURED_VARIABLES & set(accepted) else []
        if any(word in given for word in specific) and not other_variables:
            continue                                     # agrees, at least as specific: keep it
        if any(word in given for word in accepted) and not other_variables:
            tag.equipment_type = canonical               # generic "Instrument" -> "Flow transmitter"
            continue
        ctx.warn(f"Tag {tag.tag!r}: model said {tag.equipment_type!r}, but prefix {tag_prefix(tag.tag)} "
                 f"means {canonical}; using {canonical}.")
        tag.equipment_type = canonical


def tag_type(tag: str) -> str:
    """Equipment type from the tag's letter prefix (TAG_TYPE_RULES); "Other" for unknown prefixes."""
    rule = TAG_TYPE_RULES.get(tag_prefix(tag))
    return rule[0] if rule else "Other"


def _tag_base(tag: str) -> str:
    """'P-201A' -> 'P-201' (drops the letter suffix)."""
    return tag[:-1] if tag and tag[-1].isalpha() and "-" in tag else tag


def _vision_only_tags(ctx: ToolContext, ocr_tags: set[str], vision_tags: list[str]) -> list[str]:
    """Tags the vision check read that OCR did not, minus prompt echoes and suffix-less variants of OCR tags."""
    ocr_bases = {_tag_base(t) for t in ocr_tags}
    added = []
    for tag in vision_tags:
        if tag in ocr_tags or tag in added:
            continue
        if tag in PID_PROMPT_EXAMPLES:
            ctx.warn(f"Vision check returned {tag!r}, an example from its prompt that OCR did not find; ignored.")
        elif tag in ocr_bases:
            ctx.warn(f"Vision check returned {tag!r}, a shorter form of an OCR tag; ignored.")
        else:
            added.append(tag)
    return added


def merge_pid_tags(ctx: ToolContext, pages: list[PageResult]) -> PidTagList:
    """
    Fast-path tag list (no LLM): OCR tags per tile, typed by prefix, plus tags only the
    vision check read. Each tag keeps its tile(s); the description says how it was found.
    """
    confirm = next((p for p in pages if p.method == "vision_confirm"), None)
    vision_tags = confirm.text.split() if confirm is not None else []
    ocr_tags = {t for p in pages if p.method == "ocr_tiles" for t in p.text.split()}
    checked = confirm is not None and not confirm.error
    tags: list[PidTag] = []
    for page in pages:
        if page.method != "ocr_tiles":
            continue
        tile = PID_TILE_NAMES.index(page.tile) + 1 if page.tile in PID_TILE_NAMES else None
        for tag in page.text.split():
            how = ("OCR, confirmed by vision" if tag in vision_tags else "OCR only (vision did not read it)") \
                if checked else "OCR (vision check unavailable)"
            tags.append(PidTag(tag=tag, equipment_type=tag_type(tag), description=how, tile=tile))
    for tag in _vision_only_tags(ctx, ocr_tags, vision_tags):
        tags.append(PidTag(tag=tag, equipment_type=tag_type(tag), tile=None,
                           description="Vision only (OCR did not find it); check on the drawing"))
    return PidTagList(tags=tags, notes=pid_path_note(pages))


def ensure_pid_read(ctx: ToolContext, doc: DocEntry) -> DocEntry:
    """
    A drawing read with kind="auto" is one whole-page OCR pass, which misses most small tags.
    Small models often forget kind="pid", so an image is re-read here in P&ID tile mode.
    """
    ref = file_store.get_ref(doc.file_id)
    if doc.kind == "pid" or ref is None or not ref.is_image:
        return doc
    doc.pages, doc.kind = extract(doc.file_id, kind="pid").pages, "pid"
    ctx.event(EventType.LOG, "Re-read as P&ID", {"level": "info", "text": (
        f"{doc.filename} was read as a plain image; re-read it in P&ID tile mode for the tag list.")})
    return doc


def extract_pid_tags(ctx: ToolContext, doc_id: str) -> ToolOutcome:
    doc = ensure_pid_read(ctx, _doc(ctx, doc_id))
    if any(p.method in PID_FAST_METHODS for p in doc.pages):
        return _save_tag_list(ctx, merge_pid_tags(ctx, doc.pages))
    blocks = []
    for number, page in enumerate(doc.pages, start=1):
        label = page.tile or f"page {page.page}"
        blocks.append(f"--- Tile {number} ({label}) ---\n{page.text}")
    messages = [{"role": "system", "content": _PID_SYSTEM},
                {"role": "user", "content": "\n\n".join(blocks)[:DOC_PROMPT_MAX_CHARS]}]
    tag_list: PidTagList = _llm_json(ctx, PidTagList, messages, "extract_pid_tags")

    tile_count = len(doc.pages)
    for tag in tag_list.tags:
        if tag.tile is not None and not 1 <= tag.tile <= tile_count:
            ctx.warn(f"Tag {tag.tag!r}: tile {tag.tile} does not exist; tile cleared.")
            tag.tile = None
    check_tag_types(ctx, tag_list)
    return _save_tag_list(ctx, tag_list)


def _save_tag_list(ctx: ToolContext, tag_list: PidTagList) -> ToolOutcome:
    artifact = office.make_excel(tag_list, task_id=ctx.task_id)
    ctx.artifact(artifact)
    list_id = ctx.scratchpad.new_id("tags")
    ctx.scratchpad.tag_lists[list_id] = tag_list
    return ToolOutcome(ok=True, summary=f"{list_id}: {artifact.preview} (saved as {artifact.filename}). {NEXT_FINISH}")


def run_code_task(ctx: ToolContext, request: str) -> ToolOutcome:
    def emit(event_type: EventType, title: str, data: dict[str, Any]) -> None:
        ctx.event(event_type, title, data)

    if ctx.task_message and ctx.task_message.strip() not in request:
        # Small models shorten the request; the coder must still see every requirement the user gave.
        request = f"{request}\n\nOriginal user request (follow it exactly):\n{ctx.task_message}"
    try:
        result = code_flow.run_code_task(request, emit=emit)
    except code_flow.CodeFlowError as exc:
        raise ToolError(f"{exc.code}: {exc}", code=exc.code) from exc
    ctx.scratchpad.code_results[ctx.scratchpad.new_id("code")] = result

    short = secrets.token_hex(3)
    for filename, text in ((f"solution_{short}.py", result.code), (f"test_solution_{short}.py", result.tests)):
        ctx.artifact(office.save_text_artifact(filename, text, ArtifactKind.PY, task_id=ctx.task_id))
    return ToolOutcome(ok=True, summary=(
        f"Code passed its tests: {result.passed} passed, {result.failed} failed, {result.attempts} attempt(s). "
        f"Printed calculation steps:\n{trim(result.stdout_tail, 800)}\n{NEXT_FINISH}"))


def final_answer(ctx: ToolContext, text: str) -> str:
    """
    The answer the user sees. Small models retell a grounded answer in their own words and drop its
    [n] citations; an uncited retelling must not replace the checked one. Without citations:
    if the task produced files, keep the model's text (it names them) and add the Sources list;
    otherwise show the grounded answer itself.
    """
    text = humanize_ids(ctx, text)
    if not ctx.scratchpad.answers or _CITATION_RE.search(text):
        return text
    grounded = list(ctx.scratchpad.answers.values())[-1]
    _, marker, sources = grounded.partition("\n\nSources")
    if not marker:
        return text            # an image answer has no citations to lose; keep the model's wording
    return f"{text}\n\nSources{sources}" if ctx.artifacts else grounded


def finish(ctx: ToolContext, answer: str) -> ToolOutcome:
    return ToolOutcome(ok=True, summary="Finished.", finish_answer=final_answer(ctx, answer.strip()))


# ------------------------------------------------------------------ general tools (evaluation pass)
# Grounded Q&A, Word/PowerPoint documents, spreadsheet analysis and photo inspection, so the agent
# is not limited to the three demo scenarios.
_ANSWER_SYSTEM = """You answer a plant engineer's question using ONLY the numbered sources below.
Rules:
- Write a clear answer in plain words: short paragraphs or bullet points, with the actual facts,
  numbers and limits the sources give.
- After each fact, cite its source number in square brackets, e.g. [1] or [2].
- If the sources do not answer the question, say so plainly; never guess or use outside knowledge.
- Keep it short: at most 180 words. Do not mention these rules."""

_OUTLINE_SYSTEM = """You write the content of a {kind} for plant engineers, as JSON.
Use the material below; keep facts, numbers and limits exactly as written there. If the material
does not cover a point the request asks for, leave it out rather than invent it.
Each section has a short heading and 2-6 bullets; each bullet is one short, complete sentence.
Write 3-6 sections. Do not add a sources section; the system adds it."""

_TABLE_SYSTEM = """You write ONE Python 3 script, analysis.py, for a plant engineer.
It runs offline in a sandbox that has only pandas and numpy.
Rules:
- Read the data with: df = pd.read_csv("input.csv")
- Do exactly what the request asks, using the column names exactly as listed.
- Write the result table with: result.to_csv("output.csv", index=False)
- Round computed percentages and ratios with round(x, 2) BEFORE comparing them with a threshold.
- Sort the result table by the main computed column, largest first.
- Print a short plain-text summary of the key numbers (at most 15 lines).
- No plots, no internet, no other files, no input().
Reply with the full script in one fenced python code block and nothing else."""

_IMAGE_PROMPT = """You are helping a refinery engineer. Look at this image and answer:
{question}
Describe only what you can actually see (equipment, condition, damage, readings, handwriting).
If something is unclear or not visible, say so. Answer in at most 8 short bullet points."""

_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_CITATION_RE = re.compile(r"\[(\d{1,2})\]")
_ID_RE = re.compile(r"\b(kb|doc|ans)_(\d+)\b")
DOC_FORMATS = {"docx": "docx", "word": "docx", "doc": "docx", "report": "docx",
               "pptx": "pptx", "ppt": "pptx", "powerpoint": "pptx", "slides": "pptx", "presentation": "pptx"}


class _Section(BaseModel):
    heading: str
    bullets: list[str] = Field(min_length=1, max_length=8)


class _Outline(BaseModel):
    """LLM output schema for create_document (internal)."""
    sections: list[_Section] = Field(min_length=1, max_length=8)


def _llm_text(ctx: ToolContext, messages: list[dict[str, Any]], purpose: str, *, task_type: TaskType = TaskType.DOCUMENT,
              images: Optional[list[bytes]] = None, timeout_s: Optional[float] = None) -> str:
    model = registry.model_for_task(task_type)
    start = time.monotonic()
    reply = llm_client.chat(model.ollama_name, messages, images=images, purpose=purpose, timeout_s=timeout_s)
    ctx.event(EventType.LLM_CALL, f"{model.id}: {purpose}", {
        "model_id": model.id, "purpose": purpose, "duration_ms": int((time.monotonic() - start) * 1000),
        "tokens_out": reply.tokens_out,
    })
    return reply.text.strip()


def hit_label(hit: KBHit) -> str:
    return f"{hit.source}, p.{hit.page}" if hit.page else hit.source


def relevant_hits(ctx: ToolContext, query: str, limit: int = ANSWER_MAX_PASSAGES) -> list[KBHit]:
    """Best passages for `query`: a fresh KB search plus earlier hits of this task, >= MIN_KB_SCORE, no duplicates."""
    found = [h for h in knowledge.search(query, ANSWER_TOP_K) if h.score >= MIN_KB_SCORE]
    merged: dict[tuple[str, Optional[int], str], KBHit] = {}
    for hit in found + list(ctx.scratchpad.kb_hits.values()):
        key = (hit.source, hit.page, hit.text)
        if key not in merged or hit.score > merged[key].score:
            merged[key] = hit
    return sorted(merged.values(), key=lambda h: h.score, reverse=True)[:limit]


def _chosen_docs(ctx: ToolContext, doc_ids: Optional[list[str]]) -> list[DocEntry]:
    return [_doc(ctx, d) for d in doc_ids] if doc_ids else list(ctx.scratchpad.docs.values())


def numbered_sources(docs: list[DocEntry], hits: list[KBHit]) -> list[tuple[str, str]]:
    """(label, text) per source, in the order they are numbered for the model; document text is shared out."""
    sources = [(d.filename, d.full_text()[:max(1000, ANSWER_DOC_CHARS // len(docs))]) for d in docs]
    sources += [(hit_label(h), h.text[:ANSWER_SOURCE_CHARS]) for h in hits]
    return sources


def cite_sources(answer: str, labels: list[str]) -> str:
    """Append the list of sources the answer cites as [n]; if it cites none, list every source it was given."""
    cited = sorted({int(n) for n in _CITATION_RE.findall(answer) if 1 <= int(n) <= len(labels)})
    if cited:
        return f"{answer}\n\nSources:\n" + "\n".join(f"[{n}] {labels[n - 1]}" for n in cited)
    if labels:
        return f"{answer}\n\nSources consulted: " + "; ".join(labels)
    return answer


def humanize_ids(ctx: ToolContext, text: str) -> str:
    """Replace our internal ids in the final answer: kb_2 -> 'file.pdf, p.3', doc_1 -> file name, ans_1 -> the answer."""
    def swap(match: re.Match) -> str:
        key, prefix = match.group(0), match.group(1)
        if prefix == "kb" and key in ctx.scratchpad.kb_hits:
            return hit_label(ctx.scratchpad.kb_hits[key])
        if prefix == "doc" and key in ctx.scratchpad.docs:
            return ctx.scratchpad.docs[key].filename
        if prefix == "ans" and key in ctx.scratchpad.answers:
            return ctx.scratchpad.answers[key]
        return key
    return _ID_RE.sub(swap, text)


def answer_question(ctx: ToolContext, question: str, doc_ids: Optional[list[str]] = None,
                    use_knowledge: bool = True) -> ToolOutcome:
    docs = _chosen_docs(ctx, doc_ids)
    hits = relevant_hits(ctx, question) if use_knowledge else []
    if not docs and not hits:
        return ToolOutcome(ok=True, summary=(
            f"No attached document and no SOP passage is relevant to this question (minimum score "
            f"{MIN_KB_SCORE:.2f}). Tell the user the offline knowledge base does not cover it."))
    sources = numbered_sources(docs, hits)
    listing = "\n\n".join(f"[{i}] {label}\n{text}" for i, (label, text) in enumerate(sources, start=1))
    messages = [{"role": "system", "content": _ANSWER_SYSTEM},
                {"role": "user", "content": f"Sources:\n\n{listing}\n\nQuestion: {question}"}]
    answer = cite_sources(_llm_text(ctx, messages, "answer_question", timeout_s=ANSWER_TIMEOUT_S),
                          [label for label, _ in sources])
    ans_id = ctx.scratchpad.new_id("ans")
    ctx.scratchpad.answers[ans_id] = answer
    return ToolOutcome(ok=True, summary=(
        f"{ans_id}:\n{trim(answer, 1100)}\nNext step: call finish with answer {ans_id!r} (or this text with its "
        "[n] citations and Sources list), or create_document if the user asked for a Word or PowerPoint file."))


def create_document(ctx: ToolContext, format: str, title: str, instructions: str = "",
                    doc_ids: Optional[list[str]] = None) -> ToolOutcome:
    kind = DOC_FORMATS.get(format.strip().lower())
    if kind is None:
        raise ToolError(f"format must be 'docx' (Word) or 'pptx' (PowerPoint), not {format!r}")
    docs = _chosen_docs(ctx, doc_ids)
    hits = relevant_hits(ctx, f"{title}. {instructions}".strip())
    material = [f"Answer already written:\n{a}" for a in ctx.scratchpad.answers.values()]
    material += [f"From {label}:\n{text}" for label, text in numbered_sources(docs, hits)]
    if not material:
        material = ["(No plant document covers this. Write general engineering guidance and say so in the first bullet.)"]
    name = "PowerPoint deck" if kind == "pptx" else "Word report"
    messages = [{"role": "system", "content": _OUTLINE_SYSTEM.format(kind=name)},
                {"role": "user", "content": f"Title: {title}\nRequest: {instructions or ctx.task_message}\n\n"
                                            + "\n\n".join(material)[:DOC_PROMPT_MAX_CHARS]}]
    outline: _Outline = _llm_json(ctx, _Outline, messages, "create_document", timeout_s=ANSWER_TIMEOUT_S)
    sections = [(s.heading, s.bullets) for s in outline.sections]
    labels = list(dict.fromkeys([d.filename for d in docs] + [hit_label(h) for h in hits]))
    if labels:
        sections.append(("Sources", labels))
    maker = office.make_ppt if kind == "pptx" else office.make_report
    artifact = maker(title, sections, task_id=ctx.task_id)
    ctx.artifact(artifact)
    return ToolOutcome(ok=True, summary=f"{name} saved as {artifact.filename}: {artifact.preview}. {NEXT_FINISH}")


def extract_python(text: str) -> str:
    """The first fenced python block of a reply (or the whole reply when it has no fence)."""
    match = _CODE_BLOCK_RE.search(text)
    return (match.group(1) if match else text).strip()


def check_table_script(code: str) -> list[str]:
    """Problems that make running the script pointless; an empty list means run it."""
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error on line {exc.lineno}: {exc.msg}"]
    problems = []
    if TABLE_INPUT not in code:
        problems.append(f'the script must read "{TABLE_INPUT}"')
    if TABLE_OUTPUT not in code:
        problems.append(f'the script must write the result table to "{TABLE_OUTPUT}"')
    return problems


def load_table(path: Path):
    """A CSV or the first sheet of an Excel file as a pandas DataFrame (at most TABLE_MAX_ROWS rows)."""
    import pandas as pd

    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, nrows=TABLE_MAX_ROWS)
    return pd.read_excel(path, sheet_name=0, nrows=TABLE_MAX_ROWS, engine="openpyxl")


def describe_table(df) -> str:
    columns = "\n".join(f"- {name} ({dtype})" for name, dtype in df.dtypes.astype(str).items())
    return (f"{TABLE_INPUT} has {len(df)} rows. Columns and types:\n{columns}\n\n"
            f"First rows:\n{df.head(TABLE_PREVIEW_ROWS).to_csv(index=False)}")


def csv_rows(data: bytes) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))))
    return (rows[0], rows[1:]) if rows else ([], [])


def _failure_text(run) -> str:
    if run.timed_out:
        return f"the script ran longer than {settings.WB_SANDBOX_TIMEOUT_S} s"
    tail = (run.stderr or run.stdout).strip()[-TABLE_ERROR_CHARS:]
    return tail or f"the script finished but wrote no {TABLE_OUTPUT}"


def analyze_table(ctx: ToolContext, file_id: str, request: str) -> ToolOutcome:
    ref, path = file_store.get_ref(file_id), file_store.get_path(file_id)
    if ref is None or path is None:
        raise ToolError(f"unknown file_id {file_id!r}; use one of the file_ids listed in the task")
    if path.suffix.lower() not in TABLE_SUFFIXES:
        raise ToolError(f"analyze_table needs a CSV or Excel file, not {ref.filename}")
    df = load_table(path)
    input_csv = df.to_csv(index=False)
    coder = registry.model_for_task(TaskType.CODING)
    messages = [{"role": "system", "content": _TABLE_SYSTEM},
                {"role": "user", "content": f"Request: {request}\n\n{describe_table(df)}"}]
    last_error = "no attempt made"
    for attempt in range(1, settings.WB_CODE_MAX_ATTEMPTS + 1):
        code = extract_python(_llm_text(ctx, messages, "analyze_table", task_type=TaskType.CODING,
                                        timeout_s=ANSWER_TIMEOUT_S))
        problems = check_table_script(code)
        if not problems:
            run = run_in_sandbox({TABLE_INPUT: input_csv, TABLE_SCRIPT: code}, ["python", TABLE_SCRIPT],
                                 collect=[TABLE_OUTPUT])
            if run.ok and TABLE_OUTPUT in run.files:
                ctx.event(EventType.LOG, f"Attempt {attempt}: ran in sandbox", {"level": "info", "text": (
                    f"Attempt {attempt}: {TABLE_SCRIPT} ran in the offline sandbox in {run.duration_ms / 1000:.1f} s.")})
                return _save_table_result(ctx, code, run.files[TABLE_OUTPUT], run.stdout, df, attempt)
            problems = [_failure_text(run)]
        last_error = "; ".join(problems)
        ctx.warn(f"Attempt {attempt} ({coder.id}): analysis script rejected - {_one_line(last_error, 200)}")
        messages += [{"role": "assistant", "content": f"```python\n{code}\n```"},
                     {"role": "user", "content": f"The script failed: {last_error}\n"
                                                 "Fix it and reply with the full corrected script in one python block."}]
    raise ToolError(f"no working analysis after {settings.WB_CODE_MAX_ATTEMPTS} attempts; last error: "
                    f"{_one_line(last_error, 300)}", code="BAD_MODEL_OUTPUT")


def _save_table_result(ctx: ToolContext, code: str, output: bytes, stdout: str, df, attempts: int) -> ToolOutcome:
    header, rows = csv_rows(output)
    workbook = office.make_table_excel(header, rows, input_header=[str(c) for c in df.columns],
                                       input_rows=df.astype(str).values.tolist(), task_id=ctx.task_id)
    ctx.artifact(workbook)
    script_name = workbook.filename.replace(".xlsx", ".py")
    ctx.artifact(office.save_text_artifact(script_name, code, ArtifactKind.PY, task_id=ctx.task_id))
    return ToolOutcome(ok=True, summary=(
        f"Analysis ran in the sandbox ({attempts} attempt(s)); result {len(rows)} rows x {len(header)} columns "
        f"({', '.join(header[:12])}), saved as {workbook.filename} with the script {script_name}.\n"
        f"Printed summary:\n{trim(stdout, 700)}\n{NEXT_FINISH}"))


def inspect_image(ctx: ToolContext, file_id: str, question: str) -> ToolOutcome:
    ref, path = file_store.get_ref(file_id), file_store.get_path(file_id)
    if ref is None or path is None:
        raise ToolError(f"unknown file_id {file_id!r}; use one of the file_ids listed in the task")
    if not (ref.is_image or path.suffix.lower() == ".pdf"):
        raise ToolError(f"inspect_image needs an image or a PDF, not {ref.filename}; use read_document")
    answer = _llm_text(ctx, [{"role": "user", "content": _IMAGE_PROMPT.format(question=question)}], "inspect_image",
                       task_type=TaskType.VISION, images=[vision_png(path)], timeout_s=ANSWER_TIMEOUT_S)
    if not answer:
        return ToolOutcome(ok=False, summary="The vision model returned nothing for this image.",
                           error_code="BAD_MODEL_OUTPUT")
    answer = f"{answer}\n\n(Seen in {ref.filename} by the local vision model; check against the original.)"
    ans_id = ctx.scratchpad.new_id("ans")
    ctx.scratchpad.answers[ans_id] = answer
    return ToolOutcome(ok=True, summary=(
        f"{ans_id}:\n{trim(answer, 1100)}\nNext step: call finish with answer {ans_id!r}, or create_document "
        "if the user asked for a Word or PowerPoint file."))


# ------------------------------------------------------------------ registry
@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., ToolOutcome]

    def definition(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in [
    ToolSpec("read_document",
             "Read an attached file (PDF, image, Word, Excel, text). Scanned pages are OCR'd. "
             "Use kind='pid' for a P&ID drawing. Returns a doc_id and a short excerpt.",
             _schema({"file_id": {"type": "string", "description": "file_id from the task, e.g. f_1a2b3c4d5e6f"},
                      "kind": {"type": "string", "enum": ["auto", "pid"], "description": "auto (default) or pid"}},
                     ["file_id"]),
             read_document),
    ToolSpec("search_knowledge",
             "Search the offline SOP / manual knowledge base. Returns kb ids (kb_1, ...) with file name, "
             "page and a snippet. Only relevant passages are returned.",
             _schema({"query": {"type": "string", "description": "what to look for, in plain words"},
                      "top_k": {"type": "integer", "description": "how many passages (1-10, default 4)"},
                      "queries": {"type": "array", "items": {"type": "string"},
                                  "description": "optional extra queries, e.g. one per finding; results are merged"}},
                     ["query"]),
             search_knowledge),
    ToolSpec("draft_approval_note",
             "Write the inspection approval note (Word file) from a document read with read_document, "
             "citing only the chosen kb ids.",
             _schema({"doc_id": {"type": "string", "description": "e.g. doc_1"},
                      "kb_ref_ids": {"type": "array", "items": {"type": "string"},
                                     "description": "kb ids from search_knowledge to cite, e.g. [\"kb_1\"]"}},
                     ["doc_id"]),
             draft_approval_note),
    ToolSpec("extract_pid_tags",
             "List all equipment/instrument tags from a P&ID read with read_document(kind='pid') "
             "and save them as an Excel file.",
             _schema({"doc_id": {"type": "string", "description": "e.g. doc_1"}}, ["doc_id"]),
             extract_pid_tags),
    ToolSpec("run_code_task",
             "Write Python code with tests for a calculation, run it in the offline sandbox, and "
             "return the test result and printed steps.",
             _schema({"request": {"type": "string", "description": "the full coding request"}}, ["request"]),
             run_code_task),
    ToolSpec("answer_question",
             "Answer a question from the SOP knowledge base and/or documents read with read_document. "
             "Reads the full passages and returns an answer with [n] citations and a Sources list.",
             _schema({"question": {"type": "string", "description": "the user's question, in full"},
                      "doc_ids": {"type": "array", "items": {"type": "string"},
                                  "description": "optional doc ids from read_document (default: all read so far)"},
                      "use_knowledge": {"type": "boolean", "description": "search the SOPs too (default true)"}},
                     ["question"]),
             answer_question),
    ToolSpec("create_document",
             "Write a Word report (format 'docx') or a PowerPoint deck (format 'pptx') from what this task has "
             "found so far (answers, documents, SOP passages) and save it as a file.",
             _schema({"format": {"type": "string", "description": "'docx' for a Word report, 'pptx' for a PowerPoint deck"},
                      "title": {"type": "string", "description": "document title"},
                      "instructions": {"type": "string", "description": "what the document must cover"},
                      "doc_ids": {"type": "array", "items": {"type": "string"},
                                  "description": "optional doc ids to use (default: all read so far)"}},
                     ["format", "title"]),
             create_document),
    ToolSpec("analyze_table",
             "Analyse an attached CSV or Excel file: the code model writes pandas code, it runs in the offline "
             "sandbox, and the result is saved as an Excel file plus the script.",
             _schema({"file_id": {"type": "string", "description": "file_id of the CSV/XLSX from the task"},
                      "request": {"type": "string", "description": "what to calculate, in full"}},
                     ["file_id", "request"]),
             analyze_table),
    ToolSpec("inspect_image",
             "Look at an attached photo, sketch or handwritten note with the vision model and answer a question "
             "about what it shows (condition, damage, readings, handwriting). Not for P&ID tag lists: for those "
             "use read_document with kind 'pid', then extract_pid_tags.",
             _schema({"file_id": {"type": "string", "description": "file_id of the image from the task"},
                      "question": {"type": "string", "description": "what to look for or answer"}},
                     ["file_id", "question"]),
             inspect_image),
    ToolSpec("finish",
             "Give the final answer to the user and stop. Mention any files produced.",
             _schema({"answer": {"type": "string", "description": "the final answer"}}, ["answer"]),
             finish),
]}


def tool_definitions() -> list[dict[str, Any]]:
    return [spec.definition() for spec in TOOLS.values()]


# ------------------------------------------------------------------ argument checking and execution
_JSON_TYPES = {"string": str, "integer": int, "array": list, "boolean": bool}


def validate_args(spec: ToolSpec, args: Any) -> dict[str, Any]:
    """Check args against the tool's schema; returns cleaned args or raises ToolError."""
    if not isinstance(args, dict):
        raise ToolError(f"arguments for {spec.name} must be a JSON object")
    props = spec.parameters["properties"]
    missing = [name for name in spec.parameters["required"] if name not in args]
    if missing:
        raise ToolError(f"{spec.name} is missing required argument(s): {', '.join(missing)}")
    clean: dict[str, Any] = {}
    for name, value in args.items():
        if name not in props:
            continue                                  # ignore extra arguments small models invent
        expected = props[name]["type"]
        if expected == "integer" and isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if expected == "array" and isinstance(value, str):
            value = [value]
        if not isinstance(value, _JSON_TYPES[expected]) or (expected == "integer" and isinstance(value, bool)):
            raise ToolError(f"{spec.name}: argument {name!r} must be {expected}")
        if "enum" in props[name] and value not in props[name]["enum"]:
            raise ToolError(f"{spec.name}: argument {name!r} must be one of {props[name]['enum']}")
        clean[name] = value
    return clean


def execute_tool(ctx: ToolContext, name: str, args: Any) -> ToolOutcome:
    """Run one tool call with events; never raises."""
    ctx.event(EventType.TOOL_CALL, name, {"tool": name, "args": args if isinstance(args, dict) else {"raw": str(args)}})
    start = time.monotonic()
    spec = TOOLS.get(name)
    try:
        if spec is None:
            raise ToolError(f"unknown tool {name!r}; available tools: {', '.join(TOOLS)}")
        outcome = spec.fn(ctx, **validate_args(spec, args))
    except ToolError as exc:
        outcome = ToolOutcome(ok=False, summary=f"Error: {exc}", error_code=exc.code)
    except (llm_client.LLMError, office.OfficeError, DocumentExtractionError, SandboxError) as exc:
        outcome = ToolOutcome(ok=False, summary=f"Error ({exc.code}): {exc}", error_code=exc.code)
        ctx.event(EventType.ERROR, f"{name} failed", {"error": ErrorInfo(code=exc.code, message=str(exc),
                                                                          retryable=True).model_dump()})
    except Exception as exc:  # a broken tool must never crash the agent or the worker
        outcome = ToolOutcome(ok=False, summary=f"Error: {name} failed unexpectedly: {exc!r}", error_code="INTERNAL")
        ctx.event(EventType.ERROR, f"{name} failed", {"error": ErrorInfo(code="INTERNAL", message=repr(exc),
                                                                          retryable=True).model_dump()})
    outcome.summary = trim(outcome.summary)
    duration_ms = int((time.monotonic() - start) * 1000)
    ctx.event(EventType.TOOL_RESULT, f"{name}: {'ok' if outcome.ok else 'failed'}", {
        "tool": name, "ok": outcome.ok, "summary": outcome.summary, "duration_ms": duration_ms,
    })
    write_audit_record(kind="tool", name=name, task_id=ctx.task_id, duration_ms=duration_ms, ok=outcome.ok,
                       detail={"args": _one_line(json.dumps(args, default=str), AUDIT_ARGS_CHARS),
                               "error_code": outcome.error_code})
    return outcome
