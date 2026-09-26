"""
Code flow (ticket A5): the coder model writes solution.py + test_solution.py,
the sandbox runs pytest, failures go back to the model, up to
WB_CODE_MAX_ATTEMPTS attempts. Then `python solution.py` prints the
calculation steps, which land in CodeResult.stdout_tail.

The model answers with two fenced ```python blocks (not JSON): small coder
models break JSON escaping when code has quotes and newlines.

`emit(event_type, title, data)` is optional; `TaskHandle.emit` fits it, so the
A8 agent can pass it straight in. Event data keys follow shared.contracts.
"""
from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend.llm_client import LLMError, chat
from backend.registry import RegistryError, registry
from backend.settings import settings
from backend.tools.sandbox import SandboxError, SandboxResult, run_in_sandbox
from shared.contracts import ERROR_CODES, CodeResult, ErrorInfo, EventType, TaskType

EmitFn = Callable[[EventType, str, dict[str, Any]], None]

SOLUTION_FILE = "solution.py"
TESTS_FILE = "test_solution.py"
REPORT_FILE = "report.xml"
PYTEST_COMMAND = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={REPORT_FILE}", TESTS_FILE]
RUN_COMMAND = ["python", SOLUTION_FILE]
SANDBOX_TOOL = "sandbox"

# ---- named constants (no .env key exists for these)
FEEDBACK_MAX_CHARS = 3000       # pytest output sent back to the model
STDOUT_TAIL_CHARS = 2000        # CodeResult.stdout_tail, per the contract comment
FORBIDDEN_TEST_IMPORTS = frozenset({"os", "subprocess", "socket", "urllib", "requests", "importlib", "shutil"})
_SKIP_NAMES = frozenset({"skip", "skipif", "xfail", "importorskip", "skipTest"})

# Demo request for the guided CODE_CALC scenario. The fixed signature lets the
# caller verify the result independently of the model's own tests.
PIPE_THICKNESS_DEMO_REQUEST = (
    "Write a function for pipe wall thickness from design pressure, outside diameter "
    "and allowable stress, using t = P*D / (2*S), with tests, and print the calculation steps. "
    "The function must be exactly `def pipe_wall_thickness(P: float, D: float, S: float) -> float` "
    "(P in MPa, D in mm, S in MPa, returns t in mm). "
    "In the `if __name__ == \"__main__\":` block use exactly P = 10 MPa, D = 200 mm, S = 138 MPa "
    "and print each step; the result is t = 10 * 200 / (2 * 138) = 7.246 mm."
)

SYSTEM_PROMPT = f"""You are a careful Python engineer. You write a solution file and a pytest test file.

Reply with EXACTLY two fenced code blocks and nothing else, in this format:

### {SOLUTION_FILE}
```python
# code here
```

### {TESTS_FILE}
```python
# tests here
```

Rules for {SOLUTION_FILE}:
- Standard library only (math is fine). No input(), no files, no network.
- Validate inputs (raise ValueError for zero or negative values where they make no sense).
- The function itself does not print. It MUST end with an `if __name__ == "__main__":` block
  that runs one example and prints every calculation step on its own line.

Rules for {TESTS_FILE}:
- Start with `import pytest` and `from solution import ...`.
- At least 3 separate test functions named test_..., each with a real assert
  (use pytest.approx for floats; pytest.raises is fine for bad inputs).
- NEVER work out expected numbers in your head. Write each expected value as a Python
  expression of the formula, e.g. `expected = 10 * 50 / (2 * 20)`.
- Never use skip, xfail or `assert True`. Never import os, subprocess, socket, urllib or requests.

Example of the shape (for a different task, area of a rectangle):

### {SOLUTION_FILE}
```python
def rectangle_area(width: float, height: float) -> float:
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    return width * height


if __name__ == "__main__":
    width, height = 3.0, 4.0
    print("Formula: A = width * height")
    print(f"Inputs: width = {{width}} m, height = {{height}} m")
    area = rectangle_area(width, height)
    print(f"A = {{width}} * {{height}} = {{area:.3f}} m^2")
```

### {TESTS_FILE}
```python
import pytest
from solution import rectangle_area


def test_known_value():
    expected = 3.0 * 4.0
    assert rectangle_area(3.0, 4.0) == pytest.approx(expected)


def test_doubles_with_width():
    assert rectangle_area(6.0, 4.0) == pytest.approx(2 * rectangle_area(3.0, 4.0))


def test_rejects_negative():
    with pytest.raises(ValueError):
        rectangle_area(-1.0, 4.0)
```
"""

