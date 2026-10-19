# Ops profile (PR 1 of the operating standard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One `episteme.ops.profile` module that detects the hardware, computes every runtime value from the spec's rules (with env overrides), and is the only source of workers, memory caps, threads, batch sizes, part caps and PostgreSQL settings.

**Architecture:** `ops/hardware.py` detects facts (cores, RAM, disk kind per path). `ops/profile.py` holds a pure `compute_profile(hw, overrides)` and a cached `get_profile()` that feeds it detected facts and the override values that `config.py` reads. Existing stages read `get_profile()` instead of constants; `tune_postgres.sh` reads `profile show --format=pg`.

**Tech Stack:** Python 3.13, `psutil` (made a direct dependency), pytest, bash (Git Bash on Windows), PowerShell `Get-PhysicalDisk` / Linux sysfs for disk kind.

**Spec:** `docs/superpowers/specs/2026-10-19-pipeline-operating-standard-design.md` (sections 4, 5 and 9, PR 1).

## Global Constraints

- `src/episteme/config.py` is the only module that reads `os.environ`; `ops/` modules get override values from `get_settings()`.
- Memory budget (spec 5.2): OS 25% of RAM, at least 3 GB; `shared_buffers` 15% of RAM, at most 16 GB; `maintenance_work_mem` 6% of RAM, at most 2 GB; `work_mem` 32 MB; pipeline share = the rest; DuckDB cap = 50% of the pipeline share.
- Parallelism (spec 5.3): CPU workers = physical cores - 1, at least 1, at most pipeline share / 1.5 GB; I/O concurrency HDD 2, SSD 4 x cores; download threads 16, PMC 48; `max_parallel_workers_per_gather` = cores // 2; `effective_io_concurrency` HDD 2, SSD 200; batch rows 50,000, 100,000 when RAM >= 48 GB; part caps 1,000,000 rows / 512 MB, 2,000,000 rows / 1 GB on an SSD with >= 32 GB RAM.
- Unknown disk kind = HDD (spec 5.1).
- Override keys (spec 5.4): `EPISTEME_EXTRACT_WORKERS`, `EPISTEME_IO_CONCURRENCY`, `EPISTEME_DOWNLOAD_THREADS`, `EPISTEME_PMC_THREADS`, `EPISTEME_DUCKDB_MEMORY_LIMIT`, `EPISTEME_DUCKDB_TEMP_DIR`, `EPISTEME_BATCH_ROWS`, `EPISTEME_PART_ROWS`, `EPISTEME_PART_BYTES`, `EPISTEME_PG_SHARED_BUFFERS`, `EPISTEME_PG_MAINTENANCE_WORK_MEM`, `EPISTEME_PG_WORK_MEM`. An invalid value logs a warning and the rule's value is used.
- `profile show` prints each value with its source: `detected`, `rule` or `env`.
- The profile never changes a running server; `tune_postgres.sh apply` does, when the operator runs it.
- Run `ruff format` and `ruff check`; `git add` explicit paths only; tests are `not pg`.

## Review Focus

1. A very small machine (2 cores, 4 GB): the "rest" share can go to 0 or below. Expected: the pipeline share has a floor of 1 GB, the DuckDB cap a floor of 512 MB, workers at least 1, and a warning is logged. Test in Task 2.
2. `psutil.cpu_count(logical=False)` returns `None` (some VMs). Expected: fall back to `os.cpu_count()`, then 1. Test in Task 1.
3. Disk-kind detection for a path that does not exist yet, a UNC or network path, or a failing PowerShell call. Expected: `"unknown"` (HDD rules), never an exception, and a bounded 10 s timeout. Test in Task 1.
4. A memory override with units (`2GB`, `512MB`, `1.5GB`) or bare bytes, and a bad one (`lots`). Expected: parsed to bytes; a bad one warns and uses the rule. Test in Task 2.
5. `--format=pg` values must be strings `ALTER SYSTEM` accepts (`2457MB`, not `2457.6MB`). Expected: whole megabytes, rounded down. Test in Task 3.

---

## File Structure

- Create `src/episteme/ops/__init__.py` (empty package marker with a docstring).
- Create `src/episteme/ops/hardware.py`: `Hardware` dataclass and `detect(paths)`.
- Create `src/episteme/ops/profile.py`: `Overrides`, `Profile`, `compute_profile`, `get_profile`, `parse_bytes`, CLI `show`.
- Modify `src/episteme/config.py`: override fields (None when unset).
- Modify `src/episteme/data/duckdb_conn.py`, `src/episteme/data/staging_writer.py`, `src/episteme/data/postgres_loader.py`, `src/episteme/data/graph_load.py`, `src/episteme/data/pmc/download_pmc.py`, and every extractor or serializer that reads `settings.extract_workers` (`apollo`, `bookshelf`, `europepmc` manuscripts and preprints, `guidelines`, `openalex`, `pmc`, `pubmed`).
- Modify `scripts/data/db/tune_postgres.sh`.
- Modify `pyproject.toml`, `uv.lock` (psutil).
- Tests: `tests/ops/test_hardware.py`, `tests/ops/test_profile.py`, `tests/test_config.py`, `tests/test_tune_postgres.py`, `tests/data/test_duckdb_conn.py`.
- Docs: `docs/10-data-sources-runbook.md` (env table, a "Runtime profile" subsection), `docs/project-incubation-baseline.md`.

