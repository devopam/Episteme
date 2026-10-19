"""Cancel PostgreSQL backends whose client process no longer exists."""

from __future__ import annotations

import logging
from typing import Any

import psutil

from episteme import audit_trail
from episteme.config import get_settings

_LOG = logging.getLogger(__name__)

_SELECT = """
SELECT pid, application_name, query_start
FROM pg_stat_activity
WHERE application_name LIKE %s
  AND usename = current_user
  AND pid <> pg_backend_pid()
  AND state = 'active'
"""


def _client_pid(application_name: str) -> int | None:
    last = application_name.rsplit(":", 1)[-1]
    if application_name.count(":") < 2 or not last.isdigit():
        return None
    return int(last)


def sweep(conn: Any, *, name_like: str = "episteme:%") -> list[int]:
    """Cancel the running statement of every ``episteme:<run>:<pid>`` backend
    whose client PID is gone from this machine. Returns the cancelled PIDs.

    Only ``active`` backends are swept: the backend of a dead client that is
    idle or idle in transaction ends when the server reads the closed socket,
    and a cancel cannot end it anyway.

    Known limits: a PID reused by an unrelated local process makes an orphan
    look alive. ``psutil.pid_exists`` checks this machine only, so the sweep
    assumes the clients run on this host. Backends of other roles (for example
    the ``admin_dsn`` role) are not swept, because of ``usename = current_user``.

    Each cancel is audited and committed at once. If an audit write fails, the
    sweep rolls back, logs the PID, continues, and raises ``RuntimeError``
    after the last orphan.
    """
    with conn.cursor() as cur:
        cur.execute(_SELECT, (name_like,))
        rows = cur.fetchall()
    conn.rollback()  # end the read transaction
    cancelled: list[int] = []
    failed: list[int] = []
    for pid, app, started in rows:
        client = _client_pid(app)
        if client is None:
            _LOG.warning("backend %s has an unparsable application_name %r; left alone", pid, app)
            continue
        if psutil.pid_exists(client):
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT pg_cancel_backend(%s)", (pid,))
            row = cur.fetchone()
        if not (row and row[0]):
            conn.rollback()
            _LOG.info("orphan backend %s (%s) already ended; skipped", pid, app)
            continue
        try:
            audit_trail.record(
                "orphan_cancel",
                conn=conn,
                object=app,
                reason=f"client pid {client} gone; statement started {started}",
                run_id=get_settings().run_id,
            )
            audit_trail.commit(conn)
        except Exception:  # noqa: BLE001 - keep sweeping; raise after the loop
            conn.rollback()
            _LOG.error("orphan_cancel audit failed for backend pid %s", pid, exc_info=True)
            failed.append(pid)
            continue
        _LOG.warning("cancelled orphan backend %s (%s)", pid, app)
        cancelled.append(pid)
    if failed:
        raise RuntimeError(f"orphan_cancel audit failed for backend pids {failed}")
    return cancelled
