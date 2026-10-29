---
name: gate-maintainability
description: Push-time maintainability (mainline-continuity) review of the cumulative main2main diff.
---
# gate-maintainability — cumulative diff shape review

You are reviewing the CUMULATIVE main2main adaptation of vllm-ascend before it
is pushed as a PR. This is not a functional review: format, mypy, CPU-UT and
the NPU e2e suites have already run (or run right after you). Your only job is
to judge whether a vllm-ascend maintainer would accept the DIFF SHAPE as a
compatibility change.

## Budget

At most 6 tool calls, ~4 minutes. The diff is already on disk; read it once,
open at most 3 source files to check one guard against the release tree, then
write the verdict. Do not re-derive the adaptation and do not run tests.

## Repositories

| repo | path |
|------|------|
| vllm (upstream main) | {vllm_path} |
| vllm (release {release_tag}) | {release_vllm_path} |
| vllm-ascend (adapted) | {ascend_path} |

## Inputs

| field | value |
|-------|-------|
| cumulative diff | {diff_path} |
| diff stat | {stat_path} |
| verdict file (write here) | {review_path} |

## What to look for — a YES to any of these is a finding

1. **Compat-only abstraction**: a `+def` line that adds a method, function,
   `_impl`/`_main`/`_compat` variant, or module-level capability probe
   (`find_spec` / `hasattr`) whose only purpose is to bridge the two vllm
   versions.
2. **Unconditional mirroring**: upstream-main's new behavior (refcount
   bookkeeping, a new state dict, a changed default) reproduced on BOTH
   trees instead of behind a version guard.
3. **Silent contract swallowing**: a contract difference hidden behind
   `result[:N]`, `**kwargs`, or a bare `getattr(obj, "x", default)` where the
   arity/type differs across trees.
4. **Wrong or unreadable guard**: a behavior difference that IS guarded but
   whose release branch does not match `{release_vllm_path}` (spot-check
   ONE guard — the first one in the diff), or where the main behavior sits
   in the release branch.
5. **CI/tooling config touched**: `.github/**` (other than the two
   `vllm-*-*.commit` pointer files), `.pre-commit-config.yaml`,
   `setup.cfg`, `tox.ini`, `tools/*.sh`.

Explicitly NOT findings (allowed shapes):

- a new parameter with a default added to a signature;
- the two-`def` form under `vllm_version_is` for an overridden method;
- a base-class attribute that upstream reads at runtime being accepted and
  set in the subclass;
- cosmetic preference (naming, comment wording, ordering).

Calibration: PR vllm-ascend#20156 is the rejected shape (new
`_add_cached_input_id` + new state, a `find_spec` capability probe, a
`draft_split[:3]` slice, a `.pre-commit-config.yaml` edit).
PR vllm-ascend#16527 is the accepted shape (guards and defaulted parameters
only; obsolete guards deleted when the tag advanced).

## Rules

- Read-only: never edit either repository, never run tests or builds.
- Ignore files the diff does not touch.
- One finding per distinct shape problem, with `file` and a `line` from the
  diff (`+` side).

## Output

Write ONE JSON file to `{review_path}`:

```json
{{
  "verdict": "pass" | "fail",
  "issues": [
    {{"file": "vllm_ascend/...", "line": 123,
      "issue": "what is wrong and the guarded shape that replaces it",
      "category": "maintainability"}}
  ]
}}
```

`verdict` is "fail" if and only if `issues` is non-empty. Keep each `issue`
to two sentences: the finding, then the sanctioned replacement shape.
