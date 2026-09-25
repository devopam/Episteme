"""docs/09 must match the code it describes (licence vocabulary, storage, identity)."""

import re
from pathlib import Path

import pytest

from episteme.data.article_schema import (
    LICENSE_PUBLIC_DOMAIN,
    SOURCES,
    normalize_license,
    subset_from_license,
)
from episteme.data.checkpoint_markers import input_key
from episteme.data.postgres_loader import _coerce_year

from ._repo import REPO, doc, structured_sources

DOC = "09-extraction-contract.md"

SAMPLES = [
    "CC0",
    "https://creativecommons.org/licenses/by/4.0/",
    "https://creativecommons.org/licenses/by-sa/4.0/",
    "https://creativecommons.org/licenses/by-nd/4.0/",
    "https://creativecommons.org/licenses/by-nc/4.0/",
    "https://creativecommons.org/publicdomain/zero/1.0/",
    "text mining",
    "Apache-2.0",
    "something unrecognised",
]


def _section(text: str, start: str, end: str) -> str:
    return text.split(start)[1].split(end)[0]


@pytest.mark.parametrize("raw", SAMPLES)
def test_normalised_codes_are_in_the_vocabulary(raw):
    code = normalize_license(raw)[0]
    assert f"`{code}`" in doc(DOC), code


def test_public_domain_is_documented_as_override():
    text = doc(DOC)
    assert f"`{LICENSE_PUBLIC_DOMAIN}`" in text
    assert "governance override" in text
    assert subset_from_license(LICENSE_PUBLIC_DOMAIN) == "commercial"
    # never returned by normalize_license, even for the real upstream texts
    assert normalize_license("public domain")[0] != LICENSE_PUBLIC_DOMAIN
    assert normalize_license("Fair Use")[0] != LICENSE_PUBLIC_DOMAIN


def test_public_domain_sources_match_the_code():
    """The three sources the doc names are exactly the serializers that set the override."""
    setters = {
        p.parent.name
        for p in (REPO / "src" / "episteme" / "data").glob("*/serialize_*.py")
        if "lic = LICENSE_PUBLIC_DOMAIN" in p.read_text(encoding="utf-8")
    }
    assert setters == {"mesh", "pubchem", "clinvar"}
    sec = _section(doc(DOC), "### 6.3", "\n---")
    row = next(line for line in sec.splitlines() if "`public_domain` -> `commercial`" in line)
    assert all(f"`{s}`" in row for s in setters)
    assert "contributor" in sec  # PubChem / ClinVar caveat


def test_uniprot_override_and_cdisc_note():
    sec = _section(doc(DOC), "### 6.3", "\n---")
    assert "`uniprot`" in sec and "`text_mining` (hardcoded)" in sec
    assert "`cdisc_bc`" in sec and "UNVERIFIED".lower() in sec.lower() and "download-only" in sec
    row = next(line for line in sec.splitlines() if line.startswith("| `cdisc_bc`"))
    assert "until CDISC confirms" in row
    assert "training corpus" in row
    # ontologies: GO/MONDO -> commercial via the bare CC BY URL, HPO stays unknown
    go = "http://creativecommons.org/licenses/by/4.0/"
    assert normalize_license(go)[0] == "CC BY"
    assert subset_from_license("CC BY") == "commercial"
    assert normalize_license("https://hpo.jax.org/app/license")[0] == "unknown"
    assert "HPO" in sec and "`unknown`" in sec


def test_identity_and_storage_terms_present():
    text = doc(DOC)
    for term in ("input_key", "delete-by-`source_file`", "episteme.articles"):
        assert term in text, term


def test_input_key_rule_matches_code(tmp_path):
    (tmp_path / "sub").mkdir()
    nested = tmp_path / "sub" / "a.xml"
    flat = tmp_path / "b.xml"
    nested.write_text("x")
    flat.write_text("x")
    assert input_key(nested, tmp_path) == "sub__a.xml"  # joined with "__"
    assert input_key(flat, tmp_path) == "b.xml"  # flat layouts keep the basename
    sec = _section(doc(DOC), "### 4.3", "### 4.4")
    assert "`__`" in sec and "basename" in sec
    assert "deferred" not in sec  # SP6 closed the migration; pin the new truth, not the old gap


def test_literature_extractors_key_on_input_key():
    """All seven SP2 literature extractors now key marker + source_file on
    input_key (docs/09 SP6 rewrite); four discover recursively via
    discover_input_files, three deliberately stay flat/non-recursive."""
    sec = _section(doc(DOC), "### 4.3", "### 4.4")
    lit_extractors = {
        "pmc": "src/episteme/data/pmc/extract_pmc.py",
        "bookshelf": "src/episteme/data/bookshelf/extract_bookshelf.py",
        "pubmed": "src/episteme/data/pubmed/extract_pubmed.py",
        "apollo": "src/episteme/data/apollo/extract_apollo.py",
        "europepmc_manuscript": (
            "src/episteme/data/europepmc/manuscripts/extract_europepmc_manuscripts.py"
        ),
        "europepmc_preprint": (
            "src/episteme/data/europepmc/preprints/extract_europepmc_preprints.py"
        ),
        "guidelines": "src/episteme/data/guidelines/extract_guidelines.py",
    }
    recursive = {"pmc", "bookshelf", "pubmed", "apollo"}
    flat = {"europepmc_manuscript", "europepmc_preprint", "guidelines"}
    assert recursive | flat == set(lit_extractors)
    for name, rel in lit_extractors.items():
        assert f"`{name}`" in sec, name
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "input_key(" in text, name  # every one keys source_file on input_key now
        # "(" pins an actual call, not the flat three's docstrings, which name
        # discover_input_files only to say they deliberately do NOT call it.
        if name in recursive:
            assert "discover_input_files(" in text, name
        else:
            assert "discover_input_files(" not in text, name
            assert "non-recursive" in text, name  # each says so in its own discovery docstring
    assert "list_input_files" in sec  # pubmed/apollo's prior legacy call, named for history


