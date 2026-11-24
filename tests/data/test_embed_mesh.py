"""MeSH embed stage: text building and wrapper (non-pg), vector writes and restart (pg)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from episteme.data.mesh import embed_mesh
from episteme.data.mesh.embed_mesh import embed, scope_note_of, vocabulary_texts
from episteme.data.mesh.serialize_mesh import _build_text

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "data" / "mesh" / "embed_mesh.sh"
MIGRATION_0017 = REPO_ROOT / "src/episteme/data/db/migrations/0017_api_no_timeouts.sql"
MIGRATION_0018 = REPO_ROOT / "src/episteme/data/db/migrations/0018_question_terms.sql"

NOTE = "A heterogeneous group of disorders characterized by hyperglycemia."


class FakeEncoder:
    """Deterministic 768-dimension vectors from the (title, text) pair."""

    def __init__(self, revision: str = "rev-a") -> None:
        self.model_id = "fake/MedCPT-Article-Encoder"
        self.revision = revision
        self.calls: list[list[tuple[str, str]]] = []

    def encode_articles(self, pairs):
        self.calls.append(list(pairs))
        out = np.zeros((len(pairs), 768), dtype=np.float32)
        for i, (title, text) in enumerate(pairs):
            digest = hashlib.sha256(f"{title}\x00{text}".encode()).digest()
            out[i, :32] = np.frombuffer(digest, dtype=np.uint8) / 255.0
        return out


# ---- non-pg ---------------------------------------------------------------------------


def test_scope_note_strips_name_and_closing_sentence():
    text = _build_text("Diabetes Mellitus", NOTE)
    assert scope_note_of("Diabetes Mellitus", text) == NOTE


def test_scope_note_empty_when_absent():
    text = _build_text("Diabetes Mellitus", None)
    assert scope_note_of("Diabetes Mellitus", text) == ""
    assert scope_note_of("Diabetes Mellitus", None) == ""


def test_vec_text_is_halfvec_literal():
    assert embed_mesh._vec_text([1.0, 0.5, -2.25]) == "[1,0.5,-2.25]"


def test_wrapper_follows_graph_wrapper_pattern():
    text = WRAPPER.read_text(encoding="utf-8")
    assert '. "$HERE/../_lib/common.sh"' in text
    assert "load_dotenv" in text
    assert "require_env EPISTEME_ACTOR" in text
    assert "python" in text
    assert '-m episteme.data.mesh.embed_mesh "$@"' in text


def test_main_rejects_bad_limit():
    with pytest.raises(SystemExit):
        embed_mesh.main(["--limit", "0"])


# ---- pg -------------------------------------------------------------------------------

_TERMS = [
    # (descriptor_ui, term, kind, preferred)
    ("D003920", "Diabetes Mellitus", "name", True),
    ("D003920", "Sugar Diabetes", "entry", False),
    ("D003924", "Diabetes Mellitus, Type 2", "name", True),
    ("D003924", "Type 2 Diabetes", "entry", False),
    ("D003924", "NIDDM", "entry", False),
]


def _setup(pg_conn, monkeypatch, release: str = "2026") -> None:
    from ._graph_schema import setup_graph_schema

    monkeypatch.setenv("EPISTEME_ACTOR", "episteme_sys_admin")
    setup_graph_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute(MIGRATION_0017.read_text(encoding="utf-8"))
        cur.execute(MIGRATION_0018.read_text(encoding="utf-8"))
        # D003920 has no scope note, D003924 has one.
        for ui, name, note in (
            ("D003920", "Diabetes Mellitus", None),
            ("D003924", "Diabetes Mellitus, Type 2", NOTE),
        ):
            cur.execute(
                "INSERT INTO episteme.articles (id, source, source_file, source_record_id, "
                "title, text) VALUES (%s, 'mesh', 'desc2026.xml', %s, %s, %s)",
                (f"mesh:{ui}", ui, name, _build_text(name, note)),
            )
        for ui, term, kind, pref in _TERMS:
            cur.execute(
                "INSERT INTO episteme.mesh_term (descriptor_ui, term, term_norm, term_kind, "
                "is_preferred, mesh_release, source_file) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (ui, term, term.lower(), kind, pref, release, f"desc{release}.xml"),
            )
    pg_conn.commit()


def _count(pg_conn, where: str = "true", params=()) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM episteme.mesh_term_vec WHERE {where}", params)
        return cur.fetchone()[0]


@pytest.mark.pg
def test_vocabulary_texts_pairs(pg_conn, monkeypatch):
    _setup(pg_conn, monkeypatch)
    assert vocabulary_texts(pg_conn) == [
        ("D003920", "descriptor", "Diabetes Mellitus", ""),
        ("D003920", "entry", "Sugar Diabetes", ""),
        ("D003924", "descriptor", "Diabetes Mellitus, Type 2", NOTE),
        ("D003924", "entry", "NIDDM", ""),
        ("D003924", "entry", "Type 2 Diabetes", ""),
    ]


@pytest.mark.pg
def test_first_run_embeds_all_second_run_skips(pg_conn, monkeypatch):
    _setup(pg_conn, monkeypatch)
    enc = FakeEncoder()
    res = embed(pg_conn, enc)
    assert res == {"release": "2026", "deleted_stale": 0, "embedded": 2, "skipped_done": 0}
    assert _count(pg_conn) == 5
    # The encoder got exactly the pairs of vocabulary_texts.
    sent = [pair for call in enc.calls for pair in call]
    assert sent == [(t, x) for _ui, _k, t, x in vocabulary_texts(pg_conn)]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT model, model_revision, mesh_release FROM episteme.mesh_term_vec LIMIT 1"
        )
        assert cur.fetchone() == ("fake/MedCPT-Article-Encoder", "rev-a", "2026")

    enc2 = FakeEncoder()
    res = embed(pg_conn, enc2)
    assert res == {"release": "2026", "deleted_stale": 0, "embedded": 0, "skipped_done": 2}
    assert enc2.calls == []
    assert _count(pg_conn) == 5


@pytest.mark.pg
def test_stale_release_or_revision_rows_are_deleted_first(pg_conn, monkeypatch):
    _setup(pg_conn, monkeypatch)
    embed(pg_conn, FakeEncoder("rev-a"))
    res = embed(pg_conn, FakeEncoder("rev-b"))
    assert res == {"release": "2026", "deleted_stale": 5, "embedded": 2, "skipped_done": 0}
    assert _count(pg_conn, "model_revision = 'rev-b'") == 5
    assert _count(pg_conn, "model_revision = 'rev-a'") == 0

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE episteme.mesh_term_vec SET mesh_release = '2025'")
    pg_conn.commit()
    res = embed(pg_conn, FakeEncoder("rev-b"))
    assert res["deleted_stale"] == 5 and res["embedded"] == 2


@pytest.mark.pg
def test_limit_embeds_one_descriptor_with_all_its_rows(pg_conn, monkeypatch):
    _setup(pg_conn, monkeypatch)
    res = embed(pg_conn, FakeEncoder(), limit=1)
    assert res["embedded"] == 1
    assert _count(pg_conn, "descriptor_ui = 'D003920'") == 2
    assert _count(pg_conn) == 2
    res = embed(pg_conn, FakeEncoder())
    assert res["embedded"] == 1 and res["skipped_done"] == 1
    assert _count(pg_conn) == 5


@pytest.mark.pg
def test_each_batch_commits_on_its_own(pg_conn, monkeypatch):
    _setup(pg_conn, monkeypatch)

    class Dies(FakeEncoder):
        def encode_articles(self, pairs):
            if self.calls:
                raise RuntimeError("killed between batches")
            return super().encode_articles(pairs)

    with pytest.raises(RuntimeError, match="killed"):
        embed(pg_conn, Dies(), batch=1)
    pg_conn.rollback()
    # The first descriptor committed whole; the second has no rows.
    assert _count(pg_conn, "descriptor_ui = 'D003920'") == 2
    assert _count(pg_conn, "descriptor_ui = 'D003924'") == 0
    res = embed(pg_conn, FakeEncoder(), batch=1)
    assert res["embedded"] == 1 and res["skipped_done"] == 1


@pytest.mark.pg
def test_one_audit_row_per_run(pg_conn, monkeypatch):
    _setup(pg_conn, monkeypatch)

    def audit_rows() -> int:
        with pg_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM episteme._audit WHERE object = 'mesh embed'")
            return cur.fetchone()[0]

    embed(pg_conn, FakeEncoder(), batch=1)
    assert audit_rows() == 1
    embed(pg_conn, FakeEncoder())
    assert audit_rows() == 2


@pytest.mark.pg
def test_halfvec_roundtrip(pg_conn, monkeypatch):
    _setup(pg_conn, monkeypatch)
    embed(pg_conn, FakeEncoder())
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT vector_dims(embedding) FROM episteme.mesh_term_vec "
            "WHERE descriptor_ui = 'D003920' AND text_kind = 'descriptor'"
        )
        assert cur.fetchone()[0] == 768
