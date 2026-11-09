"""europepmc_preprint downloader: bulk files discovered from the folder listing.

A fake session stands in for the network; no test calls EBI.
"""

from __future__ import annotations

from episteme.data.europepmc.preprints import download_europepmc_preprints as dl

BASE = "https://ebi.example/pub/databases/pmc/preprints"
LISTING = """<html><body><pre>
<a href="?C=N;O=D">Name</a> <a href="/pub/databases/pmc/">Parent Directory</a>
<a href="PPR7001_PPR208218.xml.gz">PPR7001_PPR208218.xml.gz</a>  2026-10-31 149M
<a href="PPR1082914_PPR1327385.xml.gz">PPR1082914_PPR1327385.xml.gz</a>  2026-10-31 148M
<a href="PPR208220_PPR297873.xml.gz">PPR208220_PPR297873.xml.gz</a>  2026-10-31 171M
<a href="pprid.txt.gz">pprid.txt.gz</a>
<a href="privacy_notice.txt">privacy_notice.txt</a>
<a href="../x/PPR1_PPR2.xml.gz">evil</a>
<a href="PPR7001_PPR208218.xml.gz.md5">md5</a>
</pre></body></html>"""


class FakeResp:
    def __init__(self, status_code=200, content=b"", headers=None, text=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """GET of the base returns the listing; GET of a file returns its bytes (a
    Range request returns the tail); HEAD returns the length."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.gets: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        self.gets.append((url, kw.get("headers") or {}))
        if url.rstrip("/") == BASE:
            return FakeResp(text=LISTING)
        name = url.rsplit("/", 1)[1]
        data = self.files[name]
        rng = (kw.get("headers") or {}).get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(data):
                return FakeResp(416)
            return FakeResp(206, data[start:], {"Content-Length": str(len(data) - start)})
        return FakeResp(200, data, {"Content-Length": str(len(data))})

    def head(self, url, **kw):
        name = url.rsplit("/", 1)[1]
        return FakeResp(200, headers={"Content-Length": str(len(self.files[name]))})


FILES = {
    "PPR7001_PPR208218.xml.gz": b"a" * 50,
    "PPR208220_PPR297873.xml.gz": b"b" * 70,
    "PPR1082914_PPR1327385.xml.gz": b"c" * 30,
}


def _settings(monkeypatch):
    class S:
        europepmc_preprint_base = BASE

    monkeypatch.setattr(dl, "get_settings", lambda: S)


def test_listing_discovers_bulk_files_by_pattern_sorted_by_first_id():
    assert dl.parse_listing(LISTING) == [
        "PPR7001_PPR208218.xml.gz",
        "PPR208220_PPR297873.xml.gz",
        "PPR1082914_PPR1327385.xml.gz",
    ]


def test_download_fetches_new_files_and_skips_size_matched(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "preprints_bulk"
    raw.mkdir()
    (raw / "PPR7001_PPR208218.xml.gz").write_bytes(b"a" * 50)  # already there, same size
    sess = FakeSession(FILES)
    res = dl.download_bulk(raw, session=sess)
    assert res == {"files": 3, "fetched": 2, "skipped": 1, "errors": 0}
    assert (raw / "PPR208220_PPR297873.xml.gz").read_bytes() == b"b" * 70
    assert (raw / "PPR1082914_PPR1327385.xml.gz").read_bytes() == b"c" * 30
    assert not list(raw.glob("*.part"))
    fetched = [u.rsplit("/", 1)[1] for u, _ in sess.gets if u.endswith(".gz")]
    assert "PPR7001_PPR208218.xml.gz" not in fetched
    # second run: nothing to do
    assert dl.download_bulk(raw, session=FakeSession(FILES))["fetched"] == 0


def test_a_file_of_the_wrong_size_is_fetched_again(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    raw.mkdir()
    (raw / "PPR7001_PPR208218.xml.gz").write_bytes(b"a" * 10)  # short
    res = dl.download_bulk(raw, session=FakeSession(FILES))
    assert res["fetched"] == 3
    assert (raw / "PPR7001_PPR208218.xml.gz").read_bytes() == b"a" * 50


def test_interrupted_download_resumes_from_the_part_file(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    raw.mkdir()
    (raw / "PPR208220_PPR297873.xml.gz.part").write_bytes(b"b" * 20)
    sess = FakeSession(FILES)
    dl.download_bulk(raw, session=sess)
    assert (raw / "PPR208220_PPR297873.xml.gz").read_bytes() == b"b" * 70
    ranges = [h.get("Range") for u, h in sess.gets if u.endswith("PPR208220_PPR297873.xml.gz")]
    assert ranges == ["bytes=20-"]


def test_max_files_caps_new_fetches_and_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    res = dl.download_bulk(raw, session=FakeSession(FILES), max_files=1)
    assert res["fetched"] == 1
    raw2 = tmp_path / "c"
    res = dl.download_bulk(raw2, session=FakeSession(FILES), dry_run=True)
    assert res["fetched"] == 0 and res["files"] == 3
    assert not raw2.exists()
    assert "would fetch 3" in capsys.readouterr().out


def test_force_fetches_again(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    dl.download_bulk(raw, session=FakeSession(FILES))
    assert dl.download_bulk(raw, session=FakeSession(FILES), force=True)["fetched"] == 3


def test_force_with_max_files_touches_only_the_files_it_fetches(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    raw.mkdir()
    for name, data in FILES.items():
        (raw / name).write_bytes(b"old" + data)  # wrong size, present
    res = dl.download_bulk(raw, session=FakeSession(FILES), force=True, max_files=1)
    assert res["fetched"] == 1
    kept = [n for n in FILES if (raw / n).read_bytes().startswith(b"old")]
    assert len(kept) == 2  # the two files beyond the cap are untouched


def test_force_keeps_the_old_file_when_the_fetch_fails(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    raw.mkdir()
    old = b"old copy"
    (raw / "PPR7001_PPR208218.xml.gz").write_bytes(old)

    class Failing(FakeSession):
        def get(self, url, **kw):
            if url.endswith("PPR7001_PPR208218.xml.gz"):
                raise RuntimeError("network down")
            return super().get(url, **kw)

    res = dl.download_bulk(raw, session=Failing(FILES), force=True)
    assert res["errors"] == 1
    assert (raw / "PPR7001_PPR208218.xml.gz").read_bytes() == old


def test_a_part_file_as_big_as_the_file_is_dropped_and_fetched_fresh(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    raw.mkdir()
    (raw / "PPR208220_PPR297873.xml.gz.part").write_bytes(b"x" * 70)  # full size, stale
    (raw / "PPR1082914_PPR1327385.xml.gz.part").write_bytes(b"x" * 99)  # bigger
    dl.download_bulk(raw, session=FakeSession(FILES))
    assert (raw / "PPR208220_PPR297873.xml.gz").read_bytes() == b"b" * 70
    assert (raw / "PPR1082914_PPR1327385.xml.gz").read_bytes() == b"c" * 30


def test_a_416_answer_to_the_resume_request_starts_over(tmp_path, monkeypatch):
    _settings(monkeypatch)
    raw = tmp_path / "b"
    raw.mkdir()
    (raw / "PPR208220_PPR297873.xml.gz.part").write_bytes(b"x" * 20)

    class NoHead(FakeSession):
        def head(self, url, **kw):  # the size is unknown: only the 416 can tell
            raise RuntimeError("no HEAD")

    sess = NoHead({**FILES, "PPR208220_PPR297873.xml.gz": b"b" * 10})  # the part is bigger
    dl.download_bulk(raw, session=sess)
    assert (raw / "PPR208220_PPR297873.xml.gz").read_bytes() == b"b" * 10
