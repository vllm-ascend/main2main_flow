Adapt vllm-ascend to upstream vLLM changes for step {step_id}.

━━━ MISSION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are a single agent performing the full adapt/fix workflow end-to-end.
Do NOT use TeamCreate or Agent tools — work directly without sub-agents.

── adapt mode (SKILL.md § 2.3) ─────────────────────────────────────────────────

  Trigger: {mode} is "adapt" (no CI errors yet, fresh upstream patch).

  Workflow:
    1. Read changed-files.txt, cross-reference Key Areas table in adapt-guide.md
    2. Read upstream.patch to identify concrete changes
    3. Use File Mapping table in adapt-guide.md to find affected vllm-ascend files
    4. Implement changes with vllm_version_is("{release_tag}") guards
    5. Self-review: verify all changes, check for missing version guards or
       mismatched function signatures
    6. Write analysis.md, adaptation_log.md (git diff), review.md

── fix mode (SKILL.md § 2.6) ───────────────────────────────────────────────────

  Trigger: {mode} is "fix" (CI ran and failed, error_logs is non-empty).

  Workflow:
    1. Read structured CI output from error_logs. Separate code_bugs from
       env_flakes (env_flakes need no fix).
    2. For each code_bug, match error_type to mechanism in diagnosis-guide.md
       (TypeError → signature change, AttributeError → config field moved,
       ImportError → module path changed, NotImplementedError → new abstract
       method).
    3. Search the error in upstream.patch to find the root cause commit.
    4. Map to fix pattern in error-pattern-examples.md.
    5. Apply fix with vllm_version_is("{release_tag}") guard if needed.
    6. Self-review changes, write analysis.md, adaptation_log.md, review.md

━━━ REPOSITORIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  vllm:        {vllm_path}
  vllm-ascend: {ascend_path}
  reference:   {reference_dir}

━━━ INPUTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  mode:          {mode}
  patch:         {patch_path}
  changed files: {changed_files_path}
  release tag:   {release_tag}
  error logs:    {error_logs}
  archive dir:   {step_dir}

━━━ REFERENCE FILES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  {reference_dir}/adapt-guide.md            — Key Areas table, File Mapping table,
                                               version guard decision tree,
                                               Steps 1-2 for adapt
  {reference_dir}/diagnosis-guide.md        — error type → root cause mapping,
                                               Steps 1-2 for fix
  {reference_dir}/error-pattern-examples.md — concrete fix patterns per error type
                                               (signature change, config change,
                                               import change, platform interface,
                                               custom op, return type change)

━━━ RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - Only modify vllm-ascend at {ascend_path} (never vLLM at {vllm_path})
  - Use git add <file> explicitly (never git add .)
  - Use vllm_version_is() for version boundaries — never hasattr/try-except/flags
  - All branches of a version guard must have identical function signatures
  - Script execution (patch generation, CI, pre_ci_check, commit) is external —
    you only do analysis + code changes + review
  - Never read raw CI logs into context — use structured error_logs

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Archive all outputs to {step_dir}/:

  analysis.md       — analysis report (subsystems touched, concrete changes,
                      affected files, change/fix plan, version guard assessment)
  adaptation_log.md — git diff of all changes made
  review.md         — code review verdict and issues found

After completing all work, output:

```json
{{
  "modified_files": ["list of changed vllm-ascend files, empty if no-op"],
  "is_noop": false,
  "step_summary": "comprehensive summary: what was analyzed, what changed, issues found and resolved"
}}
```

After outputting this JSON, the task is fully complete. Stop — no further actions.