---

### Task 1: Hardware detection

**Files:**
- Create: `src/episteme/ops/__init__.py`, `src/episteme/ops/hardware.py`
- Modify: `pyproject.toml` (add `"psutil>=7.0.0"` to `dependencies`), `uv.lock` (`uv lock`)
- Test: `tests/ops/__init__.py` (empty), `tests/ops/test_hardware.py`

**Interfaces:**
- Produces: `Hardware(physical_cores: int, ram_bytes: int, disk_kinds: dict[str, str])` (frozen dataclass; `disk_kinds` maps a role name such as `"raw"`, `"processed"`, `"duckdb_temp"` to `"hdd"`, `"ssd"` or `"unknown"`); `detect(paths: dict[str, Path]) -> Hardware`; `physical_cores() -> int`; `ram_bytes() -> int`; `disk_kind(path: Path) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/test_hardware.py
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


def test_detect_builds_hardware(monkeypatch, tmp_path):
    monkeypatch.setattr(hardware, "physical_cores", lambda: 4)
    monkeypatch.setattr(hardware, "ram_bytes", lambda: 16 * 2**30)
    monkeypatch.setattr(hardware, "disk_kind", lambda p: "hdd")
    hw = hardware.detect({"raw": tmp_path, "processed": tmp_path})
    assert hw == hardware.Hardware(4, 16 * 2**30, {"raw": "hdd", "processed": "hdd"})
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python -m pytest -q tests/ops/test_hardware.py`
Expected: FAIL (`ModuleNotFoundError: episteme.ops`).

- [ ] **Step 3: Implement**

```python
# src/episteme/ops/__init__.py
"""Operations support shared by every pipeline stage: the runtime profile (and,
in later PRs, locks, retries, run status and the sample gate). See
docs/superpowers/specs/2026-10-19-pipeline-operating-standard-design.md."""
```

```python
# src/episteme/ops/hardware.py
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
```

