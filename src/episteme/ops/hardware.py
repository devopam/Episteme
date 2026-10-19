"""Hardware facts for the runtime profile: physical cores, RAM, and the disk
kind (hdd / ssd / unknown) of the disk that holds a path. Every function is
best effort and never raises; an unknown disk kind gets the HDD rules."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psutil

_DETECT_TIMEOUT_SEC = 10


@dataclass(frozen=True)
class Hardware:
    physical_cores: int
    ram_bytes: int
    disk_kinds: dict[str, str] = field(default_factory=dict)


def physical_cores() -> int:
    n = psutil.cpu_count(logical=False)
    if not n:
        n = os.cpu_count()
    return max(1, int(n or 1))


def ram_bytes() -> int:
    return int(psutil.virtual_memory().total)


def _existing_ancestor(path: Path) -> Path:
    p = Path(path).resolve() if Path(path).is_absolute() else Path(path).absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _windows_media_type(drive: str) -> str:
    """MediaType ("HDD", "SSD", "Unspecified", ...) of the physical disk holding
    drive letter ``drive`` (e.g. "D")."""
    cmd = (
        f"(Get-Partition -DriveLetter {drive} | Get-Disk | "
        "ForEach-Object { Get-PhysicalDisk -DeviceNumber $_.Number }).MediaType"
    )
    out = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
        timeout=_DETECT_TIMEOUT_SEC,
        check=False,
    )
    return (out.stdout or "").strip().splitlines()[0].strip() if out.stdout.strip() else ""


def _linux_rotational(path: Path) -> str | None:
    """'1' (rotational) / '0' / None for the block device holding ``path``."""
    dev = os.stat(path).st_dev
    major, minor = os.major(dev), os.minor(dev)
    sys_dev = Path(f"/sys/dev/block/{major}:{minor}")
    for cand in (
        sys_dev / "queue" / "rotational",
        sys_dev.resolve().parent / "queue" / "rotational",
    ):
        if cand.is_file():
            return cand.read_text().strip()
    return None


def disk_kind(path: Path) -> str:
    try:
        if str(path).startswith(("\\\\", "//")):
            return "unknown"
        p = _existing_ancestor(Path(path))
        if sys.platform == "win32":
            drive = p.drive.rstrip(":")
            if len(drive) != 1:
                return "unknown"
            raw = _windows_media_type(drive).upper()
            return {"HDD": "hdd", "SSD": "ssd"}.get(raw, "unknown")
        rot = _linux_rotational(p)
        return {"1": "hdd", "0": "ssd"}.get(rot or "", "unknown")
    except Exception:  # noqa: BLE001 - detection is best effort by design
        return "unknown"


def detect(paths: dict[str, Path]) -> Hardware:
    return Hardware(
        physical_cores=physical_cores(),
        ram_bytes=ram_bytes(),
        disk_kinds={role: disk_kind(p) for role, p in paths.items()},
    )
