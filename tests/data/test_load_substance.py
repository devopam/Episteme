"""FDA substance anchor: streaming, moieties, synonyms, stable keys.

The unmarked tests are pure. The pg-marked tests run against ``episteme_test`` only;
the schema is built once per module and each test clears the substance tables.
"""

import json
import os
import zipfile
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from episteme.data.regulatory import load_substance as ls

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "src/episteme/data/db/migrations"


def _rec(unii, names, *, moiety=None, codes=(), extra_rels=()):
    rels = []
    if moiety:
        rels.append({"type": "ACTIVE MOIETY", "related_substance": {"unii": moiety, "name": "m"}})
    rels.extend(extra_rels)
    return {
        "unii": unii,
        "names": [{"name": n, "preferred": i == 0} for i, n in enumerate(names)],
        "codes": [{"code_system": s, "code": c} for s, c in codes],
        "relationships": rels,
        "moieties": [],
    }


def _zip(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("substance.json", json.dumps({"meta": {}, "results": records}))
    return path


ACETAMINOPHEN = _rec(
    "362O9ITL9D",
    ["Acetaminophen", "PARACETAMOL", "  Paracetamol  "],
    codes=[("RXCUI", "161"), ("WHO-ATC", "n02be01"), ("WHO-VATC", "QN02BE01"), ("CAS", "103-90-2")],
    extra_rels=[{"type": "ACTIVE MOIETY", "related_substance": {"unii": "362O9ITL9D"}}],
)
METFORMIN = _rec("9100L32L2N", ["Metformin"], codes=[("RXCUI", "6809")])
METFORMIN_HCL = _rec("786Z46389E", ["Metformin hydrochloride"], moiety="9100L32L2N")


# ---------------------------------------------------------------------------
# pure
# ---------------------------------------------------------------------------
def test_stream_does_not_load_the_whole_file(tmp_path, monkeypatch):
    path = _zip(tmp_path / "s.zip", [METFORMIN, METFORMIN_HCL])

    def boom(*a, **k):
        raise AssertionError("json.loads used on the whole file")

    monkeypatch.setattr(json, "loads", boom)
    assert [r["unii"] for r in ls.iter_substances(path)] == ["9100L32L2N", "786Z46389E"]


def test_moiety_of_salt():
    assert ls.moiety_of(METFORMIN_HCL) == "9100L32L2N"


def test_moiety_of_itself_when_no_relationship():
    assert ls.moiety_of(METFORMIN) == "9100L32L2N"


def test_moiety_of_ignores_a_relationship_to_itself():
    assert ls.moiety_of(ACETAMINOPHEN) == "362O9ITL9D"


def test_moiety_of_takes_first_other_active_moiety_and_ignores_ionic():
    rec = _rec(
        "SALT00000A",
        ["x"],
        extra_rels=[
            {"type": "IONIC MOIETY", "related_substance": {"unii": "IONIC00000"}},
            {"type": "ACTIVE MOIETY", "related_substance": {"unii": "SALT00000A"}},
            {"type": "ACTIVE MOIETY", "related_substance": {"unii": "FIRST00000"}},
            {"type": "ACTIVE MOIETY", "related_substance": {"unii": "SECOND0000"}},
        ],
    )
    assert ls.moiety_of(rec) == "FIRST00000"


def test_moiety_of_without_unii_is_none():
    assert ls.moiety_of({"names": []}) is None


def test_record_xrefs_codes_and_names():
    pairs, skipped = ls.record_xrefs(ACETAMINOPHEN)
    assert set(pairs) == {
        ("name", "acetaminophen"),
        ("name", "paracetamol"),
        ("rxcui", "161"),
        ("atc", "N02BE01"),
    }
    assert skipped == 0


def test_record_xrefs_skips_names_too_long_to_index():
    pairs, skipped = ls.record_xrefs(_rec("U", ["x" * (ls.MAX_NAME_BYTES + 1), "ok"]))
    assert pairs == [("name", "ok")] and skipped == 1


def test_inputs_with_one_input_key_stop_the_load(tmp_path):
    raw = tmp_path / "raw"
    a = _zip(tmp_path / "a" / "s.zip", [METFORMIN])  # both outside raw: key = s.zip
    b = _zip(tmp_path / "b" / "s.zip", [METFORMIN])
    with pytest.raises(ValueError, match="share an input_key"):
        ls.run_files(None, [a, b], raw, tmp_path / "proc", force=False, reason=None, run_id="r")


def test_substance_files_found_under_other_substance(tmp_path):
    z = _zip(tmp_path / "other" / "substance" / "substance.zip", [METFORMIN])
    assert ls.substance_files(tmp_path) == [z]


# ---------------------------------------------------------------------------
# pg (run by the controller, not in the cloud sandbox)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _schema():
    dsn = os.environ.get("TEST_PG_DSN")
    if not dsn:
        pytest.skip("TEST_PG_DSN not set")
    if conninfo_to_dict(dsn).get("dbname") != "episteme_test":
        pytest.fail("TEST_PG_DSN does not target episteme_test; refusing to DROP SCHEMA")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS episteme CASCADE")
            cur.execute((REPO_ROOT / "src/episteme/data/db/extensions.sql").read_text("utf-8"))
            cur.execute((REPO_ROOT / "src/episteme/data/db/schema.sql").read_text("utf-8"))
            for name in ("0002_container_and_book_parts.sql", "0010_regulatory_sources.sql"):
                cur.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db(_schema, pg_conn, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "test")
    from episteme.config import get_settings

    get_settings.cache_clear()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE episteme.substance_xref, episteme.substance")
    pg_conn.commit()
    yield pg_conn
    pg_conn.rollback()
    get_settings.cache_clear()


def _keys(conn, id_type, value):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT substance_key FROM episteme.substance_xref "
            "WHERE id_type = %s AND id_value = %s ORDER BY 1",
            (id_type, value),
        )
        return [r[0] for r in cur.fetchall()]


