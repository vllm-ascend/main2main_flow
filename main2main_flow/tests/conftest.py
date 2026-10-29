"""Shared pytest fixtures for the flow's unit tests.

The gate-layer maintainability review (MAIN2MAIN_STYLE_GATE) spawns a real
opencode session, and several tests drive `_final_quality_gate` directly.
Opening a live session there would make the suite depend on the CLI being
installed (and on a model being reachable), so it is disabled by default;
`test_gate_maintainability.py::test_gate_review_*` re-enables it and scripts
`run_opencode_review` instead.
"""
import pytest


@pytest.fixture(autouse=True)
def _disable_gate_style_review(monkeypatch):
    monkeypatch.setenv("MAIN2MAIN_STYLE_GATE", "0")
