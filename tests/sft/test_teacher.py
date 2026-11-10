import logging
import types

import pytest
import requests

from episteme import config as cfg
from episteme.sft.teacher import BudgetExceeded, Teacher, TeacherError

KEY = "sk-test-secret-0123456789"


def _teacher(session, budget=1000, **kw):
    return Teacher("https://teacher.invalid/v1", KEY, "teach-1", budget, session=session, **kw)


def _settings(**kw):
    base = {
        "teacher_model": "teach-1",
        "teacher_budget_tokens": 1000,
        "teacher_request_timeout_s": 120,
    }
    return types.SimpleNamespace(**{**base, **kw})


def _msgs(chars: int) -> list[dict]:
    return [{"role": "user", "content": "x" * chars}]


def test_budget_cap_refuses_before_request(fakes):
    session = fakes.FakeSession([fakes.chat_reply("{}", {"total_tokens": 10})])
    t = _teacher(session, budget=100)
    # estimate = 400 chars / 4 + 10 = 110 > 100
    with pytest.raises(BudgetExceeded):
        t.complete(_msgs(400), max_tokens=10)
    assert session.posts == []
    assert t.used_tokens == 0


def test_budget_cap_allows_exact_fit(fakes):
    session = fakes.FakeSession([fakes.chat_reply("{}", {"total_tokens": 7})])
    t = _teacher(session, budget=110)
    assert t.estimate(_msgs(400), max_tokens=10) == 110
    assert t.complete(_msgs(400), max_tokens=10) == "{}"
    assert len(session.posts) == 1


def test_budget_counts_tokens_already_used(fakes):
    session = fakes.FakeSession([fakes.chat_reply("{}", {"total_tokens": 60})])
    t = _teacher(session, budget=100)
    t.complete(_msgs(40), max_tokens=10)  # estimate 20, uses 60
    with pytest.raises(BudgetExceeded):
        t.complete(_msgs(40), max_tokens=40)  # 60 + 50 > 100
    assert len(session.posts) == 1


def test_usage_tokens_accumulate(fakes, caplog):
    session = fakes.FakeSession(
        [
            fakes.chat_reply("a", {"total_tokens": 30, "prompt_tokens": 1}),
            fakes.chat_reply("b", {"prompt_tokens": 5, "completion_tokens": 7}),
            fakes.chat_reply("c"),
        ]
    )
    t = _teacher(session, budget=10_000)
    assert t.complete(_msgs(8), max_tokens=4) == "a"
    assert t.used_tokens == 30
    t.complete(_msgs(8), max_tokens=4)
    assert t.used_tokens == 42
    with caplog.at_level(logging.WARNING):
        t.complete(_msgs(8), max_tokens=4)  # no usage: the estimate (2 + 4) counts
    assert t.used_tokens == 48
    assert "usage" in caplog.text


def test_request_shape_has_timeout_and_no_retry(fakes):
    session = fakes.FakeSession([fakes.FakeResponse({}, status=503)])
    t = _teacher(session, timeout_s=42)
    with pytest.raises(requests.HTTPError):
        t.complete(_msgs(8), max_tokens=4)
    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["timeout"] == 42
    assert post["url"] == "https://teacher.invalid/v1/chat/completions"
    assert post["json"]["model"] == "teach-1"
    assert post["json"]["max_tokens"] == 4


def test_reply_without_choices_raises_teacher_error(fakes):
    session = fakes.FakeSession([{"usage": {"total_tokens": 3}}])
    t = _teacher(session)
    with pytest.raises(TeacherError):
        t.complete(_msgs(8), max_tokens=4)
    assert t.used_tokens == 3


def test_api_key_never_in_logs(fakes, caplog):
    caplog.set_level(logging.DEBUG)
    session = fakes.FakeSession(
        [
            fakes.chat_reply("ok", {"total_tokens": 5}),
            fakes.chat_reply("ok"),
            fakes.FakeResponse({}, status=500),
        ]
    )
    t = _teacher(session, budget=200)
    t.complete(_msgs(8), max_tokens=4)
    t.complete(_msgs(8), max_tokens=4)
    with pytest.raises(requests.HTTPError) as http_err:
        t.complete(_msgs(8), max_tokens=4)
    with pytest.raises(BudgetExceeded) as budget_err:
        t.complete(_msgs(4000), max_tokens=4)
    # The key does go into the request header, and only there.
    assert session.posts[0]["headers"]["Authorization"].endswith(KEY)
    texts = [caplog.text, str(http_err.value), str(budget_err.value), repr(t), str(t)]
    for text in texts:
        assert KEY not in text
        assert "Authorization" not in text
        assert "Bearer" not in text


def test_from_settings_requires_key_and_url_and_names_keys_only():
    s = _settings(teacher_base_url="https://teacher.invalid/v1", teacher_api_key=None)
    with pytest.raises(TeacherError, match="EPISTEME_TEACHER_API_KEY"):
        Teacher.from_settings(s)
    s = _settings(teacher_base_url=None, teacher_api_key=KEY)
    with pytest.raises(TeacherError) as err:
        Teacher.from_settings(s)
    assert "TEACHER_BASE_URL" in str(err.value)
    assert KEY not in str(err.value)
    s = _settings(
        teacher_base_url="https://teacher.invalid/v1",
        teacher_api_key=KEY,
        teacher_model="m",
        teacher_budget_tokens=99,
        teacher_request_timeout_s=7,
    )
    t = Teacher.from_settings(s)
    assert (t.model, t.budget_tokens, t.timeout_s) == ("m", 99, 7)


def test_teacher_timeout_setting(monkeypatch):
    assert cfg.DEFAULT_TEACHER_REQUEST_TIMEOUT_S == 120
    fields = cfg.Settings.__dataclass_fields__
    assert fields["teacher_request_timeout_s"].default == 120
    assert fields["teacher_max_tokens"].default == cfg.DEFAULT_TEACHER_MAX_TOKENS
    cfg.get_settings.cache_clear()
    monkeypatch.setenv("EPISTEME_TEACHER_REQUEST_TIMEOUT_S", "33")
    monkeypatch.setenv("EPISTEME_TEACHER_MAX_TOKENS", "512")
    try:
        s = cfg.get_settings()
        assert s.teacher_request_timeout_s == 33
        assert s.teacher_max_tokens == 512
    finally:
        cfg.get_settings.cache_clear()


def test_no_network_guard_fires_for_a_real_session():
    t = Teacher("https://teacher.invalid/v1", KEY, "teach-1", 1000)
    with pytest.raises(AssertionError, match="network access in a test"):
        t.complete(_msgs(8), max_tokens=4)
