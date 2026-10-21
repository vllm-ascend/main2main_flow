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
4. Plan steps. The adapter and adapter-fix read the same single
   `workspace/interface_scan/qa-review.md`, verify candidates against the
   current upstream/downstream source, and adapt only confirmed current-step
   incompatibilities. They record per-root decisions in the existing
   `step_summary.md`. After pre-CI, the original `adapter-qa` independently
   reviews the report, decisions, current cumulative tree and diff.
   Detection itself adds no model call; rejected candidates can require QA
   even when there is no business-code diff.
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
`qa-review.md` is given to both agents. The Markdown contains source evidence so deleting
the temporary clones does not remove its evidence. Local cumulative Ascend
commits are not given fabricated public GitHub links. The report retains grouped
candidates, compatibility reasons, exact old/new source locations and downstream
dependency excerpts. It omits repository-wide same-name search hits and
short-name-filtered diffs: these do not establish symbol identity or compatibility.
Missing source locations remain explicit evidence gaps; agents must inspect
possible migrations when verifying a deletion. For replacement methods,
the downstream endpoint locates the implementation definition; patch installation
file/line and guards remain separate evidence and are displayed separately. An
unresolved definition has an unknown line rather than borrowing the installation
line, even when both locations are in the same file.

The Markdown groups findings by the qualified upstream interface (file, owner,
name), not by a short method name. Within each chapter, argument and return
changes have separately labelled root IDs and explicit old/new contracts. All
finding IDs and override/patch relations remain visible. Pinned source locations
are rendered once per interface and referenced by evidence IDs. Adapter/QA
coverage remains one decision per root, not one decision per chapter. The handoff
reader accepts both legacy level-two root headings and grouped level-three
root headings, rejecting duplicate IDs across both layouts.

The report is a role-neutral, immutable scan-input snapshot. It states its
Ascend scan baseline separately from the current adaptation tree, delegates
actions to the adapter/QA prompts, and points to their existing per-step
outputs instead of claiming QA has not run or filling in review verdicts.
Evidence IDs link to anchors within the same Markdown file. Installation
conditions are shared once per interface chapter.

Source evidence uses a run-local Git-source/AST cache and bounded excerpts of
complete declarations, scoped returns, output assignments, relevant assertions
and branch headers. It excludes neighboring/nested functions' return evidence,
marks omitted ranges and the 96-line cap, and explicitly reports missing or
out-of-range locations. Syntax unsupported by the rendering runtime falls back
to labelled position-only evidence. No scanned code is imported or executed.
Inherited-state findings retain unknown old bindings as unknown, distinguish
field existence from lifecycle validity, and separately show actual Ascend
classes, upstream inheritance entries, constructor/super calls and field reads.
Associated inherited members are listed even when they share one root ID.

## Adapter decisions and original QA consumer

The flow automatically binds its report to that flow instance. It takes
precedence over `MAIN2MAIN_INTERFACE_REPORT`, which remains an optional manual
QA input when no automatic report is bound. A manual report must be outside
`workspace/` if initialization will run, because initialization recreates it.

`agents/adapter/SKILL.md` and `agents/adapter-qa/SKILL.md` contain the role
instructions. `interface_report/handoff.py` builds the input context and checks
the handoff; `flow.py` supplies paths, step ranges and retry routing.

The report must be readable UTF-8, nonempty and at most 1,000,000 bytes. An
automatic report's SHA-256 is bound to the flow so later modification is rejected.
The report describes the PRE-adaptation baseline across the complete range;
later-step candidates must be deferred until applicable. Both agents treat it
as untrusted evidence and distinguish argument/return/instance-state contracts.

For every expanded root, the adapter writes exactly one `interface-assessment`
JSON fenced block in the existing `step_summary.md`. The block binds
`report_sha256`, `step_id`, `start_commit` and `end_commit`, with `items` containing
`root_cause_id`, `decision` (`adapted`, `no_change`, `deferred`, or
`insufficient_evidence`), a specific `reason` and nonempty source `evidence`.
Both source sides should be cited where relevant. Preserve the normal summary
header; replace the block on retries. No additional handoff report is needed.

QA reads the raw report and these decisions and independently verifies them,
including rejected candidates outside the diff. An empty diff does not skip QA
while candidate decisions need review. No report/no expanded roots with an
empty diff retains the no-model fast path. QA adds `interface_report_sha256`,
`interface_step_id` and `interface_report_usage` to its existing `review.json`.
Every root needs `judgment` (`agree`, `disagree`, `deferred`, or
`insufficient_evidence`), `assessment` and source `evidence`. A confirmed unsafe
decision is a disagreement, requires `verdict: fail` and an actionable issue.
Uncertainty alone is not proof of a break or of safety.

Missing/duplicate/unknown roots, stale report/step/range identity and empty
reasons/evidence are mechanical handoff failures. They retry through existing
adapter-fix error feedback and cannot use the semantic QA one-fix escape. A
malformed handoff cannot reach NPU tests by exhausting that escape. Source
citations are checked for presence, not semantic truth: model output and even a
QA agreement do not prove runtime correctness. Existing pre-CI, semantic QA
one-fix policy and hardware/final gates remain necessary. Handoff version remains
`Main2MainFlow.INTERFACE_REPORT_VERSION == 1` (the scan report format is unchanged).

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
