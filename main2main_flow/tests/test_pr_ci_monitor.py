"""Regression pins for the PR CI closed-loop watcher (pr_ci_monitor):

- the watcher polls check-runs per head sha and stops on green / PR
  closed / no CI ever starting / timeout, never raising
- failing logs are signature-classified: rebase conflicts and CSRC
  drift get a deterministic rebase + lease push (+ baseline write-back);
  infra-shaped failures get one budgeted rerun; content failures get
  own-diff triage — own -> adapter-fix rounds (gate payload contract,
  incl. the verbatim MCP pointer), inherited (#16382 shape) -> evidence
  only, adapter-declared env-flake -> rerun instead of a push
- ci-gate is an aggregator: excluded from actionable failures
- budget exhaustion posts a deterministic PR comment; every result is
  persisted to pr_ci_watch_result.json
- flow wiring: _monitor_pr_ci runs after a real PR url, skips
  SKIP_PUSH / MAIN2MAIN_PR_WATCH=0, and never fails the run
"""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from main2main_flow import flow as flow_mod
from main2main_flow.scripts.utils import pr_ci_monitor as mon

REPO = "vllm-project/vllm-ascend"
BRANCH = "main2main_auto_2026-09-12_07-32"
H1 = "a" * 40
H2 = "b" * 40
BASE = "c" * 40
DETAILS = "https://github.com/vllm-project/vllm-ascend/actions/runs/111/job/222"

MAIN_LEG = "E2E / run-selected-tests (vllm@9d88ceb0)"
RELEASE_LEG = "E2E / run-selected-tests (vllm@v0.28.0)"


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _pr(head=H1, state="open"):
    return {"state": state, "merged": False, "mergeable": True,
            "head": {"sha": head, "ref": BRANCH}, "base": {"sha": BASE}}


def _check(name, conclusion="failure", status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "details_url": DETAILS}


def _ok(name):
    return _check(name, conclusion="success")


