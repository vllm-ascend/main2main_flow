"""Extended e2e case selection: a fixed ~30min set, or the full label scan.

After the final quality gate passes, the flow runs the extended e2e phase —
the fixed 25-case per-step set only proves the cases it contains (the
2026-09-15 run shipped PR 16575 green on pre_ci while upstream CI failed
legs the fixed set never touched).  Small fast steps + one bounded coverage
batch: the default source is the FIXED ``test_policy.json`` ``extended_e2e``
key (55 cases curated cheapest-first from the label scan to the allowlist's
estimated-time scale — sum(est) ≈ 210min ≈ ~30min measured wall — maximizing
distinct covered ``vllm_ascend.*`` modules and feature areas).
``MAIN2MAIN_EXTENDED_MODE=full`` opts into the whole-label resolver instead
(89 cases, hours): tree scan of ``tests/e2e/pull_request/**/test_*.py``
minus ``test_config.yaml`` ``skip_tests``, fixed-set coverage, and the
blocklist.  ``main2main_tests.json`` is NOT usable as a source: it is the
daily-bot regression subset (18 entries) and every entry is already inside
the fixed policy allowlist — subtracting the fixed set from it yields an
empty set.

In both modes, cases whose test file imports a module touched by the
adaptation diff are ordered first (relevance tier); the rest follow, both
tiers by upstream estimated time ascending so a bounded phase maximizes
completed cases.
"""
from __future__ import annotations

import ast
from pathlib import Path

from main2main_flow.scripts.utils.run_tests import (
    _DEFAULT_ESTIMATED_SECONDS,
    _load_estimated_times,
    _lookup_time,
)
from main2main_flow.scripts.utils.utils import ts_print

# step_id used for run_tests log/result paths within the extended phase.
EXTENDED_E2E_STEP_ID = "extended-e2e"

# Directories whose changes never map to imported vllm_ascend modules.
_IGNORED_CHANGE_PREFIXES = (".github/", "docs/", "csrc/")


def scan_e2e_pull_request_files(ascend_path: str | Path) -> list[str]:
    """All test files under tests/e2e/pull_request, relative posix paths.

    Scanning the live tree makes phantom cases structurally impossible
    (2026-09-13: candidates picked from a stale working tree produced
    pytest exit 4 and froze the blocking set).  Sorted for determinism.
    """
    root = Path(ascend_path) / "tests" / "e2e" / "pull_request"
    if not root.is_dir():
        return []
    return sorted(
        p.relative_to(ascend_path).as_posix()
        for p in root.rglob("test_*.py")
        if p.is_file()
    )


def load_upstream_skip_tests(ascend_path: str | Path) -> set[str]:
    """``test_config.yaml`` ``skip_tests`` entries (file paths)."""
    config = (Path(ascend_path) / ".github" / "workflows" / "scripts"
              / "test_config.yaml")
    try:
        import yaml
        docs = list(yaml.safe_load_all(config.read_text(encoding="utf-8")))
    except Exception:
        return set()
    skipped: set[str] = set()
    for doc in docs:
        if isinstance(doc, dict):
            skipped.update(
                t.strip() for t in (doc.get("skip_tests") or [])
                if isinstance(t, str) and t.strip())
    return skipped


def _file_of(case: str) -> str:
    return case.split("::", 1)[0]


def covered_by_fixed(candidate: str, fixed: list[str] | set[str]) -> bool:
    """True when the fixed policy set already exercises *candidate*.

    Both directions of file/node coverage:
    - candidate ``f.py``      — covered by fixed ``f.py`` or any ``f.py::node``
    - candidate ``f.py::node`` — covered by fixed ``f.py`` or ``f.py::node``
    (The extended set is file-granular — upstream select_tests runs one bare
    pytest per file — so node-level candidates only arise via overrides.)
    """
    cfile = _file_of(candidate)
    for f in fixed:
        ffile = _file_of(f)
        if ffile != cfile:
            continue
        if "::" not in f or "::" not in candidate or f == candidate:
            return True
    return False


