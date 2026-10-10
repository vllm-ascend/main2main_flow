# Optional interface evidence for adapter QA

## Producer ownership and entrypoint

The optimized v2.34.0 analyzer is owned by this automation repository at
`main2main_flow/interface_contracts`. The source-only wrapper and single-Markdown
renderer live at `main2main_flow/interface_report`. They are independent of
`kickoff`, OpenCode and device initialization; the module entrypoint works from
a source checkout with Python's standard library only:

```sh
python -B -m main2main_flow.interface_report prepare \
  --engine-root /path/to/main2main_flow --engine-sha "$FLOW_SHA" \
  --vllm-root /path/to/vllm --old "$OLD_SHA" --new "$NEW_SHA" \
  --ascend-root /path/to/vllm-ascend --ascend-sha "$ASCEND_SHA" \
  --source-mode fresh --output /path/outside/checkouts/report
python -B -m main2main_flow.interface_report scan \
  --output /path/outside/checkouts/report --cache-dir /path/outside/checkouts/cache
```

Installing this package also exposes `main2main-interface-report` with the same
arguments. Both the engine and producer are included in the wheel. Analysis
requires clean, committed source checkouts and pins the full old/new/Ascend/flow
SHAs, engine tree, wrapper hash, Python minor version and profile. Different
inputs invalidate report reuse; corrupt text caches are rescanned. Equal
old/new SHAs produce a no-change Markdown without starting the analyzer.
`--source-mode incremental_rebased` prevents fabricated remote links for a
locally rebased baseline. Full attribute and inherited-state analysis is enabled.

The source-only wrapper always disables the engine's persistent index/AST
pickle cache. The standalone analyzer retains an optional private local cache;
it must not be imported from shared artifacts. Cross-run workflow reuse is
limited to fingerprinted, checksum-verified JSON/Markdown/CSV reports, and the
QA handoff contains only `qa-review.md`. Findings are advisory (`--fail-on never`);
invalid inputs, execution errors and invalid reports fail the producer.

The `vllm-ascend` workflow remains the trigger: it selects/rebases the baseline,
freezes the vLLM target, and calls this module on a separate CPU runner. It
freezes the flow checkout SHA once and installs that same SHA for subsequent
adaptation/QA. Detection therefore runs before any NPU job is allocated, and
its implementation does not need to be copied into `vllm-ascend`.

## Existing QA consumer

Set `MAIN2MAIN_INTERFACE_REPORT` to an absolute path to a UTF-8 Markdown
report produced before adaptation. Keep it outside `workspace/`, which
initialization recreates. This is a non-secret orchestration setting, unset
by default. Without it the existing QA prompt and review behavior are unchanged.

For a nonempty adaptation diff, `_run_adapter_qa` validates the supplied file
(nonempty, readable, at most 1,000,000 bytes), then asks the original QA reviewer
to read it alongside the current diff and source. An invalid configured file
returns a critic issue before any model call; producers should validate the file
before starting the flow to avoid entering the adaptation retry loop for a
configuration error. Empty diffs retain the existing no-review fast path.

The report is evidence, not an automatic verdict. It describes the original
baseline across the complete planned range: later-step findings must not fail
an earlier step. QA distinguishes argument, return and instance-state contracts,
and records the relevant root IDs and assessments in the additional
`interface_report_usage` field of `review.json`. Existing `verdict`/`issues`
remain authoritative; usage reporting is model-generated, not a completeness
guarantee. This change does not add another model call or a new adaptation stage.

The caller can check `Main2MainFlow.INTERFACE_REPORT_VERSION == 1` to avoid
silently handing a report to an older flow version.

Run the boundary tests without a model or NPU:

```sh
python -m unittest discover -s tests -p test_interface_report.py -v
```

Run the producer's synthetic-range/cache/source tests from a clean committed
checkout, without vLLM installation or a model:

```sh
python -B -m unittest discover -s tests -p test_interface_scan.py -v
```