In `pyproject.toml`, add `"psutil>=7.0.0",` to `dependencies`, then run `uv lock`.

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/Scripts/python -m pytest -q tests/ops/test_hardware.py`
Expected: all pass.

- [ ] **Step 5: Check on this machine**

Run: `.venv/Scripts/python -c "from pathlib import Path; from episteme.ops.hardware import detect; print(detect({'raw': Path('D:/episteme/01_raw'), 'tmp': Path('C:/episteme_tmp')}))"`
Expected: 4 cores, about 16 GB, `raw` `hdd`, `tmp` `ssd` (record the real output in the report).

- [ ] **Step 6: Commit**

```bash
git add src/episteme/ops/__init__.py src/episteme/ops/hardware.py tests/ops/__init__.py tests/ops/test_hardware.py pyproject.toml uv.lock
git commit -m "feat(ops): hardware detection (cores, RAM, disk kind)"
```

---

### Task 2: Profile rules

**Files:**
- Create: `src/episteme/ops/profile.py` (the pure part)
- Test: `tests/ops/test_profile.py`

**Interfaces:**
- Consumes: `Hardware` from Task 1.
- Produces: `parse_bytes(text: str) -> int | None`; `Overrides` (frozen dataclass, every field `None` by default: `cpu_workers: int | None`, `io_concurrency: int | None`, `download_threads: int | None`, `pmc_threads: int | None`, `duckdb_memory_bytes: int | None`, `batch_rows: int | None`, `part_rows: int | None`, `part_bytes: int | None`, `pg_shared_buffers_bytes: int | None`, `pg_maintenance_work_mem_bytes: int | None`, `pg_work_mem_bytes: int | None`); `Profile` (frozen dataclass with those values resolved, plus `os_reserve_bytes`, `pipeline_bytes`, `pg_max_parallel_workers_per_gather`, `pg_effective_io_concurrency`, `hardware: Hardware`, `sources: dict[str, str]`); `compute_profile(hw: Hardware, ov: Overrides, *, data_role: str = "processed") -> Profile`; `Profile.duckdb_memory_limit -> str` (e.g. `"4096MB"`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/test_profile.py
import logging

import pytest

from episteme.ops.hardware import Hardware
from episteme.ops.profile import Overrides, compute_profile, parse_bytes

GB = 2**30
LAPTOP = Hardware(4, 16 * GB, {"processed": "hdd", "raw": "hdd"})
BIG = Hardware(16, 64 * GB, {"processed": "ssd", "raw": "ssd"})


def test_laptop_rules():
    p = compute_profile(LAPTOP, Overrides())
    assert p.os_reserve_bytes == 4 * GB
    assert p.pg_shared_buffers_bytes == int(16 * GB * 0.15)
    assert p.pg_maintenance_work_mem_bytes == int(16 * GB * 0.06)
    assert p.pg_work_mem_bytes == 32 * 2**20
    assert (
        p.pipeline_bytes
        == 16 * GB
        - p.os_reserve_bytes
        - p.pg_shared_buffers_bytes
        - p.pg_maintenance_work_mem_bytes
        - p.pg_work_mem_bytes
    )
    assert p.duckdb_memory_bytes == p.pipeline_bytes // 2
    assert p.cpu_workers == 3
    assert p.io_concurrency == 2
    assert (p.download_threads, p.pmc_threads) == (16, 48)
    assert p.pg_max_parallel_workers_per_gather == 2
    assert p.pg_effective_io_concurrency == 2
    assert p.batch_rows == 50_000
    assert (p.part_rows, p.part_bytes) == (1_000_000, 512 * 2**20)
    assert all(v == "rule" for k, v in p.sources.items() if k != "hardware")


def test_big_ssd_rules():
    p = compute_profile(BIG, Overrides())
    assert p.os_reserve_bytes == 16 * GB
    assert p.pg_shared_buffers_bytes == int(64 * GB * 0.15)
    assert p.pg_maintenance_work_mem_bytes == 2 * GB
    assert p.cpu_workers == 15
    assert p.io_concurrency == 64
    assert p.pg_max_parallel_workers_per_gather == 8
    assert p.pg_effective_io_concurrency == 200
    assert p.batch_rows == 100_000
    assert (p.part_rows, p.part_bytes) == (2_000_000, GB)


def test_shared_buffers_cap_at_16gb():
    p = compute_profile(Hardware(32, 256 * GB, {"processed": "ssd"}), Overrides())
    assert p.pg_shared_buffers_bytes == 16 * GB


def test_workers_capped_by_memory():
    # 16 cores but only 8 GB: pipeline share is small, so workers <= share / 1.5 GB
    p = compute_profile(Hardware(16, 8 * GB, {"processed": "ssd"}), Overrides())
    assert p.cpu_workers == max(1, int(p.pipeline_bytes // int(1.5 * GB)))


def test_tiny_machine_floors_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="episteme.ops.profile"):
        p = compute_profile(Hardware(2, 4 * GB, {"processed": "unknown"}), Overrides())
    assert p.pipeline_bytes >= GB
    assert p.duckdb_memory_bytes >= 512 * 2**20
    assert p.cpu_workers >= 1
    assert p.io_concurrency == 2  # unknown disk -> HDD rules
    assert "pipeline share" in caplog.text


def test_overrides_win_and_are_marked_env():
    ov = Overrides(
        cpu_workers=7, duckdb_memory_bytes=2 * GB, batch_rows=10_000, pg_work_mem_bytes=64 * 2**20
    )
    p = compute_profile(LAPTOP, ov)
    assert (p.cpu_workers, p.duckdb_memory_bytes, p.batch_rows) == (7, 2 * GB, 10_000)
    assert p.pg_work_mem_bytes == 64 * 2**20
    assert p.sources["cpu_workers"] == "env" and p.sources["io_concurrency"] == "rule"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2GB", 2 * GB),
        ("512MB", 512 * 2**20),
        ("1.5GB", int(1.5 * GB)),
        ("1048576", 2**20),
        ("64kb", 64 * 1024),
        (" 3 GiB ", 3 * GB),
    ],
)
def test_parse_bytes(text, expected):
    assert parse_bytes(text) == expected


@pytest.mark.parametrize("text", ["lots", "", "-1GB", "GB"])
def test_parse_bytes_rejects(text):
    assert parse_bytes(text) is None


def test_duckdb_memory_limit_string():
    p = compute_profile(LAPTOP, Overrides(duckdb_memory_bytes=4 * GB))
    assert p.duckdb_memory_limit == "4096MB"
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python -m pytest -q tests/ops/test_profile.py`
Expected: FAIL (`ModuleNotFoundError: episteme.ops.profile`).

- [ ] **Step 3: Implement the pure part of `profile.py`**

