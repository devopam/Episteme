"""Recalled lots: lots and substances out of the free text of ``drug/enforcement``.

``extract_lots`` reads ``code_info``. It is conservative: a miss is reported as a gap, and a
wrong lot is worse. A lot needs a keyword before it (``Lot``, ``Lots``, ``Batch``, with ``#``,
``Number``, ``Code``). After the keyword it reads a list of lot-shaped tokens. A refused token
with a digit is skipped; the first word with no digit ends the list, and so does a number with a
unit. Directly after the keyword, all digits are enough from 4 digits; later in the list, an
all-digit token needs 5, and not 10 or 11. It removes expiry and use-by phrases, dates, NDC and
UPC numbers, and item marks such as ``a)``. ``All lots ...`` gives nothing. A code with no
keyword (a bare list,
``Pedigree:``, ``Rx #``) gives nothing, and the recall counts as having no lot. A range expands
only when both ends are written, with one prefix and a numeric tail.

``build_recall_lots`` writes one ``recall_lot`` row per lot, and one NULL-lot row
(``method='none'``) for a recall with no lot. It finds the substances of each recall by the
first of these that gives a name, and records which in ``substance_method``:

1. ``openfda``: the record's normalised names, or the names of its ``openfda.unii``.
2. ``ndc``: an NDC in ``product_description`` or ``code_info``, at product level (the first 9
   digits of the 11-digit form), against the latest ``fda_ndc`` release.
3. ``product_text``: word n-grams of up to 5 words from ``product_description``, normalised,
   looked up exactly against the ``name`` rows of ``substance_xref``. Names shorter than 5
   characters are ignored. At each position the longest match wins, and it covers its words.
   Reading stops at ``Manufactured by`` and similar words. A vehicle (``VEHICLE_NAMES``) goes
   when another name remains. This is exact matching.

A route whose names all have no key in ``substance_xref`` gives way to the next. A recall that
no route ties to a key has NULL names, and counts in ``no_substance``.

``lot_matches`` joins recalls to FAERS batches: same moiety and same normalised lot. It always
returns the coverage, because zero matches is when the coverage matters most.

``reg_record`` is read with ``dataset`` and ``release_id`` as values and no join to
``reg_release``, so the plan prunes to one partition.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator

import psycopg

from episteme import audit_trail
from episteme.config import get_settings
from episteme.data.regulatory.load_reg import latest_release
from episteme.data.regulatory.normalize import (
    PLACEHOLDER_LOTS,
    fits_index,
    is_real_lot,
    norm_lot,
    norm_name,
    norm_ndc,
)

SCHEMA = "episteme"
RECALL_DATASET = "fda_enforcement"
NDC_DATASET = "fda_ndc"
_MIN_LOT_CHARS = 3
# An all-digit token this short is too often an amount, a time or a year to be taken as a lot.
_MIN_DIGIT_LOT_CHARS = 5
# Directly after a keyword, all digits need only this many.
_MIN_FIRST_DIGIT_LOT_CHARS = 4
# An all-digit token of these lengths has the shape of an NDC or a phone number.
_NDC_DIGIT_LENGTHS = (10, 11)
# An all-digit range "12345-12350" needs this width; a shorter pair is more likely one lot.
_MIN_DIGIT_RANGE_WIDTH = 5
_MAX_GRAM_WORDS = 5
_MIN_NAME_CHARS = 5

_DATE_SHAPE = (
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{1,2}/\d{2,4}"  # a typing error such as 12/018 is still no lot
    r"|(?:0?[1-9]|1[0-2])-(?:19\d{2}|20\d{2}|\d{2})"
    r"|(?:19|20)\d{2}[/-]\d{1,2}(?:[/-]\d{1,2})?"
    r"|\d{1,2}\.\d{1,2}\.\d{2,4}"
    r"|\d{1,2}\.\d{4}"
    r"|(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
)
_DATE = re.compile(rf"(?:{_DATE_SHAPE})")
# "Exp12/25" is a date with its word glued on; "EXP123" is a lot.
_GLUED_WORD = re.compile(r"(?:exp|bud)\.?", re.IGNORECASE)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_MONTH_DATE = re.compile(
    rf"(?<![A-Za-z0-9])(?:\d{{1,2}}[\s-]*)?{_MONTH}\.?[\s,/-]*(?:\d{{1,2}},?\s+)?(?:\d{{4}}|\d{{2}})"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# An expiry or use-by phrase, with the date that follows it.
_EXPIRY = re.compile(
    r"(?<![A-Za-z0-9])(?:expiration|expires?|expiry|exp"
    r"|beyond\s+use|bud|best\s+used\s+by|best\s+by|use\s+by|discard(?:\s+by|\s+after)?"
    r"|retest)(?![A-Za-z0-9])\.?(?:\s*dates?)?\s*[:.]?\s*"
    rf"(?:(?:{_DATE_SHAPE}|(?:19|20)\d{{2}})(?![0-9A-Za-z]))?",
    re.IGNORECASE,
)
_OTHER_CODE = re.compile(
    r"(?<![A-Za-z0-9])(?:ndc|upc|sku)s?\s*[#:]*\s*[\d][\d\s-]*\d", re.IGNORECASE
)
_ITEM_MARK = re.compile(r"(?<![A-Za-z0-9])[a-z]\)", re.IGNORECASE)
_KEYWORD = re.compile(
    r"(?<![A-Za-z0-9])(?:lots?|batch(?:es)?)(?![A-Za-z0-9])(?:\(s\))?"
    r"(?:\s*(?:numbers?|num\b|no'?s?\b|codes?|#'?s?))*[\s#:.,;/-]*",
    re.IGNORECASE,
)
# "All lots", "Multiple lots": the words after the keyword do not name lots.
_ALL_BEFORE = re.compile(r"(?<![A-Za-z0-9])(?:all|multiple|various)\s*$", re.IGNORECASE)
_SPLIT = re.compile(r"[\s,;&]+|(?<![A-Za-z0-9])and(?![A-Za-z0-9])", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9@:/._+-]*")
_NDC_SHAPE = re.compile(r"\d{4}-\d{4}-\d{2}|\d{5}-\d{3}-\d{2}|\d{5}-\d{4}-\d{1,2}")
# 020917L is a lot and 5L is an amount, so a one-letter unit takes few digits.
_AMOUNT = re.compile(
    r"\d+(?:\.\d+)?-?(?:mg|mcg|kg|ml|oz|iu|ct|count|tablets?|capsules?|units?)|\d{1,3}[gl]",
    re.IGNORECASE,
)
# A plain number before one of these is an amount: "500 mg", "100 tablets".
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_UNIT_WORD = re.compile(
    r"(?:mg|mcg|kg|g|ml|l|oz|iu|ct|count|tablets?|tabs?|capsules?|caps?|units?|vials?|bottles?"
    r"|bags?|syringes?|ampules?|packs?|cartons?|boxes|tubes?)(?![A-Za-z])|%",
    re.IGNORECASE,
)
_PHONE = re.compile(r"(?:1-)?\d{3}-\d{3}-\d{4}|\d{3}-\d{4}|\d{3}\.\d{3}\.\d{4}")
_RANGE_WORDS = {"through", "thru", "to", "-"}
_RANGE_END = re.compile(r"(.*?)(\d+)")
_NDC_IN_TEXT = re.compile(
    r"(?<![\d-])(?:\d{4}-\d{4}-\d{2}|\d{5}-\d{3}-\d{2}|\d{5}-\d{4}-\d{1,2})(?![\d-])"
)
# With no hyphens an NDC needs its keyword: eleven digits alone could be anything.
_NDC_11_DIGITS = re.compile(r"(?<![A-Za-z0-9])NDC[#:\s]*(\d{11})(?!\d)", re.IGNORECASE)
# A slash joins ingredients: "Estradiol/Testosterone".
_WORD_SPLIT = re.compile(r"[\s/]+")
_WORD_EDGE = re.compile(r"^[^\w]+|[^\w%]+$")
# The maker's name and address follow these words, and they hold no ingredient.
_MAKER_WORDS = re.compile(
    r"(?<![A-Za-z])(?:(?:re)?packaged|packed|manufactured|mfg|mfd|mfr|distributed|dist|marketed"
    r"|made|produced|compounded|prepared|sold)[.,:]*\s+(?:by|for)(?![A-Za-z])"
    r"|(?<![A-Za-z])made\s+in(?![A-Za-z])",
    re.IGNORECASE,
)
# What a drug is dissolved in. A data rule, not a tunable: exact names, dropped from a
# recall's text match only when another name remains, so that a recall of the vehicle itself
# ("0.9% Sodium Chloride Irrigation") keeps it.
VEHICLE_NAMES = frozenset(
    {
        "bacteriostatic water",
        "dextrose",
        "sodium chloride",
        "sterile water",
        "sterile water for injection",
        "water",
        "water for injection",
    }
)


# ---------------------------------------------------------------------------
# Lots (pure)
# ---------------------------------------------------------------------------
def _is_date(token: str) -> bool:
    bare = _GLUED_WORD.match(token)
    return bool(_DATE.fullmatch(token) or (bare and _DATE.fullmatch(token[bare.end() :])))


def _is_amount_at(tokens: list[str], i: int) -> bool:
    """Whether ``tokens[i]`` is a number with a unit word after it."""
    return (
        i + 1 < len(tokens)
        and _NUMBER.fullmatch(tokens[i]) is not None
        and _UNIT_WORD.match(tokens[i + 1]) is not None
    )


def _lot_shaped(token: str) -> bool:
    if not _TOKEN.fullmatch(token) or not any(c.isdigit() for c in token):
        return False
    if token.count("/") > 1 or _is_date(token):
        return False
    if _NDC_SHAPE.fullmatch(token) or _AMOUNT.fullmatch(token) or _PHONE.fullmatch(token):
        return False
    if token.isdigit() and (len(token) < _MIN_DIGIT_LOT_CHARS or len(token) in _NDC_DIGIT_LENGTHS):
        return False
    norm = norm_lot(token)
    return is_real_lot(norm) and len(norm) >= _MIN_LOT_CHARS and fits_index(norm)


def _expand(start: str, end: str) -> list[str] | None:
    """The lots of ``start through end``; ``None`` when they are not a clear range."""
    first, last = _RANGE_END.fullmatch(start), _RANGE_END.fullmatch(end)
    if not first or not last or first.group(1) != last.group(1):
        return None
    low, high = first.group(2), last.group(2)
    if not 0 < int(high) - int(low) < get_settings().recall_max_range_lots:
        return None
    width = len(low) if low.startswith("0") or len(low) == len(high) else 0
    prefix = first.group(1)
    return [f"{prefix}{n:0{width}d}" for n in range(int(low), int(high) + 1)]


def _hyphen_ends(token: str) -> tuple[str, str] | None:
    """The two ends of ``AB100-AB105``; ``None`` for one lot with a hyphen in it. Both ends need
    one prefix and a numeric tail. With no prefix they need the same width and a rise, because
    ``37-888833`` and ``123-456`` are ordinary lots."""
    start, sep, end = token.partition("-")
    if not sep or "-" in end:
        return None
    first, last = _RANGE_END.fullmatch(start), _RANGE_END.fullmatch(end)
    if not first or not last or first.group(1) != last.group(1):
        return None
    if not first.group(1):
        low, high = first.group(2), last.group(2)
        if len(low) != len(high) or len(low) < _MIN_DIGIT_RANGE_WIDTH or int(high) <= int(low):
            return None
    return (start, end) if _lot_shaped(start) and _lot_shaped(end) else None


def _range_lots(start: str, end: str) -> Iterator[tuple[str, str]]:
    expanded = _expand(start, end)
    if expanded is None:
        yield start, "prefix"
        yield end, "prefix"
    else:
        for lot in expanded:
            yield lot, "range"


def _list_tokens(segment: str) -> list[str]:
    """The tokens of a list. A slash between two parts with digits separates two lots:
    ``1234/5678``. ``LD/120417`` is one lot, and ``12/25`` a date."""
    parts = _SPLIT.split(segment.replace("(", " ").replace(")", " "))
    tokens: list[str] = []
    for token in (p.strip(".:[]'\"") for p in parts):
        left, slash, right = token.partition("/")
        two = (
            slash
            and any(c.isdigit() for c in left)
            and any(c.isdigit() for c in right)
            and "/" not in right
        )
        if two and not _is_date(token):
            tokens += [left, right]
        elif token:
            tokens.append(token)
    return tokens


def _first_token_lot(token: str) -> bool:
    """The token directly after a keyword has the keyword as its evidence, so all digits are
    enough: ``Lot 9781``, ``Lot 1223048610``."""
    return token.isdigit() and len(token) >= _MIN_FIRST_DIGIT_LOT_CHARS and fits_index(token)


def _segment_lots(segment: str) -> Iterator[tuple[str, str]]:
    """``(lot, method)`` of the list that follows one keyword. A refused token that holds a digit
    is skipped, and the list goes on. A number with a unit ends it, and so does a word."""
    tokens = _list_tokens(segment)
    seen_lot = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _is_amount_at(tokens, i):
            return
        direct = i == 0 and _first_token_lot(token)
        if not direct and _is_date(token):
            if not seen_lot:
                return  # a date where the lot should start: the format is not clear
            i += 1
            continue
        if not direct and not _lot_shaped(token):
            if not any(c.isdigit() for c in token):
                return  # prose: a later code such as "Rx: 37-74714-0-12-R" is no lot
            i += 1
            continue
        seen_lot = True
        follows = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        if follows in _RANGE_WORDS:
            end = tokens[i + 2] if i + 2 < len(tokens) else ""
            if _is_amount_at(tokens, i + 2):
                return  # "12345 - 12400 mg" is an amount, both ends of it
            if not _lot_shaped(end):
                yield token, "prefix"
                return
            yield from _range_lots(token, end)
            i += 3
            continue
        ends = _hyphen_ends(token)
        if ends is not None:
            yield from _range_lots(*ends)
        else:
            yield token, "prefix"
        i += 1


def _lots_with_method(code_info: str | None) -> list[tuple[str, str]]:
    """``(lot, method)`` in text order, one per normalised lot."""
    text = code_info or ""
    text = _OTHER_CODE.sub(" ", text)
    text = _MONTH_DATE.sub(" ", text)
    text = _EXPIRY.sub(" ", text)
    text = _ITEM_MARK.sub(" ", text)
    keywords = list(_KEYWORD.finditer(text))
    out: dict[str, tuple[str, str]] = {}
    for n, match in enumerate(keywords):
        if _ALL_BEFORE.search(text[: match.start()]):
            continue
        stop = keywords[n + 1].start() if n + 1 < len(keywords) else len(text)
        for lot, method in _segment_lots(text[match.end() : stop]):
            out.setdefault(norm_lot(lot), (lot, method))
    return list(out.values())


def extract_lots(code_info: str) -> list[str]:
    """The lots written in ``code_info``, as written; see the module docstring."""
    return [lot for lot, _ in _lots_with_method(code_info)]


def lot_rows(
    recall_number: str,
    release_id: str,
    names: list[str] | None,
    code_info: str | None,
    substance_method: str | None,
) -> list[tuple]:
    """The ``recall_lot`` rows of one recall, in column order: ``recall_number``,
    ``release_id``, ``substance_names``, ``lot_raw``, ``lot_norm``, ``method``,
    ``substance_method``. A recall with no lot has one row with NULL lot."""
    lots = _lots_with_method(code_info)
    if not lots:
        return [(recall_number, release_id, names, None, None, "none", substance_method)]
    return [
        (recall_number, release_id, names, lot, norm_lot(lot), method, substance_method)
        for lot, method in lots
    ]


# ---------------------------------------------------------------------------
# Substances (pure)
# ---------------------------------------------------------------------------
def find_ndc_products(text: str | None) -> list[str]:
    """The distinct NDC products in ``text``: the first 9 digits of each 11-digit form."""
    out: dict[str, None] = {}
    for found in (*_NDC_IN_TEXT.findall(text or ""), *_NDC_11_DIGITS.findall(text or "")):
        full = norm_ndc(found)
        if full:
            out[full[:9]] = None
    return list(out)


def name_grams(description: str | None) -> Iterator[tuple[int, int, str]]:
    """``(word position, words, gram)`` of each n-gram that could be a name."""
    text = description or ""
    maker = _MAKER_WORDS.search(text)
    raw_words = _WORD_SPLIT.split(text[: maker.start()] if maker else text)
    words = [w for w in (_WORD_EDGE.sub("", raw) for raw in raw_words) if w]
    for pos in range(len(words)):
        for n in range(1, min(_MAX_GRAM_WORDS, len(words) - pos) + 1):
            gram = norm_name(" ".join(words[pos : pos + n]))
            if len(gram) >= _MIN_NAME_CHARS:
                yield pos, n, gram


def pick_names(grams: Iterable[tuple[int, int, str]], known: set[str]) -> list[str]:
    """The names in ``known``, left to right: the longest match at each position, which covers
    its words, so ``metformin hydrochloride`` does not also give ``hydrochloride``. A vehicle goes
    when another name remains."""
    best: dict[int, tuple[int, str]] = {}
    for pos, n, gram in grams:
        if gram in known and n > best.get(pos, (0, ""))[0]:
            best[pos] = (n, gram)
    names: dict[str, None] = {}
    covered = 0
    for pos in sorted(best):
        if pos < covered:
            continue
        n, gram = best[pos]
        names[gram] = None
        covered = pos + n
    return [n for n in names if n not in VEHICLE_NAMES] or list(names)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def _read_recalls(cur: psycopg.Cursor, release_id: str) -> list[dict]:
    cur.execute(
        "SELECT COALESCE(payload ->> 'recall_number', record_key), substance_names, "
        "payload ->> 'code_info', payload ->> 'product_description', "
        "payload #> '{openfda,unii}' "
        f"FROM {SCHEMA}.reg_record WHERE dataset = %s AND release_id = %s ORDER BY record_key",
        (RECALL_DATASET, release_id),
    )
    return [
        {
            "recall_number": number,
            "names": names or None,
            "code_info": code_info,
            "description": description,
            "uniis": uniis if isinstance(uniis, list) else [],
        }
        for number, names, code_info, description, uniis in cur.fetchall()
    ]


def _names_with_a_key(cur: psycopg.Cursor, names: set[str]) -> set[str]:
    """Those of ``names`` that ``substance_xref`` knows. A route whose names have no key gives
    no route to a moiety, so the next route is tried."""
    cur.execute(
        f"SELECT DISTINCT id_value FROM {SCHEMA}.substance_xref "
        "WHERE id_type = 'name' AND id_value = ANY(%s::text[])",
        (sorted(names),),
    )
    return {row[0] for row in cur.fetchall()}


def _resolve_uniis(cur: psycopg.Cursor, recalls: list[dict]) -> None:
    """Method ``openfda``: the record's names, or the preferred names of its UNIIs."""
    wanted = sorted({u for r in recalls if not r["names"] for u in r["uniis"]})
    cur.execute(
        f"SELECT unii, preferred_name FROM {SCHEMA}.substance "
        "WHERE unii = ANY(%s::text[]) AND preferred_name IS NOT NULL",
        (wanted,),
    )
    names = {unii: norm_name(name) for unii, name in cur.fetchall()}
    for recall in recalls:
        if not recall["names"]:
            recall["names"] = sorted({names[u] for u in recall["uniis"] if u in names}) or None
    known = _names_with_a_key(cur, {n for r in recalls for n in r["names"] or ()})
    for recall in recalls:
        if known.intersection(recall["names"] or ()):
            recall["method"] = "openfda"
        else:
            recall["names"] = None