class _Harness:
    """Scripted gh/git layer for one watch_and_fix run.

    check_seqs maps head sha -> list of check-run lists, popped per call
    (the last entry repeats).  logs maps check name -> log text.  The
    handful of git commands the watcher runs locally are faked too.
    """

    def __init__(self, prs, check_seqs, logs=None, diff_files="",
                 conflict_files=""):
        self.prs = list(prs)
        self.check_seqs = {k: list(v) for k, v in check_seqs.items()}
        self.logs = logs or {}
        self.diff_files = diff_files
        self.conflict_files = conflict_files
        self.adapter = lambda payload: SimpleNamespace(
            session_id="sess-1", is_noop=False,
            step_summary="fixed the thing",
            modified_files=["vllm_ascend/patch/foo.py"])
        self.adapter_payloads = []
        self.pushes = []
        self.baselines = []
        self.gh_calls = []
        self.status_out = "M  vllm_ascend/patch/foo.py\n"
        self.rebase_rc = 0
        self.continue_rc = 0
        self.abort_called = False
        self.check_heads = []
        self._orig_run = subprocess.run

    # -- gh layer ----------------------------------------------------------
    def fake_pr_view(self, repo, num):
        # Replay the scripted sequence, then stick on the last entry.
        if len(self.prs) > 1:
            return self.prs.pop(0)
        return self.prs[0]

    def fake_check_runs(self, repo, sha):
        self.check_heads.append(sha)
        seq = self.check_seqs.setdefault(sha, [[_ok("E2E / pre-commit")]])
        if len(seq) > 1:
            return seq.pop(0)
        return seq[0]

    def fake_fetch_logs(self, repo, job_id, dest):
        for log_name, text in self.logs.items():
            if dest.name.startswith(mon._safe_name(log_name)[:64]):
                dest.write_text(text, encoding="utf-8")
                return text
        dest.write_text("", encoding="utf-8")
        return ""

    def fake_run_gh(self, args):
        self.gh_calls.append(args)
        return True

    def fake_adapter(self, payload, session_id=""):
        self.adapter_payloads.append(payload)
        return self.adapter(payload)

    # -- git layer ---------------------------------------------------------
    def fake_run_git(self, repo, *args):
        if args[:1] == ("branch",):
            return BRANCH + "\n"
        if args[:2] == ("diff", "--name-only") and "--diff-filter=U" in args:
            return self.conflict_files
        if args[:2] == ("diff", "--name-only"):
            return self.diff_files
        if args[:1] == ("diff",):
            return "+++ conflict markers <<<\n"
        if args[:1] == ("status",):
            return self.status_out
        return ""

    def fake_subprocess_run(self, cmd, **kw):
        if cmd[1:2] == ["rebase"] and "-c" not in cmd:
            return subprocess.CompletedProcess(cmd, self.rebase_rc, "", "")
        if cmd[-2:] == ["rebase", "--continue"]:
            return subprocess.CompletedProcess(cmd, self.continue_rc, "", "")
        if cmd[-2:] == ["rebase", "--abort"]:
            self.abort_called = True
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return self._orig_run(cmd, **kw)

    # -- fixtures ----------------------------------------------------------
    def install(self, monkeypatch, tmp_path, head_fork="fork/vllm-ascend"):
        fake_time = _FakeTime()
        monkeypatch.setattr(mon, "time", fake_time)
        monkeypatch.setattr(mon, "_pr_view", self.fake_pr_view)
        monkeypatch.setattr(mon, "_check_runs", self.fake_check_runs)
        monkeypatch.setattr(mon, "_fetch_job_logs", self.fake_fetch_logs)
        monkeypatch.setattr(mon, "_run_gh", self.fake_run_gh)
        monkeypatch.setattr(mon, "run_git", self.fake_run_git)
        monkeypatch.setattr(mon, "_push_with_lease",
                            lambda p, b: self.pushes.append(b))
        monkeypatch.setattr(mon, "_update_baseline_ref",
                            lambda p, f, b: self.baselines.append((f, b)))
        monkeypatch.setattr(mon, "_fetch_upstream_main", lambda p: True)
        monkeypatch.setattr(mon, "subprocess", SimpleNamespace(
            run=self.fake_subprocess_run,
            CompletedProcess=subprocess.CompletedProcess))
        monkeypatch.setattr(
            "main2main_flow.scripts.agent.opencode_adapter"
            ".run_opencode_adapter", self.fake_adapter)
        monkeypatch.setenv("HEAD_FORK", head_fork)
        return fake_time


def _watch(tmp_path, **kw):
    kw.setdefault("poll_sec", 1)
    kw.setdefault("round_timeout_min", 60)
    kw.setdefault("overall_timeout_min", 120)
    return mon.watch_and_fix(f"https://github.com/{REPO}/pull/16424",
                             tmp_path / "ascend", tmp_path, **kw)


def test_classify_log_signatures():
    assert mon.classify_log(
        "CONFLICT (content): Merge conflict in a.py") == "rebase"
    assert mon.classify_log(
        "error: could not apply f94f3f2... main2main: sync") == "rebase"
    assert mon.classify_log(
        "::error::CSRC build workflows changed on main") == "csrc"
    assert mon.classify_log(
        "FAILED tests/e2e/one_card/test_basic.py::t - assert") == "content"
    assert mon.classify_log("1 error during collection") == "content"
    assert mon.classify_log("##[error] The runner has received a shutdown"
                            " signal") == "infra"


def test_extract_failure_files_and_triage():
    text = ('File "/home/runner/work/x/vllm_ascend/worker/block_table.py"'
            ", line 9\nvllm_ascend/patch/foo.py:12: error\n")
    assert mon.extract_failure_files(text) == [
        "vllm_ascend/worker/block_table.py", "vllm_ascend/patch/foo.py"]
    assert mon.triage_own_diff([], ["vllm_ascend/a.py"]) == "own"
    assert mon.triage_own_diff(["vllm_ascend/a.py"],
                               ["vllm_ascend/a.py"]) == "own"
    assert mon.triage_own_diff(["vllm_ascend/b.py"],
                               ["vllm_ascend/a.py"]) == "inherited"


