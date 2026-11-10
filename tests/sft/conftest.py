"""Shared fixtures for the SFT tests: no test may reach the network."""

from __future__ import annotations

import pytest
import requests


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any request through a real ``requests.Session`` fails loudly."""

    def _refuse(self, *args, **kwargs):
        raise AssertionError("network access in a test")

    monkeypatch.setattr(requests.Session, "request", _refuse)


class FakeResponse:
    def __init__(self, body: dict, status: int = 200):
        self._body = body
        self.status_code = status

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error for url: stub")


class FakeSession:
    """Stub ``requests.Session``: returns queued bodies and records every post."""

    def __init__(self, replies: list):
        self.replies = list(replies)
        self.posts: list[dict] = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        reply = self.replies.pop(0)
        if isinstance(reply, FakeResponse):
            return reply
        return FakeResponse(reply)


def chat_reply(content: str, usage: dict | None = None) -> dict:
    body: dict = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


@pytest.fixture
def fakes():
    """The stub classes, for tests that build their own session."""
    import types

    return types.SimpleNamespace(
        FakeSession=FakeSession, FakeResponse=FakeResponse, chat_reply=chat_reply
    )
