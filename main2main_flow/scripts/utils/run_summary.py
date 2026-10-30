"""End-of-run Main2Main summary for the Actions job page.

emit_plan_summary (plan_steps.py) says what the run INTENDS to do; this module
says what actually happened — per step, how many e2e rounds it burned, what
each round's pre_ci (format/mypy/UT) and e2e verdicts were, which files the
adapter touched, and whether a PR was opened.  It is appended to
``$GITHUB_STEP_SUMMARY`` and written to ``workspace/run_result.md`` so the
artifact carries the same text.

Everything except the PR url is read back off disk rather than out of
Main2MainState: the workspace files are the artifact of record, and reading
them keeps the renderer correct for a run that died halfway (the flow calls it
from ``run()``'s finally, so it also fires on the exception path).  The PR url
is the exception because ``push_to_github`` returns it and pr_ci_monitor
persists it to ``/tmp/main2main/pr_url.txt`` — outside the workspace, so it
never reaches the artifact.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from main2main_flow.scripts.utils.run_tests import PASS_RESULTS
from main2main_flow.scripts.utils.utils import (
    EACH_STEP_SUMMARY_FILE, FINAL_STATUS_FILE, PRE_CI_ROUND_FILE_FMT,
    RUN_RESULT_FILE, STEPS_DIR, STEPS_FILE, WORKSPACE_DIR, ts_print,
)

# Adapter-authored per-step verdict (agents/adapter/SKILL.md).
RESULT_FILE = "result.json"
# Written by process_steps' zero-progress guard when a fix round reproduced
# the diff the previous e2e round already failed.
ZERO_PROGRESS_WARNING = "zero-progress-fix-warning.txt"

# Files an adaptation may touch.  Anything else in result.json's files_touched
# is flagged in the summary: run 38705391440's step-1 listed the repo-root
# .gitignore, which is a repo file no adaptation should be editing.
IN_SCOPE_PREFIXES = ("vllm_ascend/", "tests/ut/")

# Cause/Change bullets in step_summary.md, e.g. "  Change (E2E fix): ...".
_SUMMARY_BULLET_RE = re.compile(r"^\s+(Cause|Change[^:]*):\s*(.+)$")
_UPSTREAM_SOURCE_RE = re.compile(r"Upstream source:\s*(\[\S+\]\(\S+\))")
_ROUND_FILE_RE = re.compile(r"^round-(\d+)-result\.json$")
_PRE_CI_FILE_RE = re.compile(r"^pre_ci-round-(\d+)\.json$")
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")
# Suite paths repeat this prefix 23 times per row set; drop it.
_SUITE_PREFIX = "tests/e2e/pull_request/"

_MAX_BULLETS = 6
_MAX_BULLET_CHARS = 300
_MAX_BLOCKERS = 4


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── writing ───────────────────────────────────────────────────────────────────

def record_pre_ci_attempt(step_dir: Path, step_id: str, round_no: int,
                          check_result: dict[str, Any]) -> None:
    """Append one pre_ci attempt to steps/<id>/pre_ci-round-<round>.json.

    run_check returns the whole check list, but only FAILING attempts used to
    survive on disk (pre_ci_check.json, overwritten each time), so the job
    summary could not say how many format/mypy/UT rounds a step actually cost.
    The file accumulates: process_steps re-enters _ai_analysis under the same
    retry_count when the zero-progress guard re-analyzes without burning an e2e
    round, and both passes belong to that round.  The recorded ``attempt`` is
    therefore the position within the round, not the loop counter.

    Never raises — a summary must not be able to kill a run.
    """
    path = step_dir / PRE_CI_ROUND_FILE_FMT.format(round=round_no)
    data = _read_json(path) or {}
    attempts = data.get("attempts")
    if not isinstance(attempts, list):
        attempts = []
    checks = []
    for check in check_result.get("checks") or []:
        checks.append({
            "name": check.get("name", ""),
            "passed": bool(check.get("passed")),
            "skipped": bool(check.get("skipped", False)),
            "violations": len(check.get("violations") or []),
        })
    attempts.append({
        "attempt": len(attempts) + 1,
        "all_passed": bool(check_result.get("all_passed")),
        "checks": checks,
    })
    try:
        step_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"step_id": step_id, "round": round_no,
                        "attempts": attempts},
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError as exc:
        ts_print(f"[run-summary] WARNING could not record pre_ci attempt: {exc}")


# ── reading ───────────────────────────────────────────────────────────────────

def _round_key(name: str) -> int:
    m = _ROUND_FILE_RE.match(name)
    return int(m.group(1)) if m else -1


def _pre_ci_round_key(name: str) -> int:
    m = _PRE_CI_FILE_RE.match(name)
    return int(m.group(1)) if m else -1


def _step_view(step: dict[str, Any]) -> dict[str, Any]:
    """Everything the summary knows about one planned step, read off disk."""
    step_dir = WORKSPACE_DIR / STEPS_DIR / step["id"]
    result = _read_json(step_dir / RESULT_FILE) or {}

    rounds: dict[int, dict[str, Any]] = {}
    tests_dir = step_dir / "tests"
    if tests_dir.is_dir():
        for path in sorted(tests_dir.glob("round-*-result.json"),
                           key=lambda p: _round_key(p.name)):
            data = _read_json(path)
            if data:
                rounds[int(data.get("round", _round_key(path.name)))] = data

    pre_ci: dict[int, list[dict[str, Any]]] = {}
    if step_dir.is_dir():
        for path in sorted(step_dir.glob("pre_ci-round-*.json"),
                           key=lambda p: _pre_ci_round_key(p.name)):
            data = _read_json(path) or {}
            pre_ci[int(data.get("round", _pre_ci_round_key(path.name)))] = \
                data.get("attempts") or []

    summary_path = step_dir / EACH_STEP_SUMMARY_FILE
    try:
        summary_text = (summary_path.read_text(encoding="utf-8", errors="replace")
                        if summary_path.exists() else "")
    except OSError:
        summary_text = ""

    return {
        "id": step["id"],
        "start": step.get("start_commit", ""),
        "end": step.get("end_commit", ""),
        "commits": int(step.get("commit_count") or 0),
        "lines": int(step.get("effective_lines") or 0),
        "status": result.get("status", ""),
        "files": [str(f) for f in (result.get("files_touched") or [])],
        "rounds": rounds,
        "pre_ci": pre_ci,
        "summary": summary_text,
        # The zero-progress warning is only ever written by a step that was
        # already deep into its fix loop, so it counts as attempted on its own.
        "no_progress": (step_dir / ZERO_PROGRESS_WARNING).exists(),
        "attempted": bool(rounds or pre_ci or result
                          or (step_dir / ZERO_PROGRESS_WARNING).exists()),
    }


def _status_from_state(state: Any) -> dict[str, Any]:
    """final_status.json fallback for runs that ended before it was written.

    generate_final_post is what writes that file, and the no-commit path
    returns before ever reaching it.
    """
    final_status = getattr(state, "final_status", "") or ""
    if final_status == "UpgradeCompleted":
        status = "completed"
    elif final_status == "UpgradePartial":
        status = "partial"
    else:
        status = "failed"
    return {
        "status": status,
        "steps_completed": int(getattr(state, "current_step", 0) or 0),
        "steps_total": int(getattr(state, "total_steps", 0) or 0),
        "reached_commit": (getattr(state, "last_verified_commit", "")
                           or getattr(state, "cur_vllm_commit", "")),
        "old_commit": getattr(state, "base_commit", ""),
        "new_commit": (getattr(state, "target_commit", "")
                       or getattr(state, "cur_vllm_commit", "")),
    }


# ── formatting ────────────────────────────────────────────────────────────────

def _fmt_elapsed(seconds: float) -> str:
    total = int(seconds or 0)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 10:
        return f"{minutes}m{secs:02d}s"
    return f"{minutes}m"


def _short_suite(name: str) -> str:
    """'tests/e2e/pull_request/one_card/test_x.py::t[a-b]' -> 'one_card/test_x.py::t'."""
    short = name[len(_SUITE_PREFIX):] if name.startswith(_SUITE_PREFIX) else name
    head, _, tail = short.partition("::")
    if tail:
        short = f"{head}::{tail.split('[', 1)[0]}"
    return short


def _suite_counts(round_data: dict[str, Any]) -> tuple[int, int, int]:
    """(clean pass, env/precision pass, failed) over one round's suites."""
    passed = soft = failed = 0
    for suite in (round_data.get("suite_results") or {}).values():
        result = suite.get("ci_result")
        if result == "passed":
            passed += 1
        elif result in PASS_RESULTS:
            soft += 1
        else:
            failed += 1
    return passed, soft, failed


