"""OpenCode agent runner — three roles via ``opencode run`` subprocesses:

  adapter       — generates adaptations (adapter.md + adapt-guide)
  adapter-fix   — fixes failures (SKILL.md fix mode + session context)
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

from main2main_flow.scripts.agent.toolguard import guard_env
from main2main_flow.scripts.utils.utils import ts_print

_AGENT_DIR = Path(__file__).parent.parent.parent / "agents"
# A step adaptation needs ~570 tool calls at ~14s/call (~2h); a 30min cap
# killed the model mid-work repeatedly (run 32809534429: 6 kills), and each
# SIGKILL+resume made the session context larger and every later call slower.
# 60min halves the kill count; the stale/event-stale checks below still bound
# genuinely stuck sessions.  Validation runs on the fork set
# MAIN2MAIN_ADAPTER_TIMEOUT_MINUTES=10 to keep the loop testable — a killed
# session resumes with the short continue prompt, so an under-budget test
# run still exercises the real path.
_TIMEOUT_MINUTES = int(os.environ.get("MAIN2MAIN_ADAPTER_TIMEOUT_MINUTES", "60"))
_STALE_SECONDS = 300
# No JSONL progress event (step_start/tool_use/text/step_finish) for this
# long → kill.  The stdout-based stale check misses sessions that stream
# progress output while the model stalls for many minutes between events
# (observed 26 min of non-event output in an adapter-qa session).
_EVENT_STALE_SECONDS = 600
_MAX_STALE_RETRIES = 3
_DEFAULT_MODEL = os.environ.get("MAIN2MAIN_MODEL", "deepseek/deepseek-chat")

# Per-file budget for inlining error_logs content into the prompt.  Kept
# moderate: the prompt is re-processed on every tool call, so unbounded error
# text slows each call.  Head+tail truncation (not head-only) preserves the
# last failure's summary and the final traceback.
_ERROR_INLINE_LIMIT = 24000

# Prompts larger than this are piped to opencode via stdin instead of
# being passed as a positional argv element (Linux MAX_ARG_STRLEN = 128 KiB
# per argument; execve fails E2BIG beyond it — run 32873364134).
_MAX_ARGV_PROMPT_CHARS = 100_000
_ERROR_INLINE_HEAD_FRAC = 0.6

# Per-role model overrides.  The analysis/fix roles do mechanical routing
# (read diff, map symbols, edit) where deep per-call reasoning is not
# needed — a non-thinking model (deepseek-chat, reasoning:false) cuts the
# adapter phase from 30+ min to minutes.  The review role keeps the
# thinking model (its adversarial judgment benefits from deep reasoning).
_ROLE_MODEL_ENVS = {
    "adapter": "MAIN2MAIN_MODEL_ADAPT",
    "adapter-fix": "MAIN2MAIN_MODEL_FIX",
}


def _resolve_role_model(role: str) -> str | None:
    env = _ROLE_MODEL_ENVS.get(role)
    return os.environ.get(env) if env else None

# Verify opencode is available at import time
if not shutil.which("opencode"):
    raise SystemExit(
        "opencode CLI not found. Install it with:\n"
        "  curl -fsSL https://opencode.ai/install | bash\n"
        "Or: npm install -g opencode-ai"
    )

# ── prompt builder ─────────────────────────────────────────────────────────────

def _build_prompt(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    role = inputs.get("role", "adapter")
    # description-fill is a read-only analysis role with its own SKILL.md.
    if role == "description-fill":
        agent_dir = "description-fill"
    else:
        agent_dir = "adapter"
    template = (_AGENT_DIR / agent_dir / "SKILL.md").read_text(encoding="utf-8")
    ctx = {k: str(v) for k, v in inputs.items()}

    refs_loaded: list[str] = []
    if role == "adapter-fix":
        ref_content = "(reference docs already in session context — see previous messages)"
        # vllm-report context is per-step; in fix mode (same step, retry) it is
        # already in session - do not re-send.  BUT keep the MCP dynamic-mode
        # instruction (which tells the adapter to call get_adaptation_lessons):
        # the final quality gate's fix rounds pass it fresh — without it the
        # adapter fixes UT/test failures blind (it previously got
        # "vllm-report unavailable").
        if (ctx.get("vllm_report_context")
                and "MCP server is registered" not in ctx["vllm_report_context"]):
            ctx["vllm_report_context"] = "(vllm-report impact map already in session context — see previous messages)"
    elif role == "description-fill":
        # description-fill is self-contained — no reference docs needed.
        # vllm-report context is not passed (role is invoked at description
        # generation time, not per-step).
        ref_content = "(no reference docs for description-fill role)"
        ctx["vllm_report_context"] = "(vllm-report unavailable, use grep on vllm source)"
    else:
        ref_names = ["adaptation-patterns.md",
                     "common-pitfalls.md"]
        # Load code-structure-guide only on the first step (static mapping table)
        if inputs.get("step_id") == "step-1":
            ref_names.append("code-structure-guide.md")
        # Do NOT inline the reference docs: every tool-call generation
        # re-processes the full prompt, so ~32KB of static reference text
        # (~8K tokens) made each call slow (adapter prompt was 53.6K chars).
        # Pass absolute paths instead; the model reads only the section it
        # needs, when it needs it.  The docs are index-first (## Index table
        # with line ranges per section) so a section read is one `sed` call.
        ref_dir = _AGENT_DIR / agent_dir / "reference"
        ref_content = (
            "Reference docs are index-first: each starts with a `## Index` "
            "table of sections and their line ranges. Read ONLY the index and "
            "the sections whose trigger matches your change "
            "(e.g. `sed -n 'A,Bp' <path>`) — never read a whole reference file. "
            "Content you read earlier in this session is already in your "
            "context; do not re-read it.\n"
            + "\n".join(f"- {rf}: {ref_dir / rf}" for rf in ref_names)
        )
        refs_loaded = ref_names

    ctx["reference_content"] = ref_content
    # Ensure vllm_report_context placeholder is never empty (avoids KeyError
    # in format_map if flow did not pass it, e.g. CLI/debug invocations).
    ctx.setdefault("vllm_report_context", "(vllm-report unavailable, use grep)")

    # Inline error content from error_logs files (if any).
    # error_logs is a JSON array of file paths, e.g. ["/path/a.txt", "/path/b.json"].
    # Fall back to newline-separated for backward compatibility.
    # Truncation keeps BOTH ends: e2e test-errors.txt appends per-test blocks
    # (log excerpt + structured summary), so a head-only cut silently drops
    # every failure past the first few — including the last test's summary,
    # which carries the code_bugs verdict the adapter needs to distinguish a
    # real bug from an env flake.  The tail of a crashed/hung log also holds
    # the final traceback; keep it.
    error_content = ""
    error_logs_raw = inputs.get("error_logs", "").strip()
    if error_logs_raw:
        def _read_error_file(p: str) -> str:
            try:
                text = Path(p).read_text(encoding="utf-8")
            except Exception:
                return f"(could not read {p})"
            if len(text) <= _ERROR_INLINE_LIMIT:
                return text
            head = int(_ERROR_INLINE_LIMIT * _ERROR_INLINE_HEAD_FRAC)
            mark = f"\n...[truncated {len(text) - _ERROR_INLINE_LIMIT} chars]...\n"
            tail = _ERROR_INLINE_LIMIT - head - len(mark)
            return text[:head] + mark + text[-tail:]

        try:
            paths = json.loads(error_logs_raw)
            if isinstance(paths, list):
                parts = [_read_error_file(p) for p in paths if Path(p).exists()]
                error_content = "\n\n".join(parts)
        except (json.JSONDecodeError, ValueError):
            # Legacy: one path per line
            parts = [
                _read_error_file(p.strip())
                for p in error_logs_raw.splitlines()
                if p.strip() and Path(p.strip()).exists()
            ]
            error_content = "\n\n".join(parts)
    ctx["error_content"] = error_content or "(none)"

    return template.format_map(ctx), refs_loaded


def _build_continue_prompt(inputs: dict[str, Any], retry: int) -> str:
    step_dir = inputs.get("step_dir", "")
    return f"""Continue the adaptation task for step {inputs.get('step_id', '')}.