```python
# src/episteme/ops/profile.py
"""Runtime profile: every value that depends on the machine, computed from the
hardware by the rules of the operating standard (spec section 5), with an env
override for each (read by episteme.config). See
docs/superpowers/specs/2026-10-19-pipeline-operating-standard-design.md."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, fields

from episteme.ops.hardware import Hardware

_LOG = logging.getLogger(__name__)

KB, MB, GB = 2**10, 2**20, 2**30
_UNITS = {
    "": 1,
    "b": 1,
    "kb": KB,
    "kib": KB,
    "mb": MB,
    "mib": MB,
    "gb": GB,
    "gib": GB,
    "tb": 2**40,
    "tib": 2**40,
}
_BYTES_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")

PIPELINE_FLOOR = GB
DUCKDB_FLOOR = 512 * MB
WORKER_BYTES = int(1.5 * GB)


def parse_bytes(text: str) -> int | None:
    m = _BYTES_RE.match(text or "")
    if not m:
        return None
    unit = _UNITS.get(m.group(2).lower())
    if unit is None:
        return None
    return int(float(m.group(1)) * unit)


@dataclass(frozen=True)
class Overrides:
    cpu_workers: int | None = None
    io_concurrency: int | None = None
    download_threads: int | None = None
    pmc_threads: int | None = None
    duckdb_memory_bytes: int | None = None
    batch_rows: int | None = None
    part_rows: int | None = None
    part_bytes: int | None = None
    pg_shared_buffers_bytes: int | None = None
    pg_maintenance_work_mem_bytes: int | None = None
    pg_work_mem_bytes: int | None = None


@dataclass(frozen=True)
class Profile:
    hardware: Hardware
    os_reserve_bytes: int
    pipeline_bytes: int
    cpu_workers: int
    io_concurrency: int
    download_threads: int
    pmc_threads: int
    duckdb_memory_bytes: int
    batch_rows: int
    part_rows: int
    part_bytes: int
    pg_shared_buffers_bytes: int
    pg_maintenance_work_mem_bytes: int
    pg_work_mem_bytes: int
    pg_max_parallel_workers_per_gather: int
    pg_effective_io_concurrency: int
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def duckdb_memory_limit(self) -> str:
        return f"{self.duckdb_memory_bytes // MB}MB"


def compute_profile(hw: Hardware, ov: Overrides, *, data_role: str = "processed") -> Profile:
    sources: dict[str, str] = {"hardware": "detected"}

    def pick(name: str, rule_value: int) -> int:
        value = getattr(ov, name)
        if value is not None:
            sources[name] = "env"
            return value
        sources[name] = "rule"
        return rule_value

    ram = hw.ram_bytes
    ssd = hw.disk_kinds.get(data_role, "unknown") == "ssd"
    os_reserve = max(3 * GB, ram // 4)
    shared = pick("pg_shared_buffers_bytes", min(16 * GB, int(ram * 0.15)))
    maint = pick("pg_maintenance_work_mem_bytes", min(2 * GB, int(ram * 0.06)))
    work = pick("pg_work_mem_bytes", 32 * MB)
    pipeline = ram - os_reserve - shared - maint - work
    if pipeline < PIPELINE_FLOOR:
        _LOG.warning(
            "pipeline share %d MB is below the %d MB floor on this machine; using the floor",
            pipeline // MB,
            PIPELINE_FLOOR // MB,
        )
        pipeline = PIPELINE_FLOOR
    duckdb = pick("duckdb_memory_bytes", max(DUCKDB_FLOOR, pipeline // 2))
    workers = pick("cpu_workers", max(1, min(hw.physical_cores - 1, pipeline // WORKER_BYTES)))
    big = ssd and ram >= 32 * GB
    return Profile(
        hardware=hw,
        os_reserve_bytes=os_reserve,
        pipeline_bytes=pipeline,
        cpu_workers=workers,
        io_concurrency=pick("io_concurrency", 4 * hw.physical_cores if ssd else 2),
        download_threads=pick("download_threads", 16),
        pmc_threads=pick("pmc_threads", 48),
        duckdb_memory_bytes=duckdb,
        batch_rows=pick("batch_rows", 100_000 if ram >= 48 * GB else 50_000),
        part_rows=pick("part_rows", 2_000_000 if big else 1_000_000),
        part_bytes=pick("part_bytes", GB if big else 512 * MB),
        pg_shared_buffers_bytes=shared,
        pg_maintenance_work_mem_bytes=maint,
        pg_work_mem_bytes=work,
        pg_max_parallel_workers_per_gather=max(1, hw.physical_cores // 2),
        pg_effective_io_concurrency=200 if ssd else 2,
        sources=sources,
    )


PROFILE_FIELDS = tuple(f.name for f in fields(Profile) if f.name not in ("hardware", "sources"))
```

Note: `test_laptop_rules` asserts every non-hardware source is `"rule"`; `pg_max_parallel_workers_per_gather` and `pg_effective_io_concurrency` are not in `sources` (no override), so the test only iterates the keys present.

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/Scripts/python -m pytest -q tests/ops/test_profile.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/episteme/ops/profile.py tests/ops/test_profile.py
git commit -m "feat(ops): profile rules (memory budget, parallelism, sizes) with overrides"
```

---

### Task 3: Overrides from config, `get_profile()` and `profile show`

**Files:**
- Modify: `src/episteme/config.py`
- Modify: `src/episteme/ops/profile.py` (add `overrides_from_settings`, `get_profile`, `render`, `main`)
- Test: `tests/test_config.py`, `tests/ops/test_profile.py`

**Interfaces:**
- Consumes: `compute_profile`, `Overrides`, `parse_bytes`, `detect`.
- Produces: `Settings` fields (all `None` when unset): `extract_workers: int | None`, `io_concurrency: int | None`, `download_threads: int | None`, `pmc_threads: int | None`, `duckdb_memory_limit: str | None`, `batch_rows: int | None`, `part_rows: int | None`, `part_bytes: str | None`, `pg_shared_buffers: str | None`, `pg_maintenance_work_mem: str | None`, `pg_work_mem: str | None`; `overrides_from_settings(s) -> Overrides`; `get_profile() -> Profile` (lru-cached); `render(p, fmt: str) -> str` for `text`, `json`, `pg`; `python -m episteme.ops.profile show [--format ...]`.

- [ ] **Step 1: Write the failing tests**

Change the existing config tests (`tests/test_config.py`): the defaults are now `None`, not rule values, because the rule lives in the profile.

```python
def test_duckdb_memory_limit_default(fresh_config):
    assert fresh_config("").get_settings().duckdb_memory_limit is None


def test_extract_workers_default(fresh_config):
    assert fresh_config("").get_settings().extract_workers is None


