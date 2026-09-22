import re

from ._repo import REPO, doc, lit_sources, structured_sources, wrapper_table


def _rows() -> dict[str, list[str]]:
    rows = {}
    for line in doc("12-source-inventory.md").splitlines():
        m = re.match(r"\|\s*`(\w+)`\s*\|(.*)\|\s*$", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    return rows


def test_inventory_covers_exactly_the_wired_sources():
    assert set(_rows()) == set(wrapper_table())


def test_every_inventory_script_exists():
    for src, script in wrapper_table().items():
        assert (REPO / "scripts" / "data" / script).is_file(), src
        assert f"`{script}`" in doc("12-source-inventory.md"), src


def _stages(src: str) -> list[str]:
    cell = _rows()[src][1]  # columns: class, stages wired, licence, cadence, script
    cell = re.sub(r"\([^)]*\)", "", cell)  # drop parentheticals such as "(no serializer)"
    return [w.strip() for w in cell.split(",") if w.strip()]


def test_stage_column_matches_the_dispatcher_rules():
    """Stages per source follow run_pipeline.sh: lit = download/extract/load/graph,
    structured = download/serialize/load (+graph for mesh), the rest as wired there."""
    lit, structured = lit_sources(), structured_sources()
    for src in wrapper_table():
        got = _stages(src)
        if src == "pmc":  # has its own chain (docs/12); the common four must be present
            assert {"download", "extract", "load", "graph"} <= set(got), got
        elif src in lit:
            assert got == ["download", "extract", "load", "graph"], (src, got)
        elif src in structured:
            want = ["download", "serialize", "load"] + (["graph"] if src == "mesh" else [])
            assert got == want, (src, got)
        elif src == "europepmc_id_mappings":
            assert got == ["download", "load"], got
        elif src == "europepmc_lite":
            assert got == ["download", "enrich"], got
        else:
            assert got == ["download"], (src, got)


def test_licence_caveats_are_recorded_per_row():
    rows = _rows()
    assert "UNVERIFIED" in " ".join(rows["cdisc_bc"])  # cdisc_bc data licence
    for src in ("mesh", "pubchem", "clinvar"):
        assert "governance override" in " ".join(rows[src]), src
