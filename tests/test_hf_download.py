"""scripts/data/_lib/hf_download.sh `hf_fetch`: a failed Hugging Face download must
fail the wrapper.

2026-10-18: apollo and guidelines timed out, hf_fetch retried "as model", the CLI
said "Returning existing local_dir ... as remote repo cannot be accessed" with exit 0,
and both downloads reported rc=0 with empty folders. A fake `hf` on PATH replays a
scripted sequence of exit codes (and optionally that message) and logs its calls.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "scripts" / "data" / "_lib"

FAKE_HF = r"""#!/usr/bin/env bash
# exit codes from $FAKE_HF_CODES, one per call. "nr401" / "nrto" print the
# no-repo message (as huggingface_hub logs it) for a 401 or a timeout, and exit 0.
echo "$*" >> "$FAKE_HF_LOG"
n=$(wc -l < "$FAKE_HF_LOG")
code=$(echo $FAKE_HF_CODES | cut -d' ' -f"$n")
code=${code:-0}
msg="Returning existing local_dir x as remote repo cannot be accessed in snapshot_download"
case "$code" in
    nr401) echo "$msg (401 Client Error. Repository Not Found for url: u)." >&2; exit 0 ;;
    nrto) echo "$msg (Read timed out. (read timeout=10))." >&2; exit 0 ;;
esac
exit "$code"
"""


def _bash() -> str:
    for c in (r"C:\Program Files\Git\bin\bash.exe", "/usr/bin/bash", "/bin/bash"):
        if Path(c).exists():
            return c
    pytest.skip("no POSIX bash")


def _run(tmp_path: Path, codes: str, rtype: str = "dataset", prefix: str = ""):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "hf").write_bytes(FAKE_HF.encode("utf-8"))
    (fake / "hf").chmod(0o755)
    log = tmp_path / "hf.log"
    script = (
        'fb=$(cygpath -u "$FAKEBIN" 2>/dev/null || echo "$FAKEBIN"); '
        'export PATH="$fb:/usr/bin:/bin"; '
        f'{prefix}. "$LIB/hf_download.sh"; hf_fetch org/repo "$RTYPE" "$DEST"'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "HF_"))}
    env.update(
        {
            "LIB": LIB.as_posix(),
            "FAKEBIN": fake.as_posix(),
            "DEST": (tmp_path / "out").as_posix(),
            "RTYPE": rtype,
            "FAKE_HF_LOG": log.as_posix(),
            "FAKE_HF_CODES": codes,
            "EPISTEME_HF_RETRY_DELAY": "0",
        }
    )
    proc = subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return proc, calls


def test_success_on_first_call(tmp_path):
    proc, calls = _run(tmp_path, "0")
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        f"download org/repo --local-dir {(tmp_path / 'out').as_posix()} --repo-type dataset"
    ]


def test_a_failed_call_is_retried_with_the_same_repo_type(tmp_path):
    proc, calls = _run(tmp_path, "1 1 0")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 3
    assert all("--repo-type dataset" in c for c in calls)


def test_every_call_failing_fails_without_a_model_fallback(tmp_path):
    proc, calls = _run(tmp_path, "1 1 1 0")
    assert proc.returncode == 1
    assert len(calls) == 3
    assert all("--repo-type dataset" in c for c in calls)
    assert "retrying as model" not in proc.stderr


def test_no_access_with_exit_0_fails_at_once(tmp_path):
    """A 401 behind the no-repo message (gated repo, no token): exit 0 from the CLI,
    a failure here, and no retry."""
    proc, calls = _run(tmp_path, "nr401 0")
    assert proc.returncode == 1
    assert len(calls) == 1
    assert "HF_TOKEN" in proc.stderr


def test_a_timeout_behind_the_no_repo_message_is_retried(tmp_path):
    proc, calls = _run(tmp_path, "nrto 0")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 2


def test_the_no_repo_message_on_every_attempt_fails(tmp_path):
    proc, calls = _run(tmp_path, "nrto nrto nrto")
    assert proc.returncode == 1
    assert len(calls) == 3


def test_a_caller_with_set_e_still_gets_the_retries(tmp_path):
    proc, calls = _run(tmp_path, "1 0", prefix="set -e; ")
    assert proc.returncode == 0, proc.stderr
    assert len(calls) == 2


def test_hub_timeouts_default_to_60_seconds(tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "hf").write_bytes(
        b'#!/usr/bin/env bash\necho "$HF_HUB_DOWNLOAD_TIMEOUT $HF_HUB_ETAG_TIMEOUT" > "$OUT"\n'
    )
    (fake / "hf").chmod(0o755)
    out = tmp_path / "t.txt"
    script = (
        'fb=$(cygpath -u "$FAKEBIN" 2>/dev/null || echo "$FAKEBIN"); '
        'export PATH="$fb:/usr/bin:/bin"; '
        '. "$LIB/hf_download.sh"; hf_fetch org/repo dataset "$DEST"'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("EPISTEME_", "HF_"))}
    env.update(
        {
            "LIB": LIB.as_posix(),
            "FAKEBIN": fake.as_posix(),
            "DEST": (tmp_path / "out").as_posix(),
            "OUT": out.as_posix(),
        }
    )
    proc = subprocess.run(
        [_bash(), "-c", script], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8").split() == ["60", "60"]
