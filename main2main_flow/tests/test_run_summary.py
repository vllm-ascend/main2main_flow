from __future__ import annotations

import json
from pathlib import Path

import pytest

import main2main_flow.scripts.utils.run_summary as rs


# ---- workspace fixtures -------------------------------------------------------

@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "WORKSPACE_DIR", tmp_path)
    return tmp_path


def _step(i: int, start: str, end: str, commits: int = 3,
          lines: int = 100) -> dict:
    return {"id": f"step-{i}", "index": i, "start_commit": start,
            "end_commit": end, "commit_count": commits,
            "effective_lines": lines}


def _plan(ws: Path, steps: list[dict], total: int | None = None) -> None:
    (ws / "steps.json").write_text(json.dumps({
        "total_commits": total if total is not None
        else sum(s["commit_count"] for s in steps),
        "steps": steps,
    }), encoding="utf-8")


def _status(ws: Path, status: str, completed: int, total: int,
            reached: str = "") -> None:
    (ws / "final_status.json").write_text(json.dumps({
        "status": status, "steps_completed": completed, "steps_total": total,
        "reached_commit": reached, "old_commit": "a" * 40,
        "new_commit": "b" * 40,
    }), encoding="utf-8")


def _suite(ci_result: str, exit_code: int = 0, bugs: int = 0,
           flakes: int = 0) -> dict:
    return {"ci_result": ci_result, "run_suite_exit_code": exit_code,
            "code_bugs_count": bugs, "env_flakes_count": flakes}


def _round(ws: Path, step_id: str, round_no: int, passed: bool,
           suites: dict[str, dict], elapsed: float = 60.0) -> None:
    path = ws / "steps" / step_id / "tests" / f"round-{round_no}-result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "step_id": step_id, "round": round_no, "passed": passed,
        "ci_result": "passed" if passed else "failed",
        "elapsed_s": elapsed, "suite_results": suites,
    }), encoding="utf-8")


def _result(ws: Path, step_id: str, status: str,
            files: list[str]) -> None:
    path = ws / "steps" / step_id / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, "files_touched": files}),
                    encoding="utf-8")


def _summary_file(ws: Path, step_id: str, text: str) -> None:
    path = ws / "steps" / step_id / "step_summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _check(name: str, passed: bool, violations: int = 0,
           skipped: bool = False) -> dict:
    return {"name": name, "passed": passed, "skipped": skipped,
            "violations": ["x"] * violations if not skipped else []}


# ---- record_pre_ci_attempt ----------------------------------------------------

def test_record_pre_ci_attempt_writes_per_round_file(ws):
    step_dir = ws / "steps" / "step-1"
    step_dir.mkdir(parents=True)
    rs.record_pre_ci_attempt(step_dir, "step-1", 1, {
        "all_passed": False,
        "checks": [_check("format", True), _check("ut", False, violations=30),
                   _check("release_lane", True, skipped=True)],
    })
    data = json.loads((step_dir / "pre_ci-round-1.json").read_text())
    assert data["step_id"] == "step-1"
    assert data["round"] == 1
    assert len(data["attempts"]) == 1
    attempt = data["attempts"][0]
    assert attempt["attempt"] == 1
    assert attempt["all_passed"] is False
    assert [c["name"] for c in attempt["checks"]] == \
        ["format", "ut", "release_lane"]
    assert [c["violations"] for c in attempt["checks"]] == [0, 30, 0]
    assert [c["skipped"] for c in attempt["checks"]] == [False, False, True]


def test_record_pre_ci_attempt_appends_within_a_round(ws):
    # process_steps re-enters _ai_analysis under the same retry_count when the
    # zero-progress guard re-analyzes without burning an e2e round; both passes
    # belong to that round and their attempts must not overwrite each other.
    step_dir = ws / "steps" / "step-1"
    step_dir.mkdir(parents=True)
    rs.record_pre_ci_attempt(step_dir, "step-1", 0,
                             {"all_passed": True, "checks": []})
    rs.record_pre_ci_attempt(step_dir, "step-1", 0,
                             {"all_passed": False, "checks": []})
    attempts = json.loads(
        (step_dir / "pre_ci-round-0.json").read_text())["attempts"]
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert [a["all_passed"] for a in attempts] == [True, False]
    # a later round gets its own file, numbered from 1 again
    rs.record_pre_ci_attempt(step_dir, "step-1", 1,
                             {"all_passed": True, "checks": []})
    assert json.loads((step_dir / "pre_ci-round-1.json").read_text())[
        "attempts"][0]["attempt"] == 1


