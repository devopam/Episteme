from pathlib import Path

import pytest

from episteme.ops import hardware


def test_physical_cores_uses_psutil(monkeypatch):
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical=True: 4 if not logical else 8)
    assert hardware.physical_cores() == 4


def test_physical_cores_falls_back_when_psutil_returns_none(monkeypatch):
    monkeypatch.setattr(hardware.psutil, "cpu_count", lambda logical=True: None)
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: 6)
    assert hardware.physical_cores() == 6
    monkeypatch.setattr(hardware.os, "cpu_count", lambda: None)
    assert hardware.physical_cores() == 1


def test_ram_bytes(monkeypatch):
    class VM:
        total = 16 * 2**30

    monkeypatch.setattr(hardware.psutil, "virtual_memory", lambda: VM())
    assert hardware.ram_bytes() == 16 * 2**30


@pytest.mark.parametrize(
    "raw,expected", [("HDD", "hdd"), ("SSD", "ssd"), ("Unspecified", "unknown"), ("", "unknown")]
)
def test_windows_media_type_mapping(monkeypatch, tmp_path, raw, expected):
    monkeypatch.setattr(hardware.sys, "platform", "win32")
    monkeypatch.setattr(hardware, "_windows_media_type", lambda drive: raw)
    assert hardware.disk_kind(tmp_path) == expected


def test_linux_rotational(monkeypatch, tmp_path):
    monkeypatch.setattr(hardware.sys, "platform", "linux")
    monkeypatch.setattr(hardware, "_linux_rotational", lambda path: "1")
    assert hardware.disk_kind(tmp_path) == "hdd"
    monkeypatch.setattr(hardware, "_linux_rotational", lambda path: "0")
    assert hardware.disk_kind(tmp_path) == "ssd"
    monkeypatch.setattr(hardware, "_linux_rotational", lambda path: None)
    assert hardware.disk_kind(tmp_path) == "unknown"


def test_disk_kind_never_raises(monkeypatch, tmp_path):
    def boom(_):
        raise OSError("powershell missing")

    monkeypatch.setattr(hardware.sys, "platform", "win32")
    monkeypatch.setattr(hardware, "_windows_media_type", boom)
    assert hardware.disk_kind(tmp_path / "does-not-exist-yet") == "unknown"
    assert hardware.disk_kind(Path(r"\\server\share\x")) == "unknown"


def test_unc_path_skips_detection(monkeypatch):
    calls = []
    monkeypatch.setattr(hardware.sys, "platform", "win32")
    monkeypatch.setattr(hardware, "_windows_media_type", lambda drive: calls.append(drive) or "SSD")
    assert hardware.disk_kind(Path(r"\\server\share\x")) == "unknown"
    assert calls == []


def test_detect_builds_hardware(monkeypatch, tmp_path):
    monkeypatch.setattr(hardware, "physical_cores", lambda: 4)
    monkeypatch.setattr(hardware, "ram_bytes", lambda: 16 * 2**30)
    monkeypatch.setattr(hardware, "disk_kind", lambda p: "hdd")
    hw = hardware.detect({"raw": tmp_path, "processed": tmp_path})
    assert hw == hardware.Hardware(4, 16 * 2**30, {"raw": "hdd", "processed": "hdd"})