The previous opencode run was terminated for lack of progress
({_STALE_SECONDS}s without output or {_EVENT_STALE_SECONDS}s without a
progress event). This is continuation retry {retry}/{_MAX_STALE_RETRIES}.

The full task prompt, reference docs, and rules are already in this session's
context — do NOT expect them to be repeated here. Re-read them from the
session history if needed. Any file content you read before the termination
(e.g. `sed` output, reference sections, code) is also still in your context —
do NOT re-read those files; go straight to editing or writing.

Do not start from scratch. The current vllm-ascend working tree may already
contain partial code changes from the previous attempt. These files may also
contain partial results:

  - {step_dir}/analysis.md
  - {step_dir}/review.json  (critic output — only if adapter-qa ran for this step)
  - {step_dir}/step_summary.md

First inspect the existing changes and generated files. Reuse prior work, then
continue any unfinished adaptation, static review, and step_summary.md updates.
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
    base_prompt, refs_loaded = _build_prompt(inputs)
    prompt = base_prompt
    role_model = _resolve_role_model(inputs.get("role", "adapter"))
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
        _print_prompt(prompt, attempt, refs_loaded)
        if log_path:
            _log_prompt(prompt, attempt, log_path)

        lines, reason, sid, rc = _run_once(prompt, log_path, raw_path, stderr_path,
                                           session_id, model=role_model)
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
            # Print the command that was run (redact prompt content — it's in log)
            ts_print(f"[opencode] cmd: opencode run --format json --model {_DEFAULT_MODEL} "
                     f"--auto {'--session ' + (session_id or '') if session_id else ''}"
                     f" '<prompt {len(prompt)} chars>'", flush=True)
            if stderr_path and stderr_path.exists():
                err_text = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                if err_text.strip():
                    ts_print(f"[opencode] stderr tail:\n{err_text.strip()}", flush=True)
            last_reason = last_reason or "hard_failure"
            if attempt < _MAX_STALE_RETRIES:
                if session_id:
                    # Session exists - continue it with a short prompt.
                    prompt = _build_continue_prompt(inputs, attempt + 1)
                else:
                    # No session was ever established (launch crash before
                    # sessionID event).  A continue prompt would tell the model
                    # to "re-read from session history" that doesn't exist.
                    # Re-send the full base prompt so the model has context.
                    ts_print(f"\n[opencode] no session established - re-sending full prompt "
                             f"(retry {attempt + 1})", flush=True)
                    prompt = base_prompt
                continue
            break

        if reason is None:
            break

        if reason in ("stale_timeout", "event_stale_timeout") and attempt < _MAX_STALE_RETRIES:
            retry = attempt + 1
            ts_print(f"\n[opencode] retrying after {reason} ({retry}/{_MAX_STALE_RETRIES})", flush=True)
            prompt = _build_continue_prompt(inputs, retry)
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


