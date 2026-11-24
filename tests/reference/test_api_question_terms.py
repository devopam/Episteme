"""match_terms and suggest_terms (migration 0018) on fixture mesh_term and mesh_term_vec
rows (pg: episteme_test only). The descriptor UIs D9xxxxx are fixture values."""

from __future__ import annotations

import pytest

from episteme.reference import api_settings

pytestmark = pytest.mark.pg

_RELEASE = "2026"
_REV = "rev-a"
_SRC = "mesh/desc2026.gz"

# (descriptor_ui, term, term_kind, is_preferred, concept_relation)
_TERMS = [
    ("D9000001", "Stevens-Johnson Syndrome", "name", True, None),
    ("D9000001", "Stevens-Johnson Syndrome", "entry", True, None),
    ("D9000001", "SJS", "entry", True, None),
    ("D9000002", "Amoxicillin", "name", True, None),
    ("D9000003", "Chemical and Drug Induced Liver Injury", "name", True, None),
    ("D9000003", "Drug-Induced Liver Injury", "entry", True, None),
    ("D9000004", "Liver Injury", "name", True, None),
    ("D9000005", "Myocardial Infarction", "name", True, None),
    ("D9000005", "MI", "entry", True, None),
    ("D9000006", "Mitral Insufficiency", "name", True, None),
    ("D9000006", "MI", "entry", True, None),
    ("D9000007", "Anaphylaxis", "name", True, None),
    ("D9000007", "Anaphylactic Shock", "entry", False, "NRW"),
    # An entry term equal to an English stop word ("as"): never matched from one word.
    ("D9000008", "Ankylosing Spondylitis", "name", True, None),
    ("D9000008", "AS", "entry", True, None),
]

_NO_WORDS = "question has no searchable words"


def _vec(*values: float) -> str:
    """A 768-dimension vector literal: ``values`` first, then zeros."""
    full = list(values) + [0.0] * (768 - len(values))
    return "[" + ",".join(repr(v) for v in full) + "]"


_Q = _vec(1.0)
# (descriptor_ui, text_kind, text, vector): inner products with _Q are exact in fp16.
_VECS = [
    ("D9000001", "descriptor", "Stevens-Johnson Syndrome. A severe skin reaction.", _vec(0.5)),
    ("D9000001", "entry", "SJS", _vec(0.75)),
    ("D9000002", "descriptor", "Amoxicillin. A penicillin.", _vec(0.25)),
    ("D9000007", "descriptor", "Anaphylaxis. An allergic reaction.", _vec(0.125, 0.5)),
    ("D9000004", "entry", "Liver Injury", _vec(-0.5)),
]


