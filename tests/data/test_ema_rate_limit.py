"""EMA throttle handling (429 and 503) and stop handling, for both EMA downloaders.

Fake sessions only: no test calls EMA, and ``sleep`` is injected so no test waits.
"""

from __future__ import annotations

import json
import signal
from datetime import datetime, timezone

import pytest

from episteme.config import get_settings
from episteme.data.checkpoint_markers import failed_marker_path, success_marker_path
from episteme.data.ema import download_epar as dl
from episteme.data.regulatory import download_ema as fd
from episteme.data.regulatory.download_ema import RateLimited
from episteme.ops import stop

from .test_download_ema import FakeResp, FakeSession, _feed, _json_body
from .test_epar import _pdf_resp, doc

MESSAGE = "EMA rate limit: run again later; fetched files are kept"


class LogSession(FakeSession):
    """A fake session that writes every request, and every sleep, into one event list."""

    def __init__(self, responses, events, flush=lambda: None):
        super().__init__(responses)
        self.events = events
        self.flush = flush

    def get(self, url, **kwargs):
        self.flush()
        self.events.append(("get", url))
        return super().get(url, **kwargs)


def throttle(status: int = 429, retry_after: str | None = None) -> FakeResp:
    resp = FakeResp(b"slow down", status, "text/plain")
    if retry_after is not None:
        resp.headers["Retry-After"] = retry_after
    return resp


@pytest.fixture(autouse=True)
def _settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(tmp_path):
    events: list = []
    sleeps: list[float] = []
    pending: list[float] = []

    def flush() -> None:
        """The throttle wait is made of slices of 1 s at most: one wait is their sum."""
        if pending:
            total = float(sum(pending))
            pending.clear()
            sleeps.append(total)
            events.append(("sleep", total))

    def sleep(secs: float) -> None:
        if secs <= 1.0:  # a slice of a throttle wait (the delay is 2 s)
            pending.append(secs)
            return
        flush()
        sleeps.append(secs)
        events.append(("sleep", secs))

    return {
        "raw": tmp_path / "raw",
        "proc": tmp_path / "proc",
        "events": events,
        "sleeps": sleeps,
        "flush": flush,
        "kw": {
            "processed_root": tmp_path / "proc",
            "delay_s": 2.0,
            "sleep": sleep,
            "backoff_s": 60.0,
            "max_backoff_s": 600.0,
            "max_throttles": 5,
        },
    }


def _sess(env, *docs_and_resps) -> LogSession:
    return LogSession(
        {d["document_url"]: list(r) for d, r in docs_and_resps}, env["events"], env["flush"]
    )


# ---------------------------------------------------------------------------
# EPAR documents
# ---------------------------------------------------------------------------
def test_429_with_retry_after_seconds_waits_that_long_then_fetches_the_same_document(env):
    a = doc(1)
    s = _sess(env, (a, [throttle(429, "30"), _pdf_resp("a")]))
    out = dl.fetch_documents(env["raw"], [a], session=s, **env["kw"])
    assert out["fetched"] == ["1"] and not out["failed"]
    # The throttled document is asked for again after the wait, and only then.
    assert env["events"] == [
        ("get", a["document_url"]),
        ("sleep", 30.0),
        ("get", a["document_url"]),
    ]
    assert (env["raw"] / "epar" / "1.pdf").read_bytes().startswith(b"%PDF")


def test_retry_after_is_capped(env):
    a = doc(1)
    s = _sess(env, (a, [throttle(429, "99999"), _pdf_resp("a")]))
    dl.fetch_documents(env["raw"], [a], session=s, **env["kw"])
    assert env["sleeps"] == [600.0]


def test_429_with_an_http_date_waits_until_that_date(env, monkeypatch):
    monkeypatch.setattr(fd, "_utcnow", lambda: datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc))
    a = doc(1)
    s = _sess(env, (a, [throttle(429, "Wed, 04 Nov 2026 12:02:00 GMT"), _pdf_resp("a")]))
    dl.fetch_documents(env["raw"], [a], session=s, **env["kw"])
    assert env["sleeps"] == [120.0]


