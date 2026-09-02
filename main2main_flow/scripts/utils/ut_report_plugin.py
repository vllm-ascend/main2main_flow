"""pytest plugin: dump structured per-test failures and collection errors as JSON.

Loaded via ``-p main2main_flow.scripts.utils.ut_report_plugin`` from
ut_check.  pytest's hook objects carry a failure as structured data
(ReprFileLocation: path/lineno/message; the repr string: full traceback)
— no terminal-text regex parsing.  The text route is what lost a
collection ImportError entirely in run 33538038959: the violation
showed only a pytest-asyncio deprecation-warning tail, and the adapter
chased it for 3 fix rounds.

Report path comes from ``--ut-report <path>``; written at session
finish.  Collection errors (``pytest_collectreport``) are recorded
separately so a module failing to import is visible as such.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_FAILED_TESTS: list[dict] = []
_COLLECT_ERRORS: list[dict] = []


def pytest_addoption(parser) -> None:
    parser.addoption("--ut-report", action="store", default="",
                     help="write structured test report JSON to PATH")


def _longrepr_data(longrepr) -> dict:
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None:
        return {
            "path": getattr(crash, "path", ""),
            "lineno": getattr(crash, "lineno", 0),
            "message": getattr(crash, "message", ""),
            "traceback": str(longrepr),
        }
    return {"message": str(longrepr), "traceback": str(longrepr)}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call) -> None:
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        _FAILED_TESTS.append({
            "nodeid": report.nodeid,
            "outcome": report.outcome,
            "duration": round(report.duration, 3),
            "longrepr": _longrepr_data(report.longrepr),
        })


@pytest.hookimpl(hookwrapper=True)
def pytest_collectreport(report) -> None:
    outcome = yield
    if report.failed:
        _COLLECT_ERRORS.append({
            "nodeid": report.nodeid,
            "longrepr": _longrepr_data(report.longrepr),
        })


@pytest.hookimpl
def pytest_sessionfinish(session, exitstatus) -> None:
    path = session.config.getoption("--ut-report")
    if not path:
        return
    Path(path).write_text(
        json.dumps({"exitstatus": exitstatus,
                    "failed_tests": _FAILED_TESTS,
                    "collect_errors": _COLLECT_ERRORS},
                   indent=2, ensure_ascii=False),
        encoding="utf-8")