def _seed(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.mesh_term")
        cur.execute("DELETE FROM episteme.mesh_term_vec")
        for ui, term, kind, pref, rel in _TERMS:
            cur.execute(
                "INSERT INTO episteme.mesh_term (descriptor_ui, term, term_norm, term_kind, "
                "is_preferred, concept_ui, concept_relation, mesh_release, source_file) "
                "VALUES (%s, %s, episteme_api_internal.term_norm(%s), %s, %s, %s, %s, %s, %s)",
                (ui, term, term, kind, pref, "M" + ui[1:], rel, _RELEASE, _SRC),
            )
        for ui, kind, text, vec in _VECS:
            cur.execute(
                "INSERT INTO episteme.mesh_term_vec (descriptor_ui, text_kind, text, embedding, "
                "model, model_revision, mesh_release) "
                "VALUES (%s, %s, %s, %s::public.halfvec, 'medcpt-article', %s, %s)",
                (ui, kind, text, vec, _REV, _RELEASE),
            )
    conn.commit()


def _clear(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.mesh_term")
        cur.execute("DELETE FROM episteme.mesh_term_vec")
    conn.commit()


@pytest.fixture
def db(ref_db):
    api_settings.sync(ref_db)
    _seed(ref_db)
    yield ref_db
    ref_db.rollback()
    _clear(ref_db)


_COLS = (
    "kind, descriptor_ui, descriptor_name, matched_text, matched_term, term_kind, "
    "concept_relation, basis, score, mesh_release, note"
)


def _rows(conn, query, params=()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        cols = [d.name for d in cur.description]
        out = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return out


def _match(conn, question, schema="episteme_api_internal"):
    rows = _rows(conn, f"SELECT {_COLS} FROM {schema}.match_terms(%s)", (question,))
    conn.rollback()
    return rows


def _terms(rows):
    return [r["descriptor_ui"] for r in rows if r["kind"] == "term"]


def _notes(rows):
    return [r["note"] for r in rows if r["kind"] == "note"]


# ---------------------------------------------------------------------------
# match_terms
# ---------------------------------------------------------------------------
def test_acronym_and_drug_with_unmatched_word(db):
    rows = _match(db, "SJS after amoxicillin in a toddler?")
    assert _terms(rows) == ["D9000001", "D9000002"]
    sjs, amox = rows[0], rows[1]
    assert sjs == {
        "kind": "term",
        "descriptor_ui": "D9000001",
        "descriptor_name": "Stevens-Johnson Syndrome",
        "matched_text": "sjs",
        "matched_term": "SJS",
        "term_kind": "entry",
        "concept_relation": None,
        "basis": "exact",
        "score": None,
        "mesh_release": _RELEASE,
        "note": None,
    }
    assert amox["matched_term"] == "Amoxicillin" and amox["term_kind"] == "name"
    # 'after', 'in' and 'a' are English stop words: only 'toddler' is unmatched.
    assert rows[-1]["kind"] == "note"
    assert _notes(rows) == ["unmatched: toddler"]


def test_no_note_when_every_word_matches_or_is_a_stop_word(db):
    rows = _match(db, "SJS after amoxicillin")
    assert _terms(rows) == ["D9000001", "D9000002"]
    assert _notes(rows) == []


def test_rows_in_question_order(db):
    rows = _match(db, "amoxicillin causing SJS")
    assert _terms(rows) == ["D9000002", "D9000001"]
    assert _notes(rows) == ["unmatched: causing"]


def test_longest_match_wins(db):
    rows = _match(db, "chemical and drug induced liver injury")
    assert _terms(rows) == ["D9000003"]
    assert rows[0]["matched_text"] == "chemical and drug induced liver injury"
    assert rows[0]["matched_term"] == "Chemical and Drug Induced Liver Injury"
    assert _notes(rows) == []
    # The shorter phrase alone matches the other descriptor.
    assert _terms(_match(db, "liver injury")) == ["D9000004"]


def test_same_descriptor_twice_gives_one_row(db):
    rows = _match(db, "Stevens-Johnson syndrome or SJS, SJS")
    assert _terms(rows) == ["D9000001"]
    # The name row wins over the entry row with the same term_norm.
    assert rows[0]["matched_term"] == "Stevens-Johnson Syndrome"
    assert rows[0]["term_kind"] == "name"
    assert rows[0]["matched_text"] == "stevens johnson syndrome; sjs"
    assert _notes(rows) == []


def test_shared_term_gives_a_row_per_descriptor(db):
    rows = _match(db, "MI")
    assert _terms(rows) == ["D9000005", "D9000006"]
    assert {r["matched_text"] for r in rows} == {"mi"}
    assert {r["descriptor_name"] for r in rows} == {"Myocardial Infarction", "Mitral Insufficiency"}


def test_single_stop_word_is_not_a_candidate(db):
    rows = _match(db, "reaction as expected")
    assert _terms(rows) == []
    assert _notes(rows) == ["unmatched: reaction expected"]
    # The descriptor still matches by its other terms.
    assert _terms(_match(db, "ankylosing spondylitis")) == ["D9000008"]


def test_narrower_concept_term_gives_its_relation(db):
    rows = _match(db, "anaphylactic shock")
    assert _terms(rows) == ["D9000007"]
    assert rows[0]["concept_relation"] == "NRW"
    assert rows[0]["term_kind"] == "entry"
    assert rows[0]["descriptor_name"] == "Anaphylaxis"


@pytest.mark.parametrize("question", ["", "   ", "?!...", "the and of", None])
def test_no_searchable_words(db, question):
    rows = _match(db, question)
    assert rows == [
        {
            "kind": "note",
            "descriptor_ui": None,
            "descriptor_name": None,
            "matched_text": None,
            "matched_term": None,
            "term_kind": None,
            "concept_relation": None,
            "basis": None,
            "score": None,
            "mesh_release": None,
            "note": _NO_WORDS,
        }
    ]


def test_long_question_is_cut_at_1000_characters(db):
    question = "SJS " + "zzz " * 1250 + "amoxicillin"
    assert len(question) > 5000
    rows = _match(db, question)
    assert _terms(rows) == ["D9000001"]
    (note,) = _notes(rows)
    assert note == "unmatched: " + " ".join(["zzz"] * 249)


def test_public_match_terms_caps_at_resolve(db):
    rows = _match(db, "MI and SJS", schema="episteme_api")
    assert _terms(rows) == ["D9000005", "D9000006", "D9000001"]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 1 "
            "WHERE family = 'resolve'"
        )
    rows = _match(db, "MI and SJS toddler", schema="episteme_api")
    assert _terms(rows) == ["D9000005"]
    assert _notes(rows) == ["unmatched: toddler", "truncated: 1 of 3"]


# ---------------------------------------------------------------------------
# suggest_terms
# ---------------------------------------------------------------------------
def _suggest(conn, vector=_Q, revision=_REV, schema="episteme_api", limit=None):
    if schema == "episteme_api":
        q = f"SELECT {_COLS} FROM episteme_api.suggest_terms(%s::public.halfvec, %s)"
        params = (vector, revision)
    else:
        q = f"SELECT {_COLS} FROM episteme_api_internal.suggest_terms(%s::public.halfvec, %s, %s)"
        params = (vector, revision, limit)
    rows = _rows(conn, q, params)
    conn.rollback()
    return rows


def test_suggest_best_first_one_row_per_descriptor(db):
    rows = _suggest(db)
    assert [(r["descriptor_ui"], r["score"]) for r in rows] == [
        ("D9000001", 0.75),
        ("D9000002", 0.25),
        ("D9000007", 0.125),
        ("D9000004", -0.5),
    ]
    top = rows[0]
    assert top["kind"] == "term" and top["basis"] == "medcpt"
    assert top["descriptor_name"] == "Stevens-Johnson Syndrome"
    assert top["matched_term"] == "SJS" and top["term_kind"] == "entry"
    assert top["matched_text"] is None and top["mesh_release"] == _RELEASE
    assert _notes(rows) == []


def test_suggest_internal_limit(db):
    rows = _suggest(db, schema="episteme_api_internal", limit=2)
    assert _terms(rows) == ["D9000001", "D9000002"]


def test_suggest_public_cap(db):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme_api_internal.result_limit SET default_limit = 2 "
            "WHERE family = 'suggest'"
        )
    rows = _suggest(db)
    assert _terms(rows) == ["D9000001", "D9000002"]
    assert _notes(rows) == ["truncated: 2 of more than 2"]


def test_suggest_wrong_revision_is_refused(db):
    rows = _suggest(db, revision="rev-b")
    assert _terms(rows) == []
    assert _notes(rows) == ["model revision rev-b does not match the stored vectors (rev-a)"]


def test_suggest_mixed_revisions_are_refused(db):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE episteme.mesh_term_vec SET model_revision = 'rev-b' "
            "WHERE descriptor_ui = 'D9000004'"
        )
    rows = _suggest(db)
    assert _terms(rows) == []
    assert _notes(rows) == ["model revision rev-a does not match the stored vectors (rev-a, rev-b)"]


