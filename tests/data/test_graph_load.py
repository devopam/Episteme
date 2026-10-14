"""graph_load against episteme_test: per-file COPY, facets, skip, replace, force, failures."""

from __future__ import annotations

import importlib

import pytest

from episteme.data import graph_files as gf
from episteme.data.checkpoint_markers import mark_success

from ._graph_schema import setup_graph_schema

pytestmark = pytest.mark.pg

SF_A = "baseline__pubmed26n0001.xml.gz"
SF_B = "baseline__pubmed26n0002.xml.gz"
PMID = "100"
MESH_TREE = [
    ("D003920", "C18.452.394.750"),
    ("D003920", "C19.246"),
    ("D007328", "D06.472.699.587.200.500.500"),
    ("D007328", "D12.644.548.586.200.500"),
    ("D000369", "M01.060.116.100.080"),
]


def _configure(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISTEME_ACTOR", "t")
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(tmp_path))
    import episteme.config as cfg

    importlib.reload(cfg)
    cfg.get_settings.cache_clear()


def _mesh(pmid, ui, name, sf):
    return {
        "pmid": pmid,
        "descriptor_ui": ui,
        "descriptor_name": name,
        "major_topic": ui == "D003920",
        "qualifier_uis": ["Q000188"] if ui == "D003920" else [],
        "qualifiers": ["drug therapy"] if ui == "D003920" else [],
        "source_file": sf,
    }


def _write(processed, sf, pmid=PMID, *, drop_mesh=False):
    """Write one source file's graph files and its success marker; return the info."""
    mesh = [
        _mesh(pmid, "D006801", "Humans", sf),
        _mesh(pmid, "D003920", "Diabetes Mellitus", sf),
        _mesh(pmid, "D000369", "Aged, 80 and over", sf),
    ]
    if drop_mesh:
        mesh = mesh[:-1]
    with gf.GraphFileWriter(processed, "pubmed", sf) as w:
        w.add("mesh", mesh)
        w.add(
            "cites",
            [
                {
                    "src_pmid": pmid,
                    "dst_pmid": "200",
                    "dst_doi": None,
                    "ref_text": "A ref",
                    "source_file": sf,
                },
                {
                    "src_pmid": pmid,
                    "dst_pmid": None,
                    "dst_doi": "10.1/x",
                    "ref_text": "B ref",
                    "source_file": sf,
                },
            ],
        )
        w.add(
            "comments",
            [
                {
                    "pmid": pmid,
                    "ref_type": "CommentIn",
                    "ref_pmid": "300",
                    "ref_text": "c",
                    "source_file": sf,
                }
            ],
        )
        w.add(
            "chemicals",
            [
                {
                    "pmid": pmid,
                    "substance_ui": "D007328",
                    "name": "Insulin",
                    "registry_number": "9004-10-8",
                    "source_file": sf,
                },
                {
                    "pmid": pmid,
                    "substance_ui": "C000123",
                    "name": "Supplementary",
                    "registry_number": None,
                    "source_file": sf,
                },
            ],
        )
        w.add(
            "keywords",
            [
                {
                    "pmid": pmid,
                    "keyword": "diabetes",
                    "owner": "NOTNLM",
                    "major_topic": False,
                    "source_file": sf,
                }
            ],
        )
        w.add(
            "authors",
            [
                {
                    "pmid": pmid,
                    "position": 1,
                    "last_name": "Doe",
                    "fore_name": "Jane",
                    "initials": "J",
                    "collective_name": None,
                    "orcid": "0000-0002-1825-0097",
                    "affiliations": ["Univ A", "Univ B"],
                    "source_file": sf,
                }
            ],
        )
        info = w.close()
    mark_success(processed, "pubmed", sf, stats=info)
    return info


@pytest.fixture
def env(pg_conn, tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    setup_graph_schema(pg_conn)
    with pg_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO episteme.mesh_tree (descriptor_ui, tree_number, source_file) "
            "VALUES (%s, %s, 'desc2026.gz')",
            MESH_TREE,
        )
    pg_conn.commit()
    processed = tmp_path / "processed"
    return pg_conn, processed