def _resolve_ndcs(cur: psycopg.Cursor, recalls: list[dict]) -> None:
    """Method ``ndc``: the names of the product in the latest ``fda_ndc`` release."""
    release = latest_release(cur.connection, NDC_DATASET)
    if release is None:
        return
    products = {}
    for recall in recalls:
        if not recall["names"]:
            text = f"{recall['description'] or ''} {recall['code_info'] or ''}"
            products[recall["recall_number"]] = find_ndc_products(text)
    wanted = sorted({p for found in products.values() for p in found})
    cur.execute(
        f"SELECT k.key, r.substance_names FROM {SCHEMA}.reg_record r "
        "CROSS JOIN LATERAL unnest(r.product_keys) AS k(key) "
        "WHERE r.dataset = %s AND r.release_id = %s AND r.product_keys && %s::text[] "
        "AND k.key = ANY(%s::text[]) AND r.substance_names IS NOT NULL",
        (NDC_DATASET, release, wanted, wanted),
    )
    names: dict[str, set[str]] = {}
    for key, found in cur.fetchall():
        names.setdefault(key, set()).update(found)
    known = _names_with_a_key(cur, set().union(*names.values()))
    for recall in recalls:
        if not recall["names"]:
            found = set().union(*(names.get(p, ()) for p in products[recall["recall_number"]]))
            if known.intersection(found):
                recall["names"], recall["method"] = sorted(found), "ndc"


