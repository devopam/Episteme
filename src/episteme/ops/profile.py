"""Runtime profile: every value that depends on the machine, computed from the
hardware by the rules of the operating standard (spec section 5), with an env
override for each (read by episteme.config). See
docs/superpowers/specs/2026-10-19-pipeline-operating-standard-design.md."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path

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
# Part caps (part_rows / part_bytes, rule in compute_profile): a writer that
# cuts its output into parts estimates a row's staging size as the UTF-8 length
# of its ``text`` plus this fixed overhead for the other columns.
PART_ROW_OVERHEAD_BYTES = 256


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
        # DuckDB reads MB as 10^6, so use binary MiB; a value that is not a
        # whole number of MiB (an operator override) goes as exact bytes.
        if self.duckdb_memory_bytes % MB:
            return f"{self.duckdb_memory_bytes}B"
        return f"{self.duckdb_memory_bytes // MB}MiB"


def _batch_rows_rule(ram_bytes: int) -> int:
    return 100_000 if ram_bytes >= 48 * GB else 50_000


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
        batch_rows=pick("batch_rows", _batch_rows_rule(ram)),
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
        elif name.startswith("pg_") and value < MB:
            _LOG.warning("%s=%r must be at least 1MB; using the rule's value", env_key, raw)
            value = None
        kw[name] = value if value and value > 0 else None
    return Overrides(**kw)


def _paths(s) -> dict[str, Path]:
    paths = {"raw": s.raw_root, "processed": s.processed_root}
    if s.duckdb_temp_dir is not None:
        paths["duckdb_temp"] = s.duckdb_temp_dir
    return paths


def default_batch_rows() -> int:
    """Batch size for the module constants (rows per write, load, fetch).

    Uses RAM and the override only. It must not call ``get_profile()``: the
    constants call this at import, and ``get_profile()`` runs disk detection.

    It runs at import of four modules and reads the settings once: later env
    changes do not reach the constants. A bad env value raises ConfigError at
    import.
    """
    from episteme.config import get_settings
    from episteme.ops import hardware

    override = get_settings().batch_rows
    if override is not None:
        return override
    return _batch_rows_rule(hardware.ram_bytes())


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
        f"hardware: {hw['physical_cores']} cores, {hw['ram_bytes'] // GB} GB RAM, "
        f"disks {hw['disk_kinds']}  detected"
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
