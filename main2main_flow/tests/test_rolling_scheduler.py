"""Rolling scheduler: rolling device dispatch with no round barriers."""
from __future__ import annotations

import threading
import time

import pytest

from main2main_flow.scripts.utils.run_tests import (
    _RollingAllocator,
    _execute_rolling,
    _rolling_priority,
)

_POOL16 = list(range(16))


def _t(cards: int, i: int = 0) -> str:
    slugs = {1: "one_card", 2: "two_card", 3: "three_card", 4: "four_card",
             8: "eight_card"}
    return (f"tests/e2e/pull_request/{slugs[cards]}"
            f"/test_{cards}c_{i}.py")


def _cards(test: str) -> int:
    for n, slug in ((1, "one_card"), (2, "two_card"), (4, "four_card"),
                    (8, "eight_card")):
        if slug in test:
            return n
    raise AssertionError(test)


def _result(test: str) -> dict:
    return {"test": test, "run_suite_exit_code": 0, "ci_result": "passed",
            "code_bugs_count": 0, "env_flakes_count": 0,
            "log_path": "/nonexistent", "summary_path": "/nonexistent"}


class _FakeSuite:
    """launch() stand-in recording concurrency; optional barrier per test.

    A suite whose barrier is met (parties reached) was co-running with the
    other parties; a barrier timeout means it ran alone.  ``met`` records
    which tests actually passed their barrier.
    """

    def __init__(self, tests: list[str],
                 barriers: dict[str, threading.Barrier] | None = None):
        self.results = {t: _result(t) for t in tests}
        self.barriers = barriers or {}
        self.met: set[str] = set()
        self.lock = threading.Lock()
        self.busy = 0
        self.peak = 0
        self.started: dict[str, float] = {}
        self.finished: dict[str, float] = {}
        self.devices: dict[str, list[int]] = {}

    def __call__(self, test: str, devices: list[int]) -> dict:
        with self.lock:
            self.busy += _cards(test)
            self.peak = max(self.peak, self.busy)
            self.started[test] = time.monotonic()
            self.devices[test] = devices
        barrier = self.barriers.get(test)
        if barrier is not None:
            try:
                barrier.wait(timeout=2)
                with self.lock:
                    self.met.add(test)
            except threading.BrokenBarrierError:
                pass
        time.sleep(0.01)
        with self.lock:
            self.busy -= _cards(test)
            self.finished[test] = time.monotonic()
        return self.results[test]


# ---- _RollingAllocator ------------------------------------------------------

def test_allocate_first_fit_and_release():
    alloc = _RollingAllocator(_POOL16)
    assert alloc.allocate(_t(4)) == [0, 1, 2, 3]
    assert alloc.allocate(_t(4, 1)) == [4, 5, 6, 7]
    assert alloc.allocate(_t(1)) == [8]
    alloc.release([0, 1, 2, 3])
    assert alloc.allocate(_t(2)) == [0, 1]


def test_never_overallocates_beyond_capacity():
    alloc = _RollingAllocator(_POOL16)
    windows = [alloc.allocate(_t(4, i)) for i in range(4)]
    assert all(w is not None for w in windows)
    assert alloc.allocate(_t(4, 4)) is None  # 5th four-card does not fit
    alloc.release(windows[0])
    assert alloc.allocate(_t(4, 4)) is not None


def test_overrider_window_is_reserved_exclusively():
    overrider = _t(4, 99)
    alloc = _RollingAllocator(_POOL16, overriders={overrider})
    # non-overriders never touch the reserved 0..3
    for i in range(3):
        window = alloc.allocate(_t(4, i))
        assert window is not None
        assert not set(window) & {0, 1, 2, 3}
    # the overrider gets exactly its hardcoded physical 0..N-1
    assert alloc.allocate(overrider) == [0, 1, 2, 3]
    # while 0..3 are busy, the overrider cannot launch again
    assert alloc.allocate(overrider) is None
    alloc.release([0, 1, 2, 3])
    assert alloc.allocate(overrider) == [0, 1, 2, 3]


def test_pair_aligned_never_starts_on_odd_position():
    pool = [2, 3, 6, 7]  # two complete pairs, pod starting at physical 2
    alloc = _RollingAllocator(pool, pair_aligned=True)
    assert alloc.allocate(_t(2)) == [2, 3]  # position 0 (even)
    assert alloc.allocate(_t(1)) == [6]  # position 2 (even); position 3 skipped
    alloc.release([2, 3])
    assert alloc.allocate(_t(1)) == [2]  # position 0 again, not position 1


