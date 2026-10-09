"""scripts/data/_lib/common.sh `load_dotenv` must read `.env` the way python-dotenv does.

run_pipeline.sh loads `.env` through the shell `load_dotenv` and exports the values;
Python then reads them from os.environ, which outranks its own python-dotenv read. A
value the two parse differently therefore breaks every pipeline stage (seen
2026-10-08: `EPISTEME_DB_TARGET=primary   #(primary | secondary)` reached config.py
with the inline comment attached). The test runs a copy of `_lib/` in a temp
directory with its own pyproject.toml and .env, so the real repo `.env` is never read.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "data" / "_lib"

ENV_TEXT = (
    "# a full-line comment\n"
    "EPI_LDT_PLAIN=plain\n"
    "EPI_LDT_INLINE=primary   #(primary | secondary)\n"
    "EPI_LDT_TAB=value\t# tab before the hash\n"
    "EPI_LDT_NOSPACE=val#not-a-comment\n"
    'EPI_LDT_DQ="quoted # kept"\n'
    "EPI_LDT_SQ='single # kept'\n"
    "EPI_LDT_ONLYCOMMENT=   # nothing but a comment\n"
    "EPI_LDT_TRAILING=trailing   \n"
    "EPI_LDT_URL=https://example.org/a#frag\n"
    'EPI_LDT_DQ_LEADWS=  "lead ws"\n'
    "EPI_LDT_SQ_LEADWS=  'sq lead'\n"
    'EPI_LDT_DQ_COMMENT="x" # note\n'
    "EPI_LDT_SQ_COMMENT='y'   # note\n"
    'EPI_LDT_DQ_HASH_COMMENT="has # inside" # c\n'
    # backslash escapes, decoded as python-dotenv does
    'EPI_LDT_DQ_ESC_QUOTE="pa\\"ss" # c\n'
    'EPI_LDT_DQ_ESC_BS="a\\\\" # c\n'
    'EPI_LDT_DQ_ESC_CTRL="x\\ty\\nz"\n'
    'EPI_LDT_DQ_ESC_OTHER="keep\\qliteral"\n'
    "EPI_LDT_SQ_ESC='it\\'s \\\\ \\n'\n"
)
KEYS = [line.split("=", 1)[0] for line in ENV_TEXT.splitlines() if "=" in line]


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def test_shell_load_dotenv_matches_python_dotenv(tmp_path):
    lib = tmp_path / "scripts" / "data" / "_lib"
    shutil.copytree(LIB, lib)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_bytes(ENV_TEXT.encode("utf-8"))  # LF endings, as on disk

    script = (
        '. "$LIB/common.sh"; load_dotenv >/dev/null 2>&1; '
        'for k in $KEYS; do printf "%s=[%s]\\0" "$k" "${!k-<unset>}"; done'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("EPI_LDT_")}
    env.update({"LIB": lib.as_posix(), "KEYS": " ".join(KEYS)})
    proc = subprocess.run(
        [_bash(), "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    # NUL-separated: decoded values may hold newlines
    shell = dict(rec.split("=", 1) for rec in proc.stdout.split("\0") if rec)
    expected = dotenv_values(env_file)
    for key in KEYS:
        assert shell[key] == f"[{expected[key]}]", key
    # the case that broke the full load
    assert shell["EPI_LDT_INLINE"] == "[primary]"
