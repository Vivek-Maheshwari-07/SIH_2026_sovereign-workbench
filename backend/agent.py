"""
Agent loop (ticket A8, first half): route -> plan -> tool loop -> final.

Plain threads, no async (AGENTS.md rule 9). The model calls ONE tool per
step; results come back as short summaries (see agent_tools). Stops on
finish, WB_AGENT_MAX_STEPS (AGENT_STEP_LIMIT), WB_AGENT_TIMEOUT_S
(AGENT_TIMEOUT) or cancel (CANCELLED), checked before and after every
model call and tool call.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from pydantic import BaseModel, Field

from backend import agent_tools, llm_client
from backend.agent_tools import ToolContext, execute_tool, tool_definitions
from backend.file_store import file_store
from backend.flows import guided
from backend.registry import registry
from backend.router import route
from backend.settings import settings
from backend.task_store import TaskHandle
from shared.contracts import ErrorInfo, EventType, PlanStep, RouteDecision, RouteRequest, Scenario, TaskMode, TaskType

# ---- named constants (no .env key exists for these)
MAX_PLAN_STEPS = 6
CHARS_PER_TOKEN = 3.5               # rough size estimate for trimming; no tokenizer package
CONTEXT_SHARE = 0.6                 # share of WB_NUM_CTX the conversation may use (rest: tool schemas + reply)
PINNED_MESSAGES = 3                 # system prompt, task, plan: never trimmed
TRIM_NOTE = "(Older tool results were removed to save space. Ids like doc_1 and kb_1 are still valid.)"
PLAN_FOLLOW_UP = "\n\nFollow this plan. Call the first tool now."
GUIDED_FALLBACK = True              # agent mode: switch to the guided flow when the agent cannot finish
FALLBACK_CODES = frozenset({"BAD_MODEL_OUTPUT", "AGENT_STEP_LIMIT"})
FALLBACK_MESSAGE = "Agent could not finish; switched to guided flow."
AUTO_FINISH_MESSAGE = "Agent was done but kept going; finished automatically."
GUIDED_SWITCH_SHARE = 0.6           # past this share of WB_AGENT_TIMEOUT_S with no deliverable -> guided flow
TIME_SWITCH_MESSAGE = "Agent used most of its time without producing the file; switched to guided flow."
# Tools that produce a scenario's deliverable (used by the auto-finish guard).
DELIVERABLE_TOOLS: dict[str, Scenario] = {
    "draft_approval_note": Scenario.INSPECTION_NOTE,
    "run_code_task": Scenario.CODE_CALC,
    "extract_pid_tags": Scenario.PID_TAGS,
}

SYSTEM_PROMPT = """You are the Sovereign AI Workbench agent. You run fully offline on a local computer
and help plant engineers with inspection reports, SOPs, P&ID drawings and engineering calculations.

How you work:
- Call exactly ONE tool per reply. Wait for its result before the next step.
- Tool results are short summaries with ids (doc_1, kb_1, note_1). Pass these ids to later tools.
- Only use file_ids listed in the task. Never invent file names, SOP names, page numbers or amounts.
- For an inspection report: read_document -> search_knowledge -> draft_approval_note -> finish.
- For a P&ID drawing: read_document with kind "pid" -> extract_pid_tags -> finish.
- For a calculation or code request: run_code_task -> finish.
- For a simple question: search_knowledge if an SOP may help, then finish.
- When done, call finish with a short answer for the user that names any files produced.
- If a tool returns an error, fix the arguments or choose another step; do not repeat the same failing call."""

PLAN_PROMPT = f"""Make a short plan (at most {MAX_PLAN_STEPS} steps, as few as possible) for the task below.
Each step has: index (1, 2, ...), title (a few words), tool (one of: {", ".join(agent_tools.TOOLS)}, or null).
Typical plans:
- inspection report -> approval note: read_document, search_knowledge, draft_approval_note, finish
- P&ID drawing -> tag list: read_document (kind pid), extract_pid_tags, finish
- calculation / code: run_code_task, finish
The last step is finish. Do not add extra checking or conversion steps."""


class _Plan(BaseModel):
    """LLM output schema for the plan (internal; the API type is shared.contracts.PlanStep)."""
    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)


class _SwitchToGuided(Exception):
    """Internal: the time budget says the guided flow must take over now."""


def call_key(name: str, args: Any) -> str:
    """Stable identity of a tool call: same tool + same arguments -> same key."""
    try:
        return name + ":" + json.dumps(args, sort_keys=True, default=str)
    except TypeError:
        return name + ":" + repr(args)


class AgentStop(Exception):
    """Ends the run with an error code (AGENT_STEP_LIMIT, AGENT_TIMEOUT, CANCELLED, MODEL_*)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ helpers
