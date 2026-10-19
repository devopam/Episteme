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
  AND state <> 'idle'
"""


def _client_pid(application_name: str) -> int | None:
    last = application_name.rsplit(":", 1)[-1]
    if application_name.count(":") < 2 or not last.isdigit():
        return None
    return int(last)


def sweep(conn: Any, *, name_like: str = "episteme:%") -> list[int]:
    """Cancel the running statement of every ``episteme:<run>:<pid>`` backend
    whose client PID is gone from this machine. Returns the cancelled PIDs."""
    with conn.cursor() as cur:
        cur.execute(_SELECT, (name_like,))
        rows = cur.fetchall()
    cancelled: list[int] = []
    for pid, app, started in rows:
        client = _client_pid(app)
        if client is None:
            _LOG.warning("backend %s has an unparsable application_name %r; left alone", pid, app)
            continue
        if psutil.pid_exists(client):
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT pg_cancel_backend(%s)", (pid,))
        audit_trail.record(
            "orphan_cancel",
            conn=conn,
            object=app,
            reason=f"client pid {client} gone; statement started {started}",
            run_id=get_settings().run_id,
        )
        _LOG.warning("cancelled orphan backend %s (%s)", pid, app)
        cancelled.append(pid)
    if cancelled:
        audit_trail.commit(conn)
    else:
        conn.rollback()  # end the read transaction
    return cancelled
