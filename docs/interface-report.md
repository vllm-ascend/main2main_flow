# Optional interface evidence for adapter QA

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
