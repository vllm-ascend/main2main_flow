# Interface evidence owned by main2main_flow

## Production kickoff path

The existing vllm-ascend scheduler prepares its repositories/environment and
invokes `kickoff` on its existing runner. Detection needs no additional workflow
job, downloaded report, engine checkout, or environment variable in that repo.
All detection code and orchestration belong to this automation repository.

The order inside `kickoff` is:

1. Initialize the workspace and repositories as before.
2. Call the existing `detect()` to select the verified old SHA and target new
   SHA (explicit `--target-commit`, otherwise the local vLLM HEAD).
3. If there is no upgrade, exit without scanning or calling QA. Otherwise,
   scan the entire selected range once, before planning or adapting any step.
   The Ascend baseline is the committed HEAD actually supplied to kickoff,
   including any cumulative adaptations/rebase selected by the caller.
4. Plan steps, adapt, run pre-CI, and invoke the original `adapter-qa`. Each QA
   review/retry reads the same `workspace/interface_scan/qa-review.md` together
   with its current diff and source. Detection adds no model call.
5. Continue the original NPU tests, final gates, PR creation and reporting.

Detection performs CPU source analysis while the existing job holds its runner;
it does not allocate another runner or execute NPU kernels. A full kickoff still
performs its original initialization, adaptation and hardware tests. To validate
detection without these actions, use the standalone entrypoint below.

## Inputs, isolation and failures

The optimized v2.34.0 engine is packaged at `main2main_flow/interface_contracts`.
`interface_report/runtime.py` integrates it with the flow; `run.py` shares the
scan, report validation, cache verification and Markdown rendering pipeline.
Installed wheels and editable checkouts use the same packaged engine.

Kickoff can have ignored/untracked build artifacts. Detection reads temporary
local Git clones of the exact vLLM target and Ascend baseline instead of moving
the live checkouts. Tracked content changes must be committed first; the helper
chmod performed by initialization is allowed. Existing legacy `conf.py` baseline
resolution remains supported. Missing commits, inconsistent baselines, reverse
ranges, execution failures and invalid reports stop the flow before adaptation
and leave `workspace/interface_scan/run-status.json` with `scan: failed`.
Candidate findings are advisory (`--fail-on never`), not automatic failures.

Only checksum-verified JSON/Markdown/CSV reports are cached under
`$XDG_CACHE_HOME/main2main_flow/interface-reports` (default
`~/.cache/main2main_flow/interface-reports`). The key includes the installed
engine/producer source fingerprint, Python minor, exact old/new/Ascend SHAs and
analysis profile. Changed inputs create a different entry; corrupt entries are
rescanned. No executable/pickle index is restored. Cache reuse still validates
the selected source snapshots. A fresh runner without this cache scans again.

The run's diagnostics live in volatile `workspace/interface_scan/`; only
`qa-review.md` is given to QA. The Markdown contains source evidence so deleting
the temporary clones does not remove its evidence. Local cumulative Ascend
commits are not given fabricated public GitHub links.

## Original QA consumer

The flow automatically binds its report to that flow instance. It takes
precedence over `MAIN2MAIN_INTERFACE_REPORT`, which remains an optional manual
QA input when no automatic report is bound. A manual report must be outside
`workspace/` if initialization will run, because initialization recreates it.

For a nonempty diff, the original `_run_adapter_qa` validates the report as
readable UTF-8, nonempty and at most 1,000,000 bytes. Invalid evidence produces a
critic issue before the model call. Empty diffs retain their existing fast path.
The report describes the PRE-adaptation baseline across the complete range;
later-step candidates must be deferred until applicable. QA treats evidence as
untrusted data, distinguishes argument/return/instance-state contracts, and
records root IDs and assessments in `review.json`'s `interface_report_usage`.
Existing `verdict`/`issues` remain authoritative. Model-generated usage is not a
guarantee that every candidate was reviewed. The handoff version remains
`Main2MainFlow.INTERFACE_REPORT_VERSION == 1`.

## CPU-only standalone validation

The standalone module never starts kickoff, OpenCode, adaptation or device
initialization and needs only Python's standard library from a source checkout:

```sh
python -B -m main2main_flow.interface_report prepare \
  --engine-root /path/to/main2main_flow --engine-sha "$FLOW_SHA" \
  --vllm-root /path/to/vllm --old "$OLD_SHA" --new "$NEW_SHA" \
  --ascend-root /path/to/vllm-ascend --ascend-sha "$ASCEND_SHA" \
  --source-mode incremental_rebased --output /path/outside/checkouts/report
python -B -m main2main_flow.interface_report scan \
  --output /path/outside/checkouts/report --cache-dir /path/outside/checkouts/cache
```

Installation also exposes `main2main-interface-report` with these arguments.
This strict CLI requires clean source checkouts, full pinned SHAs, and output
and cache outside all checkouts. Equal old/new SHAs produce no-change Markdown
without starting the analyzer. Full attribute and inherited-state analysis is
enabled on both entrypoints, with one index worker and one analysis worker.

Run the synthetic source/cache tests and production-call-path composition tests
without models or devices from a clean committed checkout:

```sh
python -B -m unittest discover -s tests -p 'test_interface_*.py' -v
```