@pytest.mark.parametrize("bad", ["abc", "0", "-2", "2.5"])
def test_extract_workers_invalid_falls_back_with_warning(fresh_config, caplog, bad):
    cfg = fresh_config(f"EPISTEME_EXTRACT_WORKERS={bad}\n")
    with caplog.at_level(logging.WARNING, logger="episteme.config"):
        assert cfg.get_settings().extract_workers is None
    assert "EPISTEME_EXTRACT_WORKERS" in caplog.text


def test_new_override_keys_are_read(fresh_config):
    s = fresh_config(
        "EPISTEME_IO_CONCURRENCY=8\nEPISTEME_PMC_THREADS=24\nEPISTEME_BATCH_ROWS=20000\n"
        "EPISTEME_PART_ROWS=500000\nEPISTEME_PART_BYTES=256MB\nEPISTEME_PG_SHARED_BUFFERS=1GB\n"
        "EPISTEME_PG_MAINTENANCE_WORK_MEM=512MB\nEPISTEME_PG_WORK_MEM=16MB\nEPISTEME_DOWNLOAD_THREADS=8\n"
    ).get_settings()
    assert (s.io_concurrency, s.pmc_threads, s.batch_rows, s.part_rows, s.download_threads) == (
        8,
        24,
        20000,
        500000,
        8,
    )
    assert (s.part_bytes, s.pg_shared_buffers, s.pg_maintenance_work_mem, s.pg_work_mem) == (
        "256MB",
        "1GB",
        "512MB",
        "16MB",
    )
```

Keep `test_duckdb_memory_limit_override` and `test_extract_workers_override` as they are. Add to `tests/ops/test_profile.py`:

```python
import json

from episteme.ops import profile as prof


class _S:  # a stand-in for Settings with only the override fields
    extract_workers = None
    io_concurrency = None
    download_threads = None
    pmc_threads = None
    duckdb_memory_limit = None
    batch_rows = None
    part_rows = None
    part_bytes = None
    pg_shared_buffers = None
    pg_maintenance_work_mem = None
    pg_work_mem = None


def test_overrides_from_settings_parses_byte_values(caplog):
    s = _S()
    s.duckdb_memory_limit, s.part_bytes, s.pg_work_mem, s.extract_workers = "2GB", "lots", "16MB", 5
    with caplog.at_level(logging.WARNING, logger="episteme.ops.profile"):
        ov = prof.overrides_from_settings(s)
    assert (ov.duckdb_memory_bytes, ov.pg_work_mem_bytes, ov.cpu_workers) == (2 * GB, 16 * 2**20, 5)
    assert ov.part_bytes is None and "EPISTEME_PART_BYTES" in caplog.text


def test_render_pg_whole_megabytes():
    p = compute_profile(LAPTOP, Overrides())
    lines = dict(line.split("|") for line in prof.render(p, "pg").splitlines())
    assert lines["shared_buffers"] == f"{p.pg_shared_buffers_bytes // 2**20}MB"
    assert lines["maintenance_work_mem"].endswith("MB") and "." not in lines["maintenance_work_mem"]
    assert lines["work_mem"] == "32MB"
    assert lines["effective_io_concurrency"] == "2"
    assert lines["max_parallel_workers_per_gather"] == "2"


def test_render_json_and_text_name_sources():
    p = compute_profile(LAPTOP, Overrides(cpu_workers=7))
    data = json.loads(prof.render(p, "json"))
    assert data["values"]["cpu_workers"] == 7 and data["sources"]["cpu_workers"] == "env"
    text = prof.render(p, "text")
    assert "cpu_workers" in text and "env" in text and "rule" in text
```

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_config.py tests/ops/test_profile.py`
Expected: FAIL (defaults are still 3 / "4GB"; `overrides_from_settings` and `render` do not exist).

- [ ] **Step 3: Implement**

In `src/episteme/config.py`:

- Delete `DEFAULT_EXTRACT_WORKERS = 3` (the rule is in the profile) and update every import of it (`grep -rn DEFAULT_EXTRACT_WORKERS src tests`).
- Add `_get_positive_int_or_none(name: str) -> int | None`: like `_get_positive_int_or_default`, but it returns `None` when unset, and logs the same warning and returns `None` when invalid.
- In `Settings`, replace `extract_workers: int = DEFAULT_EXTRACT_WORKERS` with `extract_workers: int | None = None`, `download_threads: int` with `download_threads: int | None = None` (move it below the required fields), and `duckdb_memory_limit: str = "4GB"` with `duckdb_memory_limit: str | None = None`; add `io_concurrency: int | None = None`, `pmc_threads: int | None = None`, `batch_rows: int | None = None`, `part_rows: int | None = None`, `part_bytes: str | None = None`, `pg_shared_buffers: str | None = None`, `pg_maintenance_work_mem: str | None = None`, `pg_work_mem: str | None = None`.
- In `get_settings()`: `extract_workers=_get_positive_int_or_none("EPISTEME_EXTRACT_WORKERS")`, `download_threads=_get_positive_int_or_none("EPISTEME_DOWNLOAD_THREADS")`, `io_concurrency=_get_positive_int_or_none("EPISTEME_IO_CONCURRENCY")`, `pmc_threads=_get_positive_int_or_none("EPISTEME_PMC_THREADS")`, `batch_rows=_get_positive_int_or_none("EPISTEME_BATCH_ROWS")`, `part_rows=_get_positive_int_or_none("EPISTEME_PART_ROWS")`, and for the byte keys `(_get("EPISTEME_DUCKDB_MEMORY_LIMIT") or "").strip() or None` (same for `EPISTEME_PART_BYTES`, `EPISTEME_PG_SHARED_BUFFERS`, `EPISTEME_PG_MAINTENANCE_WORK_MEM`, `EPISTEME_PG_WORK_MEM`).

