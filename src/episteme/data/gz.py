"""Read gzip through python-isal (ISA-L, several times faster to decompress)
when it is installed, else through the standard library.

``open_gz`` takes ``gzip.open``'s arguments, binary and text mode alike. A
corrupt member raises ``EOFError`` / ``gzip.BadGzipFile`` from both; isal's
other decode error (``IsalError``) is not ``zlib.error``, so callers catch
``Exception`` (every current caller does).
"""

from __future__ import annotations

import gzip
from typing import Any

try:
    from isal import igzip as _igzip
except ImportError:  # pragma: no cover - isal ships in the data extra
    _igzip = None


def open_gz(path: Any, mode: str = "rb", **kwargs: Any):
    if _igzip is not None:
        return _igzip.open(path, mode, **kwargs)
    return gzip.open(path, mode, **kwargs)