def test_record_pre_ci_attempt_never_raises_on_unwritable_dir(ws, capsys):
    # step_dir's parent is a file, so mkdir/write both fail
    blocked = ws / "blocked"
    blocked.write_text("not a dir", encoding="utf-8")
    rs.record_pre_ci_attempt(blocked / "step-1", "step-1", 0,
                             {"all_passed": True, "checks": []})
    assert "WARNING could not record pre_ci attempt" in capsys.readouterr().out


# ---- render: shape ------------------------------------------------------------

def test_render_without_steps_says_nothing_to_adapt(ws):
    text = rs.render_run_summary(
        None, pr_note="nothing to adapt — already at the target")
    assert "## Main2Main run result" in text
    assert "_nothing to adapt — already at the target._" in text


def test_render_without_steps_and_no_note_reports_the_crash(ws):
    # a run that dies during initialize leaves no plan and no pr_note — the
    # job page must not claim "nothing to adapt" on its behalf
    text = rs.render_run_summary(None)
    assert "No step plan was written" in text
    assert "Nothing to adapt" not in text


def test_render_uses_state_when_final_status_is_missing(ws):
    # the no-commit path returns before generate_final_post writes the file
    _plan(ws, [_step(1, "a" * 8, "b" * 8)])

    class State:
        final_status = "UpgradePartial"
        current_step = 0
        total_steps = 1
        base_commit = "a" * 40
        target_commit = "b" * 40
        last_verified_commit = ""
        cur_vllm_commit = ""

    text = rs.render_run_summary(State())
    assert "`partial` · steps **0/1**" in text


