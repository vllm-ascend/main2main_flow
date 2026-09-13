"""test_policy.json allowlist is merged into every e2e round.

The effective per-step/gate case set = (WF-pinned MAIN2MAIN_TEST_CASES)
∪ (this policy allowlist) − (blocklist).  PR #16439: upstream vllm added
a kwarg to the SimpleCPUOffload coordinator call while vllm-ascend's
patch wrapper wasn't extended — the engine crashed at connector init,
but neither source listed a case that enables KVTransferConfig, so
every step/gate e2e stayed green.  The policy allowlist carries the
connector-init smoke test so this path is exercised on every round.
"""

from main2main_flow.flow import _resolve_test_cases

CPU_OFFLOAD_TEST = "tests/e2e/pull_request/one_card/test_simple_cpu_offload.py"


def test_allowlist_present_without_env(monkeypatch):
    monkeypatch.delenv("MAIN2MAIN_TEST_CASES", raising=False)
    cases = _resolve_test_cases()
    assert CPU_OFFLOAD_TEST in cases


def test_allowlist_merges_with_env(monkeypatch):
    env_case = "tests/e2e/pull_request/one_card/test_zzz_merge_probe.py"
    monkeypatch.setenv("MAIN2MAIN_TEST_CASES", env_case)
    cases = _resolve_test_cases()
    assert CPU_OFFLOAD_TEST in cases
    assert env_case in cases