def run_opencode_review(
    prompt: str,
    log_path: Path | None = None,
    raw_path: Path | None = None,
    stderr_path: Path | None = None,
    session_id: str = "",
    model: str | None = None,
) -> tuple[str, str]:
    """Run opencode as an independent reviewer.

    Uses the same streaming pattern as run_opencode_adapter (Popen, streaming
    output, stale_timeout + total_timeout + retry) but for review-only use.
    Returns (output_text, session_id).
    """
    lines, _reason, sid, _rc = _run_once(
        prompt, log_path, raw_path, stderr_path, session_id or None,
        model=model,
    )
    return "".join(lines), sid or ""


_StopReason = Literal["stale_timeout", "event_stale_timeout", "total_timeout"]


def _print_prompt(prompt: str, attempt: int, refs: list[str] | None = None) -> None:
    title = "PROMPT" if attempt == 0 else f"CONTINUE PROMPT #{attempt}"
    ts_print(f"\n{'═'*60}")
    ts_print(f"{title} ({len(prompt)} chars, ~{len(prompt)//4} tokens)")
    if refs:
        ts_print(f"  refs: {', '.join(refs)}")
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
    model: str | None = None,
) -> tuple[list[str], _StopReason | None, str, int]:
    stderr_fh = stderr_path.open("a", encoding="utf-8") if stderr_path else None
    # opencode >=2.x uses --auto, older versions use --dangerously-skip-permissions
    auto_flag = "--dangerously-skip-permissions"
    r = subprocess.run(["opencode", "run", "--help"], capture_output=True, text=True)
    if "--auto" in (r.stdout + r.stderr):
        auto_flag = "--auto"

    cmd = [
        "opencode", "run",
        "--format", "json",
        "--model", model or _DEFAULT_MODEL,
        auto_flag,
    ]
    if session_id:
        cmd += ["--session", session_id]

    # Linux caps a single argv element at 128 KiB (MAX_ARG_STRLEN): a
    # fix-mode prompt carrying several inlined test-errors logs exceeds
    # that and execve dies with E2BIG (run 32873364134: 131 KB prompt →
    # OSError [Errno 7] Argument list too long, killing the fix loop).
    # opencode also accepts the message via piped stdin (EOF terminates
    # it), so route oversized prompts there instead of argv.
    stdin_prompt = len(prompt) > _MAX_ARGV_PROMPT_CHARS
    if not stdin_prompt:
        cmd += ["--", prompt]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_prompt else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr_fh or subprocess.DEVNULL,
        text=True,
        bufsize=1,
        env=guard_env(),
    )
    if stdin_prompt:
        # opencode blocks until stdin reaches EOF — write and close now.
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()
        ts_print(f"[opencode] prompt {len(prompt)} chars routed via stdin "
                 f"(argv would exceed the 128 KiB per-arg limit)")

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
    last_event_time = time.monotonic()
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
                if now - last_event_time > _EVENT_STALE_SECONDS:
                    ts_print(f"\n[opencode] EVENT STALE TIMEOUT "
                             f"({_EVENT_STALE_SECONDS}s without a progress event), "
                             f"killing process", flush=True)
                    proc.kill()
                    stop_reason = "event_stale_timeout"
                    break
                continue

            if line is None:
                break

            last_output_time = time.monotonic()
            state.lines.append(line)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                ev = None
            if ev is not None:
                if not extracted_sid:
                    extracted_sid = ev.get("sessionID", "")
                if ev.get("type"):
                    last_event_time = time.monotonic()
            if raw_fh:
                raw_fh.write(line)
            _print_event(line, state)
            if log_fh:
                _log_event(line, log_fh)
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