def _blockers(round_data: dict[str, Any]) -> list[str]:
    out = []
    for name, suite in (round_data.get("suite_results") or {}).items():
        if suite.get("ci_result") in PASS_RESULTS:
            continue
        detail = f"exit={suite.get('run_suite_exit_code')}"
        bugs = int(suite.get("code_bugs_count") or 0)
        if bugs:
            detail += f", {bugs} code bug(s)"
        elif suite.get("env_flakes_count"):
            detail += f", {int(suite['env_flakes_count'])} env flake(s)"
        out.append(f"`{_short_suite(name)}` {detail}")
    return out


def _pre_ci_cell(attempts: list[dict[str, Any]]) -> str:
    if not attempts:
        return "—"
    last = attempts[-1]
    if last.get("all_passed"):
        return f"✅ attempt {last.get('attempt')}"
    failed = [c.get("name", "?") for c in (last.get("checks") or [])
              if not c.get("passed") and not c.get("skipped")]
    suffix = f" ({', '.join(failed)})" if failed else ""
    return f"❌ attempt {last.get('attempt')}{suffix}"


def _pre_ci_glyph(attempts: list[dict[str, Any]]) -> str:
    if not attempts:
        return "—"
    return "✅" if attempts[-1].get("all_passed") else "❌"


def _round_glyph(round_data: dict[str, Any]) -> str:
    return "✅" if round_data.get("passed") else "❌"


