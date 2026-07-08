"""OpenCode agent runner — three roles via ``opencode run`` subprocesses:

  adapter       — generates adaptations (adapter.md + adapt-guide)
  adapter-fix   — fixes failures (adapter-fix.md + diagnosis-guide + error-patterns)
  adapter-qa    — independent critic review (adapter-qa.md + review-lessons checklist)

All JSON events streamed to console and logged under step_dir.
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
from typing import Any, Literal

from pydantic import BaseModel, Field

from main2main_flow.scripts.utils.utils import ts_print

_AGENT_DIR = Path(__file__).parent.parent.parent / "agents"
_TIMEOUT_MINUTES = 30
_STALE_SECONDS = 300
_MAX_STALE_RETRIES = 3
_DEFAULT_MODEL = os.environ.get("MAIN2MAIN_MODEL", "deepseek/deepseek-chat")

# Verify opencode is available at import time
if not shutil.which("opencode"):
    raise SystemExit(
        "opencode CLI not found. Install it with:\n"
        "  curl -fsSL https://opencode.ai/install | bash\n"
        "Or: npm install -g opencode-ai"
    )

# ── prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(inputs: dict[str, Any]) -> str:
    role = inputs.get("role", "adapter")
    # Both "adapter" and "adapter-fix" use the same agents/adapter/ directory
    agent_dir = "adapter"
    template = (_AGENT_DIR / agent_dir / "SKILL.md").read_text(encoding="utf-8")
    ctx = {k: str(v) for k, v in inputs.items()}

    code_structure = _load_ref(agent_dir, "code-structure-guide.md")
    if role == "adapter-fix":
        ref_content = (_load_ref(agent_dir, "diagnosis-guide.md") + "\n\n"
                       + _load_ref(agent_dir, "error-pattern-examples.md") + "\n\n"
                       + code_structure)
    else:  # adapter
        ref_content = (_load_ref(agent_dir, "adapt-guide.md") + "\n\n"
                       + code_structure)

    ctx["reference_content"] = ref_content

    # Inline error content from error_logs files (if any)
    error_content = ""
    error_logs_val = inputs.get("error_logs", "").strip()
    if error_logs_val:
        parts = []
        for p in error_logs_val.splitlines():
            p = p.strip()
            if p and Path(p).exists():
                try:
                    parts.append(Path(p).read_text(encoding="utf-8")[:4000])
                except Exception:
                    parts.append(f"(could not read {p})")
        error_content = "\n\n".join(parts)
    ctx["error_content"] = error_content or "(none)"

    return template.format_map(ctx)


def _load_ref(role: str, filename: str) -> str:
    path = _AGENT_DIR / role / "reference" / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _build_continue_prompt(base_prompt: str, inputs: dict[str, Any], retry: int) -> str:
    step_dir = inputs.get("step_dir", "")
    return f"""Continue the adaptation task for step {inputs.get('step_id', '')}.

The previous opencode run produced no output for {_STALE_SECONDS} seconds and
was terminated. This is continuation retry {retry}/{_MAX_STALE_RETRIES}.

Do not start from scratch. The current vllm-ascend working tree may already
contain partial code changes from the previous attempt. These files may also
contain partial results:

  - {step_dir}/analysis.md
  - {step_dir}/review.md
  - {step_dir}/step_summary.md
  - {step_dir}/opencode.log
  - {step_dir}/opencode_raw.jsonl

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
    session_id: str = Field(default="")


# ── main entry point ──────────────────────────────────────────────────────────

def run_opencode_adapter(inputs: dict[str, Any],
                         session_id: str = "") -> AdaptResult:
    base_prompt = _build_prompt(inputs)
    prompt = base_prompt
    step_dir = inputs.get("step_dir", "")
    step_path = Path(step_dir) if step_dir else None
    log_path = step_path / "opencode.log" if step_path else None
    raw_path = step_path / "opencode_raw.jsonl" if step_path else None
    stderr_path = step_path / "opencode_stderr.log" if step_path else None
    new_session_id = session_id

    if log_path:
        log_path.write_text("")
    if raw_path:
        raw_path.write_text("")
    if stderr_path:
        stderr_path.write_text("")

    all_lines: list[str] = []
    last_reason: _StopReason | None = None

    for attempt in range(_MAX_STALE_RETRIES + 1):
        _print_prompt(prompt, attempt)
        if log_path:
            _log_prompt(prompt, attempt, log_path)

        lines, reason, sid, rc = _run_once(prompt, log_path, raw_path, stderr_path, session_id)
        all_lines.extend(lines)
        last_reason = reason
        if sid:
            new_session_id = sid
            session_id = sid  # retries also use the same session

        # Treat opencode exit != 0 or zero JSON events as a hard failure,
        # not a "no-op" (prevents silent false-success when the agent
        # crashes on launch, e.g. bad API key or model not available).
        if rc != 0 or not lines:
            ts_print(f"\n[opencode] HARD FAILURE: exit={rc}, events={len(lines)}", flush=True)
            last_reason = last_reason or "hard_failure"
            if attempt < _MAX_STALE_RETRIES:
                prompt = _build_continue_prompt(base_prompt, inputs, attempt + 1)
                continue
            break

        if reason is None:
            break

        if reason == "stale_timeout" and attempt < _MAX_STALE_RETRIES:
            retry = attempt + 1
            ts_print(f"\n[opencode] retrying after stale timeout ({retry}/{_MAX_STALE_RETRIES})", flush=True)
            prompt = _build_continue_prompt(base_prompt, inputs, retry)
            continue

        if stderr_path and stderr_path.exists():
            stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
            if stderr_content:
                ts_print(f"\n[opencode] stderr tail:\n{stderr_content}", flush=True)
        break

    result = _build_result(step_path, inputs.get("ascend_path", ""), "".join(all_lines))
    result.session_id = new_session_id
    if last_reason and not result.step_summary:
        result.step_summary = f"opencode process stopped due to {last_reason}"
    return result


