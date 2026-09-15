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
join the blocklist.  25 cases (51 slot-units, needs 4 rounds), measured
1437s wall (24.0min) — inside the 1500s phase-wall pin.

User requirement 2026-09-13: diff-driven test selection is retired — the
fixed set is the only source, and it should cover as many modules as
possible.  Additions covering previously-unselected modules:
test_simple_cpu_offload.py (KV-connector engine-init path — PR 16439's
upstream-inherited crash lived exactly there and every fixed case missed
it), test_cpu_offloading.py, test_multi_instance.py, four_card/
test_qwen3_mrv2_eplb.py (EPLB).  Cap raised 3→4 rounds; the binding
budget is the phase-wall pin: Σ per-round makespan ≤ 25min.

Same-day correction from the first daily-bot run on flow 28a17ad
(nv-action run 34745454795, 2026-09-13, vllm@62f3bf58): test_w8a16 and
test_w8a8_dynamic were PHANTOM cases — picked from a stale working tree,
the files don't exist on vllm-ascend upstream main, pytest exited 4, and
an unfixable collection error froze the blocking set → e2e stop-loss →
the run produced no PR.  Lesson: candidate cases must be verified
against the upstream-main tree, not a local checkout.  Also removed:
test_npu_ipc_weight_transfer — a REAL failure (/update_weights 500 on
vllm@62f3bf58); two adapter-fix rounds did not converge and it would
have wedged every future run the same way.  Root-cause it before
re-adding.  Everything else passed, including test_simple_cpu_offload —
the new guard caught nothing because nothing was broken, which is the
point.  22 cases → 4 rounds.

Durations are NOT the upstream test_config.yaml ``estimated_times`` — those
are a2-based and proved systematically off for a3-16 (cpu_offloading est
30s vs 173s real; eplb est 410s vs 300s real).  one_card/two_card/
four_card suite entries are re-measured from run 34745454795 (2026-09-13)
by diffing suite start/done timestamps; test_basic node entries from the
green a2-1 PR-CI run 34367387684 (2026-09-09, vllm@b2f68583); the
2026-09-10 set-A run 34465652074 measurements for the dsv4/dspark nodes
carry over.
User requirement 2026-09-14: the fixed set must also see what PR CI sees.
PR 16466's a3-4card-2-4 leg (run 34788679070, vllm@c377114636) failed two
nodes of four_card/context_parallel/test_accuracy_v2.py —
test_mtp_mla_spec_decode_with_pcp and test_eagle3_gqa_spec_decode_with_pcp,
both on aclnnInterleaveRope error 561002 -> EngineDeadError — a surface
(PCP spec decode) nothing in the selection touched.  Both nodes enter the
allowlist at NODE granularity (the file's two dsv3 PCP nodes passed and
stay out; the whole file would add ~590s).  Their recorded durations are
crash-terminated wall times, i.e. lower bounds of a passing run.

