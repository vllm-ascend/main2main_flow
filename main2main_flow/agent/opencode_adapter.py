"""OpenCode single-agent adapter — spawns `opencode run` subprocesses.

One agent performs the whole adapt/fix workflow per attempt (no sub-agent
teams); a separate review-only invocation acts as an independent critic.
All JSON events are printed to console and logged under step_dir with
per-attempt filenames (`opencode-{attempt_tag}.log` etc.).
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from main2main_flow.utils import (
    EACH_STEP_RESULT_FILE, EACH_STEP_REVIEW_FILE, git_intent_to_add, ts_print,
)

_PROMPT_DIR = Path(__file__).parent
_REFERENCE_DIR = Path(__file__).parent.parent / "reference"

_MAX_STALE_RETRIES = 3

_MODEL_ENV_BY_MODE = {
    "adapt": "MAIN2MAIN_MODEL_ADAPT",
    "fix": "MAIN2MAIN_MODEL_FIX",
    "fix_preci": "MAIN2MAIN_MODEL_FIX",
    "review": "MAIN2MAIN_MODEL_REVIEW",
}


def _require_opencode() -> None:
    if not shutil.which("opencode"):
        raise RuntimeError(
            "opencode CLI not found. Install it with:\n"
            "  curl -fsSL https://opencode.ai/install | bash\n"
            "Or: npm install -g opencode-ai"
        )


def _resolve_model(mode: str) -> str:
    model = os.getenv(_MODEL_ENV_BY_MODE.get(mode, ""), "") or os.getenv("MAIN2MAIN_MODEL", "")
    if not model:
        raise RuntimeError(
            "MAIN2MAIN_MODEL is not set. Export an explicit opencode "
            "provider/model id, e.g. deepseek/deepseek-v4-pro, "
            "zhipuai/glm-5.1, anthropic/claude-sonnet-4-6."
        )
    return model


def _timeout_minutes() -> int:
    return int(os.getenv("MAIN2MAIN_TIMEOUT_MIN", "30"))


def _stale_seconds() -> int:
    return int(os.getenv("MAIN2MAIN_STALE_SEC", "300"))


def _review_timeout_minutes() -> int:
    return int(os.getenv("MAIN2MAIN_REVIEW_TIMEOUT_MIN", "10"))


def _subprocess_env() -> dict[str, str]:
    # Untrusted upstream content flows into the agent; keep push credentials
    # out of its reach (only push_to_github needs them).
    return {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}


# ── prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(inputs: dict[str, Any]) -> str:
    mode = inputs.get("mode", "adapt")
    template_name = "prompt_fix_preci.md" if mode == "fix_preci" else "prompt.md"
    template = (_PROMPT_DIR / template_name).read_text(encoding="utf-8")
    ctx = {k: str(v) for k, v in inputs.items()}

    if mode == "adapt":
        refs = ["versioning-primer.md", "adapt-guide.md", "error-pattern-examples.md",
                "review-lessons.md", "output-exemplars.md"]
    elif mode == "fix":
        refs = ["versioning-primer.md", "diagnosis-guide.md", "error-pattern-examples.md",
                "review-lessons.md", "output-exemplars.md"]
    else:  # fix_preci — the template is self-contained
        refs = []

    ctx["reference_content"] = "\n\n".join(c for c in (_load_ref(f) for f in refs) if c)
    return template.format_map(ctx)


def _load_ref(filename: str) -> str:
    path = _REFERENCE_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _short_continue_prompt(inputs: dict[str, Any], retry: int) -> str:
    # Session-resume path: the model still has the original task in context,
    # so a brief pointer at the artifacts is enough.
    step_dir = inputs.get("step_dir", "")
    return f"""Continue the {inputs.get('mode', 'adapt')} task for step {inputs.get('step_id', '')}.

The previous run produced no output for a while and was interrupted
(continuation {retry}/{_MAX_STALE_RETRIES}). Resume where you stopped — do not
start over. The vllm-ascend working tree may already contain partial changes.

Check partial artifacts in {step_dir}/:
  analysis.md, review.md, step_summary.md, result.json

Finish any unfinished edits and artifacts, then write {step_dir}/result.json
as your final action.
"""


def _build_continue_prompt(base_prompt: str, inputs: dict[str, Any], retry: int) -> str:
    step_dir = inputs.get("step_dir", "")
    return f"""Continue the adaptation task for step {inputs.get('step_id', '')}.

The previous opencode run produced no output for {_stale_seconds()} seconds and
was terminated. This is continuation retry {retry}/{_MAX_STALE_RETRIES}.

