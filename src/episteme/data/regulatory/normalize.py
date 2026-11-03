"""Normalisers for reference-feed values: names, NDC, application numbers, lots.

Pure functions, no I/O. The loader stores the normalised forms in ``reg_record``
(``substance_names``, ``product_keys``); the raw values stay in ``payload``.
Salts and esters are NOT folded into their parent here: ``metformin hcl`` and
``metformin`` stay two names. Joining them is the job of ``substance_xref``.
"""

from __future__ import annotations

import re
import unicodedata

# A btree entry must fit in a page (about 2.7 kB). Text longer than this cannot be an
# index key, so a loader skips or blanks it, and counts it. A constant, not a setting: it
# is a page limit tied to the schema, not an operator choice.
MAX_INDEX_TEXT_BYTES = 2000

# Batch fields that say "no lot", in the form norm_lot gives. A data rule, not a tunable.
# The two misspellings occur in the FAERS samples. A stored batch_norm keeps these values:
# is_real_lot decides what counts as a lot.
PLACEHOLDER_LOTS = frozenset(
    {
        "ASKU",
        "NA",
        "NI",
        "NONE",
        "NOTAVAILABLE",
        "NOTKNOWN",
        "NOTREPORTED",
        "NOTSTATED",
        "UNK",
        "UNKNOWN",
        "UNKNWON",
        "UNKONWN",
    }
)

_NAME_SEPARATORS = re.compile(r"[|;]")
_SPACES = re.compile(r"\s+")
_NDC_SEGMENTS = re.compile(r"^\d{1,5}-\d{1,4}-\d{1,2}$")
_NDC_PRODUCT = re.compile(r"^\d{1,5}-\d{1,4}$")
_APP_PREFIX = re.compile(r"^(?:ANDA|NDA|BLA)\s*", re.IGNORECASE)


def fits_index(s: str) -> bool:
    """Whether ``s`` is short enough to be a btree key (``MAX_INDEX_TEXT_BYTES``)."""
    return len(s.encode("utf-8")) <= MAX_INDEX_TEXT_BYTES


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


def norm_ndc_product(s: str) -> str | None:
    """The 9-digit labeler-product key of an NDC (5 + 4 digits, zero-padded): from a
    ``labeler-product`` code, from a full ``labeler-product-package`` NDC, from 11 digits
    (the first 9) or from 9 digits. ``None`` when ``s`` is none of these. A product key
    has 9 digits; a package key (``norm_ndc``) has 11."""
    v = str(s).strip()
    if _NDC_PRODUCT.match(v):
        labeler, product = v.split("-")
        return labeler.zfill(5) + product.zfill(4)
    full = norm_ndc(v)
    if full:
        return full[:9]
    if re.fullmatch(r"\d{9}", v):
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


def is_real_lot(lot_norm: str | None) -> bool:
    """Whether a ``norm_lot`` value names a lot, and is not a placeholder such as ``UNK``."""
    return bool(lot_norm) and lot_norm not in PLACEHOLDER_LOTS


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
