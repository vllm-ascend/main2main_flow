---
name: description-fill
description: Analyze unattributed files in the accumulated patch and write Cause/Change entries for the PR description.
---
# description-fill

## Task

The accumulated patch contains files that were changed by the adapter but NOT
mentioned in any `step_summary.md` entry.  These are "unattributed" files.
The PR description's Changes table would otherwise list them in a catch-all
"(unattributed)" row with no analysis.

For each unattributed file (or group of files sharing the same upstream
trigger), write a `step_summary.md` entry with the same format as a normal
adaptation step, so `generate_final_post` can render a proper table row with
Files / Upstream vLLM change / vllm-ascend adaptation columns.

## Repositories

| repo | path |
|------|------|
| vllm (read-only) | {vllm_path} |
| vllm-ascend (read-only) | {ascend_path} |

## Inputs

| field | value |
|-------|-------|
| accumulated patch | {patch_path} |
| unattributed files | {changed_files_path} |
| existing step summaries | {previous_step_summary_path} |
| archive dir | {step_dir} |
| release tag | {release_tag} |

## Rules

- **READ-ONLY**: do NOT modify any code in vllm-ascend or vllm.  Only WRITE
  `{step_dir}/step_summary.md`.
- Do NOT run git add, git commit, git reset, or git checkout.
- Static analysis only — do not import vllm/vllm-ascend, run tests, or
  require NPU/GPU.
- Use `vllm_version_is("{release_tag}")` references in the Change field
  when the adaptation uses version guards.

## Output format

Append ONE entry per unattributed file (or group) to
`{step_dir}/step_summary.md`, using the same format as a normal step:

```
- unattributed-1: Adapted — `path/to/file.py`
  Upstream source: [<sha>](https://github.com/vllm-project/vllm/commit/<sha>)
  Cause: <what changed upstream vLLM — 1-2 sentences on the upstream diff>
  Change: <what was done in vllm-ascend — specific files, guards, new params>
```

- Number entries sequentially: `unattributed-1`, `unattributed-2`, ...
- Group files that share the same upstream trigger into ONE entry (list
  all files on the header line, backtick-quoted, comma-separated).
- Every entry MUST have all three fields: header (with files), Upstream
  source, Cause, Change.  If a field is genuinely unknown, write
  `(unknown)` rather than omitting it.
- Multi-line fields: indent continuation lines with 2 spaces.

## Workflow

1. Read `{changed_files_path}` — the list of unattributed file paths
   (one per line).
2. Read `{patch_path}` — the accumulated patch.  Focus on the diff hunks
   for the unattributed files.
3. Read `{previous_step_summary_path}` — understand what's already
   analyzed (don't duplicate).
4. For each unattributed file (or group):
   a. Examine the vllm-ascend diff for that file (what changed).
   b. Search vllm at `{vllm_path}` (via `git log`, `git diff`, grep) to
      identify the upstream commit/PR that triggered the change.
   c. Write a `step_summary.md` entry with:
      - Header: `- unattributed-N: Adapted — \`file1.py\`, \`file2.py\``
      - Upstream source: `[<short-sha>](<commit-url>)`
      - Cause: what the upstream change did
      - Change: what vllm-ascend did to adapt (specific — mention guards,
        new params, renamed methods, etc.)
5. Write all entries to `{step_dir}/step_summary.md` (overwrite the file).

## Cause vs Change — they must be DIFFERENT

- **Cause** = what the upstream vLLM commit changed (the trigger)
- **Change** = what vllm-ascend did to adapt (the response)

Do NOT write the same text for both fields.

## What if the upstream trigger can't be identified?

If grep/search in vllm can't find a clear upstream trigger for a file:
- Upstream source: `(unknown — likely vllm-ascend internal change)`
- Cause: describe the vllm-ascend-side motivation (e.g., "sync with
  v0.26.0 API rename", "follow-up to PR #XXXX")
- Change: describe what was done

This is acceptable — the goal is to give reviewers enough context to
understand WHY each file changed, not to force a perfect upstream mapping.
