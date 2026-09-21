import re

from ._repo import REPO, doc, wrapper_table


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


def test_licence_caveats_are_recorded():
    text = doc("12-source-inventory.md")
    assert "UNVERIFIED" in text  # cdisc_bc data licence
    assert "governance override" in text  # public_domain for mesh/pubchem/clinvar
