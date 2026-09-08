"""NPU pool detection moved into the flow initialize phase (from the workflow)."""
from __future__ import annotations

import os

import pytest

from main2main_flow.scripts.utils import run_tests as rt
from main2main_flow.scripts.utils.run_tests import (
    NpuProbeUnavailable,
    _parse_npu_smi,
    detect_free_npu_chips,
    pin_free_npu_chips,
)


def _npu_smi_text(occupancies):
    """Render an npu-smi info table: occupancies = [(chip, used, total), ...]."""
    lines = [
        "+-------------------------------------------------------------------------------------------+",
        "| npu-smi 24.1.rc1                 Version: 24.1.rc1                                        |",
        "+---------------------------+---------------+------------------------------------------------+",
        "| NPU     Name              | Health        | Power(W)     Temp(C)         Huge-Pages(page) |",
        "| Chip    Device             | Bus-Id        | AICore(%)    Memory-Usage(MB)                |",
    ]
    for chip, used, total in occupancies:
        lines.append("| 0       910B4              | OK            | 65.5         42              0                |")
        # npu-smi drops the space before '/' for 5-digit values ("62265/ 65536").
        sep = "/" if used >= 10000 else " / "
        lines.append(
            f"| 0       {chip}                  | 0000:C1:00.{chip:X}  | 0            {used}{sep}{total}                |")
    lines.append("+-------------------------------------------------------------------------------------------+")
    return "\n".join(lines) + "\n"


class TestParseNpuSmi:
    def test_idle_and_occupied(self):
        text = _npu_smi_text([(0, 723, 65536), (1, 62265, 65536)])
        assert _parse_npu_smi(text) == [(0, 723, 65536), (1, 62265, 65536)]

    def test_no_space_before_slash(self):
        text = _npu_smi_text([(2, 62265, 65536)])
        assert _parse_npu_smi(text) == [(2, 62265, 65536)]

    def test_non_pci_rows_skipped(self):
        # Health/Name rows carry no bus-id, and a fake chip row without one is
        # ignored too.
        text = "| NPU     Name              | Health        |\n| 0       910B4  | OK |\n"
        assert _parse_npu_smi(text) == []

    def test_last_pair_is_hbm(self):
        # Multiple "x / y" pairs on a row: the last one is HBM-Usage.
        line = "| 0       0  | 0000:C1:00.0  | 12 / 34  723    / 65536 |"
        assert _parse_npu_smi(line) == [(0, 723, 65536)]

    def test_zero_total_guarded(self):
        line = "| 0       0  | 0000:C1:00.0  | 0/0 |"
        assert _parse_npu_smi(line) == []

    def test_sorted_by_chip(self):
        text = _npu_smi_text([(3, 100, 65536), (1, 200, 65536)])
        assert [r[0] for r in _parse_npu_smi(text)] == [1, 3]


