"""scripts/data/_lib/common.sh `http_fetch`: the curl fallback must retry transient failures.

Plain `curl --retry` does not retry a connection reset (exit 56); on 2026-10-08 one
reset ended a 3.7 h reactome download. `--retry-all-errors` makes curl retry it and,
with `-C -`, resume the partial file.
"""

from __future__ import annotations

import re
from pathlib import Path

COMMON = Path(__file__).resolve().parents[1] / "scripts" / "data" / "_lib" / "common.sh"


def test_curl_fallback_retries_all_errors_and_resumes():
    curl_lines = [
        line
        for line in COMMON.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*curl\s", line)
    ]
    assert curl_lines, "http_fetch curl fallback not found"
    for line in curl_lines:
        assert "--retry-all-errors" in line, line
        assert "-C -" in line, line