In `src/episteme/ops/profile.py` add:

```python
import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

_BYTE_KEYS = {
    "duckdb_memory_limit": ("duckdb_memory_bytes", "EPISTEME_DUCKDB_MEMORY_LIMIT"),
    "part_bytes": ("part_bytes", "EPISTEME_PART_BYTES"),
    "pg_shared_buffers": ("pg_shared_buffers_bytes", "EPISTEME_PG_SHARED_BUFFERS"),
    "pg_maintenance_work_mem": (
        "pg_maintenance_work_mem_bytes",
        "EPISTEME_PG_MAINTENANCE_WORK_MEM",
    ),
    "pg_work_mem": ("pg_work_mem_bytes", "EPISTEME_PG_WORK_MEM"),
}
_INT_KEYS = {
    "extract_workers": "cpu_workers",
    "io_concurrency": "io_concurrency",
    "download_threads": "download_threads",
    "pmc_threads": "pmc_threads",
    "batch_rows": "batch_rows",
    "part_rows": "part_rows",
}


def overrides_from_settings(s) -> Overrides:
    kw: dict[str, int | None] = {}
    for attr, name in _INT_KEYS.items():
        kw[name] = getattr(s, attr)
    for attr, (name, env_key) in _BYTE_KEYS.items():
        raw = getattr(s, attr)
        if raw is None:
            kw[name] = None
            continue
        value = parse_bytes(raw)
        if value is None or value <= 0:
            _LOG.warning(
                "%s=%r is not a size like 2GB or 512MB; using the rule's value", env_key, raw
            )
        kw[name] = value if value and value > 0 else None
    return Overrides(**kw)


def _paths(s) -> dict[str, Path]:
    paths = {"raw": s.raw_root, "processed": s.processed_root}
    if s.duckdb_temp_dir is not None:
        paths["duckdb_temp"] = s.duckdb_temp_dir
    return paths


@lru_cache(maxsize=1)
def get_profile() -> Profile:
    from episteme.config import get_settings
    from episteme.ops.hardware import detect

    s = get_settings()
    return compute_profile(detect(_paths(s)), overrides_from_settings(s))


def _mb(b: int) -> str:
    return f"{b // MB}MB"


def render(p: Profile, fmt: str) -> str:
    if fmt == "pg":
        rows = {
            "shared_buffers": _mb(p.pg_shared_buffers_bytes),
            "maintenance_work_mem": _mb(p.pg_maintenance_work_mem_bytes),
            "work_mem": _mb(p.pg_work_mem_bytes),
            "effective_io_concurrency": str(p.pg_effective_io_concurrency),
            "max_parallel_workers_per_gather": str(p.pg_max_parallel_workers_per_gather),
        }
        return "\n".join(f"{k}|{v}" for k, v in rows.items())
    values = {name: getattr(p, name) for name in PROFILE_FIELDS}
    hw = {
        "physical_cores": p.hardware.physical_cores,
        "ram_bytes": p.hardware.ram_bytes,
        "disk_kinds": p.hardware.disk_kinds,
    }
    if fmt == "json":
        return json.dumps(
            {"hardware": hw, "values": values, "sources": p.sources}, indent=2, sort_keys=True
        )
    lines = [
        f"hardware: {hw['physical_cores']} cores, {hw['ram_bytes'] // GB} GB RAM, disks {hw['disk_kinds']}"
    ]
    for name in PROFILE_FIELDS:
        lines.append(f"{name:36} {values[name]!s:>16}  {p.sources.get(name, 'rule')}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m episteme.ops.profile")
    sub = ap.add_subparsers(dest="cmd", required=True)
    show = sub.add_parser("show", help="print the runtime profile")
    show.add_argument("--format", choices=("text", "json", "pg"), default="text")
    args = ap.parse_args(argv)
    print(render(get_profile(), args.format))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to see them pass**

Run: `.venv/Scripts/python -m pytest -q tests/test_config.py tests/ops`
Expected: all pass.

- [ ] **Step 5: Check on this machine**

Run: `.venv/Scripts/python -m episteme.ops.profile show` and `... show --format=pg`
Expected: 4 cores, 16 GB, disks as detected; `cpu_workers 3`, `duckdb_memory_bytes` about 4 GB; `shared_buffers|2457MB`. Record the real output in the report.

- [ ] **Step 6: Commit**

```bash
git add src/episteme/config.py src/episteme/ops/profile.py tests/test_config.py tests/ops/test_profile.py
git commit -m "feat(ops): get_profile() with env overrides from config; profile show"
```

---

### Task 4: Wire the stages to the profile

**Files:**
- Modify: `src/episteme/data/duckdb_conn.py` (default memory limit from `get_profile().duckdb_memory_limit`, when `memory_limit` is not passed and `settings.duckdb_memory_limit` is None), `src/episteme/data/staging_writer.py`, `src/episteme/data/postgres_loader.py`, `src/episteme/data/graph_load.py` (batch rows), `src/episteme/data/pmc/download_pmc.py` (`--threads` default `get_profile().pmc_threads`), and each module that uses `settings.extract_workers` (`apollo/extract_apollo.py`, `bookshelf/extract_bookshelf.py`, `europepmc/manuscripts/extract_europepmc_manuscripts.py`, `europepmc/preprints/extract_europepmc_preprints.py`, `guidelines/extract_guidelines.py`, `openalex/serialize_openalex.py`, `pmc/extract_pmc.py`, `pubmed/extract_pubmed.py`).
- Test: `tests/data/test_duckdb_conn.py`, `tests/ops/test_wiring.py`

**Interfaces:**
- Consumes: `get_profile()`.
- Produces: unchanged public signatures. Module constants keep their names (`WRITE_BATCH_ROWS`, `LOAD_BATCH_ROWS`, `FETCH_BATCH_ROWS`, `_BATCH_ROWS`) so tests can still patch them; they are now set from `default_batch_rows()` at import.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ops/test_wiring.py
from episteme.ops import profile as prof
from episteme.ops.hardware import Hardware


def _fake_profile(monkeypatch, **ov):
    p = prof.compute_profile(Hardware(8, 32 * 2**30, {"processed": "ssd"}), prof.Overrides(**ov))
    prof.get_profile.cache_clear()
    monkeypatch.setattr(prof, "get_profile", lambda: p)
    return p


def test_default_batch_rows_uses_the_override(monkeypatch):
    class S:
        batch_rows = 12_345

    monkeypatch.setattr("episteme.config.get_settings", lambda: S())
    assert prof.default_batch_rows() == 12_345


def test_default_batch_rows_rule_by_ram(monkeypatch):
    class S:
        batch_rows = None

    monkeypatch.setattr("episteme.config.get_settings", lambda: S())
    monkeypatch.setattr("episteme.ops.hardware.ram_bytes", lambda: 64 * 2**30)
    assert prof.default_batch_rows() == 100_000
    monkeypatch.setattr("episteme.ops.hardware.ram_bytes", lambda: 16 * 2**30)
    assert prof.default_batch_rows() == 50_000


def test_extract_workers_come_from_profile(monkeypatch):
    p = _fake_profile(monkeypatch)
    from episteme.data import parallel

    assert parallel.default_workers() == p.cpu_workers


def test_pmc_threads_default_from_profile(monkeypatch):
    _fake_profile(monkeypatch, pmc_threads=33)
    from episteme.data.pmc import download_pmc

    assert download_pmc.build_parser().parse_args([]).threads == 33
```