def test_an_http_date_in_the_past_or_an_unreadable_value_falls_back_to_the_backoff(
    env, monkeypatch
):
    monkeypatch.setattr(fd, "_utcnow", lambda: datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc))
    a, b = doc(1), doc(2)
    s = _sess(
        env,
        (a, [throttle(429, "Wed, 04 Nov 2026 11:00:00 GMT"), _pdf_resp("a")]),
        (b, [throttle(429, "soon"), _pdf_resp("b")]),
    )
    dl.fetch_documents(env["raw"], [a, b], session=s, **env["kw"])
    # a: 60 (the backoff; the date has passed). Then the delay between a and b. b: 60 again,
    # because a success reset the count.
    assert env["sleeps"] == [60.0, 2.0, 60.0]


def test_429_without_retry_after_doubles_the_wait_up_to_the_cap(env):
    a = doc(1)
    kw = {**env["kw"], "backoff_s": 10.0, "max_backoff_s": 35.0}
    s = _sess(env, (a, [throttle()] * 4 + [_pdf_resp("a")]))
    out = dl.fetch_documents(env["raw"], [a], session=s, **kw)
    assert out["fetched"] == ["1"]
    assert env["sleeps"] == [10.0, 20.0, 35.0, 35.0]  # 10, 20, 40 capped, 80 capped


@pytest.mark.parametrize("retry_after", [None, "7"])
def test_503_is_a_throttle_like_429(env, retry_after):
    a = doc(1)
    s = _sess(env, (a, [throttle(503, retry_after), _pdf_resp("a")]))
    out = dl.fetch_documents(env["raw"], [a], session=s, **env["kw"])
    assert out["fetched"] == ["1"] and not out["failed"]
    assert env["sleeps"] == [60.0 if retry_after is None else 7.0]


def test_a_throttle_does_not_use_up_the_attempts_of_the_document(env):
    a = doc(1)
    cut = FakeResp(b"%PDF-1.4 cut", 200, "application/pdf")
    # ATTEMPTS is 2: a throttle, then one failed try, then a good one still fits.
    s = _sess(env, (a, [throttle(), cut, _pdf_resp("a")]))
    out = dl.fetch_documents(env["raw"], [a], session=s, **env["kw"])
    assert out["fetched"] == ["1"] and not out["failed"]
    assert len(s.calls) == 3


def test_five_throttles_in_a_row_stop_the_stage_and_keep_the_earlier_files(env):
    a, b, c = doc(1), doc(2), doc(3)
    kw = {**env["kw"], "backoff_s": 1.5}
    s = _sess(env, (a, [_pdf_resp("a")]), (b, [throttle()]), (c, [_pdf_resp("c")]))
    with pytest.raises(RateLimited, match="rate limit"):
        dl.fetch_documents(env["raw"], [a, b, c], session=s, **kw)
    assert s.calls == [a["document_url"]] + [b["document_url"]] * 5  # no request for c
    assert env["sleeps"].count(2.0) == 1  # only the delay between a and b ...
    assert [x for x in env["sleeps"] if x != 2.0] == [1.5, 3.0, 6.0, 12.0]  # ... then 4 waits
    assert (env["raw"] / "epar" / "1.pdf").is_file()  # what was fetched stays
    assert not (env["raw"] / "epar" / "2.pdf").exists()
    assert not list((env["raw"] / "epar").glob(".*"))
    assert success_marker_path(env["proc"], dl.MARKER_SOURCE, "epar__1.pdf").is_file()
    marker = failed_marker_path(env["proc"], dl.MARKER_SOURCE, "epar__2.pdf")
    assert json.loads(marker.read_text("utf-8"))["error_class"] == "RateLimited"
    # A re-run resumes with the throttled document.
    s2 = _sess(env, (b, [_pdf_resp("b")]), (c, [_pdf_resp("c")]))
    out = dl.fetch_documents(env["raw"], [a, b, c], session=s2, **env["kw"])
    assert out["skipped"] == ["1"] and out["fetched"] == ["2", "3"]


def test_a_success_resets_the_throttle_count(env):
    a, b = doc(1), doc(2)
    # Four throttles, a success, four throttles, a success: eight in all, never five in a row.
    s = _sess(
        env, (a, [throttle()] * 4 + [_pdf_resp("a")]), (b, [throttle()] * 4 + [_pdf_resp("b")])
    )
    out = dl.fetch_documents(env["raw"], [a, b], session=s, **env["kw"])
    assert out["fetched"] == ["1", "2"] and not out["failed"]


