"""SP6/SP7 drift-log close-out: enrich_openmetadata's audit-failure warning
used ``print(..., file=sys.stderr)`` instead of the module-logger convention
every other module in ``episteme.data`` follows. ``tests/conftest.py``'s
autouse ``_block_real_db_audit`` fixture already forces
``episteme.data.db.connection.connection`` to raise for every non-``pg``
test, which drives ``_best_effort_audit``'s ``except Exception`` here with no
extra patching needed.
"""

from __future__ import annotations

import logging

from episteme.data import article_schema
from episteme.data.enrich_openmetadata import main

SOURCE = article_schema.SOURCES[0]


def test_audit_failure_logs_warning_not_stderr_print(tmp_path, capsys, caplog):
    with caplog.at_level(logging.WARNING, logger="episteme.data.enrich_openmetadata"):
        rc = main(["--source", SOURCE, "--processed-dir", str(tmp_path)])

    assert rc == 0

    out_path = tmp_path / "_ops" / SOURCE / "catalog" / f"{SOURCE}.openmetadata.json"
    assert out_path.is_file()

    captured = capsys.readouterr()
    # Real CLI output (the manifest path) still goes to stdout.
    assert captured.out.strip() == str(out_path)
    # The audit-failure warning must NOT be a stderr print any more.
    assert "warning:" not in captured.err

    warnings = [
        r
        for r in caplog.records
        if r.name == "episteme.data.enrich_openmetadata" and r.levelno == logging.WARNING
    ]
    assert warnings, "expected a WARNING from the audit-failure fallback"
    assert warnings[0].exc_info is not None  # exc_info=True was passed