In `tests/data/test_duckdb_conn.py`, change `test_connect_defaults_come_from_settings` so that, with `EPISTEME_DUCKDB_MEMORY_LIMIT` unset, the limit equals the profile's `duckdb_memory_limit` (patch `episteme.ops.profile.get_profile` as above); keep the 256MB-override case as a separate test.

- [ ] **Step 2: Run them to see them fail**

Run: `.venv/Scripts/python -m pytest -q tests/ops/test_wiring.py tests/data/test_duckdb_conn.py`
Expected: FAIL (`default_batch_rows` / `default_workers` do not exist).

- [ ] **Step 3: Implement**

- In `profile.py` add `default_batch_rows() -> int`: it returns `get_settings().batch_rows` when set, else `100_000` when `hardware.ram_bytes() >= 48 * GB`, else `50_000` (the batch-rows rule). It must not call `get_profile()`: the module constants call it at import, and `get_profile()` runs the disk-kind detection (a PowerShell call on Windows). The test `test_default_batch_rows_follows_profile` therefore patches `episteme.config.get_settings` (batch_rows=12_345) instead of the profile, and a second test patches `hardware.ram_bytes` to 64 GB and expects 100_000.
- In `src/episteme/data/parallel.py` add `def default_workers() -> int: return get_profile().cpu_workers` (the `EPISTEME_EXTRACT_WORKERS` override is already folded into the profile). It is called when a stage starts, never at import.
- In each module listed above, replace `settings.extract_workers if args.workers is None else max(1, args.workers)` with `default_workers() if args.workers is None else max(1, args.workers)`.
- `staging_writer.WRITE_BATCH_ROWS = default_batch_rows()`, `postgres_loader.LOAD_BATCH_ROWS = default_batch_rows()`, `duckdb_conn.FETCH_BATCH_ROWS = default_batch_rows()`, `graph_load._BATCH_ROWS = default_batch_rows()` (import inside each module from `episteme.ops.profile`).
- `duckdb_conn.connect`: `limit = memory_limit if memory_limit is not None else get_profile().duckdb_memory_limit` (the env override already lives in the profile).
- `download_pmc.build_parser`: `default=get_profile().pmc_threads` for `--threads`.

- [ ] **Step 4: Run the whole fast suite**

Run: `.venv/Scripts/python -m pytest -q -m "not pg"`
Expected: all pass (fix any test that asserted the old fixed values `3` or `"4GB"` by patching the profile instead).

- [ ] **Step 5: Commit**