def extract_imported_modules(path: str | Path) -> set[str]:
    """vllm_ascend.*/tests.* modules a test file imports (AST, top level).

    Unparsable/unreadable files yield an empty set — such a case just
    loses its tier priority, it never crashes the phase.
    """
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8",
                                              errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return set()
    mods: set[str] = set()

    def add(name: str | None) -> None:
        if not name:
            return
        if name == "vllm_ascend" or name.startswith(("vllm_ascend.", "tests.")):
            mods.add(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — no absolute module name
                continue
            base = node.module or ""
            if base == "vllm_ascend":
                # `from vllm_ascend import a` -> vllm_ascend.a
                for alias in node.names:
                    add(f"vllm_ascend.{alias.name}")
            else:
                add(base)
                if base.startswith(("vllm_ascend.", "tests.")):
                    # `from vllm_ascend.a import x` also binds vllm_ascend.a.x
                    for alias in node.names:
                        if alias.name != "*":
                            add(f"{base}.{alias.name}")
    return mods


def changed_module_names(changed_files: list[str]) -> set[str]:
    """Map adaptation-diff file paths to dotted module names (+ parents).

    ``vllm_ascend/ops/x.py`` -> {vllm_ascend.ops.x, vllm_ascend.ops};
    a package ``__init__.py`` maps to the package itself.  The immediate
    parent is added so ``from vllm_ascend.ops import x`` in a test still
    matches a change to ``vllm_ascend/ops/x.py`` — but the chain stops
    before the root package: a change to one submodule must not tier
    every test that imports any ``vllm_ascend.*`` module.
    """
    mods: set[str] = set()
    for f in changed_files or []:
        if not f or not f.endswith(".py"):
            continue
        if f.startswith(_IGNORED_CHANGE_PREFIXES):
            continue
        parts = f[:-3].split("/")
        if f.endswith("__init__.py"):
            parts = parts[:-1]
        # range starts at 2: skip the root segment (vllm_ascend/tests)
        for i in range(2, len(parts) + 1):
            mods.add(".".join(parts[:i]))
    return mods


def _touches(changed: set[str], imported: set[str]) -> bool:
    """True when a changed module and an imported module overlap.

    Either direction: the test imports the changed module (or a
    submodule/ancestor of it).  A change to a package's __init__ touches
    everything importing that package or anything inside it.
    """
    for m in changed:
        for i in imported:
            if i == m or i.startswith(m + ".") or m.startswith(i + "."):
                return True
    return False


def partition_by_import_closure(
    cases: list[str], changed_modules: set[str], ascend_path: str | Path,
) -> tuple[list[str], list[str]]:
    """Split cases into (tier1, tier2) by import overlap with the diff."""
    if not changed_modules:
        return [], list(cases)
    tier1: list[str] = []
    tier2: list[str] = []
    root = Path(ascend_path)
    for c in cases:
        imported = extract_imported_modules(root / c)
        if _touches(changed_modules, imported):
            tier1.append(c)
        else:
            tier2.append(c)
    return tier1, tier2


def _est_key(case: str, times: dict[str, int]) -> tuple[int, int, str]:
    """Sort key: known estimates first, then ascending seconds."""
    est = _lookup_time(case, times)
    known = 0 if (case in times or _file_of(case) in times) else 1
    if known:
        est = _DEFAULT_ESTIMATED_SECONDS
    return known, est, case


def order_cases(cases: list[str], tier1: list[str],
                estimated_times: dict[str, int]) -> list[str]:
    """tier1 first, both tiers by estimated seconds ascending.

    Note: run_tests' LPT scheduler re-sorts internally, so the phase must
    pass ``preserve_order=True`` for this ordering to reach execution.
    """
    t1 = set(tier1)
    ordered = sorted(t1, key=lambda c: _est_key(c, estimated_times))
    ordered += sorted((c for c in cases if c not in t1),
                      key=lambda c: _est_key(c, estimated_times))
    return ordered


def prune_fixed_set(
    ascend_path: str | Path,
    cases: list[str],
    fixed_cases: list[str],
) -> dict:
    """Runtime guards for the curated fixed extended set.

    Curation is offline (against a past tree); these guards keep a stale
    list from wasting NPU time or lying: entries the per-step allowlist
    already covers (policy drift — those ran every step), entries upstream
    has since moved to ``skip_tests``, and files missing from the current
    tree (2026-09-13 phantom-file lesson).
    """
    skip = load_upstream_skip_tests(ascend_path)
    kept: list[str] = []
    dropped_fixed: list[str] = []
    dropped_skip: list[str] = []
    dropped_missing: list[str] = []
    for c in cases:
        if covered_by_fixed(c, fixed_cases):
            dropped_fixed.append(c)
        elif c in skip:
            dropped_skip.append(c)
        elif not (Path(ascend_path) / _file_of(c)).exists():
            dropped_missing.append(c)
        else:
            kept.append(c)
    if dropped_fixed:
        ts_print(f"[extended_e2e] fixed-set drift: "
                 f"{len(dropped_fixed)} case(s) already covered by the "
                 f"per-step allowlist, dropped")
    if dropped_skip:
        ts_print(f"[extended_e2e] upstream skip_tests now covers "
                 f"{len(dropped_skip)} extended case(s), dropped")
    if dropped_missing:
        ts_print(f"[extended_e2e] {len(dropped_missing)} case(s) not on "
                 f"the tree, dropped")
    return {"cases": kept, "dropped_fixed": dropped_fixed,
            "dropped_skip": dropped_skip, "dropped_missing": dropped_missing}


def resolve_extended_cases(
    ascend_path: str | Path,
    fixed_cases: list[str],
    blocked: list[str],
    *,
    include_skipped: bool = False,
    override: list[str] | None = None,
) -> dict:
    """Resolve the FULL extended e2e case list (MODE=full opt-in).

    The default mode runs the curated fixed set instead (prune_fixed_set
    over the ``extended_e2e`` policy key).  MAIN2MAIN_EXTENDED_TEST_CASES
    (passed as *override*) replaces the whole resolution.  Otherwise: tree
    scan − upstream skip_tests − fixed-set coverage − blocklist − missing
    files.  Returns a dict with the cases, tier1 membership, and everything
    dropped (evidence).
    """
    skipped = load_upstream_skip_tests(ascend_path)
    dropped_skip: list[str] = []
    if override is not None:
        cases = list(override)
        source = "override"
    else:
        cases = scan_e2e_pull_request_files(ascend_path)
        source = "tree-scan tests/e2e/pull_request"
        before = len(cases)
        if not include_skipped:
            dropped_skip = [c for c in cases if c in skipped]
            cases = [c for c in cases if c not in skipped]
        cases = [c for c in cases
                 if not covered_by_fixed(c, fixed_cases)
                 and not covered_by_fixed(c, blocked)]
        ts_print(f"[extended_e2e] tree scan: {before} file(s), "
                 f"{len(cases)} after skip/fixed/blocklist subtraction")
    existing = [c for c in cases
                if (Path(ascend_path) / _file_of(c)).exists()]
    return {
        "cases": existing,
        "tier1": [],  # filled by the caller (needs changed_modules)
        "dropped_missing": sorted(set(cases) - set(existing)),
        "dropped_skip": sorted(set(dropped_skip)),
        "source": source,
    }
