"""Hardware facts for the runtime profile: physical cores, RAM, and the disk
kind (hdd / ssd / unknown) of the disk that holds a path. Every function is
best effort and never raises; an unknown disk kind gets the HDD rules."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

_LOG = logging.getLogger(__name__)

_DETECT_TIMEOUT_SEC = 10
_FALLBACK_RAM_BYTES = 4 * 2**30  # conservative when RAM cannot be read


@dataclass(frozen=True)
class Hardware:
    physical_cores: int
    ram_bytes: int
    disk_kinds: dict[str, str] = field(default_factory=dict)


def physical_cores() -> int:
    try:
        n = psutil.cpu_count(logical=False)
        if not n:
            n = os.cpu_count()
    except Exception as exc:  # noqa: BLE001 - detection is best effort by design
        _LOG.warning("could not read the core count (%s); using os.cpu_count() or 1", exc)
        n = os.cpu_count()
    return max(1, int(n or 1))


def ram_bytes() -> int:
    try:
        return int(psutil.virtual_memory().total)
    except Exception as exc:  # noqa: BLE001 - detection is best effort by design
        _LOG.warning(
            "could not read total RAM (%s); assuming %d GiB", exc, _FALLBACK_RAM_BYTES // 2**30
        )
        return _FALLBACK_RAM_BYTES


def _existing_ancestor(path: Path) -> Path:
    p = Path(path).resolve() if Path(path).is_absolute() else Path(path).absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _windows_media_type(drive: str, timeout: float = _DETECT_TIMEOUT_SEC) -> str:
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
        timeout=timeout,
        check=False,
    )
    return (out.stdout or "").strip().splitlines()[0].strip() if out.stdout.strip() else ""


def _media_type_kind(raw: str) -> str:
    """Map a Windows MediaType string to hdd / ssd / unknown."""
    return {"HDD": "hdd", "SSD": "ssd"}.get((raw or "").strip().upper(), "unknown")


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


def _drive_of(path: Path) -> str | None:
    """The Windows drive letter holding ``path``, or None when not applicable."""
    if sys.platform != "win32" or str(path).startswith(("\\\\", "//")):
        return None
    drive = _existing_ancestor(Path(path)).drive.rstrip(":")
    return drive if len(drive) == 1 else None


def disk_kind(path: Path, timeout: float = _DETECT_TIMEOUT_SEC) -> str:
    try:
        if str(path).startswith(("\\\\", "//")):
            return "unknown"
        p = _existing_ancestor(Path(path))
        if sys.platform == "win32":
            drive = p.drive.rstrip(":")
            if len(drive) != 1:
                return "unknown"
            return _media_type_kind(_windows_media_type(drive, timeout))
        rot = _linux_rotational(p)
        return {"1": "hdd", "0": "ssd"}.get(rot or "", "unknown")
    except Exception:  # noqa: BLE001 - detection is best effort by design
        return "unknown"


def detect(paths: dict[str, Path]) -> Hardware:
    """Probe each distinct drive once, all within one shared deadline."""
    deadline = time.monotonic() + _DETECT_TIMEOUT_SEC
    by_drive: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for role, path in paths.items():
        try:
            drive = _drive_of(path)
        except Exception:  # noqa: BLE001 - detection is best effort by design
            drive = None
        if drive is not None and drive in by_drive:
            kinds[role] = by_drive[drive]
            continue
        remaining = deadline - time.monotonic()
        kind = disk_kind(path, remaining) if remaining > 0 else "unknown"
        if drive is not None:
            by_drive[drive] = kind
        kinds[role] = kind
    return Hardware(physical_cores=physical_cores(), ram_bytes=ram_bytes(), disk_kinds=kinds)