def test_render_overview_table_and_step_details(ws):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb", commits=17, lines=723),
               _step(2, "bbbbbbbb", "cccccccc", commits=41)],
          total=58)
    _status(ws, "failed", 0, 2)
    _result(ws, "step-1", "adapted",
            ["vllm_ascend/patch/platform/patch_mamba_manager.py",
             "vllm_ascend/__init__.py", ".gitignore"])
    _summary_file(ws, "step-1",
                  "# main2main step summary\n\n"
                  "- step-1: Adapted — vllm_ascend/__init__.py\n"
                  "  Upstream source: [26ce58b4](https://github.com/x/y/26ce58b4)\n"
                  "  Cause: upstream added prefill_end.\n"
                  "  Change: forward prefill_end to super().\n")
    _round(ws, "step-1", 0, False, {
        "tests/e2e/pull_request/one_card/test_sampler.py":
            _suite("failed", exit_code=4),
        "tests/e2e/pull_request/two_card/test_prefix_caching.py":
            _suite("passed"),
    }, elapsed=82)
    _round(ws, "step-1", 1, False, {
        "tests/e2e/pull_request/one_card/test_sampler.py":
            _suite("failed", exit_code=-9),
        "tests/e2e/pull_request/two_card/model_runner_v2/test_x.py::t[a-b]":
            _suite("passed"),
        "tests/e2e/pull_request/two_card/test_y.py": _suite("env_flake_pass"),
    }, elapsed=1701)
    _round(ws, "step-1", 2, False, {
        "tests/e2e/pull_request/four_card/test_deepseek_v3_2_w8a8_pruning.py":
            _suite("failed", exit_code=1, bugs=1),
        "tests/e2e/pull_request/one_card/test_sampler.py":
            _suite("failed", exit_code=-9),
    }, elapsed=2829)
    # pre_ci trace: round 0 took 2 attempts, rounds 1-2 one each
    step_dir = ws / "steps" / "step-1"
    rs.record_pre_ci_attempt(step_dir, "step-1", 0, {
        "all_passed": False,
        "checks": [_check("format", True), _check("ut", False, violations=30)]})
    rs.record_pre_ci_attempt(step_dir, "step-1", 0, {
        "all_passed": True,
        "checks": [_check("format", True), _check("ut", True)]})
    rs.record_pre_ci_attempt(step_dir, "step-1", 1, {
        "all_passed": True,
        "checks": [_check("format", True), _check("ut", True)]})
    rs.record_pre_ci_attempt(step_dir, "step-1", 2, {
        "all_passed": True,
        "checks": [_check("format", True), _check("ut", True)]})

    text = rs.render_run_summary(None, pr_url="", pr_note="no step passed e2e")

    # header
    assert "`failed` · steps **0/2** · commits **0/58**" in text
    assert "PR **none** — no step passed e2e" in text
    # overview: per-round glyphs, unreached step is dash-only
    assert "| 1 | `aaaaaaaa..bbbbbbbb` | 17 | ✅✅✅ | ❌❌❌ | e2e exhausted (3/3) |" \
        in text
    assert "| 2 | `bbbbbbbb..cccccccc` | 41 | — | — | not reached |" in text
    # details: round 0 cost two pre_ci attempts
    assert "<b>step-1</b>" in text
    assert "| R0 | ✅ attempt 2 | 1 / – / 1 |" in text
    assert "`one_card/test_sampler.py` exit=4" in text
    assert "| R1 | ✅ attempt 1 | 1 / 1 / 1 |" in text
    assert "`two_card/model_runner_v2/test_x.py::t` exit" not in text  # passed
    assert "| R2 | ✅ attempt 1 | 0 / – / 2 |" in text
    assert "`four_card/test_deepseek_v3_2_w8a8_pruning.py` exit=1, 1 code bug(s)" \
        in text
    assert "| 1m22s |" in text and "| 28m |" in text and "| 47m |" in text
    # core changes
    assert "upstream [26ce58b4](https://github.com/x/y/26ce58b4)" in text
    assert "- **Change**: forward prefill_end to super()." in text
    assert "⚠ `.gitignore`" in text
    assert text.rstrip().endswith("</details>")


def test_render_flags_files_outside_the_adaptation_scope(ws):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "completed", 1, 1, reached="b" * 40)
    _result(ws, "step-1", "adapted",
            ["vllm_ascend/model.py", "tests/ut/test_x.py", ".gitignore",
             "setup.py"])
    text = rs.render_run_summary(None, pr_url="https://github.com/o/r/pull/7")
    assert "- files: `vllm_ascend/model.py`, `tests/ut/test_x.py`, " \
        "⚠ `.gitignore`, ⚠ `setup.py`" in text
    assert "PR [#7](https://github.com/o/r/pull/7)" in text
    assert "| shipped |" in text


def test_render_noop_step_skips_e2e_and_says_so(ws):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "completed", 1, 1, reached="b" * 40)
    _result(ws, "step-1", "noop", [])
    step_dir = ws / "steps" / "step-1"
    rs.record_pre_ci_attempt(step_dir, "step-1", 0,
                             {"all_passed": True,
                              "checks": [_check("ut", True)]})
    text = rs.render_run_summary(None)
    assert "| ✅ | skipped (no-op) | shipped (no-op) |" in text
    assert "| R0 | ✅ attempt 1 | — (skipped (no-op — the final gate covers " \
        "this run)) | — | — |" in text
    assert "- files: — (the adapter found no vllm-ascend change to make" in text
    assert "(no adapter summary recorded)" not in text


def test_render_zero_progress_round_says_the_fix_changed_nothing(ws):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "partial", 0, 1)
    _result(ws, "step-1", "adapted", ["vllm_ascend/model.py"])
    step_dir = ws / "steps" / "step-1"
    rs.record_pre_ci_attempt(step_dir, "step-1", 0,
                             {"all_passed": True, "checks": []})
    text = rs.render_run_summary(None)
    assert "| R0 | ✅ attempt 1 | — (no e2e: fix changed nothing) | — | — |" \
        in text


# ---- render: outcome / PR cell ------------------------------------------------