def _counts(conn, sf):
    from episteme.data.graph_load import TABLE_FOR

    out = {}
    with conn.cursor() as cur:
        for rel, table in TABLE_FOR.items():
            cur.execute(f"SELECT count(*) FROM {table} WHERE source_file = %s", (sf,))
            out[rel] = cur.fetchone()[0]
    return out


def _facets(conn, sf):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pmid, facet, descriptor_ui FROM episteme.article_facets WHERE source_file = %s",
            (sf,),
        )
        rows = cur.fetchall()
    return rows


def _audit(conn, event_type):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object, rows_affected, reason, input_content_hash FROM episteme._audit "
            "WHERE event_type = %s ORDER BY seq",
            (event_type,),
        )
        return cur.fetchall()


EXPECTED_FACETS = {
    (PMID, "demographic", "D006801"),
    (PMID, "condition", "D003920"),
    (PMID, "demographic", "D000369"),
    (PMID, "special_population", "D000369"),
    (PMID, "drug", "D007328"),
}


def test_first_load_copies_every_relation_and_facets(env):
    from episteme.data import graph_load

    conn, processed = env
    info = _write(processed, SF_A)
    res = graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
    )
    assert res["skipped"] is False
    assert res["rows"] == info["graph_rows"]
    assert _counts(conn, SF_A) == info["graph_rows"]
    facets = _facets(conn, SF_A)
    assert len(facets) == len(set(facets))
    assert set(facets) == EXPECTED_FACETS
    assert res["facets"] == len(EXPECTED_FACETS)
    assert not [f for f in facets if f[2] == "C000123"]  # Review Focus 5
    with conn.cursor() as cur:
        cur.execute(
            "SELECT qualifiers, qualifier_uis, major_topic FROM episteme.article_mesh "
            "WHERE descriptor_ui = 'D003920'"
        )
        assert cur.fetchone() == (["drug therapy"], ["Q000188"], True)
        cur.execute("SELECT affiliations, position FROM episteme.article_authors")
        assert cur.fetchone() == (["Univ A", "Univ B"], 1)
        cur.execute("SELECT count(*) FROM episteme.article_cites WHERE dst_pmid IS NULL")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT source, source_file, graph_sha256, rows, run_id FROM episteme._graph_lineage"
        )
        lineage = cur.fetchall()
    assert lineage == [("pubmed", SF_A, info["graph_sha256"], info["graph_rows"], "r1")]
    commits = _audit(conn, "graph_commit")
    assert len(commits) == 1
    obj, rows_affected, _reason, hash_ = commits[0]
    assert obj == f"pubmed {SF_A}"
    assert rows_affected == sum(info["graph_rows"].values()) + len(EXPECTED_FACETS)
    assert hash_ == info["graph_sha256"]


def test_unchanged_hash_is_skipped(env):
    from episteme.data import graph_load

    conn, processed = env
    _write(processed, SF_A)
    graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
    )
    res = graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r2"
    )
    assert res["skipped"] is True
    assert conn.info.transaction_status.name == "IDLE"  # the skip ended its read transaction
    assert len(_audit(conn, "graph_commit")) == 1


def test_regraph_replaces_not_duplicates(env):
    from episteme.data import graph_load

    conn, processed = env
    _write(processed, SF_A)
    graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
    )
    info2 = _write(processed, SF_A, drop_mesh=True)
    res = graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r2"
    )
    assert res["skipped"] is False
    assert _counts(conn, SF_A) == info2["graph_rows"]
    assert info2["graph_rows"]["mesh"] == 2
    facets = _facets(conn, SF_A)
    assert len(facets) == len(set(facets))
    assert set(facets) == EXPECTED_FACETS - {
        (PMID, "demographic", "D000369"),
        (PMID, "special_population", "D000369"),
    }
    with conn.cursor() as cur:
        cur.execute("SELECT graph_sha256, run_id FROM episteme._graph_lineage")
        assert cur.fetchall() == [(info2["graph_sha256"], "r2")]