def test_section_4_describes_postgres_not_iceberg():
    text = doc(DOC)
    sec4 = _section(text, "## 4. Storage", "## 5.")
    assert "Postgres" in sec4
    assert "Superseded" in sec4 and "ADR-0001" in sec4
    for term in (
        "episteme.article_body",
        "Guard (d0)",
        "delete-by-`source_file`",
        "`serialize_commit`",
    ):
        assert term in sec4, term
    # every remaining Iceberg mention in the whole doc is a supersession/history note
    for line in text.splitlines():
        if "Iceberg" in line:
            assert re.search(
                r"[Ss]upersed|ADR-000|Resolved|not Iceberg|originally|v1\.1|not the contract",
                line,
            ), line
    # the Iceberg layout is not presented as a live table/dir (only inside the superseded note)
    for line in text.splitlines():
        if "warehouse/" in line:
            assert line.startswith("> **Superseded:**"), line
    # the year sentinel the doc states is the loader's behaviour
    assert _coerce_year(None) == 0 and "`0`" in sec4


def test_structured_serialisation_contract_matches_code():
    sec = _section(doc(DOC), "### 4.4", "**Doc-versus-code")
    for key in ("inputs", "ok", "failed", "rows"):
        assert f"`{key}`" in sec
    assert 'f"{source}:{native_id}"' in sec and "skipped_no_id" in sec
    base = REPO / "src" / "episteme" / "data"
    counting = set()
    for src in structured_sources():
        text = (base / src / f"serialize_{src}.py").read_text(encoding="utf-8")
        assert 'r.get("ok")' in text and '"inputs"' in text  # summary shape
        assert f"{src}:" in text or "{SOURCE}:" in text  # source-prefixed id
        assert '"serialize_commit"' in text
        assert "input_key(" in text
        if "skipped_no_id" in text:
            counting.add(src)
    # the doc names exactly the serializers that count skipped_no_id
    named = re.search(r"\| Records with no native id \| (.*?) skip the record", sec)
    assert named, "no-native-id row missing"
    assert set(re.findall(r"`(\w+)`", named.group(1))) == counting


def test_sources_and_entrypoints_are_real():
    text = doc(DOC)
    sec3 = _section(text, "| `source` |", "| `source_file` |")
    for s in SOURCES:
        assert f"`{s}`" in sec3, s
    sec10 = _section(text, "## 10.", "## 11.")
    for p in re.findall(r"^(src/episteme/data/\w+\.py|scripts/data/[\w./]+\.sh)\s", sec10, re.M):
        assert Path(REPO / p).is_file(), p
    assert "docs/11-gxp-data-integrity.md" in sec10
    assert (REPO / "docs" / "11-gxp-data-integrity.md").is_file()


def _code_text(*roots: str) -> str:
    out = []
    for root in roots:
        for p in (REPO / root).rglob("*"):
            if p.is_file() and p.suffix in (".py", ".sh") and ".venv" not in p.parts:
                out.append(p.read_text(encoding="utf-8", errors="ignore"))
    return "\n".join(out)


def test_row_issues_has_no_writer():
    """docs/09 says nothing writes row_issues/; pin that absence."""
    assert "no code in `src/` writes it" in doc(DOC)
    assert "row_issues" not in _code_text("src", "scripts")


def test_unemitted_error_classes_have_no_emitter():
    """docs/09 says io_error / schema_violation / corrupt_source have no emitter."""
    text = doc(DOC)
    code = _code_text("src", "scripts")
    for cls in ("io_error", "schema_violation", "corrupt_source"):
        assert f"`{cls}`" in text, cls
        assert cls not in code, cls


def test_id_less_behaviour_for_uniprot_and_mesh():
    """uniprot raises on an empty accession; mesh silently drops an empty descriptor UI."""
    import io

    from episteme.data.mesh.serialize_mesh import _iter_descriptor_records
    from episteme.data.uniprot.serialize_uniprot import _parse_header

    with pytest.raises(ValueError):
        _parse_header(">sp||NAME_HUMAN Some protein OS=Homo sapiens OX=9606")
    xml = (
        b"<DescriptorRecordSet>"
        b"<DescriptorRecord><DescriptorUI></DescriptorUI>"
        b"<DescriptorName><String>NoId</String></DescriptorName></DescriptorRecord>"
        b"<DescriptorRecord><DescriptorUI>D000001</DescriptorUI>"
        b"<DescriptorName><String>Has Id</String></DescriptorName></DescriptorRecord>"
        b"</DescriptorRecordSet>"
    )
    assert [g[0] for g in _iter_descriptor_records(io.BytesIO(xml))] == ["D000001"]
    sec = _section(doc(DOC), "| Records with no native id |", "\n")
    assert "`mesh` silently drops" in sec and "`uniprot` does not skip" in sec
