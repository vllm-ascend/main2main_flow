"""The e2e time guarantee comes from case SELECTION, not a timer.

User requirement 2026-09-06: each e2e round must stay within 20min while
covering as many scenarios as possible — arranged by choosing cases whose
scheduled makespan (run_tests' greedy round packing on the 16-card a3 pool)
fits the budget, using the per-case execution times recorded upstream.

User requirement 2026-09-07: the selection may grow as long as it still
packs into at most 3 rounds — the pin below enforces the round cap while
keeping every single round within the 20min per-round budget.

User requirement 2026-09-08: revert to the proven 11-case base and add
tests/e2e/pull_request/one_card/test_gumbel_sampling.py (12 cases) — PR CI
on vllm-ascend PR 15966 caught a gumbel_sample contract break (upstream
signature change, test file unadapted) that the 11-case selection could
not see. The 2026-09-07 expansion to 18 cases is reverted, but its three
most representative additions return — one per uncovered dimension, all
reusing cached models and landing in existing round slack (ngram → spec
decode, pause_resume → RLHF state machine, hccl_weight_transfer → weight
sync): 15 cases, still 3 rounds, 10 spare slot-units.

User requirement 2026-09-10: model_runner_v2 must be guarded — PR 16159's
CI run (2026-09-09) broke on exactly the V2 path the selection never saw
(`NPUModelRunner.execute_model() got an unexpected keyword argument
'valid_dummy_state_slots'` in worker/v2 and _310p/worker/v2, plus
`dspark.utils.get_pp_group` moving to pp_utils).  Two swaps, both trading
the redundant half of a pair for its V2 twin: one_card/test_qwen3_0_6b.py
→ one_card/model_runner_v2/test_uva.py (same Qwen3-0.6B model, V1 smoke →
V2 runner), four_card/test_graph_mode.py → four_card/model_runner_v2/
test_deepseek_v4.py (both four-card graph-bearing; V2 gains MTP/DSPARK/
W4A8 — graph_mode is also skipped upstream as precision-unstable).

User requirement 2026-09-10 (same day): test_basic.py must stay — it has
historically caught the most regressions, so the whole file is selected at
NODE granularity.  The file records 2650s upstream (over the 1200s
per-case budget), but its five non-DSPARK nodes measure far under it;
per-node seconds are taken from the green a2-1 run of vllm-ascend PR-CI
run 34367387684 (2026-09-09, vllm@b2f68583) by diffing per-case engine
init timestamps — the five nodes sum to 2106s, consistent with the file
record (2444s incl. the blocked DSPARK node).  test_basic.py therefore
enters the allowlist as five ``file::node`` entries; the file-level entry
stays out and the DSPARK node stays blocked.  20 cases, 3 rounds
(690/890/321s), 48/48 slot-units — the cap is now exact, the next case
added must swap one out.

Durations vendored from vllm-ascend .github/workflows/scripts/test_config.yaml
``estimated_times`` (maintained by that repo's
schedule_update_estimated_times workflow); snapshot taken 2026-09-07,
uva/deepseek_v4 entries 2026-09-10, test_basic node entries 2026-09-10
(green a2-1 run 34367387684).
Re-sync these numbers whenever the allowlist changes or upstream re-records.
"""
import json
from pathlib import Path

from main2main_flow.scripts.utils import run_tests as rt

_POLICY = Path(__file__).parent.parent / "test_policy.json"

# test path -> recorded seconds in vllm-ascend main-repo CI
_RECORDED_S = {
    "tests/e2e/pull_request/one_card/test_sampler.py": 190,
    "tests/e2e/pull_request/one_card/test_qwen3_8b_w8a8.py": 400,
    "tests/e2e/pull_request/one_card/test_vlm.py": 890,
    # No upstream e2e record for test_gumbel_sampling.py yet. The UT twin
    # (tests/ut/sample/a2 in vllm-ascend's test_config.yaml) records 110s;
    # PR CI observed 11s when every case died on instant TypeErrors
    # (PR 15966, 2026-09-08). 110s is the conservative stand-in until the
    # first e2e execution re-records it.
    "tests/e2e/pull_request/one_card/test_gumbel_sampling.py": 110,
    "tests/e2e/pull_request/one_card/spec_decode/test_ngram.py": 170,
    # model_runner_v2 entries vendored 2026-09-10 from the same
    # test_config.yaml estimated_times block.
    "tests/e2e/pull_request/one_card/model_runner_v2/test_uva.py": 80,
    # test_basic.py node durations measured from green vllm-ascend a2-1
    # PR-CI run 34367387684 (2026-09-09, vllm@b2f68583): per-case engine
    # init timestamps diffed per parametrized case.  The file-level record
    # (2650s) exceeds the per-case budget, hence node-level selection.
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_qwen3_dense_eager_mode": 321,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_egale_spec_decoding": 521,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_dflash_spec_decoding": 398,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_mtp_spec_decoding": 293,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_qwen3_dense_graph_mode": 573,
    "tests/e2e/pull_request/one_card/rlhf/state_transitions/"
    "test_pause_resume.py": 250,
    "tests/e2e/pull_request/two_card/test_deepseek_multistream_moe.py": 120,
    "tests/e2e/pull_request/two_card/test_prefix_caching.py": 420,
    "tests/e2e/pull_request/two_card/test_disaggregated_encoder.py": 230,
    "tests/e2e/pull_request/two_card/test_hccl_weight_transfer.py": 130,
    "tests/e2e/pull_request/four_card/test_deepseek_v3_2_w8a8_pruning.py": 380,
    "tests/e2e/pull_request/four_card/model_runner_v2/test_deepseek_v4.py": 520,
    "tests/e2e/pull_request/four_card/test_data_parallel_tp2.py": 20,
    "tests/e2e/pull_request/four_card/test_pipeline_parallel.py": 690,
}

_A3_CARDS = 16
_BUDGET_S = 1200  # 20min per round
_MAX_ROUNDS = 3

# disaggregated_encoder and deepseek_v3_2_w8a8_pruning both hardcode
# physical devices 0..N-1 in their sources (Remote*Server /
# ASCEND_RT_VISIBLE_DEVICES assignment), so the runtime scheduler gives
# each a private round; simulate that here (the pin must count the rounds
# the same way run_tests will build them).
_OVERRIDERS = {
    "tests/e2e/pull_request/two_card/test_disaggregated_encoder.py",
    "tests/e2e/pull_request/four_card/test_deepseek_v3_2_w8a8_pruning.py",
}


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