def _outcome(view: dict[str, Any], index: int, done: int) -> str:
    """One short phrase for why this step ended where it did."""
    if index < done:
        if view["status"] == "noop":
            return "shipped (no-op)"
        if view["no_progress"]:
            return "shipped (after a no-op fix round)"
        return "shipped"
    if index > done:
        return "not reached"
    # index == done: the step the run stopped on.
    if view["status"] == "noop":
        return "no-op, deferred to gate"
    if not view["attempted"]:
        return "not reached"
    if view["no_progress"]:
        return "stopped — fix changed nothing"
    if view["rounds"] and view["rounds"][max(view["rounds"])].get("passed"):
        return "passed, not committed"
    if len(view["rounds"]) >= 3:
        return f"e2e exhausted ({len(view['rounds'])} rounds)"
    if not view["rounds"]:
        return "pre_ci exhausted"
    return "e2e stopped (blocking set unchanged)"


def _files_cell(view: dict[str, Any]) -> str:
    if not view["files"]:
        return "—"
    parts = []
    for path in view["files"]:
        mark = "" if path.startswith(IN_SCOPE_PREFIXES) else "⚠ "
        parts.append(f"{mark}`{path}`")
    return ", ".join(parts)


def _summary_bullets(text: str) -> list[str]:
    bullets = []
    for line in text.splitlines():
        m = _SUMMARY_BULLET_RE.match(line)
        if not m:
            continue
        body = " ".join(m.group(2).split())
        if len(body) > _MAX_BULLET_CHARS:
            body = body[:_MAX_BULLET_CHARS - 1].rstrip() + "…"
        bullets.append(f"- **{m.group(1)}**: {body}")
        if len(bullets) >= _MAX_BULLETS:
            break
    return bullets


def _upstream_source(text: str) -> str:
    m = _UPSTREAM_SOURCE_RE.search(text)
    return f" · upstream {m.group(1)}" if m else ""


def _pr_cell(pr_url: str, pr_note: str) -> str:
    if pr_url.startswith("http"):
        m = _PR_NUMBER_RE.search(pr_url)
        label = f"PR [#{m.group(1)}]({pr_url})" if m else f"PR [link]({pr_url})"
        return label
    if pr_url == "SKIP_PUSH":
        return "PR **none** (push disabled)"
    return "PR **none**" + (f" — {pr_note}" if pr_note else "")


# ── rendering ─────────────────────────────────────────────────────────────────

def _render_step_details(view: dict[str, Any]) -> list[str]:
    round_numbers = sorted(set(view["rounds"]) | set(view["pre_ci"]))
    lines = [
        f"<details><summary><b>{view['id']}</b> — "
        f"<code>{view['start'][:8]}..{view['end'][:8]}</code> · "
        f"{view['commits']} commit(s) · {view['lines']} effective lines · "
        f"{len(view['rounds'])} e2e round(s)</summary>",
        "",
    ]
    if round_numbers:
        lines += [
            "| round | pre_ci (format·mypy·UT) | suites ✅/🟡/❌ | blockers | elapsed |",
            "|-------|-------------------------|------------------|----------|---------|",
        ]
        for round_no in round_numbers:
            round_data = view["rounds"].get(round_no)
            if round_data is None:
                # A round with pre_ci but no e2e is either a no-op step (the
                # per-step e2e is deferred to the final gate) or the
                # zero-progress guard re-analyzing without burning a round.
                why = ("skipped (no-op — the final gate covers this run)"
                       if view["status"] == "noop"
                       else "no e2e: fix changed nothing")
                lines.append(
                    f"| R{round_no} | {_pre_ci_cell(view['pre_ci'].get(round_no, []))} "
                    f"| — ({why}) | — | — |")
                continue
            passed, soft, failed = _suite_counts(round_data)
            suites = f"{passed} / {soft} / {failed}" if soft else f"{passed} / – / {failed}"
            blockers = _blockers(round_data)
            if len(blockers) > _MAX_BLOCKERS:
                blockers = blockers[:_MAX_BLOCKERS] + [
                    f"… +{len(blockers) - _MAX_BLOCKERS} more"]
            lines.append(
                f"| R{round_no} | {_pre_ci_cell(view['pre_ci'].get(round_no, []))} "
                f"| {suites} | {'<br>'.join(blockers) or '—'} "
                f"| {_fmt_elapsed(round_data.get('elapsed_s'))} |")
        lines.append("")

    lines.append(f"**core changes**{_upstream_source(view['summary'])}")
    lines.append("")
    if view["status"] == "noop":
        lines.append("- files: — (the adapter found no vllm-ascend change to "
                     "make, so no step e2e ran)")
    else:
        lines.append(f"- files: {_files_cell(view)}")
    bullets = _summary_bullets(view["summary"])
    if bullets:
        lines += bullets
    elif view["status"] != "noop":
        lines.append("- (no adapter summary recorded)")
    lines += ["", "</details>", ""]
    return lines