Do not start from scratch. The current vllm-ascend working tree may already
contain partial code changes from the previous attempt. These files may also
contain partial results:

  - {step_dir}/analysis.md
  - {step_dir}/review.md
  - {step_dir}/step_summary.md
  - {step_dir}/result.json

First inspect the existing changes and generated files. Reuse prior work, then
continue any unfinished adaptation, static review, and step_summary.md updates.
Continue to follow the original task requirements below.

━━━ ORIGINAL TASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{base_prompt}
"""


# ── result model ──────────────────────────────────────────────────────────────

class AdaptResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")
    agent_failed: bool = Field(default=False)
    failure_reason: str = Field(default="")
    status: str = Field(default="")
    session_id: str = Field(default="")


# ── main entry points ─────────────────────────────────────────────────────────

def run_opencode_adapter(inputs: dict[str, Any]) -> AdaptResult:
    _require_opencode()
    mode = inputs.get("mode", "adapt")
    model = _resolve_model(mode)
    attempt_tag = str(inputs.get("attempt_tag") or "r0-a1")

    base_prompt = _build_prompt(inputs)
    prompt = base_prompt
    step_dir = inputs.get("step_dir", "")
    step_path = Path(step_dir) if step_dir else None
    log_path, raw_path, stderr_path = _attempt_paths(step_path, attempt_tag)
    _write_log_header(log_path, model, mode, attempt_tag)

    all_lines: list[str] = []
    last_reason: str | None = None
    last_rc = 0
    session_id = ""
    resume_session = ""

    for attempt in range(_MAX_STALE_RETRIES + 1):
        _print_prompt(prompt, attempt)
        if log_path:
            _log_prompt(prompt, attempt, log_path)

        lines, reason, rc = _run_once(
            prompt, model, log_path, raw_path, stderr_path,
            _timeout_minutes(), _stale_seconds(), session_id=resume_session,
        )
        all_lines.extend(lines)
        last_reason, last_rc = reason, rc
        if not session_id:
            session_id = _capture_session_id(lines)

        if reason is None:
            break

        if reason == "stale_timeout" and attempt < _MAX_STALE_RETRIES:
            retry = attempt + 1
            ts_print(f"\n[opencode] retrying after stale timeout ({retry}/{_MAX_STALE_RETRIES})", flush=True)
            if session_id:
                resume_session = session_id
                prompt = _short_continue_prompt(inputs, retry)
            else:
                prompt = _build_continue_prompt(base_prompt, inputs, retry)
            continue

        _print_stderr_tail(stderr_path)
        break

    result = _build_result(step_path, inputs.get("ascend_path", ""), "".join(all_lines))
    result.session_id = session_id
    if last_reason is not None:
        result.agent_failed = True
        result.failure_reason = (
            f"process_error(rc={last_rc})" if last_reason == "process_error" else last_reason
        )
        if not result.step_summary:
            result.step_summary = f"opencode process stopped: {result.failure_reason}"
    return result


def run_opencode_review(inputs: dict[str, Any]) -> dict[str, Any]:
    """Independent critic pass. Returns {"verdict", "issues", "agent_failed"}."""
    _require_opencode()
    model = _resolve_model("review")
    attempt_tag = str(inputs.get("attempt_tag") or "r0-a1")

    template = (_PROMPT_DIR / "review_prompt.md").read_text(encoding="utf-8")
    prompt = template.format_map({k: str(v) for k, v in inputs.items()})

    step_path = Path(inputs["step_dir"])
    review_path = step_path / EACH_STEP_REVIEW_FILE
    # step_dir is reused across attempts: a stale verdict from an earlier
    # critic run must never be re-read as this run's result.
    review_path.unlink(missing_ok=True)
    log_path, raw_path, stderr_path = _attempt_paths(step_path, f"review-{attempt_tag}")
    _write_log_header(log_path, model, "review", attempt_tag)

    _print_prompt(prompt, 0)
    if log_path:
        _log_prompt(prompt, 0, log_path)

    lines, reason, rc = _run_once(
        prompt, model, log_path, raw_path, stderr_path,
        _review_timeout_minutes(), _stale_seconds(),
    )
    agent_failed = reason is not None
    if agent_failed:
        ts_print(f"[opencode-review] agent failed: {reason} (rc={rc})", flush=True)
        _print_stderr_tail(stderr_path)

    try:
        data = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        ts_print(f"[opencode-review] no usable {EACH_STEP_REVIEW_FILE}: {exc!r} — treating as pass")
        return {"verdict": "pass", "issues": [], "agent_failed": agent_failed}

    verdict = "fail" if data.get("verdict") == "fail" else "pass"
    issues = data.get("issues")
    if not isinstance(issues, list):
        issues = []
    return {"verdict": verdict, "issues": issues, "agent_failed": agent_failed}


# ── attempt logging helpers ───────────────────────────────────────────────────

def _attempt_paths(step_path: Path | None, tag: str) -> tuple[Path | None, Path | None, Path | None]:
    if not step_path:
        return None, None, None
    return (
        step_path / f"opencode-{tag}.log",
        step_path / f"opencode-{tag}_raw.jsonl",
        step_path / f"opencode-{tag}_stderr.log",
    )


def _write_log_header(log_path: Path | None, model: str, mode: str, tag: str) -> None:
    if not log_path:
        return
    header = f"[{datetime.now(timezone.utc).isoformat()}] model={model} mode={mode} attempt={tag}\n"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(header)


def _print_prompt(prompt: str, attempt: int) -> None:
    title = "AGENT PROMPT" if attempt == 0 else f"AGENT CONTINUE PROMPT #{attempt}"
    lines = prompt.splitlines()
    shown = lines[:80]
    ts_print(f"\n{'═'*60}")
    ts_print(title)
    ts_print(f"{'═'*60}")
    ts_print("\n".join(shown))
    if len(lines) > 80:
        ts_print(f"... ({len(lines) - 80} more lines, full prompt in log)")
    ts_print(f"{'═'*60}\n")


def _log_prompt(prompt: str, attempt: int, log_path: Path) -> None:
    title = "AGENT PROMPT" if attempt == 0 else f"AGENT CONTINUE PROMPT #{attempt}"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {'═'*60}\n[{ts}] {title}:\n[{ts}] {'═'*60}\n{prompt}\n[{ts}] {'═'*60}\n\n")


def _print_stderr_tail(stderr_path: Path | None) -> None:
    if stderr_path and stderr_path.exists():
        stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        if stderr_content:
            ts_print(f"\n[opencode] stderr tail:\n{stderr_content}", flush=True)


def _capture_session_id(lines: list[str]) -> str:
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        part = ev.get("part")
        props = ev.get("properties")
        for cand in (
            ev.get("sessionID"),
            part.get("sessionID") if isinstance(part, dict) else None,
            props.get("sessionID") if isinstance(props, dict) else None,
        ):
            if cand:
                return str(cand)
    return ""


# ── subprocess runner ─────────────────────────────────────────────────────────

def _run_once(
    prompt: str,
    model: str,
    log_path: Path | None,
    raw_path: Path | None,
    stderr_path: Path | None,
    timeout_minutes: int,
    stale_seconds: int,
    session_id: str = "",
) -> tuple[list[str], str | None, int]:
    """Run one opencode subprocess.

    Returns (stdout lines, stop_reason, returncode) with stop_reason one of
    None (success), "stale_timeout", "total_timeout", "process_error",
    "no_events".
    """
    cmd = ["opencode", "run", "--format", "json", "--model", model,
           "--dangerously-skip-permissions"]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(prompt)

    stderr_fh = stderr_path.open("a", encoding="utf-8") if stderr_path else None
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=stderr_fh or subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=_subprocess_env(),
    )

    lines_queue: queue.Queue[str | None] = queue.Queue()

    def _stdout_reader():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines_queue.put(line)
        lines_queue.put(None)

    reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
    reader_thread.start()

    state = _EventState()
    log_fh = log_path.open("a", encoding="utf-8") if log_path else None
    raw_fh = raw_path.open("a", encoding="utf-8") if raw_path else None

    deadline = time.monotonic() + timeout_minutes * 60
    last_output_time = time.monotonic()
    stop_reason: str | None = None

    try:
        while True:
            try:
                line = lines_queue.get(timeout=1.0)
            except queue.Empty:
                now = time.monotonic()
                if now > deadline:
                    ts_print(f"\n[opencode] TOTAL TIMEOUT ({timeout_minutes}min), killing process", flush=True)
                    proc.kill()
                    stop_reason = "total_timeout"
                    break
                if now - last_output_time > stale_seconds:
                    ts_print(f"\n[opencode] STALE TIMEOUT ({stale_seconds}s no output), killing process", flush=True)
                    proc.kill()
                    stop_reason = "stale_timeout"
                    break
                continue

            if line is None:
                break

            last_output_time = time.monotonic()
            state.lines.append(line)
            try:
                json.loads(line)
                state.events_parsed += 1
            except json.JSONDecodeError:
                pass
            if raw_fh:
                raw_fh.write(line)
            _print_event(line, state)
            if log_fh:
                _log_event(line, state, log_fh)
    finally:
        if log_fh:
            log_fh.close()
        if raw_fh:
            raw_fh.close()
        if stderr_fh:
            stderr_fh.close()

    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stop_reason = stop_reason or "total_timeout"
        proc.wait(timeout=10)

    returncode = proc.returncode or 0
    if stop_reason is None:
        if returncode != 0:
            stop_reason = "process_error"
        elif state.events_parsed == 0:
            stop_reason = "no_events"

    return state.lines, stop_reason, returncode


# ── event state ───────────────────────────────────────────────────────────────

class _EventState:
    """Tracks callID → tool name for attributing output."""
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.events_parsed: int = 0
        self._tool_by_call: dict[str, str] = {}


# ── event printer ─────────────────────────────────────────────────────────────

def _print_event(line: str, state: _EventState) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return

    t = ev.get("type")
    part = ev.get("part", {})

    if t == "text":
        text = part.get("text", "")
        if text:
            print(text, end="", flush=True)

    elif t == "tool_use":
        tool = part.get("tool", "")
        call_id = part.get("callID", "")
        st = part.get("state", {})
        status = st.get("status", "")
        inp = st.get("input", {})

        if status == "pending":
            state._tool_by_call[call_id] = tool
            brief = json.dumps(inp, ensure_ascii=False)[:200]
            ts_print(f"\n[agent: {tool}] ← {brief}", flush=True)

        elif status == "completed":
            output = st.get("output", "")
            if output:
                tool_name = state._tool_by_call.get(call_id, "")
                label = f"agent: {tool_name}" if tool_name else "agent"
                # Truncate very long outputs for display
                display = output if len(output) <= 3000 else output[:3000] + "\n... [truncated]"
                ts_print(f"\n{'─'*60}\n[{label}] output:\n{display}\n{'─'*60}", flush=True)


# ── event logger ─────────────────────────────────────────────────────────────

def _log_event(line: str, state: _EventState, fh: Any) -> None:  # noqa: ARG001
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}"
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        fh.write(f"[{ts}] {line}")
        return

    t = ev.get("type")
    part = ev.get("part", {})

    if t == "text":
        text = part.get("text", "")
        if text:
            fh.write(f"[{ts}] {text}")

    elif t == "tool_use":
        tool = part.get("tool", "")
        st = part.get("state", {})
        inp = json.dumps(st.get("input", {}), ensure_ascii=False)
        fh.write(f"\n[{ts}] [agent: {tool}] ← {inp[:500]}\n")
        output = st.get("output", "")
        if output:
            fh.write(f"[{ts}] {'─'*60}\n[{ts}] [output]\n[{ts}] {output[:4000]}\n[{ts}] {'─'*60}\n")

    fh.flush()


# ── result builder ─────────────────────────────────────────────────────────────

def _build_result(step_dir: Path | None, ascend_path: str, jsonl: str) -> AdaptResult:
    summary = ""
    status = ""
    if step_dir:
        summary_path = step_dir / "step_summary.md"
        if summary_path.exists():
            summary = summary_path.read_text(encoding="utf-8")
        result_path = step_dir / EACH_STEP_RESULT_FILE
        if result_path.exists():
            try:
                status = str(json.loads(result_path.read_text(encoding="utf-8")).get("status", ""))
            except (OSError, json.JSONDecodeError) as exc:
                ts_print(f"[opencode] malformed {EACH_STEP_RESULT_FILE}: {exc!r}")

    if not summary:
        summary = _text_from_jsonl(jsonl)[-4000:]

    if ascend_path:
        # intent-to-add so brand-new files show up in git diff HEAD
        git_intent_to_add(ascend_path)
    modified_files = _modified_files(ascend_path)
    return AdaptResult(
        modified_files=modified_files,
        is_noop=not modified_files,
        step_summary=summary,
        status=status,
    )


def _text_from_jsonl(jsonl: str) -> str:
    text_parts: list[str] = []
    for line in jsonl.strip().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "text":
            text_parts.append(ev.get("part", {}).get("text", ""))
    return "\n".join(text_parts)


def _modified_files(ascend_path: str) -> list[str]:
    if not ascend_path:
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=ascend_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    return [line for line in result.stdout.splitlines() if line]
