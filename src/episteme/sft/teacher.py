"""Teacher client: one OpenAI-compatible chat endpoint with a hard token budget.

The budget check runs before every request: when the tokens used so far plus the
estimate of the next request (prompt characters / 4, rounded up, plus ``max_tokens``)
would pass the cap, ``BudgetExceeded`` is raised and nothing is sent. Used tokens come
from the response ``usage``. One request per call: no automatic retry in v1.

The API key goes into the request header and nowhere else: not into logs, ``repr``
or exception text.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from episteme.config import DEFAULT_TEACHER_REQUEST_TIMEOUT_S

if TYPE_CHECKING:
    import requests

log = logging.getLogger(__name__)


class TeacherError(Exception):
    """The teacher is not configured, or its reply has no completion."""


class BudgetExceeded(Exception):
    """The next request could pass the token budget; it was not sent."""

    def __init__(self, used: int, estimate: int, budget: int):
        super().__init__(
            f"teacher budget: used {used} + estimate {estimate} > budget {budget} tokens"
        )
        self.used = used
        self.estimate = estimate
        self.budget = budget


def _prompt_chars(messages: list[dict]) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)


class Teacher:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        budget_tokens: int,
        session: requests.Session | None = None,
        *,
        timeout_s: float = DEFAULT_TEACHER_REQUEST_TIMEOUT_S,
    ):
        if session is None:
            import requests

            session = requests.Session()
        self.model = model
        self.budget_tokens = budget_tokens
        self.timeout_s = timeout_s
        self.used_tokens = 0
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._session = session

    @classmethod
    def from_settings(cls, settings: Any, session: requests.Session | None = None) -> Teacher:
        """Build the teacher from ``Settings``. Errors name env keys, never values."""
        if not settings.teacher_base_url:
            raise TeacherError("TEACHER_BASE_URL is not set (scripts/data/_lib/sources.env)")
        if not settings.teacher_api_key:
            raise TeacherError("EPISTEME_TEACHER_API_KEY is not set")
        return cls(
            settings.teacher_base_url,
            settings.teacher_api_key,
            settings.teacher_model,
            settings.teacher_budget_tokens,
            session=session,
            timeout_s=settings.teacher_request_timeout_s,
        )

    def __repr__(self) -> str:
        return (
            f"Teacher(model={self.model!r}, used_tokens={self.used_tokens},"
            f" budget_tokens={self.budget_tokens})"
        )

    @staticmethod
    def estimate(messages: list[dict], *, max_tokens: int) -> int:
        """Tokens a request may cost: prompt characters / 4 (rounded up) + ``max_tokens``."""
        return -(-_prompt_chars(messages) // 4) + max_tokens

    def complete(self, messages: list[dict], *, max_tokens: int) -> str:
        """Send one chat request and return the completion text."""
        estimate = self.estimate(messages, max_tokens=max_tokens)
        if self.used_tokens + estimate > self.budget_tokens:
            log.info(
                "teacher: budget stop (used %d, estimate %d, budget %d)",
                self.used_tokens,
                estimate,
                self.budget_tokens,
            )
            raise BudgetExceeded(self.used_tokens, estimate, self.budget_tokens)
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens}
        log.info(
            "teacher: request model=%s messages=%d max_tokens=%d estimate=%d",
            self.model,
            len(messages),
            max_tokens,
            estimate,
        )
        resp = self._session.post(
            self._url,
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=self.timeout_s,
        )
        resp.raise_for_status()
        body = resp.json()
        self.used_tokens += self._usage(body, estimate)
        log.info("teacher: used %d of %d tokens", self.used_tokens, self.budget_tokens)
        try:
            content = body["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise TeacherError("teacher reply has no choices[0].message") from None
        return content or ""

    @staticmethod
    def _usage(body: Any, estimate: int) -> int:
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if isinstance(total, int):
                return total
            prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if isinstance(prompt, int) and isinstance(completion, int):
                return prompt + completion
        log.warning("teacher: reply has no usage; counting the estimate of %d tokens", estimate)
        return estimate