@pytest.mark.parametrize("pr_url,pr_note,expected", [
    ("https://github.com/o/r/pull/12", "", "PR [#12](https://github.com/o/r/pull/12)"),
    ("https://github.com/o/r/pull/12", "ignored", "PR [#12]"),
    ("https://github.com/o/r/compare/x", "", "PR [link](https://github.com/o/r/compare/x)"),
    ("SKIP_PUSH", "", "PR **none** (push disabled)"),
    ("", "final quality gate failed", "PR **none** — final quality gate failed"),
    ("", "", "PR **none**"),
])
def test_pr_cell(pr_url, pr_note, expected):
    assert expected in rs._pr_cell(pr_url, pr_note)


def test_outcome_pre_ci_exhausted_has_no_e2e_rounds(ws):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "partial", 0, 1)
    step_dir = ws / "steps" / "step-1"
    rs.record_pre_ci_attempt(step_dir, "step-1", 0, {
        "all_passed": False, "checks": [_check("mypy", False, violations=2)]})
    text = rs.render_run_summary(None)
    assert "pre_ci exhausted" in text
    assert "| ❌ | — | pre_ci exhausted |" in text
    assert "❌ attempt 1 (mypy)" in text


def test_outcome_stop_loss_when_rounds_are_fewer_than_the_budget(ws):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "partial", 0, 1)
    _result(ws, "step-1", "adapted", ["vllm_ascend/model.py"])
    _round(ws, "step-1", 0, False,
           {"tests/e2e/pull_request/one_card/test_sampler.py":
            _suite("failed", exit_code=-9)})
    _round(ws, "step-1", 1, False,
           {"tests/e2e/pull_request/one_card/test_sampler.py":
            _suite("failed", exit_code=-9)})
    assert "e2e stopped (blocking set unchanged)" in rs.render_run_summary(None)


def test_outcome_reports_a_noop_fix_round(ws):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "partial", 0, 1)
    step_dir = ws / "steps" / "step-1"
    step_dir.mkdir(parents=True)
    (step_dir / rs.ZERO_PROGRESS_WARNING).write_text("warn\n", encoding="utf-8")
    assert "stopped — fix changed nothing" in rs.render_run_summary(None)


# ---- emit ---------------------------------------------------------------------

def test_emit_writes_artifact_file_and_appends_to_job_summary(
        ws, tmp_path, monkeypatch):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "completed", 1, 1, reached="b" * 40)
    job_summary = tmp_path / "job_summary.md"
    job_summary.write_text("## Main2Main step plan\n\n| 1 |\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(job_summary))

    rs.emit_run_summary(None, pr_url="https://github.com/o/r/pull/9")

    artifact = (ws / "run_result.md").read_text(encoding="utf-8")
    assert artifact.startswith("## Main2Main run result")
    appended = job_summary.read_text(encoding="utf-8")
    assert appended.startswith("## Main2Main step plan")  # plan table kept
    assert "## Main2Main run result" in appended


def test_emit_without_github_env_still_writes_the_artifact(ws, monkeypatch):
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "completed", 1, 1, reached="b" * 40)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rs.emit_run_summary(None)
    assert (ws / "run_result.md").exists()


def test_emit_survives_a_render_error(ws, monkeypatch, capsys):
    def boom(*a, **k):
        raise ValueError("nope")

    monkeypatch.setattr(rs, "render_run_summary", boom)
    rs.emit_run_summary(None)  # must not raise — run() calls it from finally
    assert "WARNING render failed" in capsys.readouterr().out


def test_emit_survives_a_missing_workspace(ws, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(rs, "WORKSPACE_DIR", ws / "does-not-exist")
    _plan(ws, [_step(1, "aaaaaaaa", "bbbbbbbb")])
    _status(ws, "completed", 1, 1, reached="b" * 40)
    job_summary = tmp_path / "job_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(job_summary))
    rs.emit_run_summary(None)
    out = capsys.readouterr().out
    assert "WARNING could not write run_result.md" in out
    assert "## Main2Main run result" in job_summary.read_text(encoding="utf-8")
