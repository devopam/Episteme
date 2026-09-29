"""SP6/SP7 drift-log close-out: non-``pg`` tests must never append
``mirror_only`` lines to the real, developer-machine audit mirror
(``02_processed/_ops/_audit/``). ``tests/conftest.py``'s autouse
``_redirect_audit_mirror`` fixture patches ``episteme.audit_trail._mirror_dir``
so that, for tests without the ``pg`` marker, a ``processed_root`` that is NOT
already under pytest's own tmp tree gets redirected to a throwaway tmp
directory instead.

Every test here fakes ``episteme.audit_trail.get_settings`` directly so
behaviour is proven deterministically, independent of whatever the real
session-cached ``Settings.processed_root`` happens to be (usually the repo's
real ``./02_processed`` -- see ``config.get_settings``'s ``.env`` fallback).
None of these tests ever write under the real repo tree: the "configured
processed root" they simulate is always either a pytest ``tmp_path`` or a
throwaway ``tempfile.TemporaryDirectory()`` outside pytest's basetemp,
never the real one.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import episteme.audit_trail as audit_trail


def test_mirror_dir_redirects_when_processed_root_outside_basetemp(monkeypatch, tmp_path_factory):
    """A ``processed_root`` outside pytest's tmp tree (the real-.env case)
    must NOT be honored as-is -- the mirror must land under basetemp
    instead."""
    outside = tmp_path_factory.getbasetemp().parent / "outside_basetemp_fake_root"
    monkeypatch.setattr(
        audit_trail, "get_settings", lambda: SimpleNamespace(processed_root=outside)
    )

    result = audit_trail._mirror_dir()

    assert result != outside / "_ops" / "_audit"
    base = tmp_path_factory.getbasetemp().resolve()
    assert result.resolve() == base or base in result.resolve().parents


def test_mirror_dir_keeps_own_redirect_when_already_inside_basetemp(monkeypatch, tmp_path):
    """A test that already points EPISTEME_PROCESSED_ROOT (hence
    ``Settings.processed_root``) at its own ``tmp_path`` must keep getting
    exactly that location -- the blanket redirect must not steal it."""
    custom_root = tmp_path / "custom_processed"
    monkeypatch.setattr(
        audit_trail, "get_settings", lambda: SimpleNamespace(processed_root=custom_root)
    )

    result = audit_trail._mirror_dir()

    assert result == custom_root / "_ops" / "_audit"


def test_mirror_only_write_lands_under_tmp_not_configured_root(monkeypatch, tmp_path_factory):
    """Regression: an actual ``mirror_only`` write, with a "configured"
    processed_root outside pytest's tmp tree, must land under pytest's tmp
    tree -- never under the (simulated) configured root."""
    marker = f"regression-{uuid.uuid4()}"

    with tempfile.TemporaryDirectory() as fake_configured_root:
        fake_root = Path(fake_configured_root)
        monkeypatch.setattr(
            audit_trail, "get_settings", lambda: SimpleNamespace(processed_root=fake_root)
        )

        audit_trail.mirror_only(
            "extract_commit", object=marker, rows_affected=0, run_id="regression-test"
        )

        base = tmp_path_factory.getbasetemp()
        redirected_hits = list(base.rglob("audit-*.jsonl"))
        found_in_tmp = any(marker in p.read_text(encoding="utf-8") for p in redirected_hits)
        assert found_in_tmp, "expected the mirror_only write to land under pytest's tmp tree"

        # And NOT under the "configured" processed_root this test simulated.
        real_mirror_dir = fake_root / "_ops" / "_audit"
        if real_mirror_dir.exists():
            configured_hits = list(real_mirror_dir.glob("audit-*.jsonl"))
            assert not any(marker in p.read_text(encoding="utf-8") for p in configured_hits)