def test_extract_failure_files_ignores_test_listing():
    # Job logs print the whole selected-test list; a bare path there is
    # not a root cause — only traceback frames / FAILED / mypy lines are.
    text = ("collected: tests/e2e/one_card/test_vlm.py"
            " tests/e2e/one_card/test_sampler.py\n"
            "FAILED tests/e2e/one_card/test_sampler.py::t - AssertionError\n"
            'File "/__w/x/vllm_ascend/worker/v2/model_runner.py", line 9\n')
    assert mon.extract_failure_files(text) == [
        "tests/e2e/one_card/test_sampler.py",
        "vllm_ascend/worker/v2/model_runner.py"]


def test_long_check_names_get_distinct_logs(monkeypatch, tmp_path):
    # Two matrix jobs whose names share the 80-char safe-name prefix
    # (part 2-4 / part 3-4) must not overwrite each other's logs.
    name2 = MAIN_LEG + " / a3-4 card-(part 2-4) extra padding".ljust(90, "x")
    name3 = MAIN_LEG + " / a3-4 card-(part 3-4) extra padding".ljust(90, "x")
    h = _Harness(
        [_pr()], {H1: [[_check(name2), _check(name3)],
                       [_ok("E2E / pre-commit")]]},
        logs={name2: "FAILED tests/e2e/a.py::t\n"
                      'File "/x/vllm_ascend/patch/foo.py", line 1\n',
              name3: "FAILED tests/e2e/b.py::t\n"},
        diff_files="vllm_ascend/patch/foo.py\n")
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "fixed"
    logs = sorted(p.name for p in (tmp_path / "pr_ci_watch" / "round-0")
                  .glob("*.log"))
    assert len(logs) == 2 and logs[0] != logs[1]


def test_green_round0_no_actions(monkeypatch, tmp_path):
    h = _Harness([_pr()], {H1: [[_ok("E2E / pre-commit"), _ok(RELEASE_LEG)]]})
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "green"
    assert result["actions"] == []
    assert h.adapter_payloads == []


def test_pr_closed_exit(monkeypatch, tmp_path):
    h = _Harness([_pr(state="closed")], {})
    h.install(monkeypatch, tmp_path)
    assert _watch(tmp_path)["status"] == "pr-closed"


def test_no_ci_grace_exit(monkeypatch, tmp_path):
    h = _Harness([_pr()], {H1: [[]]})
    fake_time = h.install(monkeypatch, tmp_path)
    monkeypatch.setattr(mon, "_gh_api", lambda path: {"workflow_runs": []})
    result = _watch(tmp_path)
    assert result["status"] == "no-ci"
    # the grace window must actually elapse before giving up
    assert fake_time.now >= mon.NO_CI_GRACE_SEC


def test_timeout_round_deadline(monkeypatch, tmp_path):
    pending = [_check("E2E / cpu-ut / cpu-ut card-", status="in_progress")]
    h = _Harness([_pr()], {H1: [pending]})
    h.install(monkeypatch, tmp_path)
    assert _watch(tmp_path, round_timeout_min=1)["status"] == "timeout"


def test_head_moved_between_rounds(monkeypatch, tmp_path):
    # infra failure on H1 -> rerun -> next round the PR head is H2 (bot
    # merge) and its checks are green.
    cache_leg = "E2E / prepare-csrc-cache / Prepare csrc cache checks"
    h = _Harness([_pr(head=H1), _pr(head=H2)],
                 {H1: [[_check(cache_leg)]], H2: [[_ok("E2E / pre-commit")]]},
                 logs={cache_leg: "##[error] cache miss, restore failed"})
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "fixed"
    assert result["actions"] == ["round-0:rerun"]
    assert h.gh_calls == [["run", "rerun", "111", "--failed"]]
    assert H2 in h.check_heads


