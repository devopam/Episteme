"""SP2 Task 7 — europepmc_preprint REST-harvest downloader (mocked network).

EBI discontinued the bulk Europe PMC preprint feed (2026-09-08 spike): the FTP
dir now holds only ``pprid.txt.gz`` (~73k ``PPR...`` ids) + a privacy notice.
``download_preprints`` fetches that id list, then GETs each preprint's
``{europepmc_base}/{id}/fullTextXML`` and writes ``raw_dir/{id}.xml``, with a
``raw_dir/.harvest_state`` resume list.

All tests monkeypatch ``requests.get`` (and ``time.sleep``) in the module
namespace — no real network, no ``responses`` dependency.
"""

from __future__ import annotations

import gzip
from collections import Counter

import episteme.data.europepmc.preprints.download_europepmc_preprints as dl
from episteme.data.europepmc.preprints.download_europepmc_preprints import download_preprints


class FakeResp:
    def __init__(self, status_code: int = 200, content: bytes = b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status on {self.status_code}")


def _fake_get(idlist_bytes: bytes, responses: dict[str, list[tuple[int, bytes]]] | None = None):
    """Build a fake ``requests.get``.

    ``responses`` maps a PPR id -> an ordered list of ``(status, body)`` returned
    on successive calls (the last entry repeats). Any id not in the map returns
    ``200 <article/>``.
    """
    responses = responses or {}
    calls = {"idlist": 0, "perid": Counter()}

    def _get(url, timeout=None, **kw):
        if url.endswith("pprid.txt.gz"):
            calls["idlist"] += 1
            return FakeResp(200, gzip.compress(idlist_bytes))
        ppr = url.rstrip("/").split("/")[-2]  # .../{id}/fullTextXML
        seq = responses.get(ppr, [(200, b"<article/>")])
        idx = min(calls["perid"][ppr], len(seq) - 1)
        calls["perid"][ppr] += 1
        status, body = seq[idx]
        return FakeResp(status, body)

    return _get, calls


def test_harvest_writes_per_id_xml(tmp_path, monkeypatch):
    get, _calls = _fake_get(b"PPR1\nPPR2\n")
    monkeypatch.setattr(dl.requests, "get", get)

    res = download_preprints(tmp_path, max_files=2)
    assert res["ids"] == 2
    assert res["fetched"] == 2
    assert res["errors"] == 0
    assert (tmp_path / "PPR1.xml").exists()
    assert (tmp_path / "PPR2.xml").exists()
    state = (tmp_path / ".harvest_state").read_text(encoding="utf-8").split()
    assert set(state) == {"PPR1", "PPR2"}

    # re-run: both already done -> skipped, nothing fetched
    get2, _ = _fake_get(b"PPR1\nPPR2\n")
    monkeypatch.setattr(dl.requests, "get", get2)
    res2 = download_preprints(tmp_path, max_files=2)
    assert res2["fetched"] == 0
    assert res2["skipped"] == 2


def test_harvest_404_counts_error_and_continues(tmp_path, monkeypatch):
    get, _calls = _fake_get(b"PPR1\nPPR2\n", {"PPR2": [(404, b"")]})
    monkeypatch.setattr(dl.requests, "get", get)

    res = download_preprints(tmp_path, max_files=5)
    assert res["fetched"] == 1
    assert res["errors"] == 1
    assert (tmp_path / "PPR1.xml").exists()
    assert not (tmp_path / "PPR2.xml").exists()
    # 404 id NOT written to .harvest_state -> a later run can retry it
    state = (tmp_path / ".harvest_state").read_text(encoding="utf-8").split()
    assert state == ["PPR1"]


def test_harvest_backoff_on_503(tmp_path, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(dl.time, "sleep", lambda s: slept.append(s))
    get, _calls = _fake_get(b"PPR1\n", {"PPR1": [(503, b""), (503, b""), (200, b"<article/>")]})
    monkeypatch.setattr(dl.requests, "get", get)

    res = download_preprints(tmp_path, max_files=1)
    assert res["fetched"] == 1
    assert res["errors"] == 0
    assert (tmp_path / "PPR1.xml").exists()
    assert len(slept) >= 2  # backed off before each retry


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    get, _calls = _fake_get(b"PPR1\nPPR2\nPPR3\n")
    monkeypatch.setattr(dl.requests, "get", get)

    res = download_preprints(tmp_path, max_files=5, dry_run=True)
    assert res["ids"] == 3
    assert res["fetched"] == 0
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path / ".harvest_state").exists()


def test_dry_run_offline_safe(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise OSError("EBI unreachable")

    monkeypatch.setattr(dl.requests, "get", _boom)
    res = download_preprints(tmp_path, max_files=5, dry_run=True)
    assert res == {"ids": 0, "fetched": 0, "skipped": 0, "errors": 0}
    assert list(tmp_path.iterdir()) == []


def test_since_is_a_documented_noop(tmp_path, monkeypatch, capsys):
    get, _calls = _fake_get(b"PPR1\n")
    monkeypatch.setattr(dl.requests, "get", get)
    download_preprints(tmp_path, max_files=1, since="2026-01-01")
    captured = capsys.readouterr()
    msg = (captured.out + captured.err).lower()
    assert "since" in msg and "no-op" in msg


def test_main_dry_run_rc0(tmp_path, monkeypatch):
    get, _calls = _fake_get(b"PPR1\nPPR2\n")
    monkeypatch.setattr(dl.requests, "get", get)
    rc = dl.main(["--raw-dir", str(tmp_path), "--max-files", "5", "--dry-run"])
    assert rc == 0
    assert list(tmp_path.iterdir()) == []


def test_main_returns_0_even_when_all_ids_error(tmp_path, monkeypatch):
    """A resumed harvest where every remaining id 404s must NOT fail the stage
    (run_stage would log 'stage failed' + a failure-path run_end audit row on a
    non-zero rc) — a per-id 404/429/503 is an expected, individually-logged
    outcome, never fatal. Regression test for a rc=1-on-fetched==0-errors>0 bug
    caught in review."""
    get, _calls = _fake_get(b"PPR1\nPPR2\n", {"PPR1": [(404, b"")], "PPR2": [(404, b"")]})
    monkeypatch.setattr(dl.requests, "get", get)
    rc = dl.main(["--raw-dir", str(tmp_path), "--max-files", "5"])
    assert rc == 0
