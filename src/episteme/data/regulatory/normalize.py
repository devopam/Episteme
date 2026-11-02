"""Normalisers for reference-feed values: names, NDC, application numbers, lots.

Pure functions, no I/O. The loader stores the normalised forms in ``reg_record``
(``substance_names``, ``product_keys``); the raw values stay in ``payload``.
Salts and esters are NOT folded into their parent here: ``metformin hcl`` and
``metformin`` stay two names. Joining them is the job of ``substance_xref``.
"""

from __future__ import annotations

import re
import unicodedata

_NAME_SEPARATORS = re.compile(r"[|;]")
_SPACES = re.compile(r"\s+")
_NDC_SEGMENTS = re.compile(r"^\d{1,5}-\d{1,4}-\d{1,2}$")
_NDC_PRODUCT = re.compile(r"^\d{1,5}-\d{1,4}$")
_APP_PREFIX = re.compile(r"^(?:ANDA|NDA|BLA)\s*", re.IGNORECASE)


def norm_name(s: str) -> str:
    """Case-folded, NFKC-normalised name with single spaces and no outer space."""
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", str(s)).casefold()).strip()


def split_names(s: str) -> list[str]:
    """Split a multi-name cell on ``|`` and ``;`` (Article 57 uses ``|``). Parts
    are stripped. Empty parts are dropped. The names are not normalised."""
    return [p for p in (part.strip() for part in _NAME_SEPARATORS.split(str(s))) if p]


def norm_ndc(s: str) -> str | None:
    """The 11-digit, 5-4-2 form of an NDC; ``None`` when ``s`` is not one.

    Accepts the three 10-digit hyphenated layouts (4-4-2, 5-3-2, 5-4-1) and any
    shorter segments: each segment is zero-padded to 5, 4 and 2 digits. An
    unhyphenated value must already have 11 digits (10 digits cannot say where
    the segments split)."""
    v = str(s).strip()
    if _NDC_SEGMENTS.match(v):
        labeler, product, package = v.split("-")
        return labeler.zfill(5) + product.zfill(4) + package.zfill(2)
    if re.fullmatch(r"\d{11}", v):
        return v
    return None


def norm_application_number(s: str) -> str | None:
    """An FDA application number without its ``NDA``/``ANDA``/``BLA`` prefix, digits
    only (leading zeros kept); ``None`` when no digits remain or letters are left."""
    v = _APP_PREFIX.sub("", str(s).strip())
    if not v or not re.fullmatch(r"[\d\s-]+", v):
        return None
    digits = re.sub(r"\D", "", v)
    return digits or None


def norm_lot(s: str) -> str | None:
    """A lot or batch number as upper-case letters and digits only; ``None`` if empty."""
    v = re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", str(s)).upper())
    return v or None


def norm_product_key(s: str) -> str | None:
    """One ``product_keys`` entry. An NDC becomes its 11-digit form (a labeler-product
    NDC becomes its 9 digits). An application number loses its prefix. Anything else
    (an EMA product number) is kept, stripped. ``None`` for an empty value."""
    v = str(s).strip()
    if not v:
        return None
    ndc = norm_ndc(v)
    if ndc:
        return ndc
    if _NDC_PRODUCT.match(v):
        labeler, product = v.split("-")
        return labeler.zfill(5) + product.zfill(4)
    if _APP_PREFIX.match(v):
        return norm_application_number(v) or v
    return v
