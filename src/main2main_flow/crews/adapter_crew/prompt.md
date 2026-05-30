Adapt vllm-ascend to upstream vLLM changes for step {step_id}.
Previous step: {previous_step_id}
Previous step summary: {previous_step_summary_path}

━━━ MISSION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are a single agent performing the full adapt/fix workflow end-to-end.
Do NOT use TeamCreate or Agent tools — work directly without sub-agents.

── adapt mode ─────────────────────────────────────────────────

  Trigger: {mode} is "adapt" (no CI errors yet, fresh upstream patch).

  Workflow:
    1. If {previous_step_summary_path} exists, read it to carry forward prior
       adaptation context
    2. Read changed_files.txt; use code-structure-guide.md only when upstream
       paths/symbols need routing to likely vllm-ascend files
    3. Read upstream.patch to identify concrete changes
    4. Use the File Mapping table in code-structure-guide.md when needed to find
       affected vllm-ascend files
    5. Implement changes with vllm_version_is("{release_tag}") guards
    6. Static self-review only: inspect the code changes for missing version
       guards, mismatched function signatures, and step_summary.md accuracy
    7. Write analysis.md, review.md, step_summary.md

── fix mode ───────────────────────────────────────────────────

  Trigger: {mode} is "fix" (CI ran and failed, error_logs is non-empty).

  Workflow:
    1. If {previous_step_summary_path} exists, read it to carry forward prior
       adaptation context
    2. Read structured CI output from error_logs. Separate code_bugs from
       env_flakes (env_flakes need no fix).
    3. For each code_bug, match error_type to mechanism in diagnosis-guide.md
       (TypeError → signature change, AttributeError → config field moved,
       ImportError → module path changed, NotImplementedError → new abstract
       method).
    4. Search the error in upstream.patch to find the root cause commit.
    5. Map to fix pattern in error-pattern-examples.md.
    6. Apply fix with vllm_version_is("{release_tag}") guard if needed.
    7. Static self-review changes and step_summary.md accuracy, then write
       analysis.md, review.md, step_summary.md

━━━ REPOSITORIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  vllm:        {vllm_path}
  vllm-ascend: {ascend_path}
  reference:   {reference_dir}

━━━ INPUTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  mode:                  {mode}
  current step:          {step_id}
  previous step:         {previous_step_id}
  previous step summary: {previous_step_summary_path}
  is last step:          {is_last_step}
  patch:                 {patch_path}
  changed files:         {changed_files_path}
  release tag:           {release_tag}
  error logs:            {error_logs}
  archive dir:           {step_dir}

━━━ CUMULATIVE STEP MODEL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This is a cumulative multi-step adaptation.

The vllm-ascend working tree already contains all successful adaptations from
previous steps. Do not treat this as a fresh repository state.

Before making changes:
  1. Read {previous_step_summary_path} if it exists.
  2. Inspect existing vllm-ascend changes when they are relevant.
  3. Reuse prior compatibility guards, helper functions, imports, and patterns.
  4. Avoid reverting prior adaptations unless the current upstream change or CI
     failure proves they are obsolete.

After making changes:
  - step_summary.md must be cumulative.
  - Preserve previous step summary content.
  - Append a new "Step {step_id}" section summarizing only the new analysis and
    modifications from this step.
  - Include a "Carry forward to next step" subsection for future AI runs.

The generated step_target.patch is expected to be cumulative: it represents the
full diff from the original vllm-ascend base HEAD to the current working tree
after step {step_id}.

━━━ REFERENCE FILES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  {reference_dir}/adapt-guide.md            — workflow, cumulative adapt model,
                                               version guard decision tree
  {reference_dir}/code-structure-guide.md   — Key Areas table, vllm-ascend file
                                               locations, File Mapping table
  {reference_dir}/diagnosis-guide.md        — error type → root cause mapping,
                                               Steps 1-2 for fix
  {reference_dir}/error-pattern-examples.md — concrete fix patterns per error type
                                               (signature change, config change,
                                               import change, platform interface,
                                               custom op, return type change)

━━━ RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - Only modify vllm-ascend at {ascend_path} (never vLLM at {vllm_path})
  - Do not run git add, git commit, git reset, or git checkout in vllm-ascend.
    The main2main flow collects the cumulative patch externally with git diff HEAD.
  - Use vllm_version_is() for version boundaries — never hasattr/try-except/flags
  - All branches of a version guard must have identical function signatures
  - Script execution (patch generation, CI, pre_ci_check, commit) is external —
    you only do analysis + code changes + static review
  - Do not run local validation that imports vllm/vllm-ascend, runs tests,
    launches models, checks devices, or requires NPU/GPU/runtime dependencies.
    The local adaptation environment may contain source code only.
  - Do not treat ModuleNotFoundError, missing NPU/GPU, or missing runtime
    dependencies from local commands as adaptation failures.
  - Runtime validation is handled later by _run_e2e_test; during this AI step,
    rely on static code inspection, diffs, reference docs, and structured
    error_logs from prior CI/e2e runs.
  - Never read raw CI logs into context — use structured error_logs
  - If is last step is True, check whether {reference_dir}/code-structure-guide.md
    is stale after the cumulative vllm-ascend changes. If vllm-ascend directories,
    key files, or upstream-to-Ascend mappings changed, write an updated version
    as {step_dir}/{code_structure_guide_file}. Do NOT modify the original
    {reference_dir}/code-structure-guide.md — only output the new version to
    the workspace step directory.

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Archive all outputs to {step_dir}/:

  analysis.md       — analysis report (subsystems touched, concrete changes,
                      affected files, change/fix plan, version guard assessment)
  review.md         — code review verdict and issues found
  step_summary.md   — accumulated summary document for this step: start from the
                      previous step_summary.md content if available, then append
                      this step's analysis, changes, and resolved issues

step_summary.md must preserve prior step summaries and add a clearly marked
section for step {step_id}. Do not overwrite historical summary content or
rewrite old step sections except to correct obvious factual errors.

Use this structure for the new step section:

## Step {step_id}

### Upstream changes analyzed
- ...

### Existing adaptations reused
- ...

### New adaptations made in this step
- ...

### Files changed in vllm-ascend
- ...

### Checked but unchanged
- ...

### Version guards / compatibility decisions
- ...

### Issues found and resolved
- ...

### Carry forward to next step
- ...

After completing all work and writing all archive files, the task is fully
complete. Stop — no final JSON or extra summary output is required.