class TestDetectFreeNpuChips:
    def test_local_probe(self, monkeypatch):
        monkeypatch.delenv("MAIN2MAIN_REMOTE_HOST", raising=False)
        monkeypatch.delenv("MAIN2MAIN_REMOTE_CONTAINER", raising=False)
        cmds = []

        def fake_run(cmd_list, **kw):
            cmds.append(cmd_list[-1])
            if "command -v" in cmd_list[-1]:
                return type("R", (), {"returncode": 0, "stdout": "/usr/bin/npu-smi", "stderr": ""})()
            return type("R", (), {"returncode": 0,
                                  "stdout": _npu_smi_text([(0, 723, 65536), (1, 62265, 65536)]),
                                  "stderr": ""})()

        monkeypatch.setattr(rt.subprocess, "run", fake_run)
        free, total = detect_free_npu_chips()
        assert (free, total) == ([0], 2)
        assert "command -v npu-smi" in cmds[0]

    def test_remote_probe(self, monkeypatch):
        monkeypatch.setenv("MAIN2MAIN_REMOTE_HOST", "host1")
        monkeypatch.setenv("MAIN2MAIN_REMOTE_CONTAINER", "ctr1")
        recorded = {}

        def fake_ssh(host, cmd, **kw):
            recorded["host"], recorded["cmd"] = host, cmd
            if "command -v" in cmd:
                return type("R", (), {"returncode": 0, "stdout": "/usr/bin/npu-smi", "stderr": ""})()
            return type("R", (), {"returncode": 0,
                                  "stdout": _npu_smi_text([(0, 723, 65536), (1, 62265, 65536)]),
                                  "stderr": ""})()

        monkeypatch.setattr(rt, "_ssh", fake_ssh)
        free, total = detect_free_npu_chips()
        assert (free, total) == ([0], 2)
        assert recorded["host"] == "host1"
        assert "docker exec ctr1" in recorded["cmd"]
        assert "npu-smi info" in recorded["cmd"]

    def test_missing_npu_smi(self, monkeypatch):
        monkeypatch.delenv("MAIN2MAIN_REMOTE_HOST", raising=False)
        monkeypatch.delenv("MAIN2MAIN_REMOTE_CONTAINER", raising=False)
        monkeypatch.setattr(
            rt.subprocess, "run",
            lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})())
        with pytest.raises(NpuProbeUnavailable):
            detect_free_npu_chips()

    def test_failing_npu_smi(self, monkeypatch):
        monkeypatch.delenv("MAIN2MAIN_REMOTE_HOST", raising=False)
        monkeypatch.delenv("MAIN2MAIN_REMOTE_CONTAINER", raising=False)

        def fake_run(cmd_list, **kw):
            if "command -v" in cmd_list[-1]:
                return type("R", (), {"returncode": 0, "stdout": "/usr/bin/npu-smi", "stderr": ""})()
            return type("R", (), {"returncode": 14, "stdout": "", "stderr": "usb error"})()

        monkeypatch.setattr(rt.subprocess, "run", fake_run)
        with pytest.raises(NpuProbeUnavailable, match="exit 14"):
            detect_free_npu_chips()

    def test_no_parsable_rows(self, monkeypatch):
        monkeypatch.delenv("MAIN2MAIN_REMOTE_HOST", raising=False)
        monkeypatch.delenv("MAIN2MAIN_REMOTE_CONTAINER", raising=False)

        def fake_run(cmd_list, **kw):
            if "command -v" in cmd_list[-1]:
                return type("R", (), {"returncode": 0, "stdout": "/usr/bin/npu-smi", "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": "nothing here", "stderr": ""})()

        monkeypatch.setattr(rt.subprocess, "run", fake_run)
        with pytest.raises(NpuProbeUnavailable, match="no parsable"):
            detect_free_npu_chips()


class TestPinFreeNpuChips:
    def test_skip_when_env_test(self, monkeypatch, capsys):
        monkeypatch.setenv("SKIP_E2E_TEST", "true")
        monkeypatch.setattr(
            rt, "detect_free_npu_chips",
            lambda: (_ for _ in ()).throw(AssertionError("must not probe")))
        pin_free_npu_chips()
        assert "ASCEND_RT_VISIBLE_DEVICES" not in os.environ

    def test_preset_env_untouched(self, monkeypatch):
        monkeypatch.setenv("ASCEND_RT_VISIBLE_DEVICES", "4,5")
        monkeypatch.setattr(
            rt, "detect_free_npu_chips",
            lambda: (_ for _ in ()).throw(AssertionError("must not probe")))
        pin_free_npu_chips()
        assert os.environ["ASCEND_RT_VISIBLE_DEVICES"] == "4,5"

    def test_zero_free_is_fatal(self, monkeypatch):
        monkeypatch.delenv("SKIP_E2E_TEST", raising=False)
        monkeypatch.delenv("ASCEND_RT_VISIBLE_DEVICES", raising=False)
        monkeypatch.setattr(rt, "detect_free_npu_chips", lambda: ([], 8))
        with pytest.raises(SystemExit):
            pin_free_npu_chips()

    def test_probe_unavailable_tolerated(self, monkeypatch, capsys):
        monkeypatch.delenv("SKIP_E2E_TEST", raising=False)
        monkeypatch.delenv("ASCEND_RT_VISIBLE_DEVICES", raising=False)
        monkeypatch.setattr(
            rt, "detect_free_npu_chips",
            lambda: (_ for _ in ()).throw(NpuProbeUnavailable("npu-smi not found")))
        pin_free_npu_chips()
        assert "skipped" in capsys.readouterr().out

    def test_success_pins_env(self, monkeypatch, capsys):
        monkeypatch.delenv("SKIP_E2E_TEST", raising=False)
        monkeypatch.delenv("ASCEND_RT_VISIBLE_DEVICES", raising=False)
        monkeypatch.setattr(rt, "detect_free_npu_chips", lambda: ([2, 5, 6], 8))
        pin_free_npu_chips()
        assert os.environ["ASCEND_RT_VISIBLE_DEVICES"] == "2,5,6"
        assert "3/8 chips free" in capsys.readouterr().out