def test_pair_aligned_odd_need_consumes_alignment(monkeypatch):
    # no "three_card" path pattern exists — a 3-card need comes via overrides
    odd = _t(3)
    monkeypatch.setattr("main2main_flow.scripts.utils.run_tests._CARD_OVERRIDES",
                        {odd: 3})
    alloc = _RollingAllocator(list(range(8)), pair_aligned=True)
    assert alloc.allocate(odd) == [0, 1, 2]  # even start, odd need allowed
    # next allocation may not START on position 3 (odd die of pair 2-3)
    assert alloc.allocate(_t(2)) == [4, 5]
    assert alloc.allocate(_t(1)) == [6]


# ---- _rolling_priority ------------------------------------------------------

def test_priority_is_lpt_unless_preserve_order():
    est = {_t(4): 60, _t(4, 1): 600, _t(1): 60, _t(2): 300}
    tests = [_t(1), _t(2), _t(4), _t(4, 1)]
    assert _rolling_priority(tests, est, preserve_order=False) == [
        _t(4, 1), _t(4), _t(2), _t(1)]  # cards desc, then estimate desc
    assert _rolling_priority(tests, est, preserve_order=True) == tests


# ---- _execute_rolling -------------------------------------------------------

def test_every_suite_runs_exactly_once_and_cards_respected():
    tests = [_t(4), _t(4, 1), _t(2), _t(2, 1)] + [_t(1, i) for i in range(6)]
    fake = _FakeSuite(tests)
    results, launches, peak = _execute_rolling(
        tests, capacity=16, pool=_POOL16, est_times={}, overriders=set(),
        pair_aligned=False, preserve_order=False, launch=fake)
    assert sorted(r["test"] for r in results) == sorted(tests)
    assert [l["launch"] for l in launches] == list(range(1, len(tests) + 1))
    assert peak == fake.peak <= 16


def test_one_card_suites_truly_overlap():
    """Both 1-card suites must be co-running (barrier met), not serialized."""
    tests = [_t(1), _t(1, 1)]
    barrier = threading.Barrier(2)
    fake = _FakeSuite(tests, barriers={t: barrier for t in tests})
    _execute_rolling(tests, capacity=16, pool=_POOL16, est_times={},
                     overriders=set(), pair_aligned=False,
                     preserve_order=False, launch=fake)
    assert fake.met == set(tests)  # a serial scheduler would leave met empty


def test_overrider_coexists_with_one_card_suite_on_its_own_window():
    overrider = _t(4)
    mate = _t(1)
    barrier = threading.Barrier(2)
    fake = _FakeSuite([overrider, mate],
                      barriers={overrider: barrier, mate: barrier})
    results, _, _ = _execute_rolling(
        [overrider, mate], capacity=16, pool=_POOL16, est_times={},
        overriders={overrider}, pair_aligned=False, preserve_order=False,
        launch=fake)
    # barrier met → the overrider truly co-ran with the 1-card suite
    assert fake.met == {overrider, mate}
    assert fake.devices[overrider] == [0, 1, 2, 3]
    assert not set(fake.devices[mate]) & {0, 1, 2, 3}
    assert {r["test"] for r in results} == {overrider, mate}


def test_two_overriders_serialize():
    overrider_a, overrider_b = _t(4), _t(4, 1)
    barrier = threading.Barrier(2)
    fake = _FakeSuite([overrider_a, overrider_b],
                      barriers={overrider_a: barrier, overrider_b: barrier})
    _execute_rolling([overrider_a, overrider_b], capacity=16, pool=_POOL16,
                     est_times={}, overriders={overrider_a, overrider_b},
                     pair_aligned=False, preserve_order=False, launch=fake)
    assert fake.met == set()  # co-run would complete the barrier
    no_overlap = (fake.finished[overrider_a] <= fake.started[overrider_b]
                  or fake.finished[overrider_b] <= fake.started[overrider_a])
    assert no_overlap


def test_re_dispatch_after_completion_fits_pool_sized_rounds():
    """6 four-card suites on 16 cards: 4 concurrent, then re-dispatch."""
    tests = [_t(4, i) for i in range(6)]
    fake = _FakeSuite(tests)
    results, _, peak = _execute_rolling(
        tests, capacity=16, pool=_POOL16, est_times={}, overriders=set(),
        pair_aligned=False, preserve_order=False, launch=fake)
    assert len(results) == 6
    assert peak == 16