def default_plan(task_type: TaskType, has_files: bool) -> list[PlanStep]:
    tools_by_type = {
        TaskType.DOCUMENT: ["read_document", "search_knowledge", "draft_approval_note"] if has_files
        else ["search_knowledge"],
        TaskType.VISION: ["read_document", "extract_pid_tags"],
        TaskType.CODING: ["run_code_task"],
        TaskType.GENERAL: ["search_knowledge"],
    }
    tools = tools_by_type.get(task_type, []) + ["finish"]
    return [PlanStep(index=i, title=tool.replace("_", " ").capitalize(), tool=tool) for i, tool in enumerate(tools, 1)]


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    size = 0
    for message in messages:
        size += len(str(message.get("content") or ""))
        if message.get("tool_calls"):
            size += len(json.dumps(message["tool_calls"]))
    return int(size / CHARS_PER_TOKEN)


def context_budget() -> int:
    return int(settings.WB_NUM_CTX * CONTEXT_SHARE)


def trim_conversation(messages: list[dict[str, Any]], budget: Optional[int] = None) -> list[dict[str, Any]]:
    """
    Drop the oldest tool exchanges (assistant call + tool result) until the
    conversation fits the budget. The pinned messages (system, task, plan)
    and the newest exchange are always kept.
    """
    budget = budget or context_budget()
    if estimate_tokens(messages) <= budget:
        return messages
    pinned, rest = messages[:PINNED_MESSAGES], [m for m in messages[PINNED_MESSAGES:] if m.get("content") != TRIM_NOTE]
    while len(rest) > 2 and estimate_tokens(pinned + [{"content": TRIM_NOTE}] + rest) > budget:
        rest = rest[2:] if rest[0].get("role") == "assistant" and len(rest) > 1 else rest[1:]
    return pinned + [{"role": "user", "content": TRIM_NOTE}] + rest


def _task_message(handle: TaskHandle) -> str:
    lines = [f"Task: {handle.message}"]
    if handle.file_ids:
        lines.append("Attached files:")
        for file_id in handle.file_ids:
            ref = file_store.get_ref(file_id)
            if ref is None:
                lines.append(f"- {file_id} (unknown file)")
                continue
            kind = "image" if ref.is_image else ("scanned PDF" if ref.has_text_layer is False else ref.mime_type)
            pages = f", {ref.pages} pages" if ref.pages else ""
            lines.append(f"- file_id {file_id}: {ref.filename} ({kind}{pages})")
    else:
        lines.append("No files attached.")
    return "\n".join(lines)


def _plan_text(plan: list[PlanStep]) -> str:
    return "Plan:\n" + "\n".join(f"{s.index}. {s.title}" + (f" [{s.tool}]" if s.tool else "") for s in plan)


