Adapt vllm-ascend to upstream vLLM changes for step {step_id}.
Previous step: {previous_step_id}
Previous step summary: {previous_step_summary_path}

You are a single agent performing the full {mode} workflow end-to-end.
Do NOT use TeamCreate or Agent tools — work directly without sub-agents.

━━━ REPOSITORIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  vllm:        {vllm_path}
  vllm-ascend: {ascend_path}

━━━ INPUTS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  mode:           {mode}
  current step:   {step_id} / is last step: {is_last_step}
  release tag:    {release_tag}
  patch:          {patch_path}
  changed files:  {changed_files_path}
  error logs:     {error_logs}
  archive dir:    {step_dir}

  error content (inlined):
{error_content}

━━━ RULES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - Only modify vllm-ascend at {ascend_path} (never vLLM at {vllm_path})
  - Do not run git add, git commit, git reset, or git checkout in vllm-ascend
  - Use vllm_version_is("{release_tag}") for version boundaries — never hasattr/try-except
  - All branches of a version guard must have identical function signatures
  - Static analysis only: do not import vllm/vllm-ascend, run tests, launch models,
    check devices, or require NPU/GPU/runtime dependencies
  - Do not treat ModuleNotFoundError, missing NPU/GPU, or missing runtime
    dependencies from local commands as adaptation failures
  - Never read raw CI logs into context — use structured error_logs from {error_logs}
  - vllm-ascend step_summary.md in {step_dir} is pre-seeded with previous steps'
    content; append a new "{step_id}" section, do not rewrite prior sections
  - Write {step_dir}/result.json:
    {{"status": "adapted" | "noop", "files_touched": [...]}} as your final action

━━━ CODE EXPLORATION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If mcp__codegraph__* tools are available, prefer them:
  - understanding an area / "how does X work" → mcp__codegraph__codegraph_explore
  - locating a definition → mcp__codegraph__codegraph_search
  - callers / callees → mcp__codegraph__codegraph_callers / codegraph_callees
  - blast radius of a change → mcp__codegraph__codegraph_impact
  - project layout → mcp__codegraph__codegraph_files
If they are not available or fail, use grep/glob/file reads — do not stall on
missing tools.

━━━ CUMULATIVE STEP MODEL ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The vllm-ascend working tree already contains all successful adaptations from
previous steps. Before making changes:
  1. Read {previous_step_summary_path} if it exists
  2. Reuse prior guards, helpers, imports, and patterns
  3. Avoid reverting prior adaptations unless the current change proves them obsolete

The step_target.patch is cumulative (git diff HEAD).

For this step, write a CONCISE step_summary.md entry following this format:

**No-op steps** (vllm-ascend unchanged) — ONE line only:
  - {step_id}: No-op — <one-line reason, e.g. "upstream CUDA-only change">
    Do NOT list checked files, subsystems, or files with zero impact.

**Adapted steps** (vllm-ascend changed) — brief entry:
  - {step_id}: Adapted — <vllm-ascend files changed>
    Upstream commit: <vllm commit hash (first 8 chars)>
    Cause: <what upstream change required this adaptation, 1-2 lines>
    Change: <what was done in vllm-ascend, 1-2 lines>
    Do NOT list "files checked but unchanged" unless a reviewer needs it.

━━━ LAST STEP ONLY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If is_last_step is True, check whether code-structure-guide.md is stale after
cumulative vllm-ascend changes. If so, write updated version as
{step_dir}/{code_structure_guide_file}. Do NOT modify the original file.

Stale mapping check results (upstream paths in the File Mapping Table missing
at this commit):
{stale_mappings}

━━━ OUTPUT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Archive to {step_dir}/:
  analysis.md       — subsystems touched, changes, affected files, version guard assessment
  review.md         — static review verdict, guard/signature/import checks, remaining risks
  step_summary.md   — pre-seeded with prior steps; append the "{step_id}" section
  result.json       — {{"status": "adapted" | "noop", "files_touched": [...]}} (final action)

After completing all work, stop — no extra summary output is required.

━━━ REFERENCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{reference_content}

Code-structure routing tables: read {reference_dir}/code-structure-guide.md on demand.

━━━ RECAP ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  - Deliverables in {step_dir}: analysis.md, review.md, step_summary.md, result.json
  - Never modify vllm ({vllm_path})
  - Only vllm_version_is("{release_tag}") guards; identical signatures across branches
  - No git add/commit/reset/checkout
  - Static analysis only — never run tests or import the packages
