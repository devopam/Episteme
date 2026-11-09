"""EMA feed download: fake responses only, no network."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from episteme.data.checkpoint_markers import failed_marker_path, success_marker_path
from episteme.data.regulatory import download_ema as dl
from episteme.data.regulatory.download_ema import FeedVerificationError, fetch_ema_feeds
from episteme.data.regulatory.feeds import Feed

SOURCE = "ema_download"


class FakeResp:
    def __init__(
        self, body: bytes, status: int = 200, ctype: str = "application/json", cl=None, enc=None
    ):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        if enc:
            self.headers["Content-Encoding"] = enc
        if cl is not False:
            self.headers["Content-Length"] = str(len(body) if cl is None else cl)
        self._body = body

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), 7):  # small chunks: exercises the streaming path
            yield self._body[i : i + 7]

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses: dict[str, list[FakeResp]]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        queue = self.responses[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]


def _feed(name: str, fmt: str = "ema_json") -> Feed:
    return Feed(name, f"K_{name.upper()}", fmt, "EU", ("id",), (), (), None, "monthly")


def _json_body(n: int, total: int | None = None) -> bytes:
    data = [{"id": str(i)} for i in range(n)]
    return json.dumps(
        {"meta": {"total_records": n if total is None else total}, "data": data}
    ).encode()


def _xlsx_body() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
    return buf.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "source_url", lambda key: f"https://ema.test/{key}")
    sleeps: list[float] = []
    return {
        "raw": tmp_path / "raw" / "ema",
        "proc": tmp_path / "proc",
        "sleeps": sleeps,
        "kw": {"processed_root": tmp_path / "proc", "delay_s": 2.0, "sleep": sleeps.append},
    }


def _url(feed: Feed) -> str:
    return f"https://ema.test/{feed.url_key}"


def test_feeds_download_one_at_a_time_with_a_delay_and_a_marker_each(env):
    a, b = _feed("ema_a"), _feed("ema_b")
    session = FakeSession({_url(a): [FakeResp(_json_body(3))], _url(b): [FakeResp(_json_body(2))]})
    out = fetch_ema_feeds(env["raw"], {"ema_a": a, "ema_b": b}, session=session, **env["kw"])
    assert out["fetched"] == ["ema_a", "ema_b"] and not out["failed"]
    assert (env["raw"] / "ema_a.json").is_file() and (env["raw"] / "ema_b.json").is_file()
    assert env["sleeps"] == [2.0]  # between the two requests, not before the first
    for name in ("ema_a.json", "ema_b.json"):
        assert success_marker_path(env["proc"], SOURCE, name).is_file()
    assert not list(env["raw"].glob("*.tmp")) and not list(env["raw"].glob(".*"))


def test_rerun_skips_a_feed_with_a_success_marker_unless_forced(env):
    a = _feed("ema_a")
    session = FakeSession({_url(a): [FakeResp(_json_body(3))]})
    fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert out["skipped"] == ["ema_a"] and len(session.calls) == 1
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, force=True, **env["kw"])
    assert out["fetched"] == ["ema_a"] and len(session.calls) == 2


def test_only_ema_feeds_are_fetched(env):
    fda = Feed("fda_ndc", "OPENFDA_CATALOG", "openfda_json", "US", (), (), (), None, "monthly")
    session = FakeSession({})
    out = fetch_ema_feeds(env["raw"], {"fda_ndc": fda}, session=session, **env["kw"])
    assert out == {"fetched": [], "skipped": [], "failed": {}} and session.calls == []


def test_article57_workbook_is_stored_as_xlsx(env):
    f = _feed("ema_article57", "article57_xlsx")
    session = FakeSession({_url(f): [FakeResp(_xlsx_body(), ctype="application/vnd.ms-excel")]})
    out = fetch_ema_feeds(env["raw"], {"ema_article57": f}, session=session, **env["kw"])
    assert out["fetched"] == ["ema_article57"]
    assert (env["raw"] / "ema_article57.xlsx").is_file()


def test_ema_verification_page_stops_the_stage(env):
    a, b = _feed("ema_a"), _feed("ema_b")
    html = b"<!DOCTYPE html><html><body>Please verify you are human</body></html>"
    session = FakeSession(
        {_url(a): [FakeResp(html, 200, "text/html; charset=utf-8")], _url(b): [FakeResp(b"{}")]}
    )
    with pytest.raises(FeedVerificationError):
        fetch_ema_feeds(env["raw"], {"ema_a": a, "ema_b": b}, session=session, **env["kw"])
    assert session.calls == [_url(a)]  # no retry, and the next feed is never tried
    assert not list(env["raw"].glob("*"))  # no file stored, no temp file left
    marker = failed_marker_path(env["proc"], SOURCE, "ema_a.json")
    assert marker.is_file()
    assert "FeedVerificationError" in marker.read_text(encoding="utf-8")
    assert not success_marker_path(env["proc"], SOURCE, "ema_a.json").exists()


def test_html_body_with_a_json_content_type_is_still_a_verification_page(env):
    a = _feed("ema_a")
    session = FakeSession({_url(a): [FakeResp(b"  <html>check</html>", 200, "application/json")]})
    with pytest.raises(FeedVerificationError):
        fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert not list(env["raw"].glob("*"))


def test_truncated_download_is_retried_once_then_fails(env):
    a, b = _feed("ema_a"), _feed("ema_b")
    full = _json_body(40)
    cut = full[: len(full) // 2]
    # The body is cut and the declared length is the full length: curl error 18.
    session = FakeSession(
        {
            _url(a): [FakeResp(cut, cl=len(full))],
            _url(b): [FakeResp(_json_body(2))],
        }
    )
    out = fetch_ema_feeds(env["raw"], {"ema_a": a, "ema_b": b}, session=session, **env["kw"])
    assert session.calls.count(_url(a)) == 2  # one retry, no more
    assert "ema_a" in out["failed"] and out["fetched"] == ["ema_b"]
    assert not (env["raw"] / "ema_a.json").exists()
    assert failed_marker_path(env["proc"], SOURCE, "ema_a.json").is_file()
    assert not success_marker_path(env["proc"], SOURCE, "ema_a.json").exists()
    assert not list(env["raw"].glob("*.tmp"))


def test_truncated_json_without_a_content_length_is_caught_by_parsing(env):
    a = _feed("ema_a")
    full = _json_body(40)
    session = FakeSession({_url(a): [FakeResp(full[:-30], cl=False)]})
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert "ema_a" in out["failed"] and session.calls.count(_url(a)) == 2
    assert not (env["raw"] / "ema_a.json").exists()


def test_a_retry_that_succeeds_stores_the_file(env):
    a = _feed("ema_a")
    full = _json_body(10)
    session = FakeSession({_url(a): [FakeResp(full[:20], cl=len(full)), FakeResp(full)]})
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert out["fetched"] == ["ema_a"] and not out["failed"]
    assert len(session.calls) == 2
    assert env["sleeps"] == [2.0]  # the delay also applies before a retry


def test_server_error_is_retried_once_then_fails(env):
    a = _feed("ema_a")
    session = FakeSession({_url(a): [FakeResp(b"oops", 500, "text/plain")]})
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert "ema_a" in out["failed"] and len(session.calls) == 2


def test_row_count_differing_from_total_is_recorded_not_fatal(env):
    # The herbal feed carries more rows than meta.total_records; the loader decides.
    a = _feed("ema_a")
    session = FakeSession({_url(a): [FakeResp(_json_body(5, total=3))]})
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert out["fetched"] == ["ema_a"]
    marker = json.loads(success_marker_path(env["proc"], SOURCE, "ema_a.json").read_text())
    assert marker["stats"]["rows"] == 5 and marker["stats"]["total_records"] == 3


def test_delay_default_comes_from_settings(tmp_path, monkeypatch):
    monkeypatch.setattr("episteme.config._find_project_dotenv", lambda: None)
    monkeypatch.setenv("EPISTEME_EMA_DELAY_SECONDS", "7")
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    import episteme.config as cfg

    cfg.get_settings.cache_clear()
    try:
        assert cfg.get_settings().ema_delay_seconds == 7.0
        monkeypatch.delenv("EPISTEME_EMA_DELAY_SECONDS")
        cfg.get_settings.cache_clear()
        assert cfg.get_settings().ema_delay_seconds == 5.0
    finally:
        cfg.get_settings.cache_clear()


def test_source_url_reads_sources_env_through_config(monkeypatch):
    import episteme.config as cfg

    monkeypatch.setattr(cfg, "_SOURCES", {"SOME_FEED_URL": "https://feed.test/a.json"})
    monkeypatch.setattr(cfg, "get_settings", lambda: None)  # sources.env is not read here
    monkeypatch.delenv("SOME_FEED_URL", raising=False)
    monkeypatch.delenv("NO_SUCH_KEY_ANYWHERE", raising=False)
    assert cfg.source_url("SOME_FEED_URL") == "https://feed.test/a.json"
    monkeypatch.setenv("SOME_FEED_URL", "https://override.test/b.json")  # the environment wins
    assert cfg.source_url("SOME_FEED_URL") == "https://override.test/b.json"
    with pytest.raises(cfg.ConfigError):
        cfg.source_url("NO_SUCH_KEY_ANYWHERE")


def test_a_gzip_encoded_response_is_not_judged_truncated(env):
    # requests decodes gzip: the bytes written are the decoded ones, and Content-Length
    # counts the encoded ones. The two differ, and the file is still whole.
    a = _feed("ema_a")
    body = _json_body(30)
    session = FakeSession({_url(a): [FakeResp(body, cl=len(body) // 3, enc="gzip")]})
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert out["fetched"] == ["ema_a"] and not out["failed"] and len(session.calls) == 1


def test_a_truncated_gzip_encoded_response_is_still_caught_by_parsing(env):
    a = _feed("ema_a")
    body = _json_body(30)
    session = FakeSession({_url(a): [FakeResp(body[:-40], cl=len(body) // 3, enc="gzip")]})
    out = fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    assert "ema_a" in out["failed"] and len(session.calls) == 2


@pytest.mark.parametrize("status", [404, 500])
def test_an_html_error_page_is_an_ordinary_error_not_the_verification_page(env, status):
    a, b = _feed("ema_a"), _feed("ema_b")
    page = b"<html><body>Not here</body></html>"
    session = FakeSession(
        {_url(a): [FakeResp(page, status, "text/html")], _url(b): [FakeResp(_json_body(2))]}
    )
    out = fetch_ema_feeds(env["raw"], {"ema_a": a, "ema_b": b}, session=session, **env["kw"])
    assert "ema_a" in out["failed"] and out["fetched"] == ["ema_b"]  # the stage goes on
    assert session.calls.count(_url(a)) == 2  # retried once, as for any error
    assert failed_marker_path(env["proc"], SOURCE, "ema_a.json").is_file()
    assert not (env["raw"] / "ema_a.json").exists()


def test_marker_and_file_names_are_input_keys(env):
    from episteme.data.checkpoint_markers import input_key

    a = _feed("ema_a")
    session = FakeSession({_url(a): [FakeResp(_json_body(1))]})
    fetch_ema_feeds(env["raw"], {"ema_a": a}, session=session, **env["kw"])
    stored = env["raw"] / "ema_a.json"
    assert input_key(stored, env["raw"]) == "ema_a.json"
    assert success_marker_path(env["proc"], SOURCE, input_key(stored, env["raw"])).is_file()


def test_configured_ema_feeds_never_share_a_stored_file_or_marker():
    from episteme.data.checkpoint_markers import input_key
    from episteme.data.regulatory.feeds import load_feeds

    raw = Path("raw") / "ema"
    feeds = dl.ema_feeds(load_feeds())
    assert len(feeds) == 13
    names = [input_key(raw / f"{f.name}{dl._EXT[f.format]}", raw) for f in feeds]
    assert len(set(names)) == len(names)
    markers = {success_marker_path(Path("p"), SOURCE, n) for n in names}
    assert len(markers) == len(names)


def test_same_stem_in_two_formats_keeps_two_files_and_two_markers(env):
    j = _feed("ema_x", "ema_json")
    x = Feed("ema_x_book", "K_EMA_X_BOOK", "article57_xlsx", "EU", (), (), (), None, "monthly")
    session = FakeSession(
        {_url(j): [FakeResp(_json_body(1))], _url(x): [FakeResp(_xlsx_body(), ctype="x/y")]}
    )
    out = fetch_ema_feeds(env["raw"], {"a": j, "b": x}, session=session, **env["kw"])
    assert out["fetched"] == ["ema_x", "ema_x_book"]
    assert sorted(p.name for p in env["raw"].iterdir()) == ["ema_x.json", "ema_x_book.xlsx"]
    for n in ("ema_x.json", "ema_x_book.xlsx"):
        assert success_marker_path(env["proc"], SOURCE, n).is_file()


def test_two_feeds_with_one_name_are_refused_before_any_download(env):
    a1 = _feed("ema_a")
    a2 = Feed("ema_a", "K_OTHER", "ema_json", "EU", (), (), (), None, "monthly")
    session = FakeSession({})
    with pytest.raises(ValueError, match="ema_a.json"):
        fetch_ema_feeds(env["raw"], {"one": a1, "two": a2}, session=session, **env["kw"])
    assert session.calls == []