def _resolve_text(cur: psycopg.Cursor, recalls: list[dict]) -> None:
    """Method ``product_text``. One lookup of all distinct grams, not one per recall."""
    open_recalls = [r for r in recalls if not r["names"]]
    grams = {id(r): list(name_grams(r["description"])) for r in open_recalls}
    cur.execute("DROP TABLE IF EXISTS pg_temp._recall_gram")
    cur.execute("CREATE TEMP TABLE _recall_gram (gram text) ON COMMIT DROP")
    distinct = {g for found in grams.values() for _, _, g in found if fits_index(g)}
    with cur.copy("COPY _recall_gram FROM STDIN") as cp:
        for gram in distinct:
            cp.write_row((gram,))
    cur.execute("ANALYZE _recall_gram")
    cur.execute(
        f"SELECT DISTINCT g.gram FROM _recall_gram g JOIN {SCHEMA}.substance_xref x "
        "ON x.id_type = 'name' AND x.id_value = g.gram"
    )
    known = {row[0] for row in cur.fetchall()}
    for recall in open_recalls:
        found = pick_names(grams[id(recall)], known)
        if found:
            recall["names"], recall["method"] = found, "product_text"


def build_recall_lots(
    conn: psycopg.Connection, release_id: str, *, run_id: str | None = None
) -> dict:
    """Replace the ``recall_lot`` rows of one ``fda_enforcement`` release. The caller commits.

    Returns ``recalls``, ``with_lot``, ``no_lot``, ``lots``; ``by_method`` (recalls per
    ``substance_method``) and ``no_substance`` (recalls whose names reach no substance key)."""
    with conn.transaction(), conn.cursor() as cur:
        recalls = _read_recalls(cur, release_id)
        for recall in recalls:
            recall["method"] = None
        _resolve_uniis(cur, recalls)
        _resolve_ndcs(cur, recalls)
        _resolve_text(cur, recalls)
        cur.execute(f"DELETE FROM {SCHEMA}.recall_lot WHERE release_id = %s", (release_id,))
        lots = with_lot = 0
        with cur.copy(
            f"COPY {SCHEMA}.recall_lot (recall_number, release_id, substance_names, lot_raw, "
            "lot_norm, method, substance_method) FROM STDIN"
        ) as cp:
            for recall in recalls:
                rows = lot_rows(
                    recall["recall_number"],
                    release_id,
                    recall["names"],
                    recall["code_info"],
                    recall["method"],
                )
                if rows[0][4] is not None:
                    with_lot += 1
                    lots += len(rows)
                for row in rows:
                    cp.write_row(row)
        audit_trail.record(
            "load_commit",
            conn=conn,
            object=f"recall_lot {release_id}",
            rows_affected=lots,
            run_id=run_id,
        )
    by_method = Counter(r["method"] for r in recalls if r["method"])
    return {
        "recalls": len(recalls),
        "with_lot": with_lot,
        "no_lot": len(recalls) - with_lot,
        "lots": lots,
        "by_method": dict(by_method),
        "no_substance": sum(1 for r in recalls if not r["method"]),
    }


