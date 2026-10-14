"""Day-bounded step planning: at most MAX_STEPS_PER_DAY steps per calendar
day (user decision 2026-10-14, run 38512795089 planned 17 steps for one
day of 191 commits)."""
from __future__ import annotations

from typing import Any

from main2main_flow.scripts.utils.plan_steps import (
    _day_buckets,
    _plan_steps,
    _split_evenly,
)


def _commit(sha: str, date: str, subject: str = "s") -> dict[str, str]:
    return {"sha": sha, "subject": subject, "date": date}


def _commits(n: int, date: str, lines: int = 100,
             prefix: str = "c") -> list[dict[str, str]]:
    return [_commit(f"{prefix}{i}", date) for i in range(n)]


def _lines(commits: list[dict[str, str]], lines: int) -> dict[str, int]:
    return {c["sha"]: lines for c in commits}


def _sha_at(steps: list[dict[str, Any]], day: int, i: int) -> str:
    return steps[day]["commits"][i]["sha"]


# ---- _day_buckets ------------------------------------------------------------

def test_day_buckets_group_consecutive_dates():
    commits = _commits(2, "2026-10-13", prefix="a") + \
        _commits(3, "2026-10-14", prefix="b")
    buckets = _day_buckets(commits)
    assert [len(b) for b in buckets] == [2, 3]
    assert all(c["date"] == "2026-10-13" for c in buckets[0])
    assert all(c["date"] == "2026-10-14" for c in buckets[1])


# ---- _split_evenly -----------------------------------------------------------

def test_split_evenly_balances_effective_lines():
    commits = _commits(4, "2026-10-13")
    effective = [100, 100, 1, 1]
    buckets = _split_evenly(commits, effective, 2)
    assert [[c["sha"] for c in b] for b in buckets] == \
        [["c0", "c1"], ["c2", "c3"]]


def test_split_evenly_huge_commit_lands_alone():
    # One giant commit + a long zero-effective tail (ascend_affected=False
    # commits): the boundary falls right after the giant commit.
    commits = _commits(5, "2026-10-13")
    effective = [9000, 0, 0, 0, 0]
    buckets = _split_evenly(commits, effective, 2)
    assert [c["sha"] for c in buckets[0]] == ["c0"]
    assert [c["sha"] for c in buckets[1]] == ["c1", "c2", "c3", "c4"]


def test_split_evenly_fewer_commits_than_k():
    commits = _commits(2, "2026-10-13")
    buckets = _split_evenly(commits, [10, 10], 4)
    assert [[c["sha"] for c in b] for b in buckets] == [["c0"], ["c1"]]


def test_split_evenly_never_produces_empty_buckets():
    # All the weight sits at the front: zero-line tails merge into the last
    # bucket instead of forming empty steps.
    commits = _commits(4, "2026-10-13")
    buckets = _split_evenly(commits, [50, 50, 0, 0], 3)
    assert all(b for b in buckets)
    assert sum(len(b) for b in buckets) == 4


# ---- _plan_steps -------------------------------------------------------------

def _plan(commits, lines, impacts=None, max_per_day=2, base="base"):
    import main2main_flow.scripts.utils.plan_steps as ps
    old = ps.MAX_STEPS_PER_DAY
    ps.MAX_STEPS_PER_DAY = max_per_day
    try:
        return _plan_steps(commits, lines, impacts or {}, base)
    finally:
        ps.MAX_STEPS_PER_DAY = old


def test_small_day_is_one_step():
    commits = _commits(3, "2026-10-13", lines=100)
    steps = _plan(commits, _lines(commits, 100))
    assert len(steps) == 1
    assert steps[0]["start_commit"] == "base"
    assert steps[0]["end_commit"] == "c2"
    assert steps[0]["commit_count"] == 3


def test_big_day_is_exactly_two_steps():
    # 191 commits / 33k lines — the run 38512795089 shape that planned 17
    # steps — now caps at two.
    commits = _commits(191, "2026-10-13", lines=172)
    steps = _plan(commits, _lines(commits, 172))
    assert len(steps) == 2
    assert sum(s["commit_count"] for s in steps) == 191
    # chaining: step 1 starts at base, step 2 starts at step 1's end
    assert steps[0]["start_commit"] == "base"
    assert steps[1]["start_commit"] == steps[0]["end_commit"]
    # balanced effective-line share
    assert abs(steps[0]["effective_lines"] - steps[1]["effective_lines"]) <= 172


def test_steps_never_span_days():
    # A tiny day then a huge day: 1 step + 2 steps, none spanning a date.
    commits = _commits(2, "2026-10-12", prefix="s") + \
        _commits(191, "2026-10-13", lines=172, prefix="b")
    lines = {**_lines(commits[:2], 10), **_lines(commits[2:], 172)}
    steps = _plan(commits, lines)
    assert len(steps) == 3
    for step in steps:
        dates = {c["date"] for c in step["commits"]}
        assert len(dates) == 1
    assert steps[0]["commit_count"] == 2


def test_single_commit_exceeding_budget_is_its_own_day_step():
    # A single huge commit in its own day stays one step (the day has one
    # commit); a following small day is separate.
    commits = [_commit("huge", "2026-10-13")] + \
        _commits(2, "2026-10-14", lines=10, prefix="t")
    lines = {"huge": 90000, "t0": 10, "t1": 10}
    steps = _plan(commits, lines)
    assert [s["commit_count"] for s in steps] == [1, 2]
    assert steps[0]["commits"][0]["sha"] == "huge"


def test_affected_false_commits_contribute_zero_lines():
    # vllm-report routed everything to no-op: the day stays one step even
    # with hundreds of commits.
    commits = _commits(50, "2026-10-13")
    lines = _lines(commits, 5000)
    impacts = {c["sha"]: {"ascend_affected": False} for c in commits}
    steps = _plan(commits, lines, impacts)
    assert len(steps) == 1
    assert steps[0]["effective_lines"] == 0
    assert steps[0]["needs_adaptation"] is False


def test_max_steps_per_day_env_one_keeps_one_step_per_day():
    commits = _commits(191, "2026-10-13", lines=172)
    steps = _plan(commits, _lines(commits, 172), max_per_day=1)
    assert len(steps) == 1


def test_every_commit_placed_in_order():
    commits = _commits(7, "2026-10-13", lines=900) + \
        _commits(7, "2026-10-14", lines=900, prefix="d")
    steps = _plan(commits, _lines(commits, 900))
    placed = [c["sha"] for s in steps for c in s["commits"]]
    assert placed == [c["sha"] for c in commits]
    for prev, nxt in zip(steps, steps[1:]):
        assert nxt["start_commit"] == prev["end_commit"]
