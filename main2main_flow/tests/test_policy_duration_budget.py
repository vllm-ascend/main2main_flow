"""The e2e time guarantee comes from case SELECTION, not a timer.

User requirement 2026-09-06: each e2e round must stay within 20min while
covering as many scenarios as possible — arranged by choosing cases whose
scheduled makespan (run_tests' greedy round packing on the 16-card a3 pool)
fits the budget, using the per-case execution times recorded upstream.

User requirement 2026-09-07: the selection may grow as long as it still
packs into at most 3 rounds — the pin below enforces the round cap while
keeping every single round within the 20min per-round budget.

Durations vendored from vllm-ascend .github/workflows/scripts/test_config.yaml
``estimated_times`` (maintained by that repo's
schedule_update_estimated_times workflow); snapshot taken 2026-09-07.
Re-sync these numbers whenever the allowlist changes or upstream re-records.
"""
import json
from pathlib import Path

from main2main_flow.scripts.utils import run_tests as rt

_POLICY = Path(__file__).parent.parent / "test_policy.json"

# test path -> recorded seconds in vllm-ascend main-repo CI
_RECORDED_S = {
    "tests/e2e/pull_request/one_card/test_qwen3_0_6b.py": 220,
    "tests/e2e/pull_request/one_card/test_sampler.py": 190,
    "tests/e2e/pull_request/one_card/test_qwen3_8b_w8a8.py": 400,
    "tests/e2e/pull_request/one_card/test_vlm.py": 890,
    "tests/e2e/pull_request/one_card/spec_decode/test_ngram.py": 170,
    "tests/e2e/pull_request/one_card/test_attention_fa3.py": 30,
    "tests/e2e/pull_request/one_card/rlhf/state_transitions/"
    "test_pause_resume.py": 250,
    "tests/e2e/pull_request/two_card/test_deepseek_multistream_moe.py": 120,
    "tests/e2e/pull_request/two_card/test_prefix_caching.py": 420,
    "tests/e2e/pull_request/two_card/test_disaggregated_encoder.py": 230,
    "tests/e2e/pull_request/two_card/aclgraph/"
    "test_aclgraph_capture_replay.py": 20,
    "tests/e2e/pull_request/two_card/test_hccl_weight_transfer.py": 130,
    "tests/e2e/pull_request/four_card/test_deepseek_v3_2_w8a8_pruning.py": 380,
    "tests/e2e/pull_request/four_card/test_graph_mode.py": 480,
    "tests/e2e/pull_request/four_card/test_data_parallel_tp2.py": 20,
    "tests/e2e/pull_request/four_card/test_pipeline_parallel.py": 690,
    "tests/e2e/pull_request/four_card/rlhf/consistency/"
    "test_batch_invariant_tp4.py": 210,
    "tests/e2e/pull_request/four_card/test_profiling_chunk_performance.py":
        190,
}

_A3_CARDS = 16
_BUDGET_S = 1200  # 20min per round
_MAX_ROUNDS = 3

# disaggregated_encoder's servers hardcode physical devices 0..N-1, so the
# runtime scheduler gives it a private round; simulate that here (the pin
# must count the round the same way run_tests will build it).
_OVERRIDERS = {"tests/e2e/pull_request/two_card/test_disaggregated_encoder.py"}


def _allowlist() -> list[str]:
    return json.loads(_POLICY.read_text())["allowlist"]


def _scheduled() -> list[list[str]]:
    return rt._schedule_rounds(_allowlist(), _A3_CARDS, _RECORDED_S,
                               device_overriders=_OVERRIDERS,
                               pair_aligned=True)


def test_every_selected_case_has_a_recorded_duration():
    for t in _allowlist():
        assert t in _RECORDED_S, (
            f"{t} has no recorded duration — measure it upstream and add it "
            "to _RECORDED_S before it can be selected")


def test_selected_set_packs_into_at_most_3_rounds():
    rounds = _scheduled()
    assert len(rounds) <= _MAX_ROUNDS, (
        f"selected cases schedule to {len(rounds)} rounds — exceeds the "
        f"{_MAX_ROUNDS}-round cap; drop or swap cases")


def test_every_single_round_within_20min():
    rounds = _scheduled()
    worst = max(rt._lookup_time(t, _RECORDED_S) for r in rounds for t in r)
    assert worst <= _BUDGET_S, (
        f"a round's longest case runs {worst}s — exceeds the "
        f"{_BUDGET_S}s per-round budget; drop or swap cases")


def test_gemma4_is_not_selected():
    # removed from the selection outright (user 2026-09-06) — the allowlist
    # IS the selected set; no blocklist bookkeeping.
    assert "tests/e2e/pull_request/two_card/test_gemma4.py" not in _allowlist()
