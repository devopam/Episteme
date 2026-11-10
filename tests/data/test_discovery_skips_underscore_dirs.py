"""Discovery ignores directories whose name starts with ``_`` (quarantine and ops folders).

2026-11-10: ``discover_outer_zips`` read the zips under ``01_raw/dailymed/_bad_<date>/``,
where the download scripts move damaged files.
"""

from __future__ import annotations

from pathlib import Path

from episteme.data.aact.load_aact import release_zips
from episteme.data.checkpoint_markers import (
    discover_input_files,
    in_skipped_dir,
    list_input_files,
)
from episteme.data.dailymed.extract_dailymed import discover_outer_zips
from episteme.data.regulatory.faers.generation import faers_zips
from episteme.data.regulatory.load_substance import substance_files


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_in_skipped_dir_tests_directories_not_the_file_name(tmp_path):
    assert in_skipped_dir(tmp_path / "_bad_1" / "a.zip", tmp_path)
    assert in_skipped_dir(tmp_path / "a" / ".cache" / "b.zip", tmp_path)
    assert not in_skipped_dir(tmp_path / "_a.zip", tmp_path)
    assert not in_skipped_dir(tmp_path / "a" / "b.zip", tmp_path)


def test_a_root_that_itself_starts_with_underscore_is_not_skipped(tmp_path):
    root = tmp_path / "_root"
    assert not in_skipped_dir(root / "a.zip", root)


def test_dailymed_discovery_skips_the_quarantine_folder(tmp_path):
    good = _touch(tmp_path / "dm_spl_release_human_rx_part1.zip")
    _touch(tmp_path / "_bad_20261110" / "dm_spl_release_human_rx_part2.zip.bad")
    _touch(tmp_path / "_bad_20261110" / "dm_spl_release_human_rx_part3.zip")
    _touch(tmp_path / "_bad_20261110" / "dm_spl_weekly_update_20261101.zip")
    assert discover_outer_zips(tmp_path, ("prescription", "otc")) == [good]


def test_discover_input_files_skips_underscore_directories(tmp_path):
    good = _touch(tmp_path / "sub" / "a.json")
    _touch(tmp_path / "_bad_x" / "b.json")
    assert discover_input_files(tmp_path, ["*.json"]) == [good]
    assert list_input_files(tmp_path, ["*.json"]) == [good]


def test_faers_substance_and_feed_listings_skip_underscore_directories(tmp_path):
    f = _touch(tmp_path / "drug" / "event" / "a.zip")
    _touch(tmp_path / "drug" / "event" / "_bad_1" / "b.zip")
    assert faers_zips(tmp_path) == [f]
    s = _touch(tmp_path / "other" / "substance" / "a.zip")
    _touch(tmp_path / "other" / "substance" / "_bad_1" / "b.zip")
    assert substance_files(tmp_path) == [s]


def test_aact_release_zips_skip_underscore_directories(tmp_path):
    good = _touch(tmp_path / "20261103_export.zip")
    _touch(tmp_path / "_bad_1" / "20261104_export.zip")
    assert release_zips(tmp_path) == [good]