# ------------------------------------------------------------------ the run
class AgentRun:
    def __init__(self, handle: TaskHandle) -> None:
        self.handle = handle
        self.started = time.monotonic()
        self.model = registry.model_for_task(TaskType.GENERAL)
        self.ctx = ToolContext(task_id=handle.task_id, emit=handle.emit, add_artifact=handle.add_artifact,
                               task_message=handle.message)
        self.messages: list[dict[str, Any]] = []
        self.step = 0
        self.scenario: Optional[Scenario] = None
        self.seen_calls: dict[str, str] = {}        # call_key -> summary of the first result

    # ---- stop conditions
    def checkpoint(self) -> None:
        if self.handle.is_cancelled():
            raise AgentStop("CANCELLED", "Task cancelled by user.")
        if time.monotonic() - self.started > settings.WB_AGENT_TIMEOUT_S:
            raise AgentStop("AGENT_TIMEOUT", f"Task exceeded {settings.WB_AGENT_TIMEOUT_S} s.")

    def emit(self, event_type: EventType, title: str, data: dict[str, Any], step: Optional[int] = None) -> None:
        self.handle.emit(event_type, title, data, step=step)

    def deliverable_ready(self) -> bool:
        return self.scenario is not None and guided.has_expected_artifact(self.ctx.artifacts, self.scenario)

    def auto_finish_answer(self) -> str:
        files = ", ".join(a.filename for a in self.ctx.artifacts) or "none"
        return f"Finished automatically. Files produced: {files}."

    def check_time_budget(self) -> None:
        """Switch to the guided flow while there is still time for it to finish."""
        used = time.monotonic() - self.started
        if (GUIDED_FALLBACK and self.scenario is not None and not self.deliverable_ready()
                and used > GUIDED_SWITCH_SHARE * settings.WB_AGENT_TIMEOUT_S):
            raise _SwitchToGuided()

    def log(self, level: str, text: str) -> None:
        self.emit(EventType.LOG, text[:80], {"level": level, "text": text}, step=self.step or None)

    def llm_event(self, purpose: str, duration_ms: int, tokens_out: Optional[int]) -> None:
        self.emit(EventType.LLM_CALL, f"{self.model.id}: {purpose}", {
            "model_id": self.model.id, "purpose": purpose, "duration_ms": duration_ms, "tokens_out": tokens_out,
        }, step=self.step or None)

    # ---- phases
    def do_route(self) -> RouteDecision:
        decision = route(RouteRequest(message=self.handle.message, file_ids=self.handle.file_ids))
        self.handle.set_route(decision)
        self.emit(EventType.ROUTE, f"Routed to {decision.task_type.value} ({decision.ollama_name})",
                  {"decision": decision.model_dump(mode="json")})
        return decision

    def do_plan(self, decision: RouteDecision, task_text: str) -> list[PlanStep]:
        self.checkpoint()
        messages = [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": task_text}]
        try:
            result = llm_client.chat_json_meta(self.model.ollama_name, messages, _Plan, purpose="plan")
            self.llm_event("plan", result.duration_ms, result.tokens_out)
            plan = [PlanStep(index=i, title=s.title[:120], tool=s.tool if s.tool in agent_tools.TOOLS else None)
                    for i, s in enumerate(result.value.steps, start=1)]
        except llm_client.LLMError as exc:
            if exc.code != "BAD_MODEL_OUTPUT":
                raise AgentStop(exc.code, str(exc)) from exc
            plan = default_plan(decision.task_type, bool(self.handle.file_ids))
            self.log("warn", "The model's plan was not valid JSON twice; using a default plan.")
        self.checkpoint()
        self.handle.set_plan(plan)
        self.emit(EventType.PLAN, f"Plan with {len(plan)} steps", {"steps": [s.model_dump(mode="json") for s in plan]})
        return plan

    def ask_model(self) -> llm_client.ChatResult:
        self.checkpoint()
        self.messages = trim_conversation(self.messages)
        start = time.monotonic()
        try:
            reply = llm_client.chat(self.model.ollama_name, self.messages, tools=tool_definitions(),
                                    purpose="agent_step")
        except llm_client.LLMError as exc:
            raise AgentStop(exc.code, str(exc)) from exc
        self.llm_event("agent_step", int((time.monotonic() - start) * 1000), reply.tokens_out)
        self.checkpoint()
        return reply

    def one_step(self) -> Optional[str]:
        """Run one model turn + one tool. Returns the final answer when the agent finishes."""
        if self.step >= settings.WB_AGENT_MAX_STEPS:
            raise AgentStop("AGENT_STEP_LIMIT", f"Agent used all {settings.WB_AGENT_MAX_STEPS} steps without finishing.")
        self.step += 1
        self.ctx.step = self.step
        reply = self.ask_model()

        if reply.tool_calls:
            call = reply.tool_calls[0]
            name, args = str(call.get("name", "")), call.get("arguments", {})
            if len(reply.tool_calls) > 1:
                self.log("warn", f"Model asked for {len(reply.tool_calls)} tools at once; running only {name}.")
        elif reply.text.strip():
            name, args = "finish", {"answer": reply.text.strip()}      # plain text answer = finish
        else:
            self.messages.append({"role": "user", "content": "Your reply was empty. Call one tool."})
            self.log("warn", "Model returned an empty reply.")
            return None

        assistant_turn = {"role": "assistant", "content": reply.text or "",
                          "tool_calls": [{"function": {"name": name, "arguments": args}}]}
        key = call_key(name, args)
        if name != "finish":
            other_deliverable = DELIVERABLE_TOOLS.get(name) not in (None, self.scenario)
            if self.deliverable_ready() and (key in self.seen_calls or other_deliverable):
                self.log("warn", AUTO_FINISH_MESSAGE)
                return self.auto_finish_answer()
            if key in self.seen_calls:
                self.log("warn", f"Skipped repeated call: {name} with the same arguments was already run.")
                self.messages.append(assistant_turn)
                self.messages.append({"role": "tool", "tool_name": name, "content": (
                    f"You already called {name} with these arguments. Its result was: {self.seen_calls[key]} "
                    "Use that result, choose a different step, or call finish.")})
                return None

        self.emit(EventType.STEP_START, f"Step {self.step}: {name}", {"index": self.step, "title": name},
                  step=self.step)
        outcome = execute_tool(self.ctx, name, args)
        self.checkpoint()
        if outcome.finish_answer is not None and outcome.ok:
            return outcome.finish_answer or "Done."
        self.seen_calls[key] = outcome.summary

        self.messages.append(assistant_turn)
        self.messages.append({"role": "tool", "tool_name": name, "content": outcome.summary})
        return None

    def run_guided(self, scenario: Scenario) -> str:
        try:
            return guided.run_guided(self, scenario, self.handle.message, self.handle.file_ids,
                                     set_plan=self.handle.set_plan)
        except guided.GuidedError as exc:
            raise AgentStop(exc.code, str(exc)) from exc

    def run_agent_loop(self, decision: RouteDecision) -> str:
        task_text = _task_message(self.handle)
        plan = self.do_plan(decision, task_text)
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_text},
            # A user turn, not an assistant turn: after its "own" last message the model replies with nothing.
            {"role": "user", "content": _plan_text(plan) + PLAN_FOLLOW_UP},
        ]
        while True:
            self.check_time_budget()
            answer = self.one_step()
            if answer is not None:
                return answer

    def run(self) -> None:
        decision = self.do_route()
        scenario = guided.pick_scenario(self.handle.scenario, decision, self.handle.file_ids)
        self.scenario = scenario
        if self.handle.mode == TaskMode.GUIDED:
            if scenario is None:
                raise AgentStop("BAD_REQUEST", "Guided mode needs a scenario (or an attachment that matches one).")
            answer = self.run_guided(scenario)
        else:
            try:
                answer = self.run_agent_loop(decision)
            except _SwitchToGuided:
                self.log("warn", TIME_SWITCH_MESSAGE)
                answer = self.run_guided(scenario)
            except AgentStop as stop:
                if not (GUIDED_FALLBACK and scenario is not None and stop.code in FALLBACK_CODES):
                    raise
                self.log("warn", f"{FALLBACK_MESSAGE} (reason: {stop.code})")
                answer = self.run_guided(scenario)
            else:
                # Only for an explicitly requested scenario: an agent that "finishes" without the
                # deliverable the user asked for gets the guided flow too.
                if (GUIDED_FALLBACK and self.handle.scenario is not None
                        and not guided.has_expected_artifact(self.ctx.artifacts, self.handle.scenario)):
                    self.log("warn", f"{FALLBACK_MESSAGE} (reason: no {guided.EXPECTED_ARTIFACT[self.handle.scenario].value} file)")
                    answer = self.run_guided(self.handle.scenario)
        self.handle.set_final_answer(answer)
        self.emit(EventType.FINAL, "Done", {"answer": answer})


def run(handle: TaskHandle) -> None:
    """Task-store entry point. Never raises for expected stops; the worker survives anything else."""
    agent = AgentRun(handle)
    try:
        agent.run()
    except AgentStop as stop:
        error = ErrorInfo(code=stop.code, message=str(stop), retryable=stop.code != "CANCELLED")
        handle.emit(EventType.ERROR, stop.code.replace("_", " ").capitalize(), {"error": error.model_dump()},
                    step=agent.step or None)
        if stop.code != "CANCELLED":        # the worker marks cancelled tasks itself
            handle.fail(stop.code, str(stop))
    except Exception as exc:
        error = ErrorInfo(code="INTERNAL", message=f"Unexpected agent error: {exc!r}")
        handle.emit(EventType.ERROR, "Internal error", {"error": error.model_dump()}, step=agent.step or None)
        handle.fail("INTERNAL", error.message)