def test_the_wait_settings_come_from_settings(env, monkeypatch):
    monkeypatch.setenv("EPISTEME_EMA_BACKOFF_SECONDS", "3")
    monkeypatch.setenv("EPISTEME_EMA_MAX_BACKOFF_SECONDS", "4")
    monkeypatch.setenv("EPISTEME_EMA_MAX_THROTTLES", "3")
    get_settings.cache_clear()
    a = doc(1)
    s = _sess(env, (a, [throttle()]))
    kw = {
        k: v
        for k, v in env["kw"].items()
        if k not in ("backoff_s", "max_backoff_s", "max_throttles")
    }
    with pytest.raises(RateLimited):
        dl.fetch_documents(env["raw"], [a], session=s, **kw)
    assert len(s.calls) == 3 and env["sleeps"] == [3.0, 4.0]


def test_the_wait_is_made_of_slices_of_one_second_at_most(env):
    a = doc(1)
    slices: list[float] = []
    kw = {**env["kw"], "sleep": slices.append}
    s = _sess(env, (a, [throttle(429, "5"), _pdf_resp("a")]))
    dl.fetch_documents(env["raw"], [a], session=s, **kw)
    assert slices == [1.0] * 5 and sum(slices) == 5.0
    slices.clear()
    s = _sess(env, (doc(2), [throttle(429, "2"), _pdf_resp("b")]))
    dl.fetch_documents(env["raw"], [doc(2)], session=s, **kw)
    assert max(slices) <= 1.0 and sum(slices) == 2.0


def test_a_stop_raised_in_a_slice_ends_the_run(env):
    a, b = doc(1), doc(2)
    seen: list[float] = []

    def sleep(secs: float) -> None:
        seen.append(secs)
        if len(seen) == 3:  # the stop arrives in the third second of a long wait
            raise KeyboardInterrupt

    s = _sess(env, (a, [throttle(429, "600"), _pdf_resp("a")]), (b, [_pdf_resp("b")]))
    kw = {**env["kw"], "sleep": sleep}
    with pytest.raises(KeyboardInterrupt):
        dl.fetch_documents(env["raw"], [a, b], session=s, **kw)
    assert len(seen) == 3 and s.calls == [a["document_url"]]
    assert not list((env["raw"] / "epar").glob(".*"))


def test_a_500_between_throttles_does_not_reset_the_count(env):
    docs = [doc(1), doc(2), doc(3)]
    kw = {**env["kw"], "backoff_s": 1.0}
    # Each of the first two documents: 429, 500, 429, 500 (both tries spent). Then 429 is the fifth.
    seq = [throttle(), throttle(500), throttle(), throttle(500)]
    s = _sess(
        env,
        (docs[0], list(seq)),
        (docs[1], [throttle(), throttle(500), throttle(), throttle(500)]),
        (docs[2], [throttle(), _pdf_resp("c")]),
    )
    with pytest.raises(RateLimited):
        dl.fetch_documents(env["raw"], docs, session=s, **kw)
    assert s.calls.count(docs[2]["document_url"]) == 1  # the fifth throttle, in the third document


@pytest.mark.parametrize("value", ["٣٠", "١"])  # Arabic-Indic digits
def test_a_unicode_digit_retry_after_falls_back_to_the_backoff(env, value):
    assert fd.parse_retry_after(value) is None
    a = doc(1)
    s = _sess(env, (a, [throttle(429, value), _pdf_resp("a")]))
    dl.fetch_documents(env["raw"], [a], session=s, **env["kw"])
    assert env["sleeps"] == [60.0]


@pytest.mark.parametrize("retry_after", ["0", None])
def test_a_wait_is_at_least_one_second(env, retry_after):
    a = doc(1)
    kw = {**env["kw"], "backoff_s": 0.0}
    s = _sess(env, (a, [throttle(429, retry_after), _pdf_resp("a")]))
    dl.fetch_documents(env["raw"], [a], session=s, **kw)
    assert env["sleeps"] == [1.0]


def test_a_503_with_an_html_body_is_a_throttle_not_the_verification_page(env):
    a = doc(1)
    html = FakeResp(b"<html>Service Unavailable</html>", 503, "text/html; charset=utf-8")
    s = _sess(env, (a, [html, _pdf_resp("a")]))
    out = dl.fetch_documents(env["raw"], [a], session=s, **env["kw"])
    assert out["fetched"] == ["1"] and env["sleeps"] == [60.0]
    assert not failed_marker_path(env["proc"], dl.MARKER_SOURCE, "epar__1.pdf").exists()


# ---------------------------------------------------------------------------
# EMA feeds
# ---------------------------------------------------------------------------
@pytest.fixture
def fenv(env, monkeypatch):
    monkeypatch.setattr(fd, "source_url", lambda key: f"https://ema.test/{key}")
    env["raw"] = env["raw"].parent / "ema"
    return env