def render_run_summary(state: Any, pr_url: str = "",
                       pr_note: str = "") -> str:
    """The '## Main2Main run result' markdown section."""
    plan = _read_json(WORKSPACE_DIR / STEPS_FILE) or {}
    steps = plan.get("steps") or []
    if not steps:
        # No plan on disk means either the run had nothing to do (the flow
        # says so via pr_note) or it died before planning finished — a
        # distinction worth keeping, since a crash is exactly when someone
        # goes looking at the job page.
        note = (f"_{pr_note}._\n" if pr_note
                else "_No step plan was written — the run ended before "
                     "planning completed._\n")
        return "## Main2Main run result\n\n" + note

    status = _read_json(WORKSPACE_DIR / FINAL_STATUS_FILE) or \
        _status_from_state(state)
    views = [_step_view(s) for s in steps]
    done = max(0, min(int(status.get("steps_completed") or 0), len(views)))
    done_commits = sum(v["commits"] for v in views[:done])
    total_commits = plan.get("total_commits") or sum(v["commits"] for v in views)
    reached = (status.get("reached_commit") or "")[:8] or "—"

    lines = [
        "## Main2Main run result",
        "",
        f"`{status.get('status', 'unknown')}` · steps **{done}/{len(views)}** · "
        f"commits **{done_commits}/{total_commits}** · reached `{reached}` · "
        f"{_pr_cell(pr_url, pr_note)}",
        "",
        "| # | upstream range | commits | pre_ci (per round) | e2e (per round) | outcome |",
        "|---|----------------|---------|--------------------|-----------------|---------|",
    ]
    for index, view in enumerate(views):
        round_numbers = sorted(set(view["rounds"]) | set(view["pre_ci"]))
        if not view["attempted"]:
            pre_cell = e2e_cell = "—"
        else:
            # A run from before the pre_ci trace existed has no glyphs at all;
            # render one dash rather than one per round.
            pre_cell = ("".join(_pre_ci_glyph(view["pre_ci"].get(r, []))
                                for r in round_numbers) or "—") \
                if view["pre_ci"] else "—"
            if view["status"] == "noop":
                e2e_cell = "skipped (no-op)"
            else:
                e2e_cell = "".join(_round_glyph(view["rounds"][r])
                                   for r in sorted(view["rounds"])) or "—"
        lines.append(
            f"| {index + 1} | `{view['start'][:8]}..{view['end'][:8]}` "
            f"| {view['commits']} | {pre_cell} | {e2e_cell} "
            f"| {_outcome(view, index, done)} |")
    lines.append("")

    for view in views:
        if view["attempted"]:
            lines += _render_step_details(view)
    return "\n".join(lines).rstrip() + "\n"


# ── entry point ───────────────────────────────────────────────────────────────

def emit_run_summary(state: Any, pr_url: str = "", pr_note: str = "") -> None:
    """Write the run summary to the workspace and the Actions job page.

    Called from Flow.run()'s finally, so a render bug here must never take the
    run down with it — every failure is reported and swallowed.
    """
    try:
        text = render_run_summary(state, pr_url=pr_url, pr_note=pr_note)
    except Exception as exc:  # noqa: BLE001 — never kill the run here
        ts_print(f"[run-summary] WARNING render failed: {exc!r}")
        return

    # No mkdir: like the other workspace-level writes, initialize() has
    # already created the directory — and creating it here would leave a
    # stray workspace/ behind in unit tests that run the flow on tmp_path.
    try:
        (WORKSPACE_DIR / RUN_RESULT_FILE).write_text(text, encoding="utf-8")
    except OSError as exc:
        ts_print(f"[run-summary] WARNING could not write {RUN_RESULT_FILE}: {exc}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n" + text)
    except OSError as exc:
        ts_print(f"[run-summary] WARNING could not append to job summary: {exc}")
