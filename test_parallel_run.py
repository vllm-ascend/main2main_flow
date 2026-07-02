#!/usr/bin/env python3
"""Minimal harness to verify parallel test execution locally.

Reuses schedule/execute helpers from main2main_flow/scripts/run_tests.py but
skips all repo setup (clone/checkout/pip install). Assumes vllm-ascend is
already installed and checked out at --ascend-path.

Defaults are pre-filled so you can run it with no arguments:
    python3 test_parallel_run.py

Default test cases mirror the CI workflow's MAIN2MAIN_TEST_CASES env var in
vllm-benchmarks/.github/workflows/schedule_main2main.yaml. Override --ascend-path
via env var VLLM_ASCEND_PATH or CLI flag.

Three modes:
  --dry-run : print the schedule only, no execution
  --mock    : run `sleep N` instead of pytest (no NPU needed, verifies timing)
  (default) : actually run pytest in parallel on local NPU
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from main2main_flow.scripts import run_tests as rt
from main2main_flow.scripts.run_tests import (
    _assign_devices,
    _build_test_cmd,
    _detect_cards,
    _run_one_test,
    _schedule_rounds,
    _test_cards,
)
from main2main_flow.utils import ts_print

# Pre-filled defaults — matches CI MAIN2MAIN_TEST_CASES.
DEFAULT_ASCEND_PATH = Path("/Users/luweijun/project/2026/github/vllm-ascend")
DEFAULT_TESTS = [
    "tests/e2e/pull_request/one_card/test_qwen3_5_0_8b.py::test_mamba_ssm_multimodal_reasoning_mtp_full_decode_only",
    "tests/e2e/pull_request/one_card/test_qwen3_8b_w8a8.py::test_dense_w8a8_eagle3_full_graph",
    "tests/e2e/pull_request/two_card/test_qwen3_30b_a3b.py::test_moe_tp_ep_eplb_full_decode_only",
    "tests/e2e/pull_request/two_card/test_qwen3_vl_30b_a3b_instruct.py::test_multimodal_reasoning_pp_full_decode_only",
]


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--ascend-path", type=Path,
                   default=Path(os.getenv("VLLM_ASCEND_PATH") or DEFAULT_ASCEND_PATH),
                   help=f"Path to vllm-ascend checkout (default: {DEFAULT_ASCEND_PATH} "
                        f"or $VLLM_ASCEND_PATH).")
    p.add_argument("--tests", nargs="+", default=DEFAULT_TESTS,
                   help="Test files or node IDs (default: CI MAIN2MAIN_TEST_CASES).")
    p.add_argument("--step-id", type=int, default=0)
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--log-dir", type=Path, default=Path("/tmp/m2m-parallel-test"))
    p.add_argument("--sequential", action="store_true",
                   help="Force one test per round (baseline for comparing parallel speedup).")
    p.add_argument("--dry-run", action="store_true",
                   help="Print schedule, don't execute.")
    p.add_argument("--mock", action="store_true",
                   help="Run `sleep N` instead of pytest. N = cards * 120 * --mock-scale.")
    p.add_argument("--mock-scale", type=float, default=1.0)
    args = p.parse_args()

    ascend_path = args.ascend_path.resolve()

    # ---- detect cards (skip for mock/dry-run if no NPU) ----
    if args.mock or args.dry_run:
        total_cards, phy_ids = 4, "0,1,2,3"
        ts_print(f"Cards: assuming {total_cards} (mock/dry-run mode)")
    else:
        run_cmd = lambda cmd: subprocess.run(["sh", "-c", cmd],
                                             capture_output=True, text=True)
        total_cards, phy_ids = _detect_cards(run_cmd)
        if total_cards <= 0:
            ts_print("Error: no NPU detected. Use --mock or --dry-run.",
                     file=sys.stderr)
            sys.exit(1)
        ts_print(f"Cards: {total_cards} (Phy-IDs: {phy_ids})")
    all_phy_ids = [int(x) for x in phy_ids.split(",")]

    # ---- schedule ----
    rounds = ([[t] for t in args.tests] if args.sequential
              else _schedule_rounds(args.tests, total_cards))
    device_rounds = _assign_devices(rounds, all_phy_ids)

    parallel_count = sum(1 for r in rounds if len(r) > 1)
    ts_print(f"Schedule: {len(rounds)} round(s), {parallel_count} parallel")
    for i, rnd in enumerate(device_rounds, 1):
        usage = sum(_test_cards(t) for t, _ in rnd)
        mode = "parallel" if len(rnd) > 1 else "serial"
        ts_print(f"  Round {i} ({mode}, {usage}/{total_cards} cards):")
        for t, d in rnd:
            ts_print(f"    {t}  ({_test_cards(t)}c, devs={d})")

    if args.dry_run:
        ts_print("[dry-run] skipping execution")
        return

    # ---- env ----
    env = os.environ.copy()
    env.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")
    env.setdefault("VLLM_USE_MODELSCOPE", "true")

    ci_log_summary = Path(rt.__file__).parent / "ci_log_summary.py"
    ci_dir = args.log_dir / str(args.step_id) / "tests"
    ci_dir.mkdir(parents=True, exist_ok=True)

    # ---- execute ----
    t0 = time.monotonic()
    all_results: list[dict] = []

    for round_idx, rnd in enumerate(device_rounds, 1):
        round_t0 = time.monotonic()
        ts_print(f"\n== Round {round_idx}/{len(device_rounds)}: {len(rnd)} test(s) ==")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(rnd)) as executor:
            futs: dict = {}
            for test, devices in rnd:
                slug = test.replace("/", "__").replace(".py", "").replace("::", "--")
                lp = ci_dir / f"round-{args.round}-{slug}.log"
                sp = ci_dir / f"round-{args.round}-{slug}-summary.json"
                cmd = _build_test_cmd(
                    test, devices,
                    ascend_path=ascend_path,
                    remote_host=None, remote_container=None,
                    remote_ascend=ascend_path,
                    mock=args.mock, mock_scale=args.mock_scale,
                    s_env=env,
                )
                fut = executor.submit(
                    _run_one_test, cmd, lp, sp, test, devices,
                    ci_log_summary, ascend_path,
                    args.step_id, args.round, env.copy(),
                    is_remote=False, is_mock=args.mock,
                )
                futs[fut] = test
                ts_print(f"  [{test}] started ({_test_cards(test)}c, devs={devices})",
                         flush=True)

            for fut in concurrent.futures.as_completed(futs):
                r = fut.result()
                all_results.append(r)
                ts_print(f"  [{futs[fut]}] done: exit={r['run_suite_exit_code']}, "
                         f"result={r['ci_result']}", flush=True)

        ts_print(f"  Round {round_idx} elapsed: {time.monotonic() - round_t0:.1f}s",
                 flush=True)

    elapsed = time.monotonic() - t0
    outcomes = {r["ci_result"] for r in all_results}
    if "failed" in outcomes:
        overall = "failed"
    elif outcomes == {"passed"}:
        overall = "passed"
    else:
        overall = "mixed"

    result = {
        "step_id": args.step_id, "round": args.round,
        "tests": [r["test"] for r in all_results],
        "ci_result": overall,
        "sequential": args.sequential,
        "total_cards": total_cards,
        "elapsed_s": round(elapsed, 1),
        "suite_results": {r["test"]: r for r in all_results},
    }
    out = ci_dir / f"round-{args.round}-result.json"
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")

    ts_print(f"\nDone in {elapsed:.1f}s. Overall: {overall}")
    ts_print(f"Logs: {ci_dir}")
    ts_print(f"Result: {out}")


if __name__ == "__main__":
    main()
