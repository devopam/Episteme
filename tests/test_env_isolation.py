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
