"""Regression test for tests/conftest.py's ``_block_real_db_audit`` guard
itself (Fix 4, whole-branch review): confirms the guard's patch of
``episteme.data.db.connection.get_pool`` closes the module-top-import bypass
route (e.g. ``load_articles.py``'s ``from ... import connection``), not just
the lazy-import seam most extractors use. No explicit patching here -- the
autouse, session-wide guard in ``tests/conftest.py`` is already active for
this (``not pg``) test.
"""

from __future__ import annotations

import pytest


def test_guard_blocks_module_top_import_caller():
    from episteme.data import load_articles

    with pytest.raises(RuntimeError, match="blocked by tests/conftest.py"):
        with load_articles.connection():
            pass
