"""PATH-level command interception for opencode agent sessions.

The adapter model keeps trying to run tests/checks inside its session
("All pass. Let me run tests/ut/ops") despite the SKILL.md ban — each run
burns 5-15min and pushes the session past the total timeout, so the step
dies mid-work.  PATH-prepended wrapper scripts block the entry points;
exit 0 so the model does not treat it as a fixable failure.

Adding new interception types:
- A bare command name (e.g. `pytest`) only needs to join BLOCKED_CHECK_CMDS;
  the generic blocker template covers it.
- A command with pass-through logic (like `python`/`uv`, which must forward
  non-banned invocations to the real binary) needs a new *_script template
  registered in ensure_tool_guard().
"""

import os
import sys
import tempfile
from pathlib import Path

BLOCKED_CHECK_CMDS = frozenset({
    "pytest", "mypy", "ruff", "pre-commit", "flake8", "black", "isort",
})
BLOCKED_PY_MODULES = frozenset({
    "pytest", "mypy", "ruff", "pre_commit", "py_compile", "flake8",
    "black", "isort",
})
GUARD_MSG = (
    "BLOCKED by main2main_flow: running tests/checks is forbidden during "
    "adaptation (SKILL.md Rules). Static analysis only — read code and edit "
    "files. Do not retry this command."
)
GUARD_DIR = Path(tempfile.gettempdir()) / "m2m-adapt-tool-guard"


def _blocker_script() -> str:
    return (f"#!{sys.executable}\n"
            "import sys\n"
            f"print({GUARD_MSG!r}, flush=True)\n"
            "sys.exit(0)\n")


def _python_script() -> str:
    blocked = ", ".join(repr(m) for m in sorted(BLOCKED_PY_MODULES))
    return (f"#!{sys.executable}\n"
            "import os, sys\n"
            f"_BLOCKED = {{{blocked}}}\n"
            "if len(sys.argv) >= 3 and sys.argv[1] == '-m' "
            "and sys.argv[2] in _BLOCKED:\n"
            f"    print({GUARD_MSG!r}, flush=True)\n"
            "    sys.exit(0)\n"
            "os.execv(sys.executable, [sys.executable] + sys.argv[1:])\n")


def _uv_script() -> str:
    blocked = ", ".join(repr(m) for m in sorted(BLOCKED_CHECK_CMDS))
    modules = ", ".join(repr(m) for m in sorted(BLOCKED_PY_MODULES))
    return (f"#!{sys.executable}\n"
            "import os, sys\n"
            f"_BLOCKED = {{{blocked}}}\n"
            f"_MODULES = {{{modules}}}\n"
            "def _real(cmd):\n"
            "    guard = os.path.dirname(os.path.abspath(__file__))\n"
            "    for d in os.environ.get('PATH', '').split(os.pathsep):\n"
            "        if not d or os.path.abspath(d) == guard:\n"
            "            continue\n"
            "        p = os.path.join(d, cmd)\n"
            "        if os.path.isfile(p) and os.access(p, os.X_OK):\n"
            "            return p\n"
            "    return None\n"
            "args = sys.argv[1:]\n"
            "if args and args[0] == 'run':\n"
            "    tail = args[1:]\n"
            "    for i, a in enumerate(tail):\n"
            "        if a in _BLOCKED or (a in ('python', 'python3') and "
            "i + 2 < len(tail) and tail[i + 1] == '-m' and tail[i + 2] in _MODULES):\n"
            f"            print({GUARD_MSG!r}, flush=True)\n"
            "            sys.exit(0)\n"
            "real = _real('uv')\n"
            "if real:\n"
            "    os.execv(real, [real] + args)\n"
            "sys.exit('uv not found')\n")


def ensure_tool_guard() -> Path:
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    scripts = {name: _blocker_script()
               for name in BLOCKED_CHECK_CMDS}
    scripts["python"] = _python_script()
    scripts["python3"] = _python_script()
    scripts["uv"] = _uv_script()
    for name, body in scripts.items():
        p = GUARD_DIR / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    return GUARD_DIR


def guard_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(ensure_tool_guard()) + os.pathsep + env.get("PATH", "")
    return env


__all__ = [
    "BLOCKED_CHECK_CMDS",
    "BLOCKED_PY_MODULES",
    "GUARD_MSG",
    "GUARD_DIR",
    "ensure_tool_guard",
    "guard_env",
]
