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