User requirement 2026-09-14 (same day): the main lane gets an eagle3
case.  PR 16483's diff lived on eagle_proposer.py, yet the lane's
spec-decode methods were mtp/egale/dflash/dspark — eagle3 was unguarded.
test_spec_decode.py::test_eagle3_sliding_window enters the allowlist
(109s, measured from PR 16483's a3-2card release-leg job): Qwen3-8B main
is already cached and it packs into R3's free slot, so the wall holds at
1431s.  test_hang stays RELEASE-ONLY: the mrope version-compat class it
guards is invisible on the main lane (the API exists on vllm main), the
release smoke already runs it per-tree, and its 35B DP+EP model is a
bad budget trade.
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
    # one_card suite durations re-measured cold from the first daily-bot
    # run on flow 28a17ad (vllm-ascend nv-action run 34745454795,
    # 2026-09-13, vllm@62f3bf58) by diffing suite start/done timestamps;
    # test_basic nodes from green a2-1 PR-CI run 34367387684 (2026-09-09).
    "tests/e2e/pull_request/one_card/test_sampler.py": 95,
    "tests/e2e/pull_request/one_card/test_qwen3_8b_w8a8.py": 166,
    "tests/e2e/pull_request/one_card/test_vlm.py": 304,
    "tests/e2e/pull_request/one_card/test_simple_cpu_offload.py": 151,
    "tests/e2e/pull_request/one_card/test_cpu_offloading.py": 173,
    "tests/e2e/pull_request/one_card/test_multi_instance.py": 102,
    "tests/e2e/pull_request/one_card/rlhf/state_transitions/"
    "test_pause_resume.py": 147,
    "tests/e2e/pull_request/one_card/model_runner_v2/test_uva.py": 99,
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
    "tests/e2e/pull_request/two_card/test_deepseek_multistream_moe.py": 110,
    "tests/e2e/pull_request/two_card/test_prefix_caching.py": 357,
    "tests/e2e/pull_request/two_card/test_disaggregated_encoder.py": 126,
    "tests/e2e/pull_request/two_card/test_hccl_weight_transfer.py": 122,
    # 2026-09-14 addition, measured from PR 16483's a3-2card release leg
    # part 4-4 (run 34801745274, node-start timestamp diffing): every
    # other node in that job passed; test_hang was the mrope break.  The
    # lane's spec-decode methods were mtp/egale/dflash/dspark — eagle3 had
    # NO case, and eagle_proposer.py is exactly where PR 16483's diff
    # lived.  Sliding window is the cheapest eagle3 node (Qwen3-8B main is
    # already cached; the 30B p-eagle/vwn nodes need a ~60GB download).
    "tests/e2e/pull_request/two_card/spec_decode/test_spec_decode.py::"
    "test_eagle3_sliding_window": 109,
    "tests/e2e/pull_request/four_card/test_deepseek_v3_2_w8a8_pruning.py": 364,
    # One DSPARK node of test_deepseek_v4.py: the two DSPARK params are
    # redundant twins (full_decode_only vs default_full_and_piecewise), so
    # only the piecewise one is selected — it carries DSPARK+W4A8+
    # piecewise-cudagraph, the surface the 09-10 V2 break lived on.
    "tests/e2e/pull_request/four_card/model_runner_v2/test_deepseek_v4.py::"
    "test_dspark_spec_decoding[default_full_and_piecewise-False-1024-"
    "UploadWeight/DeepSeek-V4-Flash-DSpark-w4a8-test]": 401,
    "tests/e2e/pull_request/four_card/test_data_parallel_tp2.py": 32,
    "tests/e2e/pull_request/four_card/test_pipeline_parallel.py": 524,
    # 2026-09-13 module-coverage expansion (user: maximize module coverage,
    # diff-driven selection retired).  Kept additions, all measured in the
    # same 34745454795 run: test_simple_cpu_offload (KV-connector
    # engine-init path — PR #16439's upstream-inherited crash lived there),
    # test_cpu_offloading, test_multi_instance, four_card MRV2 EPLB.
    # REMOVED same day after the run: test_w8a16 + test_w8a8_dynamic
    # (phantom cases — selected from a stale working tree; the files don't
    # exist on vllm-ascend upstream main, so pytest exited 4 and the
    # adapter could never shrink the blocking set → stop-loss), and
    # test_npu_ipc_weight_transfer (real /update_weights 500 on
    # vllm@62f3bf58; two adapter-fix rounds did not converge — root-cause
    # before re-adding).
    "tests/e2e/pull_request/four_card/test_qwen3_mrv2_eplb.py": 300,
    # 2026-09-14 additions from PR 16466's CI (nv-action run 34788679070,
    # job a3-4card-2-4, vllm@c377114636): both nodes died with
    # aclnnInterleaveRope error 561002 -> EngineDeadError — a surface no
    # fixed case covered (context_parallel / PCP spec decode).  Durations
    # are crash-terminated wall times (per-node engine-init to failure),
    # so they are LOWER bounds of a passing run — re-sync from the first
    # run where the nodes go green.
    "tests/e2e/pull_request/four_card/context_parallel/test_accuracy_v2.py::"
    "test_mtp_mla_spec_decode_with_pcp": 221,
    "tests/e2e/pull_request/four_card/context_parallel/test_accuracy_v2.py::"
    "test_eagle3_gqa_spec_decode_with_pcp": 181,
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