def _load(conn, tmp_path, records, name="s.zip"):
    out = ls.load_substances(conn, _zip(tmp_path / name, records), run_id="t")
    conn.commit()
    return out


@pytest.mark.pg
def test_synonyms_share_one_key(db, tmp_path):
    _load(db, tmp_path, [ACETAMINOPHEN])
    a, p = _keys(db, "name", "acetaminophen"), _keys(db, "name", "paracetamol")
    assert len(a) == 1 and a == p
    assert _keys(db, "rxcui", "161") == a and _keys(db, "atc", "N02BE01") == a
    assert _keys(db, "unii", "362O9ITL9D") == a


@pytest.mark.pg
def test_salt_groups_under_moiety_in_either_order(db, tmp_path):
    for order in ([METFORMIN, METFORMIN_HCL], [METFORMIN_HCL, METFORMIN]):
        with db.cursor() as cur:
            cur.execute("TRUNCATE episteme.substance_xref, episteme.substance")
        db.commit()
        _load(db, tmp_path, order)
        with db.cursor() as cur:
            cur.execute(
                "SELECT s.unii, m.unii FROM episteme.substance s "
                "JOIN episteme.substance m ON m.substance_key = s.moiety_key ORDER BY 1"
            )
            assert cur.fetchall() == [("786Z46389E", "9100L32L2N"), ("9100L32L2N", "9100L32L2N")]


@pytest.mark.pg
def test_moiety_without_its_own_record_still_gets_a_row(db, tmp_path):
    out = _load(db, tmp_path, [METFORMIN_HCL])
    assert out["moiety_stubs"] == 1
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM episteme.substance s JOIN episteme.substance m "
            "ON m.substance_key = s.moiety_key"
        )
        assert cur.fetchone()[0] == 2


@pytest.mark.pg
def test_keys_are_stable_across_reloads(db, tmp_path):
    _load(db, tmp_path, [ACETAMINOPHEN, METFORMIN, METFORMIN_HCL])
    with db.cursor() as cur:
        cur.execute("SELECT unii, substance_key, moiety_key FROM episteme.substance ORDER BY 1")
        before = cur.fetchall()
        cur.execute("SELECT count(*) FROM episteme.substance_xref")
        n_before = cur.fetchone()[0]
    again = _load(db, tmp_path, [METFORMIN_HCL, ACETAMINOPHEN, METFORMIN])
    with db.cursor() as cur:
        cur.execute("SELECT unii, substance_key, moiety_key FROM episteme.substance ORDER BY 1")
        assert cur.fetchall() == before
        cur.execute("SELECT count(*) FROM episteme.substance_xref")
        assert cur.fetchone()[0] == n_before
    assert again["xref_added"] == 0


@pytest.mark.pg
def test_combination_name_maps_to_two_substances(db, tmp_path):
    a = _rec("AAAAAAAAAA", ["Amlodipine", "amlodipine/benazepril"])
    b = _rec("BBBBBBBBBB", ["Benazepril", "Amlodipine/Benazepril"])
    _load(db, tmp_path, [a, b])
    assert len(_keys(db, "name", "amlodipine/benazepril")) == 2
    out = ls.resolve_names(db, ["AMLODIPINE/BENAZEPRIL"], "ema")
    db.commit()
    assert out == {"matched": 1, "unmatched": 0}
    assert len(_keys(db, "ema_name", "amlodipine/benazepril")) == 2


@pytest.mark.pg
def test_unmatched_names_are_counted_not_guessed(db, tmp_path):
    _load(db, tmp_path, [ACETAMINOPHEN])
    out = ls.resolve_names(db, ["Paracetamol", "paracetamol ", "Parcetamol", "unknownium"], "faers")
    db.commit()
    assert out == {"matched": 1, "unmatched": 2}
    assert _keys(db, "faers_name", "parcetamol") == []
    assert len(_keys(db, "faers_name", "paracetamol")) == 1


@pytest.mark.pg
def test_record_without_unii_is_skipped_and_counted(db, tmp_path):
    out = _load(db, tmp_path, [_rec("", ["nameless"]), METFORMIN])
    assert out["no_unii"] == 1 and out["substances"] == 1