# Built-in opencode tools — NOT MCP tools.  When a tool_use event's tool
# name is in this set, it's a native tool (bash/read/edit/write/Agent/etc.).
# Any other tool name is an MCP tool call — surface it with a clear [MCP]
# marker so CI logs show the dynamic MCP calls at a glance.
_BUILTIN_TOOLS = frozenset({
    "bash", "read", "edit", "write", "grep", "glob",
    "Agent", "TeamCreate", "SendMessage", "TaskCreate", "TaskUpdate",
    "TaskStop", "TodoWrite", "WebFetch", "WebSearch",
})


def _is_mcp_tool(tool: str) -> bool:
    """True if tool is an MCP server tool (not a built-in opencode tool)."""
    return tool not in _BUILTIN_TOOLS


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
        is_mcp = _is_mcp_tool(tool)

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
            elif is_mcp:
                # MCP tool call — surface with clear [MCP] marker so CI
                # logs show the dynamic vllm-report calls at a glance.
                brief = json.dumps(inp, ensure_ascii=False)[:300]
                ts_print(f"\n[MCP] → {tool}({brief})", flush=True)
            else:
                brief = json.dumps(inp, ensure_ascii=False)[:200]
                ts_print(f"\n[tool: {tool}] ← {brief}", flush=True)

        elif status == "completed":
            output = st.get("output", "")
            agent = state._tool_by_call.get(call_id, "") or tool
            if is_mcp:
                # MCP tool completed — show return size (lines/chars) so
                # the user can see what the MCP server returned without
                # dumping the full output (which can be 200+ lines).
                if output:
                    lines = len(output.splitlines())
                    chars = len(output)
                    preview = output[:200].replace("\n", " ").strip()
                    ts_print(f"\n[MCP] ← {tool} returned {lines} lines "
                             f"({chars} chars): {preview}...", flush=True)
                else:
                    ts_print(f"\n[MCP] ← {tool} returned empty", flush=True)
            elif output:
                label = f"agent: {agent}" if agent else "agent"
                display = output if len(output) <= 3000 else output[:3000] + "\n... [truncated]"
                ts_print(f"\n{'─'*60}\n[{label}] output:\n{display}\n{'─'*60}", flush=True)


# ── event logger ─────────────────────────────────────────────────────────────

def _log_event(line: str, fh: Any) -> None:
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
    files = [line for line in result.stdout.splitlines() if line]
    # Exclude the tracking file (.github/vllm-main-verified.commit) — flow
    # always updates it for the step's upstream commit, but that's not an
    # adaptation change.  A step whose only modified file is the tracking
    # file is a true no-op (adapter did not change any vllm-ascend code).
    return [f for f in files if f != ".github/vllm-main-verified.commit"]