def _furl(feed) -> str:
    return f"https://ema.test/{feed.url_key}"


def _fsess(env, *feeds_and_resps) -> LogSession:
    return LogSession({_furl(f): list(r) for f, r in feeds_and_resps}, env["events"], env["flush"])


def test_feed_429_waits_and_asks_again(fenv):
    a = _feed("ema_a")
    s = _fsess(fenv, (a, [throttle(429, "30"), FakeResp(_json_body(3))]))
    kw = {k: v for k, v in fenv["kw"].items()}
    out = fd.fetch_ema_feeds(fenv["raw"], {"ema_a": a}, session=s, **kw)
    assert out["fetched"] == ["ema_a"] and not out["failed"]
    assert fenv["events"] == [("get", _furl(a)), ("sleep", 30.0), ("get", _furl(a))]


def test_feed_503_without_retry_after_doubles_the_wait(fenv):
    a = _feed("ema_a")
    s = _fsess(fenv, (a, [throttle(503)] * 3 + [FakeResp(_json_body(3))]))
    kw = {**fenv["kw"], "backoff_s": 5.0, "max_backoff_s": 12.0}
    out = fd.fetch_ema_feeds(fenv["raw"], {"ema_a": a}, session=s, **kw)
    assert out["fetched"] == ["ema_a"] and fenv["sleeps"] == [5.0, 10.0, 12.0]


def test_feed_five_throttles_stop_the_stage_and_keep_earlier_feeds(fenv):
    a, b, c = _feed("ema_a"), _feed("ema_b"), _feed("ema_c")
    s = _fsess(
        fenv, (a, [FakeResp(_json_body(2))]), (b, [throttle()]), (c, [FakeResp(_json_body(2))])
    )
    kw = {**fenv["kw"], "backoff_s": 1.0}
    with pytest.raises(RateLimited):
        fd.fetch_ema_feeds(fenv["raw"], {"ema_a": a, "ema_b": b, "ema_c": c}, session=s, **kw)
    assert s.calls == [_furl(a)] + [_furl(b)] * 5
    assert (fenv["raw"] / "ema_a.json").is_file()
    assert not list(fenv["raw"].glob(".*")) and not (fenv["raw"] / "ema_b.json").exists()
    marker = failed_marker_path(fenv["proc"], fd.MARKER_SOURCE, "ema_b.json")
    assert json.loads(marker.read_text("utf-8"))["error_class"] == "RateLimited"


def test_feed_success_resets_the_count(fenv):
    a, b = _feed("ema_a"), _feed("ema_b")
    s = _fsess(
        fenv,
        (a, [throttle()] * 4 + [FakeResp(_json_body(2))]),
        (b, [throttle()] * 4 + [FakeResp(_json_body(2))]),
    )
    out = fd.fetch_ema_feeds(fenv["raw"], {"ema_a": a, "ema_b": b}, session=s, **fenv["kw"])
    assert out["fetched"] == ["ema_a", "ema_b"]


# ---------------------------------------------------------------------------
# main: exit code 3 and the stop request
# ---------------------------------------------------------------------------
@pytest.fixture
def restore_signals(monkeypatch, tmp_path):
    """stage_guard installs process-wide handlers: put the old ones back."""
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path / "proc"))
    get_settings.cache_clear()
    saved = {
        name: signal.getsignal(getattr(signal, name))
        for name in ("SIGTERM", "SIGBREAK", "SIGINT")
        if hasattr(signal, name)
    }
    yield
    for name, handler in saved.items():
        signal.signal(getattr(signal, name), handler)
    stop._TERM_SEEN = False
    stop._STOPPING = False
    get_settings.cache_clear()


def _epar_main(monkeypatch, tmp_path, docs, session):
    out_dir = tmp_path / "ema"
    out_dir.mkdir()
    (out_dir / dl.FEED_FILE).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dl, "load_documents", lambda *a, **k: docs)
    real = dl.fetch_documents
    monkeypatch.setattr(
        dl, "fetch_documents", lambda *a, **k: real(*a, session=session, sleep=lambda s: None, **k)
    )
    return ["--output-dir", str(out_dir)]


