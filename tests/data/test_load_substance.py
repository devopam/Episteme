"""FDA substance anchor: streaming, moieties, synonyms, stable keys.

The unmarked tests are pure. The pg-marked tests run against ``episteme_test`` only;
the schema is built once per module and each test clears the substance tables.
"""

import contextlib
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


def test_moiety_of_cleans_both_sides():
    rec = _rec(
        "ABC", ["x"], extra_rels=[{"type": "ACTIVE MOIETY", "related_substance": {"unii": " ABC "}}]
    )
    assert ls.moiety_of({**rec, "unii": " ABC\x00"}) == "ABC"


class _Audit:
    """Stands in for ``audit_trail`` in ``run_files`` (no database)."""

    calls: list = []

    @staticmethod
    @contextlib.contextmanager
    def deferred_mirror(conn):
        yield

    @staticmethod
    def record(event, **kw):
        _Audit.calls.append(event)

    @staticmethod
    def commit(conn):
        pass

    @staticmethod
    def rollback(conn):
        pass


def _run(tmp_path, monkeypatch, files, *, force=False, database="episteme_test", in_db=True):
    loads = []
    _Audit.calls = []
    monkeypatch.setattr(ls, "audit_trail", _Audit)
    monkeypatch.setattr(ls, "database_name", lambda conn: database)
    monkeypatch.setattr(ls, "substances_loaded", lambda conn: in_db)
    monkeypatch.setattr(
        ls,
        "load_substances",
        lambda conn, path, *, run_id: (
            loads.append(path)
            or {
                "records": 1,
                "substances": 1,
                "no_unii": 0,
                "moiety_stubs": 0,
                "multi_moiety": 0,
                "long_names": 0,
                "xref_added": 1,
            }
        ),
    )
    out = ls.run_files(
        None, files, tmp_path / "raw", tmp_path / "proc", force=force, reason="why", run_id="r"
    )
    return out, loads


def test_run_files_skips_a_file_with_a_matching_marker(tmp_path, monkeypatch):
    z = _zip(tmp_path / "raw" / "other" / "substance" / "s.zip", [METFORMIN])
    first, loads = _run(tmp_path, monkeypatch, [z])
    assert first == {"loaded": 1, "skipped": 0, "failed": []} and loads == [z]
    again, loads = _run(tmp_path, monkeypatch, [z])
    assert again == {"loaded": 0, "skipped": 1, "failed": []} and loads == []


def test_run_files_loads_when_the_marker_matches_but_the_database_is_empty(
    tmp_path, monkeypatch, capsys
):
    z = _zip(tmp_path / "raw" / "other" / "substance" / "s.zip", [METFORMIN])
    _run(tmp_path, monkeypatch, [z])
    out, loads = _run(tmp_path, monkeypatch, [z], in_db=False)
    assert out == {"loaded": 1, "skipped": 0, "failed": []} and loads == [z]
    assert "holds no fda_substance rows" in capsys.readouterr().out


def test_run_files_marker_of_episteme_test_is_not_used_for_episteme(tmp_path, monkeypatch):
    z = _zip(tmp_path / "raw" / "other" / "substance" / "s.zip", [METFORMIN])
    _run(tmp_path, monkeypatch, [z], database="episteme_test")
    out, loads = _run(tmp_path, monkeypatch, [z], database="episteme")
    assert out["loaded"] == 1 and loads == [z]
    again, loads = _run(tmp_path, monkeypatch, [z], database="episteme")
    assert again["skipped"] == 1 and loads == []
    payloads = [json.loads(p.read_text("utf-8")) for p in (tmp_path / "proc").rglob("*.ok")]
    assert sorted(p["database"] for p in payloads) == ["episteme", "episteme_test"]
    assert {p["source_file"] for p in payloads} == {"other__substance__s.zip"}


def test_run_files_reloads_when_the_file_changed(tmp_path, monkeypatch):
    z = _zip(tmp_path / "raw" / "other" / "substance" / "s.zip", [METFORMIN])
    _run(tmp_path, monkeypatch, [z])
    _zip(z, [METFORMIN, METFORMIN_HCL])
    out, loads = _run(tmp_path, monkeypatch, [z])
    assert out["loaded"] == 1 and loads == [z]


