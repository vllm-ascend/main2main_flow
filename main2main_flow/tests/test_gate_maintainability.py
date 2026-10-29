"""Gate-layer maintainability (mainline-continuity) review.

The per-step adapter-qa only sees one step; the gate reviews the CUMULATIVE
diff before push, so a shape problem inherited from an earlier run (PR
#20156's new helper + mirrored state) is caught by the next run too.  Its
findings enter ONE adapter-fix rework round; whatever survives is recorded in
state.style_risks (surfaced in the PR body) and never blocks a PR whose
statics and e2e are green.

Everything here is fail-soft: missing opencode, an unavailable model or an
unparseable verdict must not break the gate.
"""

import json
import os
import subprocess
from pathlib import Path

from main2main_flow import flow as flow_mod


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


def _flow(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    f = flow_mod.Main2MainFlow()
    repo = tmp_path / "ascend"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "vllm_ascend").mkdir()
    (repo / "vllm_ascend" / "x.py").write_text("A = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    f.state.vllm_ascend_path = str(repo)
    f.state.vllm_path = str(tmp_path / "vllm")
    f.state.vllm_release_path = str(tmp_path / "vllm-release")
    f.state.release_tag = "0.30.0"
    f.state.original_ascend_ref = "HEAD"
    (tmp_path / "vllm").mkdir()
    (tmp_path / "vllm-release").mkdir()
    return f


def _fail_verdict(monkeypatch, gate_dir: Path, issues):
    """Script run_opencode_review to write *issues* into the verdict file."""
    def fake_review(prompt, log_path=None, raw_path=None, stderr_path=None,
                    session_id="", model=None):
        (gate_dir / "maintainability_review.json").write_text(json.dumps({
            "verdict": "fail" if issues else "pass",
            "issues": issues,
        }), encoding="utf-8")
        return "ok", ""
    monkeypatch.setattr(flow_mod, "run_opencode_review", fake_review)


def test_gate_review_disabled_by_env(monkeypatch, tmp_path: Path) -> None:
    f = _flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_STYLE_GATE", "0")
    assert f._gate_maintainability_review(tmp_path) == ([], "")


def test_gate_review_records_maintainability_findings(
        monkeypatch, tmp_path: Path) -> None:
    f = _flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_STYLE_GATE", "1")
    monkeypatch.delenv("SKIP_AI_ANALYSIS", raising=False)
    _fail_verdict(monkeypatch, tmp_path, [
        {"file": "vllm_ascend/ec_manager/score_ec_manager.py", "line": 42,
         "issue": "compat-only helper; replace with one guarded branch",
         "category": "maintainability"},
        {"file": "vllm_ascend/x.py", "line": 7,
         "issue": "runtime nit", "category": "runtime"},
    ])

    issues, report = f._gate_maintainability_review(tmp_path)

    assert issues == [
        "vllm_ascend/ec_manager/score_ec_manager.py:42: compat-only helper; "
        "replace with one guarded branch"]
    assert Path(report).exists()
    assert (tmp_path / "maintainability_diff.patch").exists()
    assert (tmp_path / "maintainability_diff.stat.txt").exists()


def test_gate_review_pass_records_nothing(monkeypatch, tmp_path: Path) -> None:
    f = _flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_STYLE_GATE", "1")
    _fail_verdict(monkeypatch, tmp_path, [])
    assert f._gate_maintainability_review(tmp_path) == ([], "")


def test_gate_review_is_fail_soft(monkeypatch, tmp_path: Path) -> None:
    f = _flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_STYLE_GATE", "1")
    monkeypatch.delenv("SKIP_AI_ANALYSIS", raising=False)

    def boom(*a, **k):
        raise RuntimeError("opencode CLI probe failed")
    monkeypatch.setattr(flow_mod, "run_opencode_review", boom)

    # A missing CLI must not fail a gate that is otherwise green.
    assert f._gate_maintainability_review(tmp_path) == ([], "")


def test_gate_review_skipped_when_ai_is_skipped(monkeypatch,
                                                tmp_path: Path) -> None:
    f = _flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_STYLE_GATE", "1")
    monkeypatch.setenv("SKIP_AI_ANALYSIS", "true")
    called: list[int] = []
    monkeypatch.setattr(flow_mod, "run_opencode_review",
                        lambda *a, **k: called.append(1))
    assert f._gate_maintainability_review(tmp_path) == ([], "")
    assert called == []


def test_style_risks_default_empty() -> None:
    # The PR body renders this list; it must exist on a fresh Flow even when
    # no review ever ran.
    f = flow_mod.Main2MainFlow()
    assert f.state.style_risks == []