def test_epar_main_exits_3_with_the_message_on_a_rate_limit(
    tmp_path, monkeypatch, capsys, restore_signals
):
    a = doc(1)
    s = FakeSession({a["document_url"]: [throttle()]})
    argv = _epar_main(monkeypatch, tmp_path, [a], s)
    assert dl.main(argv) == 3
    assert MESSAGE in capsys.readouterr().err
    assert len(s.calls) == 5


def test_epar_main_stop_request_ends_the_stage_with_143(tmp_path, monkeypatch, restore_signals):
    a, b, c = doc(1), doc(2), doc(3)

    class StoppingSession(FakeSession):
        def get(self, url, **kwargs):
            resp = super().get(url, **kwargs)
            if len(self.calls) == 2:  # the stop arrives while the second document is asked for
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return resp

    s = StoppingSession({d["document_url"]: [_pdf_resp(d["id"])] for d in (a, b, c)})
    argv = _epar_main(monkeypatch, tmp_path, [a, b, c], s)
    with pytest.raises(SystemExit) as exc:
        dl.main(argv)
    assert exc.value.code == stop.EXIT_TERM
    assert s.calls == [a["document_url"], b["document_url"]]  # no request for the third
    epar = tmp_path / "ema" / "epar"
    assert (epar / "1.pdf").is_file()  # fetched before the stop: kept
    assert not (epar / "2.pdf").exists() and not list(epar.glob(".*"))  # no partial file


def _feed_main(monkeypatch, tmp_path, feeds, session):
    out_dir = tmp_path / "ema"
    monkeypatch.setattr(fd, "load_feeds", lambda: feeds)
    monkeypatch.setattr(fd, "source_url", lambda key: f"https://ema.test/{key}")
    real = fd.fetch_ema_feeds
    monkeypatch.setattr(
        fd, "fetch_ema_feeds", lambda *a, **k: real(*a, session=session, sleep=lambda s: None, **k)
    )
    return ["--output-dir", str(out_dir)]


def test_feed_main_exits_3_with_the_message_on_a_rate_limit(
    tmp_path, monkeypatch, capsys, restore_signals
):
    a = _feed("ema_a")
    s = FakeSession({_furl(a): [throttle()]})
    argv = _feed_main(monkeypatch, tmp_path, {"ema_a": a}, s)
    assert fd.main(argv) == 3
    assert MESSAGE in capsys.readouterr().err
    assert len(s.calls) == 5


def test_feed_main_stop_request_ends_the_stage_with_143(tmp_path, monkeypatch, restore_signals):
    a, b = _feed("ema_a"), _feed("ema_b")

    class StoppingSession(FakeSession):
        def get(self, url, **kwargs):
            resp = super().get(url, **kwargs)
            if len(self.calls) == 2:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return resp

    s = StoppingSession({_furl(a): [FakeResp(_json_body(2))], _furl(b): [FakeResp(_json_body(2))]})
    argv = _feed_main(monkeypatch, tmp_path, {"ema_a": a, "ema_b": b}, s)
    with pytest.raises(SystemExit) as exc:
        fd.main(argv)
    assert exc.value.code == stop.EXIT_TERM
    assert s.calls == [_furl(a), _furl(b)]
    assert (tmp_path / "ema" / "ema_a.json").is_file()
    assert not (tmp_path / "ema" / "ema_b.json").exists()
    assert not list((tmp_path / "ema").glob(".*"))


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def test_throttle_settings_defaults_and_overrides(monkeypatch):
    monkeypatch.setattr("episteme.config._find_project_dotenv", lambda: None)
    monkeypatch.setenv("EPISTEME_ACTOR", "x")
    for name in (
        "EPISTEME_EMA_DELAY_SECONDS",
        "EPISTEME_EMA_BACKOFF_SECONDS",
        "EPISTEME_EMA_MAX_BACKOFF_SECONDS",
        "EPISTEME_EMA_MAX_THROTTLES",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    s = get_settings()
    assert (s.ema_delay_seconds, s.ema_backoff_seconds) == (5.0, 60.0)
    assert (s.ema_max_backoff_seconds, s.ema_max_throttles) == (600.0, 5)
    monkeypatch.setenv("EPISTEME_EMA_BACKOFF_SECONDS", "9")
    monkeypatch.setenv("EPISTEME_EMA_MAX_BACKOFF_SECONDS", "90")
    monkeypatch.setenv("EPISTEME_EMA_MAX_THROTTLES", "2")
    get_settings.cache_clear()
    s = get_settings()
    assert (s.ema_backoff_seconds, s.ema_max_backoff_seconds, s.ema_max_throttles) == (9.0, 90.0, 2)