def test_force_requires_reason_and_is_audited(env):
    from episteme.data import graph_load

    conn, processed = env
    info = _write(processed, SF_A)
    graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
    )
    with pytest.raises(graph_load.GraphLoadError, match="--force requires --reason"):
        graph_load.load_one(
            conn,
            source="pubmed",
            source_file=SF_A,
            processed_dir=processed,
            run_id="r2",
            force=True,
        )
    with pytest.raises(graph_load.GraphLoadError, match="--force requires --reason"):
        graph_load.build_pubmed_graph(conn, processed_dir=processed, run_id="r2", force=True)
    res = graph_load.load_one(
        conn,
        source="pubmed",
        source_file=SF_A,
        processed_dir=processed,
        run_id="r3",
        force=True,
        reason="re-check counts",
    )
    assert res["skipped"] is False
    assert _counts(conn, SF_A) == info["graph_rows"]  # deleted then re-copied, no duplicates
    assert len(_facets(conn, SF_A)) == len(EXPECTED_FACETS)
    overrides = _audit(conn, "force_override")
    assert len(overrides) == 1
    assert overrides[0][0] == f"pubmed graph {SF_A}"
    assert overrides[0][2] == "re-check counts"
    assert len(_audit(conn, "graph_commit")) == 2


def test_missing_relation_file_fails_that_file_only(env, capsys):
    from episteme.data import graph_load

    conn, processed = env
    info_a = _write(processed, SF_A, pmid="100")
    info_b = _write(processed, SF_B, pmid="101")
    assert info_a and info_b
    gf.graph_file_path(processed, "pubmed", "keywords", SF_A).unlink()
    res = graph_load.build_pubmed_graph(conn, processed_dir=processed, run_id="r1")
    assert res["failed"] == [SF_A]
    assert res["files"] == 2
    assert res["loaded"] == 1
    assert res["skipped"] == 0
    assert res["rows"] == info_b["graph_rows"]
    assert _counts(conn, SF_A) == {rel: 0 for rel in gf.RELATIONS}
    assert _facets(conn, SF_A) == []
    assert _counts(conn, SF_B) == info_b["graph_rows"]
    assert "graph file missing for relation 'keywords'" in capsys.readouterr().err
    with conn.cursor() as cur:
        cur.execute("SELECT source_file FROM episteme._graph_lineage")
        assert cur.fetchall() == [(SF_B,)]


def test_count_mismatch_rolls_back_the_file(env):
    from episteme.data import graph_load

    conn, processed = env
    info = _write(processed, SF_A)
    info["graph_rows"]["mesh"] = 99
    mark_success(processed, "pubmed", SF_A, stats=info)
    with pytest.raises(graph_load.GraphLoadError, match="mesh"):
        graph_load.load_one(
            conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
        )
    assert _counts(conn, SF_A) == {rel: 0 for rel in gf.RELATIONS}
    assert _audit(conn, "graph_commit") == []


def test_old_marker_is_refused_with_reextract_message(env):
    from episteme.data import graph_load

    conn, processed = env
    mark_success(processed, "pubmed", SF_A, stats={"rows": 5})
    with pytest.raises(graph_load.GraphLoadError, match=r"re-extract with schema >= 1\.6"):
        graph_load.load_one(
            conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
        )
    _write(processed, SF_B)
    res = graph_load.build_pubmed_graph(conn, processed_dir=processed, run_id="r1")
    assert res["failed"] == [SF_A]
    assert res["loaded"] == 1


def test_empty_mesh_tree_refuses_to_start(env):
    from episteme.data import graph_load

    conn, processed = env
    _write(processed, SF_A)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM episteme.mesh_tree")
    conn.commit()
    with pytest.raises(graph_load.GraphLoadError, match="mesh_tree is empty"):
        graph_load.build_pubmed_graph(conn, processed_dir=processed, run_id="r1")
    assert _counts(conn, SF_A) == {rel: 0 for rel in gf.RELATIONS}


def test_graph_builder_routes_pubmed_and_main_exits_1_on_failure(env, capsys):
    from episteme.data import graph_builder

    conn, processed = env
    info = _write(processed, SF_A)
    res = graph_builder.build(
        conn, source="pubmed", raw_dir=None, run_id="r1", processed_dir=processed
    )
    assert res["loaded"] == 1
    assert res["rows"] == info["graph_rows"]

    mark_success(processed, "pubmed", SF_B, stats={"rows": 1})
    assert graph_builder.main(["--source", "pubmed", "--processed-dir", str(processed)]) == 1
    out = capsys.readouterr()
    assert "failed=1" in out.out
    assert "re-extract" in out.err


