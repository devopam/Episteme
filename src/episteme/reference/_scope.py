"""Shared read-only helpers of the reference lookups: the substance family scope and
the availability checks of a source (relation, latest release).

Family scope: the input's moiety ``M = COALESCE(moiety_key, substance_key)``, and every
substance whose own ``COALESCE(moiety_key, substance_key)`` is ``M`` (the moiety, its
salts and esters). Every PBRER lookup uses this one scope, so all sections of a draft
cover the same substances; the FAERS signal tables count the same family as one moiety.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from episteme.data.regulatory.normalize import norm_name

SCHEMA = "episteme"
# xref id types that hold a normalised name. ema_name / aact_name rows repeat values of
# name rows (resolve_names adds them only on an exact match), so name is enough.
NAME_ID_TYPES = ("name",)

# COALESCE(moiety_key, substance_key) = M, written so that substance_moiety_key_idx and
# the primary key can serve it.
_FAMILY_SQL = """
SELECT substance_key, unii, preferred_name FROM episteme.substance
WHERE moiety_key = %(m)s OR (moiety_key IS NULL AND substance_key = %(m)s)
ORDER BY substance_key
"""


@dataclass(frozen=True)
class SubstanceScope:
    """The keys, UNIIs and normalised names of a substance's moiety family."""

    substance_key: int
    moiety_key: int
    keys: tuple[int, ...]
    uniis: tuple[str, ...]
    names: tuple[str, ...]


def relation_exists(conn: psycopg.Connection, name: str) -> bool:
    """Whether ``name`` (``schema.relation``) exists, by ``to_regclass``."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
        return bool(cur.fetchone()[0])


def latest_release(conn: psycopg.Connection, dataset: str) -> str | None:
    """The ``is_latest`` ``release_id`` of ``dataset``, or ``None`` (also when
    ``reg_release`` does not exist)."""
    if not relation_exists(conn, f"{SCHEMA}.reg_release"):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT release_id FROM episteme.reg_release WHERE dataset = %s AND is_latest",
            (dataset,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def substance_scope(conn: psycopg.Connection, substance_key: int) -> SubstanceScope | None:
    """The moiety family of ``substance_key``, or ``None`` when the substance tables or
    the substance do not exist. Names: the preferred names (``norm_name`` form, as
    ``reg_record.substance_names``) and the ``name`` xref rows of every family member."""
    if not (
        relation_exists(conn, f"{SCHEMA}.substance")
        and relation_exists(conn, f"{SCHEMA}.substance_xref")
    ):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(moiety_key, substance_key) FROM episteme.substance "
            "WHERE substance_key = %s",
            (substance_key,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        moiety = int(row[0])
        cur.execute(_FAMILY_SQL, {"m": moiety})
        family = cur.fetchall()
        keys = tuple(dict.fromkeys([int(substance_key)] + [int(k) for k, _, _ in family]))
        cur.execute(
            "SELECT DISTINCT id_value FROM episteme.substance_xref "
            "WHERE substance_key = ANY(%s) AND id_type = ANY(%s) ORDER BY id_value",
            (list(keys), list(NAME_ID_TYPES)),
        )
        xref_names = [r[0] for r in cur.fetchall()]
    names = [norm_name(p) for _, _, p in family if p] + xref_names
    return SubstanceScope(
        substance_key=int(substance_key),
        moiety_key=moiety,
        keys=keys,
        uniis=tuple(u for _, u, _ in family if u),
        names=tuple(n for n in dict.fromkeys(names) if n),
    )
