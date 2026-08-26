"""Tool-guard: PATH-level interception of banned commands in agent sessions.

Wrappers in GUARD_DIR are prepended to PATH so banned commands (test
runners, checkers) print a BLOCKED notice and exit 0 instead of burning
agent tool rounds; everything else passes through to the real binary.
See guard.py for how to add new interception types.
"""

from main2main_flow.scripts.agent.toolguard.guard import (
    BLOCKED_CHECK_CMDS,
    BLOCKED_PY_MODULES,
    GUARD_DIR,
    GUARD_MSG,
    ensure_tool_guard,
    guard_env,
)

__all__ = [
    "BLOCKED_CHECK_CMDS",
    "BLOCKED_PY_MODULES",
    "GUARD_MSG",
    "GUARD_DIR",
    "ensure_tool_guard",
    "guard_env",
]
