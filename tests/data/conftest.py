"""Data-test guards."""

from __future__ import annotations

import pytest

from episteme.data import pacing


@pytest.fixture(autouse=True)
def _isolated_pacing_dir(tmp_path, monkeypatch):
    """A test that does not pass ``pacing_dir`` still never writes under the real raw root."""
    monkeypatch.setattr(pacing, "default_pacing_dir", lambda: tmp_path / "_pacing_default")
