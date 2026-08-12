"""pytest plugin: keep the ascend repo's top-level namespace packages ahead
of same-named regular packages in a vllm checkout sharing sys.path.

The UT batch runs with ``PYTHONPATH=<ascend>:<vllm>`` so two vllm versions
can be tested from checkouts.  vllm's ``examples/`` is a REGULAR package
(has ``__init__.py``) while ascend's is a namespace package, and Python
prefers regular packages over namespace packages regardless of path order —
so ``import examples.disaggregated_prefill_v1`` resolves into the vllm
checkout and fails collection (run 31563761175 batch exit=2; the test
passes in real CI where vllm is installed and has no ``examples/`` on
sys.path).

Pre-registering the ascend directories as namespace packages in
``sys.modules`` makes them win, matching the installed-vllm behavior.
"""
from __future__ import annotations

import os
import sys
import types


def _register_ascend_top_level_packages() -> None:
    ascend_root = sys.path[0] if sys.path else None
    if not ascend_root or not os.path.isdir(ascend_root):
        return
    for name in sorted(os.listdir(ascend_root)):
        if name.startswith(".") or name in sys.modules:
            continue
        ascend_dir = os.path.join(ascend_root, name)
        if not os.path.isdir(ascend_dir):
            continue
        if os.path.isfile(os.path.join(ascend_dir, "__init__.py")):
            continue  # regular package in ascend already wins by path order
        # ascend has a namespace dir; check whether a later sys.path entry
        # shadows it with a regular package (e.g. vllm's examples/).
        for entry in sys.path[1:]:
            if not entry:
                continue
            other = os.path.join(entry, name)
            if os.path.isfile(os.path.join(other, "__init__.py")):
                pkg = types.ModuleType(name)
                pkg.__path__ = [ascend_dir]
                sys.modules[name] = pkg
                break


def pytest_configure(config) -> None:  # noqa: ARG001
    _register_ascend_top_level_packages()
