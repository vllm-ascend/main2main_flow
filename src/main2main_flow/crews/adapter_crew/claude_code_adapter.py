"""Claude Code CLI adapter for the adapter crew.

Agent descriptions and task descriptions are loaded from config/agents.yaml
and config/tasks.yaml — the same source used by CrewAI — so both frameworks
share a single definition.

Spawns `claude -p --output-format stream-json` as a subprocess.
The orchestrator uses Claude Code's built-in Agent tool to run 4 subagents:
  patch_analyzer ↔ analyzer_qa  (up to 3 rounds)
  code_adapter   ↔ code_reviewer (up to 3 rounds)
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

_CONFIG_DIR = Path(__file__).parent / "config"

# agent key → task key
_AGENT_TASK_MAP = {
    "patch_analyzer": "analyze_patch_task",
    "analyzer_QA":    "analyze_qa_task",
    "code_adapter":   "adapt_code_task",
    "code_reviewer":  "review_code_task",
}


# ── config loaders ────────────────────────────────────────────────────────────

def _load_yaml(name: str) -> dict:
    return yaml.safe_load((_CONFIG_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def _build_agent_prompt(agent_key: str, inputs: dict[str, Any]) -> str:
    """Combine role/goal/backstory + task description into one agent prompt."""
    agents = _load_yaml("agents")
    tasks = _load_yaml("tasks")
    task_key = _AGENT_TASK_MAP[agent_key]

    a = agents[agent_key]
    t = tasks[task_key]

    role      = a.get("role", "").strip()
    goal      = a.get("goal", "").strip()
    backstory = a.get("backstory", "").strip()
    desc      = t.get("description", "").strip()
    expected  = t.get("expected_output", "").strip()

    # substitute known inputs; leave unknown placeholders intact
    safe = {k: str(v) for k, v in inputs.items()}
    def fmt(s: str) -> str:
        try:
            return s.format_map(safe)
        except (KeyError, ValueError):
            return s

    return (
        f"You are a {fmt(role)}.\n\n"
        f"GOAL:\n{fmt(goal)}\n\n"
        f"BACKGROUND:\n{fmt(backstory)}\n\n"
        f"## Task\n\n{fmt(desc)}\n\n"
        f"## Expected output\n\n{fmt(expected)}"
    )


# ── result model ──────────────────────────────────────────────────────────────

class AdaptResult(BaseModel):
    modified_files: list[str] = Field(default_factory=list)
    is_noop: bool = Field(default=False)
    step_summary: str = Field(default="")


# ── main entry point ──────────────────────────────────────────────────────────

def run_claude_code_adapter(inputs: dict[str, Any]) -> AdaptResult:
    prompt = _build_orchestrator_prompt(inputs)

    proc = subprocess.Popen(
        [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ],
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        bufsize=1,
    )

    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        _print_event(line)
    proc.wait()

    return _parse_result("".join(lines))


# ── event printer ─────────────────────────────────────────────────────────────

def _print_event(line: str) -> None:
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return

    t = ev.get("type")

    if t == "assistant":
        for block in ev.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text":
                print(block.get("text", ""), end="", flush=True)
            elif btype == "tool_use":
                name = block.get("name", "")
                inp  = block.get("input", {})
                if name == "Agent":
                    agent_type = inp.get("subagent_type", "?")
                    print(f"\n{'━'*60}", flush=True)
                    print(f"▶ subagent [{agent_type}] starting", flush=True)
                    print(f"{'━'*60}", flush=True)
                else:
                    brief = json.dumps(inp, ensure_ascii=False)[:200]
                    print(f"\n[{name}] ← {brief}", flush=True)

    elif t == "user":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                for c in content:
                    if c.get("type") == "text" and c.get("text"):
                        print(f"\n{'─'*60}", flush=True)
                        print(f"◀ tool result:", flush=True)
                        print(c["text"], flush=True)
                        print(f"{'─'*60}\n", flush=True)
            elif isinstance(content, str) and content:
                print(f"\n{'─'*60}", flush=True)
                print(f"◀ tool result:", flush=True)
                print(content, flush=True)
                print(f"{'─'*60}\n", flush=True)

    elif t == "result" and ev.get("is_error"):
        print(f"\n[error] {ev.get('result', '')}", flush=True)


# ── orchestrator prompt ───────────────────────────────────────────────────────

def _build_orchestrator_prompt(inputs: dict[str, Any]) -> str:
    step_id    = inputs.get("step_id", "")
    step_dir   = inputs.get("step_dir", "")
    ascend_path = inputs.get("ascend_path", "")
    vllm_path  = inputs.get("vllm_path", "")
    release_tag = inputs.get("release_tag", "")
    reference_dir = inputs.get("reference_dir", "")
    patch_path = inputs.get("patch_path", "")
    changed_files_path = inputs.get("changed_files_path", "")

    # build each agent's full system prompt from YAML
    analyzer_prompt = _build_agent_prompt("patch_analyzer", inputs)
    qa_prompt       = _build_agent_prompt("analyzer_QA", inputs)
    adapter_prompt  = _build_agent_prompt("code_adapter", inputs)
    reviewer_prompt = _build_agent_prompt("code_reviewer", inputs)

    return f"""\