RETRY_ADVICE = (
    "Fix the problem and reply again with BOTH files as two fenced code blocks. "
    "Do not repeat the same code. Keep every existing test function. "
    "Check which side is wrong: if the solution follows the formula in the task, the test's "
    "expected value is wrong - rewrite it as a Python expression of the formula "
    "(e.g. `expected = 10 * 50 / (2 * 20)`). Otherwise fix the solution."
)

LOCKED_TESTS_ADVICE = (
    f"The tests in {TESTS_FILE} are given and correct; they will not be changed. "
    f"Fix {SOLUTION_FILE} so every test passes, following the formula in the task. "
    f"Reply with the complete fixed {SOLUTION_FILE} as one fenced code block, "
    "keeping the `if __name__ == \"__main__\":` block."
)


class CodeFlowError(Exception):
    """A code task that could not finish. `code` is a key from ERROR_CODES."""

    def __init__(self, code: str, message: str, result: Optional[CodeResult] = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code}")
        super().__init__(message)
        self.code = code
        self.result = result

    def error_info(self) -> ErrorInfo:
        return ErrorInfo(code=self.code, message=str(self), retryable=self.code != "BAD_REQUEST")


class ParseError(ValueError):
    """The model reply did not contain the expected fenced code blocks."""


@dataclass
class AttemptRecord:
    """What happened in one attempt (also used for logs and tests)."""
    number: int
    source: str                         # "model" or "seed"
    outcome: str = ""                   # "passed", "tests_failed", "parse_error", "rejected", "timeout"
    detail: str = ""
    passed: int = 0
    failed: int = 0
    duration_ms: int = 0


@dataclass
class _State:
    solution: Optional[str] = None
    tests: Optional[str] = None
    last_test_count: Optional[int] = None
    feedback: Optional[str] = None
    last_run: Optional[SandboxResult] = None
    tests_locked: bool = False          # seed tests are the spec: the model may only change solution.py
    attempts: list[AttemptRecord] = field(default_factory=list)