```bash
git add src/episteme/ops/profile.py src/episteme/data/parallel.py src/episteme/data/duckdb_conn.py src/episteme/data/staging_writer.py src/episteme/data/postgres_loader.py src/episteme/data/graph_load.py src/episteme/data/pmc/download_pmc.py src/episteme/data/apollo/extract_apollo.py src/episteme/data/bookshelf/extract_bookshelf.py src/episteme/data/europepmc/manuscripts/extract_europepmc_manuscripts.py src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py src/episteme/data/guidelines/extract_guidelines.py src/episteme/data/openalex/serialize_openalex.py src/episteme/data/pmc/extract_pmc.py src/episteme/data/pubmed/extract_pubmed.py tests/ops/test_wiring.py tests/data/test_duckdb_conn.py
git commit -m "feat(ops): stages take workers, memory, threads and batch sizes from the profile"
```

---

### Task 5: `tune_postgres.sh` from the profile

**Files:**
- Modify: `scripts/data/db/tune_postgres.sh`
- Test: `tests/test_tune_postgres.py`

**Interfaces:**
- Consumes: `python -m episteme.ops.profile show --format=pg` (lines `name|value`).
- Produces: the same `check` and `apply` modes; the profile values replace the fixed `shared_buffers` and `maintenance_work_mem`, and add `work_mem`, `effective_io_concurrency`, `max_parallel_workers_per_gather`. `max_wal_size`, `min_wal_size`, `checkpoint_timeout`, `wal_compression` and `effective_cache_size` stay fixed.

- [ ] **Step 1: Write the failing test**

In `tests/test_tune_postgres.py`, add a fake `python` to the harness's fake bin that prints:

```
shared_buffers|2457MB
maintenance_work_mem|983MB
work_mem|32MB
effective_io_concurrency|2
max_parallel_workers_per_gather|2
```

set `PYTHON` to it in `_run`'s env, change the expected `shared_buffers` in the harness's `_rows` default from `2GB` to `2457MB`, and add:

```python
def test_settings_come_from_the_profile(tmp_path):
    proc, calls = _run(tmp_path, "check", rows=_rows({}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    joined = "\n".join(calls)
    for name in (
        "shared_buffers",
        "maintenance_work_mem",
        "work_mem",
        "effective_io_concurrency",
        "max_parallel_workers_per_gather",
    ):
        assert f"'{name}'" in joined


def test_a_failing_profile_stops_the_script(tmp_path):
    proc, _ = _run(tmp_path, "check", rows=_rows({}), python_exit=1)
    assert proc.returncode != 0
    assert "profile" in proc.stderr
```

(`_run` gains `python_exit: int = 0`; the fake python exits with it.)

- [ ] **Step 2: Run to see it fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_tune_postgres.py`
Expected: FAIL (the script still uses fixed values and never calls python).

- [ ] **Step 3: Implement**

In `tune_postgres.sh`, after `load_dotenv`: find `PY` with the same pattern as `run_pipeline.sh` (`${PYTHON:-}`, then the repo `.venv`), then

```bash
profile_rows="$("$PY" -m episteme.ops.profile show --format=pg)" || die "tune_postgres.sh: could not compute the runtime profile (python -m episteme.ops.profile show --format=pg)"
SETTINGS=(
    "max_wal_size|16GB"
    "min_wal_size|2GB"
    "checkpoint_timeout|30min"
    "wal_compression|lz4"
    "effective_cache_size|8GB"
)
while IFS= read -r row; do
    case "$row" in *"|"*) SETTINGS+=("$row") ;; esac
done <<< "$profile_rows"
```

Move the `SETTINGS=( ... )` array below the mode check and the `PY` lookup, and delete the old fixed `shared_buffers` / `maintenance_work_mem` entries. Update the header comment: the five memory and parallelism values come from the profile.

- [ ] **Step 4: Run to see it pass**

Run: `.venv/Scripts/python -m pytest -q tests/test_tune_postgres.py` and `shellcheck scripts/data/db/tune_postgres.sh`
Expected: all pass; shellcheck clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/data/db/tune_postgres.sh tests/test_tune_postgres.py
git commit -m "feat(ops): tune_postgres.sh takes its memory and parallelism values from the profile"
```

---

### Task 6: Docs

**Files:**
- Modify: `docs/10-data-sources-runbook.md` (env table rows for every key in Global Constraints; a new subsection "Runtime profile" under section 2 with `python -m episteme.ops.profile show`, the memory-budget and parallelism tables from spec 5.2 and 5.3, and the rule that an env key wins; the `tune_postgres.sh` description now says its memory and parallelism values come from the profile), `docs/project-incubation-baseline.md` (a drift-log entry: PR 1 of the operating standard, what changed, the defaults that changed meaning: `EPISTEME_EXTRACT_WORKERS` and `EPISTEME_DUCKDB_MEMORY_LIMIT` unset now mean "the profile's rule").

- [ ] **Step 1: Write the docs**

Use the tables from the spec verbatim, plus the real `profile show` output from Task 3 Step 5 as the laptop example.

- [ ] **Step 2: Run the docs tests**

Run: `.venv/Scripts/python -m pytest -q -m "not pg" tests/docs`
Expected: all pass (a docs test that pins the old `4GB` text gets the new wording).

- [ ] **Step 3: Commit**

```bash
git add docs/10-data-sources-runbook.md docs/project-incubation-baseline.md
git commit -m "docs: runtime profile (operating standard PR 1)"
```
