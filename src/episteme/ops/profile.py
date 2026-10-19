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