def test_run_files_force_reloads_and_audits(tmp_path, monkeypatch):
    z = _zip(tmp_path / "raw" / "other" / "substance" / "s.zip", [METFORMIN])
    _run(tmp_path, monkeypatch, [z])
    out, loads = _run(tmp_path, monkeypatch, [z], force=True)
    assert out == {"loaded": 1, "skipped": 0, "failed": []} and loads == [z]
    assert "force_override" in _Audit.calls


def test_run_files_refuses_more_than_one_substance_file(tmp_path, monkeypatch, capsys):
    raw = tmp_path / "raw" / "other" / "substance"
    a, b = _zip(raw / "a.zip", [METFORMIN]), _zip(raw / "b.zip", [METFORMIN_HCL])
    out, loads = _run(tmp_path, monkeypatch, [a, b])
    assert out == {"loaded": 0, "skipped": 0, "failed": [ls.DATASET]}
    assert loads == [] and _Audit.calls == []
    assert "exactly one" in capsys.readouterr().out


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
def db(_schema, pg_conn, clean_substance, monkeypatch):
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
    assert out == {"matched": 1, "unmatched": 0, "skipped_long": 0}
    assert len(_keys(db, "ema_name", "amlodipine/benazepril")) == 2


@pytest.mark.pg
def test_unmatched_names_are_counted_not_guessed(db, tmp_path):
    _load(db, tmp_path, [ACETAMINOPHEN])
    out = ls.resolve_names(db, ["Paracetamol", "paracetamol ", "Parcetamol", "unknownium"], "faers")
    db.commit()
    assert out == {"matched": 1, "unmatched": 2, "skipped_long": 0}
    assert _keys(db, "faers_name", "parcetamol") == []
    assert len(_keys(db, "faers_name", "paracetamol")) == 1


@pytest.mark.pg
def test_record_without_unii_is_skipped_and_counted(db, tmp_path):
    out = _load(db, tmp_path, [_rec("", ["nameless"]), METFORMIN])
    assert out["no_unii"] == 1 and out["substances"] == 1


@pytest.mark.pg
def test_long_names_are_counted_as_unmatched(db, tmp_path):
    _load(db, tmp_path, [ACETAMINOPHEN])
    out = ls.resolve_names(db, ["paracetamol", "x" * (ls.MAX_NAME_BYTES + 1)], "ema")
    db.commit()
    assert out == {"matched": 1, "unmatched": 1, "skipped_long": 1}


@pytest.mark.pg
def test_reload_drops_gone_rows_and_keeps_keys(db, tmp_path):
    old_salt = _rec(
        "786Z46389E",
        ["Metformin hydrochloride", "OldName"],
        moiety="9100L32L2N",
        codes=[("RXCUI", "111")],
    )
    _load(db, tmp_path, [METFORMIN, old_salt, _rec("CCCCCCCCCC", ["Other"])])
    ls.resolve_names(db, ["OldName"], "ema")
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT unii, substance_key FROM episteme.substance")
        keys = dict(cur.fetchall())
    new_salt = _rec("786Z46389E", ["Metformin hydrochloride"], moiety="CCCCCCCCCC")
    out = _load(db, tmp_path, [METFORMIN, new_salt, _rec("CCCCCCCCCC", ["Other", "OldName"])])
    assert out["xref_removed"] >= 2
    # OldName now belongs to CCCCCCCCCC only; the dropped RxCUI is gone; no false combination
    assert _keys(db, "name", "oldname") == [keys["CCCCCCCCCC"]]
    assert _keys(db, "rxcui", "111") == []
    with db.cursor() as cur:
        cur.execute("SELECT unii, substance_key FROM episteme.substance")
        assert dict(cur.fetchall()) == keys
        cur.execute(
            "SELECT m.unii FROM episteme.substance s JOIN episteme.substance m "
            "ON m.substance_key = s.moiety_key WHERE s.unii = '786Z46389E'"
        )
        assert cur.fetchone()[0] == "CCCCCCCCCC"
        # rows of other sources survive a reload
        cur.execute("SELECT count(*) FROM episteme.substance_xref WHERE source = 'ema'")
        assert cur.fetchone()[0] == 1