# ------------------------------------------------------------------ parsing
_FENCE_RE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[^\n]*\n(.*?)(?:\n[ \t]*```|\Z)", re.DOTALL)
_NAME_RE = re.compile(r"(test_solution\.py|solution\.py)")


def _name_for_block(before: str, body: str) -> Optional[str]:
    first_line = body.lstrip().split("\n", 1)[0]
    if first_line.startswith("#"):
        found = _NAME_RE.findall(first_line)
        if found:
            return found[-1]
    tail_lines = [line for line in before.rstrip().split("\n")[-2:]]
    found = _NAME_RE.findall("\n".join(tail_lines))
    return found[-1] if found else None


def _looks_like_tests(body: str) -> bool:
    return bool(re.search(r"^\s*def test_", body, re.MULTILINE)) or "from solution import" in body


def parse_code_blocks(text: str) -> dict[str, str]:
    """
    Pull solution.py / test_solution.py out of fenced code blocks. File names
    come from the heading before each block or a `# file` comment on its
    first line; unnamed blocks are told apart by whether they hold tests.
    Returns only the files found; raises ParseError if none are found.
    """
    blocks: list[tuple[Optional[str], str]] = []
    for match in _FENCE_RE.finditer(text or ""):
        lang, body = match.group(1).lower(), match.group(2).strip("\n")
        if lang not in ("", "python", "py", "python3") or not body.strip():
            continue
        blocks.append((_name_for_block(text[: match.start()], body), body + "\n"))

    if not blocks:
        raise ParseError("No fenced ```python code blocks found in your reply.")

    files: dict[str, str] = {}
    for name, body in blocks:
        if name is None:
            name = TESTS_FILE if _looks_like_tests(body) else SOLUTION_FILE
        files.setdefault(name, body)
    return files


# ------------------------------------------------------------------ anti-cheat
def _is_test_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test")


def _test_functions(tree: ast.Module) -> list[ast.FunctionDef]:
    found: list[ast.FunctionDef] = []
    for node in tree.body:
        if _is_test_function(node):
            found.append(node)  # type: ignore[arg-type]
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            found.extend(n for n in node.body if _is_test_function(n))  # type: ignore[misc]
    return found


def _has_assertion(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Assert):
            return True
        if isinstance(node, ast.Attribute) and node.attr == "raises":
            return True
    return False


def count_test_functions(tests: str) -> int:
    return len(_test_functions(ast.parse(tests)))


def check_tests(tests: str, previous_count: Optional[int] = None) -> list[str]:
    """
    Anti-cheat guard for model-written tests. Returns a list of problems;
    empty means the tests are acceptable.
    """
    try:
        tree = ast.parse(tests)
    except SyntaxError as exc:
        return [f"{TESTS_FILE} has a syntax error on line {exc.lineno}: {exc.msg}"]

    problems: list[str] = []
    funcs = _test_functions(tree)
    if not funcs:
        problems.append("there are no test functions (def test_...)")
    if previous_count is not None and len(funcs) < previous_count:
        problems.append(f"the number of test functions went down from {previous_count} to {len(funcs)}")

    for func in funcs:
        if not _has_assertion(func):
            problems.append(f"{func.name} has no assert")

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and node.test.value:
            problems.append(f"line {node.lineno} uses `assert {node.test.value!r}`, which always passes")
        elif isinstance(node, ast.Attribute) and node.attr in _SKIP_NAMES:
            problems.append(f"line {node.lineno} uses {node.attr}; skipping tests is not allowed")
        elif isinstance(node, ast.Name) and node.id in _SKIP_NAMES:
            problems.append(f"line {node.lineno} uses {node.id}; skipping tests is not allowed")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_TEST_IMPORTS:
                    problems.append(f"line {node.lineno} imports {alias.name}, which is forbidden in tests")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in FORBIDDEN_TEST_IMPORTS:
                problems.append(f"line {node.lineno} imports from {node.module}, which is forbidden in tests")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            problems.append(f"line {node.lineno} calls __import__, which is forbidden in tests")
    return problems


def check_solution(solution: str) -> list[str]:
    """solution.py must have a __main__ block, so running it prints the calculation steps."""
    if re.search(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", solution, re.MULTILINE):
        return []
    return [f'{SOLUTION_FILE} has no `if __name__ == "__main__":` block that prints the calculation steps']


# ------------------------------------------------------------------ prompts
def _fenced(files: dict[str, str]) -> str:
    return "\n\n".join(f"### {name}\n```python\n{body.rstrip()}\n```" for name, body in files.items())


def _user_prompt(request_text: str, state: _State) -> str:
    parts = [f"Task:\n{request_text}"]
    current = {k: v for k, v in ((SOLUTION_FILE, state.solution), (TESTS_FILE, state.tests)) if v}
    if current:
        parts.append(f"Current files:\n\n{_fenced(current)}")
    if state.feedback:
        parts.append(f"Problem with the last attempt:\n{state.feedback}")
        parts.append(LOCKED_TESTS_ADVICE if state.tests_locked else RETRY_ADVICE)
    return "\n\n".join(parts)


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else "...\n" + text[-limit:]


def _pytest_feedback(run: SandboxResult) -> str:
    if run.timed_out:
        return f"The tests did not finish within {settings.WB_SANDBOX_TIMEOUT_S} s (infinite loop?)."
    if run.oom_killed:
        return f"The tests were killed for using more than {settings.WB_SANDBOX_MEM} of memory."
    output = (run.stdout + ("\n" + run.stderr if run.stderr.strip() else "")).strip()
    return "pytest output:\n" + _trim(output, FEEDBACK_MAX_CHARS)


# ------------------------------------------------------------------ helpers
def _first_failure(run: SandboxResult) -> str:
    """One short line naming the first failing test, for logs and the timeline."""
    for line in (run.stdout + "\n" + run.stderr).splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            return line[:200]
    return f"exit code {run.exit_code}"


def _emit(emit: Optional[EmitFn], event_type: EventType, title: str, data: dict[str, Any]) -> None:
    if emit is not None:
        emit(event_type, title, data)


def _coder_model():
    try:
        return registry.model_for_task(TaskType.CODING)
    except RegistryError as exc:
        raise CodeFlowError("MODEL_UNAVAILABLE", f"No coding model configured: {exc}") from exc


def _ask_model(model, request_text: str, state: _State, attempt: int, emit: Optional[EmitFn]) -> str:
    purpose = "write code" if attempt == 1 and not state.feedback else "fix code"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _user_prompt(request_text, state)},
    ]
    start = time.monotonic()
    try:
        reply = chat(model.ollama_name, messages, purpose=f"code_flow: {purpose}")
    except LLMError as exc:
        raise CodeFlowError(exc.code, str(exc)) from exc
    duration_ms = int((time.monotonic() - start) * 1000)
    _emit(
        emit,
        EventType.LLM_CALL,
        f"Coder model: {purpose} (attempt {attempt})",
        {"model_id": model.id, "purpose": purpose, "duration_ms": duration_ms, "tokens_out": reply.tokens_out},
    )
    return reply.text


def _run(files: dict[str, str], command: list[str], title: str, emit: Optional[EmitFn]) -> SandboxResult:
    _emit(emit, EventType.TOOL_CALL, title, {"tool": SANDBOX_TOOL, "args": {"command": command, "files": sorted(files)}})
    try:
        run = run_in_sandbox(files, command)
    except SandboxError as exc:
        _emit(emit, EventType.TOOL_RESULT, f"{title}: sandbox unavailable",
              {"tool": SANDBOX_TOOL, "ok": False, "summary": str(exc), "duration_ms": 0})
        raise CodeFlowError(exc.code, str(exc)) from exc
    if run.timed_out:
        summary = f"timed out after {settings.WB_SANDBOX_TIMEOUT_S} s"
    elif run.tests_passed is not None:
        summary = f"{run.tests_passed} passed, {run.tests_failed} failed"
    else:
        summary = f"exit code {run.exit_code}" + (" (out of memory)" if run.oom_killed else "")
    _emit(emit, EventType.TOOL_RESULT, f"{title}: {summary}",
          {"tool": SANDBOX_TOOL, "ok": run.ok, "summary": summary, "duration_ms": run.duration_ms})
    return run


def _log(emit: Optional[EmitFn], record: AttemptRecord) -> None:
    level = "info" if record.outcome == "passed" else "warn"
    text = f"Attempt {record.number} ({record.source}): {record.outcome}"
    if record.outcome in ("passed", "tests_failed"):
        text += f", {record.passed} passed / {record.failed} failed"
    if record.detail:
        text += f" - {record.detail}"
    text += f" [{record.duration_ms / 1000:.1f} s]"
    _emit(emit, EventType.LOG, f"Attempt {record.number}: {record.outcome}", {"level": level, "text": text})


def _seed_files(seed_code: str) -> dict[str, str]:
    """A seed is either both files as fenced blocks, or bare solution code."""
    if "```" in seed_code:
        try:
            return parse_code_blocks(seed_code)
        except ParseError:
            pass
    return {SOLUTION_FILE: seed_code.rstrip() + "\n"}


def _result(state: _State, steps_stdout: str) -> CodeResult:
    run = state.last_run
    return CodeResult(
        code=state.solution or "",
        tests=state.tests or "",
        passed=(run.tests_passed or 0) if run else 0,
        failed=(run.tests_failed or 0) if run else 0,
        attempts=max(1, len(state.attempts)),
        stdout_tail=steps_stdout[-STDOUT_TAIL_CHARS:],
    )


# ------------------------------------------------------------------ one attempt
def _get_candidate(
    model, request_text: str, state: _State, record: AttemptRecord, emit: Optional[EmitFn]
) -> Optional[dict[str, str]]:
    """Ask the model for files, parse and guard them. Returns None (and sets feedback) on rejection."""
    reply = _ask_model(model, request_text, state, record.number, emit)
    try:
        files = parse_code_blocks(reply)
    except ParseError as exc:
        record.outcome, record.detail = "parse_error", str(exc)
        state.feedback = (
            f"{exc} Reply with two fenced blocks: `### {SOLUTION_FILE}` then ```python ... ``` "
            f"and `### {TESTS_FILE}` then ```python ... ```."
        )
        return None

    solution = files.get(SOLUTION_FILE)
    if state.tests_locked:
        tests = state.tests                      # model-sent tests are ignored when the seed tests are locked
    else:
        tests = files.get(TESTS_FILE, state.tests)   # a retry may resend only the fixed solution
    if solution is None or tests is None:
        missing = SOLUTION_FILE if solution is None else TESTS_FILE
        record.outcome, record.detail = "parse_error", f"missing the {missing} block"
        state.feedback = f"Your reply is missing the {missing} code block. Send both files."
        return None

    problems = check_solution(solution) + check_tests(tests, state.last_test_count)
    if problems:
        record.outcome, record.detail = "rejected", "; ".join(problems)
        state.feedback = "Your files were rejected without running because: " + "; ".join(problems) + "."
        return None
    return {SOLUTION_FILE: solution, TESTS_FILE: tests}


def run_code_task(
    request_text: str,
    emit: Optional[EmitFn] = None,
    seed_code: Optional[str] = None,
) -> CodeResult:
    """
    Generate, test and fix code for `request_text`. Returns a CodeResult
    whose tests all passed. Raises CodeFlowError (with an ERROR_CODES code
    and, when available, the last CodeResult) if it cannot get there.
    """
    try:
        return _run_code_task(request_text, emit, seed_code)
    except CodeFlowError:
        raise
    except Exception as exc:  # never crash the caller with a raw exception
        raise CodeFlowError("INTERNAL", f"Unexpected error in code flow: {exc!r}") from exc


def _run_code_task(request_text: str, emit: Optional[EmitFn], seed_code: Optional[str]) -> CodeResult:
    if not request_text or not request_text.strip():
        raise CodeFlowError("BAD_REQUEST", "Code task request is empty.")
    model = _coder_model()
    state = _State()
    seed = _seed_files(seed_code) if seed_code else {}
    state.solution = seed.get(SOLUTION_FILE)
    state.tests = seed.get(TESTS_FILE)
    state.tests_locked = state.tests is not None

    for number in range(1, settings.WB_CODE_MAX_ATTEMPTS + 1):
        started = time.monotonic()
        use_seed = number == 1 and state.solution is not None and state.tests is not None
        record = AttemptRecord(number=number, source="seed" if use_seed else "model")
        state.attempts.append(record)

        if use_seed:
            problems = check_tests(state.tests)
            candidate = None if problems else {SOLUTION_FILE: state.solution, TESTS_FILE: state.tests}
            if problems:
                record.outcome, record.detail = "rejected", "; ".join(problems)
                state.feedback = "The seed tests were rejected because: " + "; ".join(problems) + "."
        else:
            candidate = _get_candidate(model, request_text, state, record, emit)

        if candidate is not None:
            state.solution, state.tests = candidate[SOLUTION_FILE], candidate[TESTS_FILE]
            state.last_test_count = count_test_functions(state.tests)
            run = _run(candidate, PYTEST_COMMAND, f"Run tests (attempt {number})", emit)
            state.last_run = run
            record.passed, record.failed = run.tests_passed or 0, run.tests_failed or 0
            if run.ok and record.failed == 0 and record.passed > 0:
                record.outcome = "passed"
                record.duration_ms = int((time.monotonic() - started) * 1000)
                _log(emit, record)
                steps = _run(candidate, RUN_COMMAND, "Run solution.py (calculation steps)", emit)
                steps_text = steps.stdout if steps.ok else (steps.stdout + "\n" + steps.stderr).strip()
                return _result(state, steps_text)
            record.outcome = "timeout" if run.timed_out else "tests_failed"
            state.feedback = _pytest_feedback(run)
            record.detail = _first_failure(run) if record.outcome == "tests_failed" else state.feedback

        record.duration_ms = int((time.monotonic() - started) * 1000)
        _log(emit, record)

    last = state.attempts[-1]
    code = "SANDBOX_TIMEOUT" if last.outcome == "timeout" else "BAD_MODEL_OUTPUT"
    raise CodeFlowError(
        code,
        f"Code did not pass its tests after {len(state.attempts)} attempt(s); last problem: {last.outcome}"
        + (f" ({last.detail})" if last.detail else ""),
        result=_result(state, "") if state.solution else None,
    )
