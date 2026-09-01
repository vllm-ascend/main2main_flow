# Vendored select_tests — vllm-ascend @ 1e6e557bf

Copied verbatim from vllm-project/vllm-ascend
`.github/workflows/scripts/{select_tests.py,test_config.yaml,runner_label.json}`
at commit 1e6e557bf — the last generation that accepted `--changed-files`
and ran green in main2main runs (33406387872 / 33432455202).

Why vendored: the upstream CI overhaul (#14793 series, 2026-09-01)
replaced select_tests.py with a four-mode contract
(`--test-list-file` / `--explicit-e2e-tests` / `--all-tests` /
`--curated`), deleted `--changed-files`, and moved the
source-path→test mapping out of test_config.yaml into a coverage/SQLite
pipeline whose artifacts are not available to the flow mid-run.  The
flow passes uncommitted working-tree diffs, so it needs the
changed-file-driven generation.  main2main runs 33485959915 and
33501194953 died exit 2 in <100ms per e2e round on the new contract.

To refresh: copy the three files from a newer upstream commit only if it
re-introduces a changed-file-driven mode, then run the
`test_compute_test_groups*` pins in test_e2e_dispatch.py.