def test_content_own_fix_pushes_and_writes_baseline(monkeypatch, tmp_path):
    log = ("vllm_ascend/patch/foo.py:12: error\n"
           "FAILED tests/e2e/one_card/test_basic.py::t\n")
    h = _Harness([_pr()], {H1: [[_check(RELEASE_LEG)],
                                [_ok("E2E / pre-commit")]]},
                 logs={RELEASE_LEG: log},
                 diff_files="vllm_ascend/patch/foo.py\nvllm_ascend/other.py\n")
    h.install(monkeypatch, tmp_path)
    guide = tmp_path / "steps" / "step-1" / "code-structure-guide.md"
    guide.parent.mkdir(parents=True)
    guide.write_text("guide", encoding="utf-8")
    result = _watch(tmp_path)
    assert result["status"] == "fixed"
    assert result["actions"] == ["round-0:adapter-fix"]
    assert h.pushes == [BRANCH]
    assert h.baselines == [("fork/vllm-ascend", BRANCH)]
    payload = h.adapter_payloads[0]
    assert payload["role"] == "adapter-fix"
    assert payload["ascend_path"] == str(tmp_path / "ascend")
    assert "MCP server is registered" in payload["vllm_report_context"]
    logs_arg = json.loads(payload["error_logs"])
    assert logs_arg and all(Path(p).exists() for p in logs_arg)
    assert payload["code_structure_guide_file"] == str(guide)


def test_content_env_flake_reruns_without_push(monkeypatch, tmp_path):
    log = ("FAILED tests/e2e/one_card/test_basic.py::t\n"
           'File "/x/vllm_ascend/patch/foo.py", line 3, in forward\n')
    h = _Harness([_pr()], {H1: [[_check(MAIN_LEG)],
                                [_ok("E2E / pre-commit")]]},
                 logs={MAIN_LEG: log},
                 diff_files="vllm_ascend/patch/foo.py\n")
    h.adapter = lambda payload: SimpleNamespace(
        session_id="", is_noop=True,
        step_summary="env-flake: npu OOM on the runner, code is correct",
        modified_files=[])
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "fixed"
    assert result["actions"] == ["round-0:rerun(env-flake)"]
    assert h.pushes == [] and h.baselines == []
    assert h.gh_calls == [["run", "rerun", "111", "--failed"]]


def test_content_inherited_records_without_adapter(monkeypatch, tmp_path):
    # Root-cause file entirely outside the adaptation diff (#16382 shape):
    # the failing test path and the crashing frame are both upstream code.
    log = ("FAILED tests/e2e/one_card/test_basic.py::t\n"
           'File "/x/vllm_ascend/worker/block_table.py", line 9\n')
    h = _Harness([_pr()], {H1: [[_check(RELEASE_LEG)]]},
                 logs={RELEASE_LEG: log},
                 diff_files="vllm_ascend/patch/foo.py\n")
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "inherited"
    assert h.adapter_payloads == [] and h.pushes == []
    triage = json.loads(
        (tmp_path / "pr_ci_watch" / "round-0" / "triage.json")
        .read_text(encoding="utf-8"))
    assert triage["verdict"] == "inherited"
    assert triage["root_files"] == ["tests/e2e/one_card/test_basic.py",
                                    "vllm_ascend/worker/block_table.py"]


def test_rebase_conflict_clean_rebase(monkeypatch, tmp_path):
    h = _Harness([_pr()], {H1: [[_check("E2E / cpu-ut / cpu-ut card-")],
                                [_ok("E2E / pre-commit")]]},
                 logs={"E2E / cpu-ut / cpu-ut card-":
                       "CONFLICT (content): Merge conflict in a.py\n"})
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "fixed"
    assert result["actions"] == ["round-0:rebase-rebase"]
    assert h.adapter_payloads == [] and not h.abort_called
    assert h.pushes == [BRANCH]
    assert h.baselines == [("fork/vllm-ascend", BRANCH)]