def lot_matches(conn: psycopg.Connection, moiety_key: int) -> dict:
    """Recalled lots of a moiety that FAERS batches name: same moiety, same normalised lot.

    Returns ``release_id`` (the latest ``fda_enforcement``, ``None`` when there is none);
    ``recall_lots_built``, false when that release has no ``recall_lot`` rows, so that zero
    recalls is not read as "no recall";
    ``matches``, each with ``recall_number``, ``lot_raw``, ``lot_norm``, ``method``,
    ``substance_method`` (how strong the link to the moiety is), ``reports``, ``first_quarter``,
    ``last_quarter``; ``batch_coverage`` (``drug_rows``, ``with_batch``, over all quarters);
    for this moiety ``recalls``, ``recalls_no_lot``, ``recalls_by_method``; and over the whole
    release ``recalls_no_substance``, the recalls that cannot be tied to any moiety. Leaves a
    read transaction open on ``conn``: the caller ends it."""
    release = latest_release(conn, RECALL_DATASET)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(sum(drug_rows), 0)::bigint, COALESCE(sum(with_batch), 0)::bigint "
            f"FROM {SCHEMA}.faers_batch_coverage WHERE substance_key = %s",
            (moiety_key,),
        )
        drug_rows, with_batch = cur.fetchone()
        out: dict = {
            "release_id": release,
            "recall_lots_built": False,
            "matches": [],
            "batch_coverage": {"drug_rows": drug_rows, "with_batch": with_batch},
            "recalls": 0,
            "recalls_no_lot": 0,
            "recalls_by_method": {},
            "recalls_no_substance": 0,
        }
        if release is None:
            return out
        cur.execute(
            f"SELECT EXISTS (SELECT 1 FROM {SCHEMA}.recall_lot WHERE release_id = %s)", (release,)
        )
        out["recall_lots_built"] = cur.fetchone()[0]
        cur.execute(
            f"SELECT DISTINCT x.id_value FROM {SCHEMA}.substance s "
            f"JOIN {SCHEMA}.substance_xref x ON x.substance_key = s.substance_key "
            "WHERE COALESCE(s.moiety_key, s.substance_key) = %s AND x.id_type = 'name'",
            (moiety_key,),
        )
        names = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT r.recall_number, r.lot_raw, r.lot_norm, r.method, r.substance_method, "
            "sum(b.reports)::bigint, min(b.quarter), max(b.quarter) "
            f"FROM {SCHEMA}.recall_lot r JOIN {SCHEMA}.faers_batch_quarter b "
            "ON b.substance_key = %s AND b.batch_norm = r.lot_norm "
            "WHERE r.release_id = %s AND r.substance_names && %s::text[] "
            "AND r.lot_norm <> ALL(%s::text[]) "
            "GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 3",
            (moiety_key, release, names, sorted(PLACEHOLDER_LOTS)),
        )
        keys = (
            "recall_number",
            "lot_raw",
            "lot_norm",
            "method",
            "substance_method",
            "reports",
            "first_quarter",
            "last_quarter",
        )
        out["matches"] = [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]
        cur.execute(
            "SELECT substance_method, count(DISTINCT recall_number), "
            "count(DISTINCT recall_number) FILTER (WHERE lot_norm IS NULL) "
            f"FROM {SCHEMA}.recall_lot WHERE release_id = %s AND substance_names && %s::text[] "
            "GROUP BY 1",
            (release, names),
        )
        for method, recalls, no_lot in cur.fetchall():
            out["recalls_by_method"][method] = recalls
            out["recalls"] += recalls
            out["recalls_no_lot"] += no_lot
        cur.execute(
            f"SELECT count(DISTINCT recall_number) FROM {SCHEMA}.recall_lot "
            "WHERE release_id = %s AND substance_names IS NULL",
            (release,),
        )
        out["recalls_no_substance"] = cur.fetchone()[0]
    return out