You are the orchestrator for adapting vllm-ascend to upstream vLLM changes (step {step_id}).

REPOSITORIES:
  vllm:         {vllm_path}
  vllm-ascend:  {ascend_path}
  reference:    {reference_dir}

ARCHIVE DIRECTORY: {step_dir}

━━━ YOUR TEAM ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You have 4 specialist subagents. Spawn each via the Agent tool with
subagent_type="claude" and the prompt shown below.

┌─ patch_analyzer ──────────────────────────────────────────────────────────┐
{analyzer_prompt}
└───────────────────────────────────────────────────────────────────────────┘

┌─ analyzer_qa ─────────────────────────────────────────────────────────────┐
{qa_prompt}
└───────────────────────────────────────────────────────────────────────────┘

┌─ code_adapter ────────────────────────────────────────────────────────────┐
{adapter_prompt}
└───────────────────────────────────────────────────────────────────────────┘

┌─ code_reviewer ───────────────────────────────────────────────────────────┐
{reviewer_prompt}
└───────────────────────────────────────────────────────────────────────────┘

━━━ WORKFLOW ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHASE 1 — Analysis + QA (up to 3 rounds):

  Round loop:
  a) Spawn patch_analyzer. Pass prior QA rejection feedback (if any).
     Append output to {step_dir}/analysis.md with "## Round N" header.

  b) Spawn analyzer_qa. Pass patch_analyzer's full output.
     Append output to {step_dir}/analysis_qa.md with "## Round N" header.

  c) REJECTED → feed rejection back to patch_analyzer, repeat (max 3).
     APPROVED → proceed to Phase 2.

PHASE 2 — Code Adaptation + Review (up to 3 rounds):

  Round loop:
  a) Spawn code_adapter. Pass approved analysis + prior reviewer feedback (if any).
     Append output to {step_dir}/adaptation_log.md with "## Round N" header.

  b) Spawn code_reviewer. Pass analysis plan + code_adapter output.
     Append output to {step_dir}/review.md with "## Round N" header.

  c) Issues found → feed back to code_adapter, repeat (max 3).
     APPROVED → output final result.

━━━ FINAL OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Return the code_reviewer's final JSON block verbatim:
```json
{{
  "modified_files": ["vllm_ascend/foo.py"],
  "is_noop": false,
  "step_summary": "..."
}}
```
"""


# ── result parser ─────────────────────────────────────────────────────────────

def _parse_result(jsonl: str) -> AdaptResult:
    text_parts: list[str] = []
    for line in jsonl.strip().splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = ev.get("type")
        if t == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        elif t == "result":
            text_parts.append(ev.get("result", ""))

    full_text = "\n".join(text_parts)
    matches = re.findall(r"```json\s*(.*?)\s*```", full_text, re.DOTALL)
    if matches:
        try:
            return AdaptResult(**json.loads(matches[-1]))
        except (json.JSONDecodeError, TypeError):
            pass

    return AdaptResult(step_summary=full_text[-4000:] if full_text else "")