def test_suggest_stale_release_is_refused(db):
    with db.cursor() as cur:
        cur.execute("UPDATE episteme.mesh_term_vec SET mesh_release = '2025'")
    rows = _suggest(db)
    assert _terms(rows) == []
    assert _notes(rows) == [
        "vocabulary vectors are from MeSH 2025, terms are from MeSH 2026; run mesh embed"
    ]


def test_suggest_without_vectors(db):
    with db.cursor() as cur:
        cur.execute("DELETE FROM episteme.mesh_term_vec")
    rows = _suggest(db)
    assert _terms(rows) == []
    assert _notes(rows) == ["no vocabulary vectors; run mesh embed"]


def test_suggest_restores_ef_search(db):
    with db.cursor() as cur:
        cur.execute("SET LOCAL hnsw.ef_search = 41")
        cur.execute(
            "SELECT count(*) FROM episteme_api.suggest_terms(%s::public.halfvec, %s)",
            (_Q, _REV),
        )
        assert cur.fetchone()[0] == 4
        cur.execute("SELECT current_setting('hnsw.ef_search')")
        assert cur.fetchone()[0] == "41"
    db.rollback()


def test_suggest_ef_search_covers_a_large_limit_and_is_restored(db):
    """ef_search is greatest(suggest_ef_search, 4 * limit): a limit of 100 scans 400
    candidates; the caller's value comes back after the call."""
    with db.cursor() as cur:
        cur.execute("SET LOCAL hnsw.ef_search = 41")
        cur.execute(
            "SELECT count(*) FROM episteme_api_internal.suggest_terms(%s::public.halfvec, %s, 100)",
            (_Q, _REV),
        )
        assert cur.fetchone()[0] == 4
        cur.execute("SELECT current_setting('hnsw.ef_search')")
        assert cur.fetchone()[0] == "41"
    db.rollback()


@pytest.mark.parametrize("vector, revision", [(None, _REV), (_Q, None), (None, None)])
def test_suggest_needs_vector_and_revision(db, vector, revision):
    rows = _suggest(db, vector=vector, revision=revision)
    assert _terms(rows) == []
    assert _notes(rows) == ["vector and model revision are required"]


def test_suggest_needs_768_dimensions(db):
    rows = _suggest(db, vector="[1,0,0]")
    assert _terms(rows) == []
    assert _notes(rows) == ["vector must have 768 dimensions, got 3"]