def _add_rule_v2(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO episteme.mesh_facet_rule (facet, kind, value, rule_version) "
            "SELECT facet, kind, value, 2 FROM episteme.mesh_facet_rule WHERE rule_version = 1"
        )
        cur.execute("INSERT INTO episteme.mesh_facet_rule VALUES ('test', 'tree_prefix', 'C19', 2)")
    conn.commit()


def test_facet_rebuild_and_status(env):
    from episteme.data import graph_load

    conn, processed = env
    _write(processed, SF_A)
    graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
    )
    assert graph_load.facet_status(conn) == {
        "rule_version": 1,
        "stale_rows": 0,
        "stale_files": 0,
        "files": 1,
    }
    _add_rule_v2(conn)
    st = graph_load.facet_status(conn)
    assert st["rule_version"] == 2
    assert st["stale_files"] == 1
    assert st["stale_rows"] == len(EXPECTED_FACETS)

    res = graph_load.rebuild_facets(conn, run_id="r2")
    assert res == {"rebuilt": 1, "skipped": 0}
    facets = _facets(conn, SF_A)
    assert (PMID, "test", "D003920") in facets
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT rule_version FROM episteme.article_facets WHERE source_file = %s",
            (SF_A,),
        )
        assert cur.fetchall() == [(2,)]
    assert graph_load.facet_status(conn)["stale_rows"] == 0
    assert [o for o, *_ in _audit(conn, "graph_commit")][-1] == f"facets {SF_A}"

    assert graph_load.rebuild_facets(conn, run_id="r3") == {"rebuilt": 0, "skipped": 1}


def test_facet_rebuild_force_needs_reason_and_is_audited(env):
    from episteme.data import graph_load

    conn, processed = env
    _write(processed, SF_A)
    graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
    )
    with pytest.raises(graph_load.GraphLoadError, match="--force requires --reason"):
        graph_load.rebuild_facets(conn, run_id="r2", force=True)
    res = graph_load.rebuild_facets(conn, run_id="r2", force=True, reason="re-derive")
    assert res == {"rebuilt": 1, "skipped": 0}
    ov = _audit(conn, "force_override")
    assert [(o, r) for o, _, r, _ in ov] == [(f"facets {SF_A}", "re-derive")]
    assert len(_facets(conn, SF_A)) == len(EXPECTED_FACETS)


def test_facets_cli_status_exit_code_and_rebuild(env, capsys):
    from episteme.data import graph_builder, graph_load

    conn, processed = env
    _write(processed, SF_A)
    graph_load.load_one(
        conn, source="pubmed", source_file=SF_A, processed_dir=processed, run_id="r1"
    )
    assert graph_builder.main(["facets", "--status"]) == 0
    _add_rule_v2(conn)
    assert graph_builder.main(["facets", "--status"]) == 1
    assert "stale_files=1" in capsys.readouterr().out
    assert graph_builder.main(["facets", "--rebuild"]) == 0
    assert "rebuilt=1" in capsys.readouterr().out
    assert graph_builder.main(["facets", "--status"]) == 0
    assert graph_builder.main(["facets", "--rebuild", "--force"]) == 2


def test_truncated_marker_fails_that_file_and_the_run_continues(env, capsys):
    from episteme.data import graph_load
    from episteme.data.checkpoint_markers import success_marker_path

    conn, processed = env
    info_b = _write(processed, SF_B, pmid="101")
    _write(processed, SF_A, pmid="100")
    success_marker_path(processed, "pubmed", SF_A).write_text('{"stats": {"graph_', "utf-8")
    res = graph_load.build_pubmed_graph(conn, processed_dir=processed, run_id="r1")
    assert res["failed"] == [SF_A]
    assert res["files"] == 2
    assert res["loaded"] == 1
    assert _counts(conn, SF_B) == info_b["graph_rows"]
    assert "unreadable success marker" in capsys.readouterr().err
