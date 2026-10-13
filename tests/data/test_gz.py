"""open_gz reads the same bytes and text through isal and through gzip."""

from __future__ import annotations

import gzip

import pytest

from episteme.data import gz

PAYLOAD = ("line one\nlíne two — ünïcode\n" * 5000).encode("utf-8")
BACKENDS = ["isal", "gzip"]


def _use(backend: str, monkeypatch) -> None:
    if backend == "isal":
        pytest.importorskip("isal")
        assert gz._igzip is not None
    else:
        monkeypatch.setattr(gz, "_igzip", None)


@pytest.fixture
def gz_file(tmp_path):
    p = tmp_path / "x.gz"
    p.write_bytes(gzip.compress(PAYLOAD))
    return p


@pytest.mark.parametrize("backend", BACKENDS)
def test_binary_read_identical(gz_file, monkeypatch, backend):
    _use(backend, monkeypatch)
    with gz.open_gz(gz_file, "rb") as f:
        assert f.read() == PAYLOAD


@pytest.mark.parametrize("backend", BACKENDS)
def test_text_read_identical(gz_file, monkeypatch, backend):
    _use(backend, monkeypatch)
    with gz.open_gz(gz_file, "rt", encoding="utf-8", errors="replace") as f:
        assert f.read() == PAYLOAD.decode("utf-8")


@pytest.mark.parametrize("backend", BACKENDS)
def test_truncated_file_raises_eof(tmp_path, monkeypatch, backend):
    _use(backend, monkeypatch)
    data = gzip.compress(PAYLOAD)
    p = tmp_path / "t.gz"
    p.write_bytes(data[: len(data) // 2])
    with pytest.raises(EOFError), gz.open_gz(p, "rb") as f:
        f.read()


@pytest.mark.parametrize("backend", BACKENDS)
def test_corrupt_pubmed_file_fails_that_file_only(tmp_path, monkeypatch, backend):
    _use(backend, monkeypatch)
    from episteme.data.pubmed.extract_pubmed import extract_pubmed

    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "pubmed26n9999.xml.gz").write_bytes(b"\x1f\x8b\x08\x00" + b"garbage" * 100)
    res = extract_pubmed(raw, tmp_path / "p", workers=1)
    assert res == {"inputs": 1, "ok": 0, "failed": 1, "rows": 0}
    assert list((tmp_path / "p" / "_ops" / "pubmed" / "failed").glob("*"))
