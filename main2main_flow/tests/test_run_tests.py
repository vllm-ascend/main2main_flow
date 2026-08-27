"""Pair-aligned device assignment for dual-die NPUs (A3)."""
from __future__ import annotations

import pytest

from main2main_flow.scripts.utils.run_tests import (
    _assign_devices,
    _test_cards,
    _validate_pair_aligned,
)

def _make_round(*counts):
    """One round of tests whose paths encode the given card counts."""
    slugs = {
        1: "one_card", 2: "two_card", 4: "four_card", 8: "eight_card",
    }
    return [[f"tests/e2e/pull_request/{slugs[c]}/test_{i}.py"
             for i, c in enumerate(counts)]]


def _devices(rnd):
    return [d for _, d in rnd]


# ---- _validate_pair_aligned -------------------------------------------------

def test_validation_accepts_complete_pairs():
    _validate_pair_aligned([0, 1, 2, 3, 4, 5])
    _validate_pair_aligned([2, 3, 4, 5])  # pod starting at physical 2
    _validate_pair_aligned([0, 1, 6, 7])  # gaps still fine as long as pairs close


def test_validation_rejects_lone_dies():
    with pytest.raises(ValueError, match="lone"):
        _validate_pair_aligned([1, 2, 3])  # die 1's partner 0 missing
    with pytest.raises(ValueError, match="lone"):
        _validate_pair_aligned([0, 1, 3, 4, 5])  # 3 split from 2
    with pytest.raises(ValueError, match="lone"):
        _validate_pair_aligned([0, 2, 3])  # 0's partner 1 missing


# ---- _assign_devices (pair-aligned) -----------------------------------------

def test_pair_aligned_even_test_gets_complete_pairs():
    rnd = _make_round(2, 2)  # two two_card tests
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0,1", "2,3"]


def test_pair_aligned_one_card_never_takes_odd_die():
    rnd = _make_round(1, 1)  # two one_card tests
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0", "2"]  # die 1 left unused, not shared


def test_pair_aligned_odd_offset_skipped():
    # one_card then two_card: the two_card must not start on odd die 1
    rnd = _make_round(1, 2)
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0", "2,3"]


def test_pair_aligned_four_card_matches_hardcoded_range():
    rnd = _make_round(4)
    out = _assign_devices(rnd, [0, 1, 2, 3, 4, 5], pair_aligned=True)
    assert _devices(out[0]) == ["0,1,2,3"]


def test_pair_aligned_pod_starting_at_even_physical_id():
    # pod allocated physical 2-5: logical ids mirror them, still paired
    rnd = _make_round(2, 2)
    out = _assign_devices(rnd, [2, 3, 4, 5], pair_aligned=True)
    assert _devices(out[0]) == ["2,3", "4,5"]


def test_pair_aligned_rejects_broken_pod_allocation():
    rnd = _make_round(2)
    with pytest.raises(ValueError, match="lone"):
        _assign_devices(rnd, [1, 3], pair_aligned=True)


def test_non_pair_aligned_behavior_unchanged():
    # default path keeps the historical sequential packing
    rnd = _make_round(1, 1)
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=False)
    assert _devices(out[0]) == ["0", "1"]


def test_multiple_rounds_each_restart_from_zero():
    rounds = [_make_round(2)[0], _make_round(1)[0]]
    out = _assign_devices(rounds, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0,1"]
    assert _devices(out[1]) == ["0"]


def test_cards_inferred_from_path():
    assert _test_cards("tests/e2e/pull_request/one_card/test_x.py") == 1
    assert _test_cards("tests/e2e/pull_request/two_card/test_x.py") == 2
    assert _test_cards("tests/e2e/pull_request/four_card/test_x.py") == 4