def test_rebase_conflict_adapter_resolves(monkeypatch, tmp_path):
    h = _Harness([_pr()], {H1: [[_check("E2E / cpu-ut / cpu-ut card-")],
                                [_ok("E2E / pre-commit")]]},
                 logs={"E2E / cpu-ut / cpu-ut card-":
                       "CONFLICT (content): Merge conflict in a.py\n"},
                 conflict_files="tests/ut/spec_decode/test_eagle_proposer.py\n")
    h.rebase_rc = 1
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "fixed"
    assert not h.abort_called
    payload = h.adapter_payloads[0]
    assert payload["step_id"] == "pr-ci-rebase-0"
    assert "test_eagle_proposer.py" in json.loads(payload["error_logs"])[1]
    assert h.pushes == [BRANCH]


def test_ci_gate_aggregator_excluded(monkeypatch, tmp_path):
    # ci-gate failing alone (no upstream job): nothing actionable, stop
    # instead of rerunning the aggregator.
    h = _Harness([_pr()], {H1: [[_check("E2E / ci-gate")]]})
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "exhausted"
    assert h.adapter_payloads == [] and h.pushes == []


def test_budget_exhaustion_posts_comment(monkeypatch, tmp_path):
    log = ("FAILED tests/e2e/x.py::t\n"
           'File "/x/vllm_ascend/patch/foo.py", line 3\n')
    h = _Harness([_pr()], {H1: [[_check(RELEASE_LEG)]]},
                 logs={RELEASE_LEG: log},
                 diff_files="vllm_ascend/patch/foo.py\n")
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path, fix_rounds=1)
    assert result["status"] == "exhausted"
    assert len(h.adapter_payloads) == 1
    comments = [c for c in h.gh_calls if c[:2] == ["pr", "comment"]]
    assert len(comments) == 1
    result_json = json.loads(
        (tmp_path / "pr_ci_watch" / "pr_ci_watch_result.json")
        .read_text(encoding="utf-8"))
    assert result_json["status"] == "exhausted"


def test_no_fix_produced_stops(monkeypatch, tmp_path):
    # Adapter runs but changes nothing and declares no flake: pushing
    # would replay the same failure, so stop instead of looping.
    log = ("FAILED tests/e2e/x.py::t\n"
           'File "/x/vllm_ascend/patch/foo.py", line 3\n')
    h = _Harness([_pr()], {H1: [[_check(RELEASE_LEG)]]},
                 logs={RELEASE_LEG: log},
                 diff_files="vllm_ascend/patch/foo.py\n")
    h.adapter = lambda payload: SimpleNamespace(
        session_id="", is_noop=False, step_summary="nothing to change",
        modified_files=[])
    h.status_out = ""  # a no-fix adapter leaves the tree clean
    h.install(monkeypatch, tmp_path)
    result = _watch(tmp_path)
    assert result["status"] == "exhausted"
    assert h.pushes == []


def test_monitor_pr_ci_skips_and_swallows(monkeypatch, tmp_path):
    calls = []

    def fake_watch(*a, **k):
        calls.append((a, k))
        return {"session_id": "sess-9"}

    monkeypatch.setattr(flow_mod, "watch_and_fix", fake_watch)
    f = flow_mod.Main2MainFlow()
    f.state.vllm_ascend_path = str(tmp_path)
    f.state.session_id = "sess-0"

    f._monitor_pr_ci("")
    f._monitor_pr_ci("SKIP_PUSH")
    monkeypatch.setenv("MAIN2MAIN_PR_WATCH", "0")
    f._monitor_pr_ci("https://github.com/o/r/pull/1")
    monkeypatch.delenv("MAIN2MAIN_PR_WATCH")
    assert calls == []

    f._monitor_pr_ci("https://github.com/o/r/pull/1")
    assert len(calls) == 1
    assert calls[0][0] == ("https://github.com/o/r/pull/1", str(tmp_path),
                           str(flow_mod.WORKSPACE_DIR))
    assert calls[0][1]["session_id"] == "sess-0"
    assert f.state.session_id == "sess-9"

    def boom(*a, **k):
        raise RuntimeError("gh down")

    monkeypatch.setattr(flow_mod, "watch_and_fix", boom)
    f._monitor_pr_ci("https://github.com/o/r/pull/1")  # must not raise
