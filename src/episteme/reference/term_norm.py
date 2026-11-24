"""Term key for MeSH term matching; the Python twin of episteme_api_internal.term_norm.

Lower case; every character that is not a letter or a digit becomes a space; runs of
spaces collapse to one; trimmed. The pg parity test compares both sides.
"""

from __future__ import annotations

import re

_NOT_ALNUM = re.compile(r"[^\w]|_")
_SPACES = re.compile(r" +")


def term_norm(text: str) -> str:
    return _SPACES.sub(" ", _NOT_ALNUM.sub(" ", text.lower())).strip(" ")
