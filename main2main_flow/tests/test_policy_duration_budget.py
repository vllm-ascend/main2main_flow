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
stays out and the DSPARK node stays blocked.  test_graph_mode.py joins the
blocklist the same day (user decision after its aclgraph cases failed the
decode-logprob tolerance again in run 34437651018): it was already swapped
out of the allowlist, upstream skip-lists it as precision-unstable, and
the blocklist entry is what actually removes it from the effective
selection when the union source is upstream's own main2main_tests.json.
That day the selection stood at 20 cases / 48 slot-units.

User requirement 2026-09-10 (25min, later that day): the whole e2e phase
must fit 25min.  The stale a2-based estimates were systematically wrong
for a3-16 (deepseek_v4 est 520s vs 1046-1278s real; vlm est 890s vs 328s
real), so every duration below was re-measured cold from set A of
nv-action run 34465652074 (2026-09-10, 16/16 cards free) by diffing
per-case pytest start/end timestamps.  Two levers, no round-cap change:
- test_deepseek_v4.py as a whole file is the only boulder (warm 1046s,
  cold 1278s — over the 25min budget alone).  Its two DSPARK params are
  redundant twins, so the file enters as ONE node entry,
  test_dspark_spec_decoding[default_full_and_piecewise-...] (401s): it
  carries the DSPARK+W4A8+piecewise-cudagraph surface — exactly what the
  V2 break above (dspark.utils.get_pp_group) lived on.  The V4-MTP node is
  dropped (test_basic's mtp node guards the MTP mechanism on V2).  Being
  4c it runs parallel to pipeline_parallel, hidden under its 501s.
- test_ngram.py (V1 spec decode, 4 V2 spec nodes already guard the
  mechanism) and test_gumbel_sampling.py (UT-guarded; sampler covers the
  sampling surface) leave the selection — their 4 slot-units restore the
  packing slack the dsv4 node split consumed.
Upstream's own main2main_tests.json union also leaked two strays into the
run (22 cases → 52 slot-units → an unwanted 4th round of gumbel+uva), so
one_card/test_qwen3_0_6b.py and two_card/test_qwen3_vl_30b_a3b_instruct.py
join the blocklist.  18 cases, 44/48 slot-units (4 spare — the next case
added must swap one out), 3 rounds measured 1048s wall (17.5min).

User requirement 2026-09-13: diff-driven test selection is retired — the
fixed set is the only source, and it should cover as many modules as
possible.  Six additions, all covering previously-unselected modules:
test_simple_cpu_offload.py (KV-connector engine-init path — PR 16439's
upstream-inherited crash lived exactly there and every fixed case missed
it), test_cpu_offloading.py, test_multi_instance.py, test_npu_ipc_weight_
transfer.py, test_w8a16.py, test_w8a8_dynamic.py, four_card/test_qwen3_
mrv2_eplb.py (EPLB).  25 cases → 4 rounds (cap raised 3→4); the binding
budget is the phase-wall pin: Σ per-round makespan ≤ 25min.  The six new
durations are upstream a2 estimates (PROVISIONAL — re-measure cold on
a3-16 at the first green run); every round's makepan still comes from a
measured case, so the wall estimate holds even if the new cases inflate.

Durations are NOT the upstream test_config.yaml ``estimated_times`` — those
are a2-based and proved systematically off for a3-16.  They are cold
per-case measurements from nv-action run 34465652074 set A (2026-09-10,
16 cards free); test_basic node entries from the green a2-1 PR-CI run
34367387684 (2026-09-09, vllm@b2f68583).
Re-sync these numbers whenever the allowlist changes or a fresh run
re-measures.
"""
import json
from pathlib import Path

from main2main_flow.scripts.utils import run_tests as rt

_POLICY = Path(__file__).parent.parent / "test_policy.json"

# test path -> recorded seconds.  Cold a3-16 measurements from nv-action
# run 34465652074 set A (2026-09-10), except the test_basic nodes (green
# a2-1 PR-CI run 34367387684, 2026-09-09).
_RECORDED_S = {
    "tests/e2e/pull_request/one_card/test_sampler.py": 107,
    "tests/e2e/pull_request/one_card/test_qwen3_8b_w8a8.py": 171,
    "tests/e2e/pull_request/one_card/test_vlm.py": 328,
    # upstream-measured per-file duration (vllm-ascend test_config.yaml)
    "tests/e2e/pull_request/one_card/test_simple_cpu_offload.py": 220,
    "tests/e2e/pull_request/one_card/rlhf/state_transitions/"
    "test_pause_resume.py": 144,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_uva.py": 63,
    # test_basic.py node durations measured from green vllm-ascend a2-1
    # PR-CI run 34367387684 (2026-09-09, vllm@b2f68583): per-case engine
    # init timestamps diffed per parametrized case.  The file-level record
    # (2650s) exceeds the per-case budget, hence node-level selection.
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_qwen3_dense_eager_mode": 167,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_egale_spec_decoding": 240,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_dflash_spec_decoding": 229,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_mtp_spec_decoding": 198,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::"
    "test_qwen3_dense_graph_mode": 376,
    "tests/e2e/pull_request/two_card/test_deepseek_multistream_moe.py": 93,
    "tests/e2e/pull_request/two_card/test_prefix_caching.py": 363,
    "tests/e2e/pull_request/two_card/test_disaggregated_encoder.py": 145,
    "tests/e2e/pull_request/two_card/test_hccl_weight_transfer.py": 113,
    "tests/e2e/pull_request/four_card/test_deepseek_v3_2_w8a8_pruning.py": 352,
    # One DSPARK node of test_deepseek_v4.py: the two DSPARK params are
    # redundant twins (full_decode_only vs default_full_and_piecewise), so
    # only the piecewise one is selected — it carries DSPARK+W4A8+
    # piecewise-cudagraph, the surface the 09-10 V2 break lived on.
    "tests/e2e/pull_request/four_card/model_runner_v2/test_deepseek_v4.py::"
    "test_dspark_spec_decoding[default_full_and_piecewise-False-1024-"
    "UploadWeight/DeepSeek-V4-Flash-DSpark-w4a8-test]": 401,
    "tests/e2e/pull_request/four_card/test_data_parallel_tp2.py": 31,
    "tests/e2e/pull_request/four_card/test_pipeline_parallel.py": 501,
    # 2026-09-13 module-coverage expansion (user: maximize module coverage,
    # diff-driven selection retired).  Durations below are upstream a2
    # estimates from test_config.yaml — PROVISIONAL until cold-measured on
    # a3-16; re-measure on the first green run and re-sync.  Every new
    # module here was previously uncovered by the selection: quant w8a16/
    # w8a8-dynamic, multi-instance, IPC weight transfer, the cpu-offload
    # weight path, and MRV2 EPLB.
    "tests/e2e/pull_request/one_card/test_cpu_offloading.py": 30,
    "tests/e2e/pull_request/one_card/test_multi_instance.py": 160,
    "tests/e2e/pull_request/one_card/test_npu_ipc_weight_transfer.py": 180,
    "tests/e2e/pull_request/one_card/test_w8a16.py": 40,
    "tests/e2e/pull_request/one_card/test_w8a8_dynamic.py": 30,
    "tests/e2e/pull_request/four_card/test_qwen3_mrv2_eplb.py": 410,
}

_A3_CARDS = 16
_BUDGET_S = 1200  # 20min per round
_MAX_ROUNDS = 4   # was 3 (2026-09-07); the 2026-09-13 module-coverage
                  # expansion packs to 4 rounds — the phase-wall pin below
                  # is the binding budget now
_PHASE_WALL_S = 1500  # 25min total e2e phase (user requirement 2026-09-10)

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


def test_selected_set_packs_into_at_most_4_rounds():
    rounds = _scheduled()
    assert len(rounds) <= _MAX_ROUNDS, (
        f"selected cases schedule to {len(rounds)} rounds — exceeds the "
        f"{_MAX_ROUNDS}-round cap; drop or swap cases")


def test_whole_phase_within_25min():
    # Sequential rounds: phase wall = Σ per-round makespan (longest case).
    # This is the binding budget since the 2026-09-13 expansion.
    rounds = _scheduled()
    wall = sum(max(_RECORDED_S.get(t, 0) for t in r) for r in rounds)
    assert wall <= _PHASE_WALL_S, (
        f"phase wall {wall}s exceeds the {_PHASE_WALL_S}s (25min) budget; "
        f"drop or swap cases")


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