@pytest.mark.pg
def test_moiety_stub_is_found_by_its_unii(db, tmp_path):
    _load(db, tmp_path, [METFORMIN_HCL])
    assert len(_keys(db, "unii", "9100L32L2N")) == 1


@pytest.mark.pg
def test_two_records_with_one_unii_use_one_row(db, tmp_path):
    first = _rec("DUPDUPDUP1", [], moiety="9100L32L2N")  # no name
    second = _rec("DUPDUPDUP1", ["Named"], moiety="9100L32L2N")
    third = _rec("DUPDUPDUP1", ["Third"], moiety="OTHEROTHER")
    _load(db, tmp_path, [first, second, third])
    with db.cursor() as cur:
        cur.execute(
            "SELECT s.preferred_name, m.unii FROM episteme.substance s JOIN episteme.substance m "
            "ON m.substance_key = s.moiety_key WHERE s.unii = 'DUPDUPDUP1'"
        )
        assert cur.fetchall() == [("Named", "9100L32L2N")]  # first row with a name


@pytest.mark.pg
def test_resolve_names_again_drops_the_stale_row(db, tmp_path):
    a = _rec("AAAAAAAAAA", ["Alpha", "shared"])
    b = _rec("BBBBBBBBBB", ["Beta"])
    _load(db, tmp_path, [a, b])
    ls.resolve_names(db, ["shared"], "ema")
    ls.resolve_names(db, ["beta"], "faers")
    db.commit()
    with db.cursor() as cur:
        cur.execute("SELECT substance_key FROM episteme.substance WHERE unii = 'BBBBBBBBBB'")
        b_key = cur.fetchone()[0]
    # the reload moves "shared" from A to B
    _load(db, tmp_path, [_rec("AAAAAAAAAA", ["Alpha"]), _rec("BBBBBBBBBB", ["Beta", "shared"])])
    ls.resolve_names(db, ["shared"], "ema")
    db.commit()
    assert _keys(db, "ema_name", "shared") == [b_key]
    assert len(_keys(db, "faers_name", "beta")) == 1  # another source's row stays


@pytest.mark.pg
def test_a_marker_with_an_empty_xref_table_loads_again(db, tmp_path):
    raw = tmp_path / "raw"
    z = _zip(raw / "other" / "substance" / "s.zip", [METFORMIN])
    proc = tmp_path / "proc"

    def run():
        out = ls.run_files(db, [z], raw, proc, force=False, reason=None, run_id="t")
        db.rollback()
        return out

    assert run()["loaded"] == 1
    assert run()["skipped"] == 1  # marker and rows agree
    (marker,) = proc.rglob("*.ok")
    assert marker.name.startswith("episteme_test_")  # the database name is in the key
    with db.cursor() as cur:  # the rows go (a reset, or a marker from elsewhere)
        cur.execute("TRUNCATE episteme.substance_xref, episteme.substance")
    db.commit()
    assert run()["loaded"] == 1
    assert len(_keys(db, "name", "metformin")) == 1


@pytest.mark.pg
def test_a_marker_made_for_episteme_test_is_not_used_for_episteme(db, tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    z = _zip(raw / "other" / "substance" / "s.zip", [METFORMIN])
    proc = tmp_path / "proc"

    def run():
        out = ls.run_files(db, [z], raw, proc, force=False, reason=None, run_id="t")
        db.rollback()
        return out

    assert run()["loaded"] == 1  # the real episteme_test
    monkeypatch.setattr(ls, "database_name", lambda conn: "episteme")
    assert run()["loaded"] == 1  # the rows are there, but this database's marker is not
    assert run()["skipped"] == 1
    assert len(list(proc.rglob("*.ok"))) == 2
