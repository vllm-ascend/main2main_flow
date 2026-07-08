Fix pre-CI check failures for step {step_id} in vllm-ascend.

━━━ REPOSITORIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  vllm:        {vllm_path}   (read-only — never modify)
  vllm-ascend: {ascend_path} (the repo you fix)

━━━ PRE-CI FAILURE (inlined) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{error_content}

Also available as files: {error_logs}

━━━ PRE-CI POLICIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The deterministic pre-CI gate enforces these repository policies:
  1. Version guards must use vllm_version_is("{release_tag}") — the tag string
     must be exactly "{release_tag}", and hasattr/try-except guards are not
     accepted.
  2. No temporary files may remain in the working tree (logs, backups, scratch
     scripts, editor leftovers).
  3. format.sh must run clean (lint + formatting).
  4. Every newly added `from vllm...` import must resolve to a module that
     exists in the vllm tree at {vllm_path}, or be wrapped in a
     vllm_version_is guard.

━━━ TASK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Make the SMALLEST possible change that fixes the reported violations, and
change nothing else. Do not refactor or "improve" code the pre-CI report does
not mention. Do not run git add/commit/reset/checkout. Static analysis only —
never run tests or import the packages.

Afterwards, update in {step_dir}/:
  analysis.md      — append one line describing the fix
  review.md        — append one line confirming the violated policy now holds
  step_summary.md  — amend the "{step_id}" section only if the fix changed what was done
  result.json      — {{"status": "adapted" | "noop", "files_touched": [...]}} (final action)
