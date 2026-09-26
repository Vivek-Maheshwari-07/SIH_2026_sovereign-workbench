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

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend import llm_client
from backend.audit import write_audit_record
from backend.file_store import file_store
from backend.flows import code_flow
from backend.registry import registry
from backend.tools import knowledge, office
from backend.tools.documents import (
    PID_FAST_METHODS,
    PID_PROMPT_EXAMPLES,
    PID_TILE_NAMES,
    PageResult,
    extract,
    pid_path_note,
)
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
AUDIT_ARGS_CHARS = 300              # tool arguments kept in the audit record
# Seen live: after the Excel file was saved the 4B model wandered off into run_code_task and timed out.
NEXT_FINISH = "The file is ready. Next step: call finish with a short answer naming the file."

# Tag letter prefix -> (equipment type to use, words that count as agreeing with it).
# Common ISA / plant conventions; extend here. Unknown prefixes keep the model's answer.
_INSTRUMENT = ("instrument",)
TAG_TYPE_RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "P": ("Pump", ("pump",)),
    "V": ("Vessel", ("vessel", "drum", "separator", "receiver")),
    "T": ("Tank", ("tank",)),
    "E": ("Heat exchanger", ("exchanger", "cooler", "heater", "condenser", "reboiler")),
    "C": ("Column", ("column", "tower")),
    "K": ("Compressor", ("compressor",)),
    "FT": ("Flow transmitter", ("flow", "transmitter") + _INSTRUMENT),
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


def _llm_json(ctx: ToolContext, schema, messages: list[dict[str, Any]], purpose: str):
    model = registry.model_for_task(TaskType.DOCUMENT)
    result = llm_client.chat_json_meta(model.ollama_name, messages, schema, purpose=purpose)
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


def search_knowledge(ctx: ToolContext, query: str, top_k: int = 4) -> ToolOutcome:
    top_k = max(1, min(int(top_k), MAX_TOP_K))
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
- findings: one per defect found; severity is low, medium, high or critical;
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
    note: ApprovalNote = _llm_json(ctx, ApprovalNote, messages, "draft_approval_note")
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
    """Correct equipment types that clearly contradict the tag's letter prefix (TAG_TYPE_RULES)."""
    for tag in tag_list.tags:
        rule = TAG_TYPE_RULES.get(tag_prefix(tag.tag))
        if rule is None:
            continue                                     # unknown prefix: keep the model's answer
        canonical, accepted = rule
        given = (tag.equipment_type or "").lower()
        if not any(word in given for word in accepted):
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


def extract_pid_tags(ctx: ToolContext, doc_id: str) -> ToolOutcome:
    doc = _doc(ctx, doc_id)
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


def finish(ctx: ToolContext, answer: str) -> ToolOutcome:
    return ToolOutcome(ok=True, summary="Finished.", finish_answer=answer.strip())


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
                      "top_k": {"type": "integer", "description": "how many passages (1-10, default 4)"}},
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
    ToolSpec("finish",
             "Give the final answer to the user and stop. Mention any files produced.",
             _schema({"answer": {"type": "string", "description": "the final answer"}}, ["answer"]),
             finish),
]}


def tool_definitions() -> list[dict[str, Any]]:
    return [spec.definition() for spec in TOOLS.values()]


# ------------------------------------------------------------------ argument checking and execution
_JSON_TYPES = {"string": str, "integer": int, "array": list}


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
    except (llm_client.LLMError, office.OfficeError) as exc:
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
