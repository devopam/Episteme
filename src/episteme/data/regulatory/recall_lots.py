"""Recalled lots: lots and substances out of the free text of ``drug/enforcement``.

``extract_lots`` reads ``code_info``. It is conservative: a miss is reported as a gap, and a
wrong lot is worse. A lot needs a keyword before it (``Lot``, ``Lots``, ``Batch``, with ``#``,
``Number``, ``Code``). After the keyword it reads a list of lot-shaped tokens, and stops at the
first other word. It removes expiry and use-by phrases, dates, NDC and UPC numbers, and item
marks such as ``a)``. ``All lots ...`` gives nothing. A code with no keyword (a bare list,
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
   This is exact matching. It reads the whole description, so a diluent counts too.

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
# A wider "X through Y" is more likely a typing error than a real list, so only its two ends
# are kept. A data rule, not a tunable.
_MAX_RANGE_LOTS = 1000
_MIN_LOT_CHARS = 3
_MAX_GRAM_WORDS = 5
_MIN_NAME_CHARS = 5

_DATE_SHAPE = (
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|\d{1,2}/\d{2,4}"  # a typing error such as 12/018 is still no lot
    r"|(?:0?[1-9]|1[0-2])-(?:19\d{2}|20\d{2}|\d{2})"
    r"|(?:19|20)\d{2}[/-]\d{1,2}(?:[/-]\d{1,2})?"
)
_DATE = re.compile(rf"(?:{_DATE_SHAPE})")
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
    r"|retest)(?![A-Za-z])\.?(?:\s*dates?)?\s*[:.]?\s*"
    rf"(?:(?:{_DATE_SHAPE})(?![0-9]))?",
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
_ALL_BEFORE = re.compile(r"(?<![A-Za-z0-9])all\s*$", re.IGNORECASE)
_SPLIT = re.compile(r"[\s,;&]+|(?<![A-Za-z0-9])and(?![A-Za-z0-9])", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9@:/._+-]*")
_NDC_SHAPE = re.compile(r"\d{4}-\d{4}-\d{2}|\d{5}-\d{3}-\d{2}|\d{5}-\d{4}-\d{1,2}")
# 020917L is a lot and 5L is an amount, so a one-letter unit takes few digits.
_AMOUNT = re.compile(
    r"\d+(?:\.\d+)?-?(?:mg|mcg|kg|ml|oz|iu|ct|count|tablets?|capsules?|units?)|\d{1,3}[gl]",
    re.IGNORECASE,
)
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


# ---------------------------------------------------------------------------
# Lots (pure)
# ---------------------------------------------------------------------------
def _lot_shaped(token: str) -> bool:
    if not _TOKEN.fullmatch(token) or not any(c.isdigit() for c in token):
        return False
    if token.count("/") > 1 or _DATE.fullmatch(token):
        return False
    if _NDC_SHAPE.fullmatch(token) or _AMOUNT.fullmatch(token):
        return False
    norm = norm_lot(token)
    return is_real_lot(norm) and len(norm) >= _MIN_LOT_CHARS and fits_index(norm)


def _expand(start: str, end: str) -> list[str] | None:
    """The lots of ``start through end``; ``None`` when they are not a clear range."""
    first, last = _RANGE_END.fullmatch(start), _RANGE_END.fullmatch(end)
    if not first or not last or first.group(1) != last.group(1):
        return None
    low, high = first.group(2), last.group(2)
    if not 0 < int(high) - int(low) < _MAX_RANGE_LOTS:
        return None
    width = len(low) if low.startswith("0") or len(low) == len(high) else 0
    prefix = first.group(1)
    return [f"{prefix}{n:0{width}d}" for n in range(int(low), int(high) + 1)]


def _segment_lots(segment: str) -> Iterator[tuple[str, str]]:
    """``(lot, method)`` of the list that follows one keyword."""
    parts = _SPLIT.split(segment.replace("(", " ").replace(")", " "))
    tokens = [t for t in (p.strip(".:[]'\"") for p in parts) if t]
    seen_lot = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _DATE.fullmatch(token):
            if not seen_lot:
                return  # a date where the lot should start: the format is not clear
            i += 1
            continue
        if not _lot_shaped(token):
            return
        seen_lot = True
        follows = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        if follows in _RANGE_WORDS:
            end = tokens[i + 2] if i + 2 < len(tokens) else ""
            if not _lot_shaped(end):
                yield token, "prefix"
                return
            expanded = _expand(token, end)
            if expanded is None:
                yield token, "prefix"
                yield end, "prefix"
            else:
                for lot in expanded:
                    yield lot, "range"
            i += 3
            continue
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
    raw_words = _WORD_SPLIT.split(description or "")
    words = [w for w in (_WORD_EDGE.sub("", raw) for raw in raw_words) if w]
    for pos in range(len(words)):
        for n in range(1, min(_MAX_GRAM_WORDS, len(words) - pos) + 1):
            gram = norm_name(" ".join(words[pos : pos + n]))
            if len(gram) >= _MIN_NAME_CHARS:
                yield pos, n, gram


def pick_names(grams: Iterable[tuple[int, int, str]], known: set[str]) -> list[str]:
    """The names in ``known``, left to right: the longest match at each position, which covers
    its words, so ``metformin hydrochloride`` does not also give ``hydrochloride``."""
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
    return list(names)


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
        if recall["names"]:
            recall["method"] = "openfda"


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
    for recall in recalls:
        if not recall["names"]:
            found = set().union(*(names.get(p, ()) for p in products[recall["recall_number"]]))
            if found:
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
    ``substance_method``) and ``no_substance`` (recalls with no route to a substance)."""
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