_StopReason = Literal["stale_timeout", "total_timeout"]


def _print_prompt(prompt: str, attempt: int) -> None:
    title = "PROMPT" if attempt == 0 else f"CONTINUE PROMPT #{attempt}"
    ts_print(f"\n{'═'*60}")
    ts_print(title)
    ts_print(f"{'═'*60}")
    ts_print(prompt)
    ts_print(f"{'═'*60}\n")


def _log_prompt(prompt: str, attempt: int, log_path: Path) -> None:
    title = "PROMPT" if attempt == 0 else f"CONTINUE PROMPT #{attempt}"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{ts}] {'═'*60}\n[{ts}] {title}:\n[{ts}] {'═'*60}\n{prompt}\n[{ts}] {'═'*60}\n\n")


def _run_once(
    prompt: str,
    log_path: Path | None,
    raw_path: Path | None,
    stderr_path: Path | None,
    session_id: str | None = None,
) -> tuple[list[str], _StopReason | None, str, int]:
    stderr_fh = stderr_path.open("a", encoding="utf-8") if stderr_path else None
    cmd = [
        "opencode", "run",
        "--format", "json",
        "--model", _DEFAULT_MODEL,
        "--dangerously-skip-permissions",
    ]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(prompt)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=stderr_fh or subprocess.DEVNULL,
        text=True,
        bufsize=1,
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

    deadline = time.monotonic() + _TIMEOUT_MINUTES * 60
    last_output_time = time.monotonic()
    stop_reason: _StopReason | None = None
    extracted_sid = ""

    try:
        while True:
            try:
                line = lines_queue.get(timeout=1.0)
            except queue.Empty:
                now = time.monotonic()
                if now > deadline:
                    ts_print(f"\n[opencode] TOTAL TIMEOUT ({_TIMEOUT_MINUTES}min), killing process", flush=True)
                    proc.kill()
                    stop_reason = "total_timeout"
                    break
                if now - last_output_time > _STALE_SECONDS:
                    ts_print(f"\n[opencode] STALE TIMEOUT ({_STALE_SECONDS}s no output), killing process", flush=True)
                    proc.kill()
                    stop_reason = "stale_timeout"
                    break
                continue

            if line is None:
                break

            last_output_time = time.monotonic()
            state.lines.append(line)
            if not extracted_sid:
                try:
                    ev = json.loads(line)
                    extracted_sid = ev.get("sessionID", "")
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

    return state.lines, stop_reason, extracted_sid, proc.returncode


# ── event state ───────────────────────────────────────────────────────────────

class _EventState:
    """Tracks callID → tool name for attributing output."""
    def __init__(self) -> None:
        self.lines: list[str] = []
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
            if tool == "Agent":
                agent_name = inp.get("name", "") or inp.get("subagent_type", "?")
                ts_print(f"\n{'━'*60}", flush=True)
                ts_print(f"▶ [agent: {agent_name}] spawning ({tool})", flush=True)
                ts_print(f"{'━'*60}", flush=True)
            elif tool == "TeamCreate":
                team_name = inp.get("team_name", "?")
                ts_print(f"\n▶ [agent] creating team '{team_name}'", flush=True)
            elif tool == "SendMessage":
                to = inp.get("to", "?")
                summary = inp.get("summary", "")
                ts_print(f"\n▶ [agent] → {to}: {summary}", flush=True)
            else:
                brief = json.dumps(inp, ensure_ascii=False)[:200]
                ts_print(f"\n[agent: {tool}] ← {brief}", flush=True)

        elif status == "completed":
            output = st.get("output", "")
            if output:
                agent = state._tool_by_call.get(call_id, "")
                label = f"agent: {agent}" if agent else "agent"
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
    if step_dir:
        summary_path = step_dir / "step_summary.md"
        if summary_path.exists():
            summary = summary_path.read_text(encoding="utf-8")

    if not summary:
        summary = _text_from_jsonl(jsonl)[-4000:]

    modified_files = _modified_files(ascend_path)
    return AdaptResult(
        modified_files=modified_files,
        is_noop=not modified_files,
        step_summary=summary,
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
        # git diff excludes untracked files — add intent-to-add first
        subprocess.run(["git", "add", "-N", "."], cwd=ascend_path,
                       capture_output=True)
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
