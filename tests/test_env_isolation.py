"""Regression test for tests/conftest.py's env isolation (Task 8, SP6 Phase C.2).

Root cause: ``episteme.config.get_settings()`` is ``@lru_cache``-memoized and
calls ``dotenv.load_dotenv()`` on its first invocation per session, which
writes the project ``.env``'s real values directly into ``os.environ``
(correct, required behavior for production). Nothing restored ``os.environ``
afterward, so whichever test happened to be first (in whatever collection
order) to reach ``get_settings()`` — directly, or via an imported module such
as ``download_europepmc_preprints.download_preprints()`` — leaked real
``.env`` values into ``os.environ`` for the rest of the pytest session.

The fix in ``tests/conftest.py`` is two layers, both unconditional and
suite-wide (unlike ``tests/test_config.py``'s ``fresh_config``, which only
protects tests that opt into it):

1. ``_isolate_os_environ``, a general, autouse, function-scoped fixture that
   snapshots ``os.environ`` before every test and restores it after.
2. A ``pytest_runtest_setup`` / ``pytest_runtest_teardown`` hookwrapper pair,
   needed because pytest does not guarantee fixture (1) tears down after
   every other same-scope fixture — ``monkeypatch``'s own finalizer can run
   *after* it and re-introduce a value ``load_dotenv`` wrote directly into
   ``os.environ`` (observed for real in
   ``tests/test_config.py::test_dotenv_overrides_sources_env``). The hooks
   snapshot/restore outside the whole fixture lifecycle, so they hold
   regardless of fixture teardown order.

This test proves that combination works even when a test mutates
``os.environ`` *directly* (no ``monkeypatch``, which only restores what it
was used to set): test_a sets a probe var straight into ``os.environ``;
test_b (which runs after it, by file order) asserts the probe is gone. Keyed
on a probe key that nothing else in the suite uses, mirroring
``tests/test_config.py``'s ``TestFreshConfigNoLeak`` marker-value idiom.
"""

from __future__ import annotations

import os

import pytest

_PROBE_KEY = "EPISTEME_TEST_ENV_ISOLATION_PROBE"
_PROBE_VALUE = "should-not-survive-teardown"
_ran = False


def test_a_sets_probe_directly_without_monkeypatch():
    global _ran
    assert _PROBE_KEY not in os.environ, "probe leaked in from an earlier run"
    os.environ[_PROBE_KEY] = _PROBE_VALUE  # deliberate direct mutation, no monkeypatch
    _ran = True


def test_b_probe_is_gone_after_teardown():
    if not _ran:
        pytest.skip("needs test_a to have run first (file order)")
    assert _PROBE_KEY not in os.environ


class TestMonkeypatchAfterDirectWriteDoesNotReintroduce:
    """Pins the hookwrapper pair's actual justification (review finding on Task 8).

    test_a_sets_probe_directly_without_monkeypatch above proves the simpler
    case -- direct mutation, no monkeypatch involved -- which the autouse
    fixture alone already catches. It does NOT exercise the specific failure
    mode that made the fixture alone insufficient: a direct write to
    os.environ (mirroring what load_dotenv does) followed, in the SAME test,
    by monkeypatch.setenv on that same key. monkeypatch's own finalizer
    remembers and restores the pre-monkeypatch value (the direct write), and
    if that finalizer runs after _isolate_os_environ's restore, the direct
    write survives past this test's teardown -- exactly the shape of
    tests/test_config.py::test_dotenv_overrides_sources_env. Without the
    pytest_runtest_setup/teardown hookwrapper pair, test_b below would fail.
    """

    _MARK_KEY = "EPISTEME_TEST_ENV_ISOLATION_MONKEYPATCH_PROBE"
    _DIRECT_VALUE = "direct-write-should-not-survive"
    _ran = False

    def test_a_direct_write_then_monkeypatch_same_key(self, monkeypatch):
        assert self._MARK_KEY not in os.environ, "probe leaked in from an earlier run"
        os.environ[self._MARK_KEY] = self._DIRECT_VALUE  # mirrors load_dotenv's direct write
        monkeypatch.setenv(self._MARK_KEY, "overridden-by-monkeypatch")
        assert os.environ[self._MARK_KEY] == "overridden-by-monkeypatch"
        type(self)._ran = True
        # monkeypatch's finalizer runs at this test's teardown and will try to
        # restore _DIRECT_VALUE (the value it observed before setenv) -- the
        # hookwrapper pair must win over that restore.

    def test_b_neither_value_survives_teardown(self):
        if not self._ran:
            pytest.skip("needs test_a to have run first (file order)")
        assert self._MARK_KEY not in os.environ
