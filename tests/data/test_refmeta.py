"""Reference code lists: parsers, the EDQM signature, and download planning. No network."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from episteme.data.refmeta import download, edqm
from episteme.data.refmeta.codelists import parse_codelist_dir
from episteme.data.refmeta.iso639 import parse_iso639
from episteme.data.refmeta.iso3166 import parse_iso3166
from episteme.data.refmeta.load import collect
from episteme.data.refmeta.loinc import parse_loinc_zip
from episteme.data.refmeta.model import CodeRelease, CodeRow
from episteme.data.refmeta.oids import parse_oid_pdf, parse_oid_text
from episteme.data.refmeta.ucum import parse_ucum

_NOTICE = "test notice"
_OID_TEXT = """
Table 1: IDMP
D.8.r.2b Name ISO11615 MPID
D.8.r.3b Name ISO11616 PhPID
D.10.8.r.2b Name ISO11615 MPID
D.10.8.r.3b Name ISO11616 PhPID
G.k.2.1.1b Name ISO11615 MPID
G.k.2.1.2b Name ISO11616 PhPID
G.k.2.3.r.2b Name ISO11238 Substance
G.k.4.r.9.2b Name ISO11239 Forms
G.k.4.r.10.2b Name ISO11239 Routes
G.k.4.r.11.2b Name ISO11239 Parent
Table 2: MedDRA
D.8.r.7b Indication 2.16.840.1.113883.6.163 LLT
"""
_CL = """<?xml version="1.0"?>
<CodeList>
  <Identification>
    <ShortName>ich-type-of-report</ShortName>
    <CanonicalUri>2.16.840.1.113883.3.989.2.1.1.2</CanonicalUri>
  </Identification>
  <SimpleCodeList>
    <Row>
      <Value ColumnRef="code"><SimpleValue>1</SimpleValue></Value>
      <Value ColumnRef="en"><SimpleValue>Spontaneous Report</SimpleValue></Value>
      <Value ColumnRef="ja"><SimpleValue>自発報告</SimpleValue></Value>
      <Value ColumnRef="cf"><SimpleValue>y</SimpleValue></Value>
    </Row>
    <Row>
      <Value ColumnRef="code"><SimpleValue>4</SimpleValue></Value>
      <Value ColumnRef="en"><SimpleValue>Not Available</SimpleValue></Value>
      <Value ColumnRef="cf"><SimpleValue>n</SimpleValue></Value>
    </Row>
  </SimpleCodeList>
</CodeList>
"""
_IG = Path(r"D:\episteme\01_raw\ich\e2\ig_v1_11\1_ICH_ICSR_Implementation_Guide_v5_03.pdf")


def test_empty_notice_and_duplicate_code_are_refused():
    row = CodeRow(code="a", display="b")
    with pytest.raises(ValueError, match="licence notice"):
        CodeRelease("s", "r", "  ", "f", (row,))
    with pytest.raises(ValueError, match="duplicate"):
        CodeRelease("s", "r", _NOTICE, "f", (row, row))


def test_oid_text_keeps_element_ids_and_not_the_oid_as_the_code():
    release = parse_oid_text(_OID_TEXT, source_file="ig")
    by_code = {row.code: row for row in release.codes}
    assert "t1:G.k.4.r.11.2b" in by_code
    assert by_code["t1:G.k.4.r.11.2b"].extra["oid"] is None
    assert by_code["t2:D.8.r.7b"].extra["oid"] == "2.16.840.1.113883.6.163"
    assert len(release.codes) == 11


@pytest.mark.skipif(not _IG.is_file(), reason="local ICH IG package")
def test_real_ig_oid_tables():
    release = parse_oid_pdf(_IG, source_file="ig")
    assert len(release.codes) == 89
    oids = [row.extra.get("oid") for row in release.codes]
    assert oids.count("1.0.3166.1.2.2") == 6
    assert oids.count("1.0.639.2") == 2
    assert oids.count("1.0.5218") == 2
    assert oids.count("2.16.840.1.113883.6.8") == 1
    assert oids.count("2.16.840.1.113883.6.163") == 12
    elements = {row.extra["element_id"] for row in release.codes if row.extra["table"] == "1"}
    assert "G.k.4.r.11.2b" in elements
    assert len(elements) == 10


def test_code_list_prefers_gc_xml_and_keeps_english(tmp_path: Path):
    (tmp_path / "E2B CL2 ich-type-of-report.gc-revised.xml").write_text(_CL, encoding="utf-8")
    (tmp_path / "E2B CL2 ich-type-of-report.gc.xml").write_text(_CL, encoding="utf-8")
    release = parse_codelist_dir(tmp_path, source_file="lists")
    assert [row.code for row in release.codes] == ["CL2:1", "CL2:4"]
    assert release.codes[0].display == "Spontaneous Report"
    assert release.codes[0].extra["ja"] == "自発報告"
    assert release.codes[1].status == "retired"
    assert release.release_id.startswith("v2.11-")


def test_ucum_codes_use_the_code_attribute(tmp_path: Path):
    ucum = parse_ucum(
        _write(
            tmp_path / "ucum-essence.xml",
            "<root><unit Code='10*'><name>the number ten for arbitrary powers</name>"
            "<printSymbol>10</printSymbol></unit>"
            "<unit Code='10^'><name>the number ten for arbitrary powers</name>"
            "<printSymbol>10</printSymbol></unit></root>",
        ),
        notice=_NOTICE,
        source_file="ucum",
        release_id="abc",
    )
    assert [row.code for row in ucum.codes] == ["unit:10*", "unit:10^"]
    assert [row.extra["print_symbol"] for row in ucum.codes] == ["10", "10"]


def test_ucum_iso639_iso3166_and_loinc(tmp_path: Path):
    ucum = parse_ucum(
        _write(
            tmp_path / "ucum-essence.xml",
            "<root><prefix Code='k'><name>kilo</name><printSymbol>k</printSymbol></prefix>"
            "<unit Code='m/s'><name>meter per second</name></unit></root>",
        ),
        notice=_NOTICE,
        source_file="ucum",
        release_id="abc",
    )
    assert [row.code for row in ucum.codes] == ["prefix:k", "unit:m/s"]
    languages = parse_iso639(
        "eng|eng|en|English|anglais\n",
        notice=_NOTICE,
        source_file="iso",
        release_id="abc",
    )
    assert languages.codes[0].code == "eng"
    assert languages.codes[0].extra["alpha2"] == "en"
    countries = parse_iso3166(
        "alpha_2,alpha_3,numeric,short_name_en\nJP,JPN,392,Japan\n",
        notice=_NOTICE,
        source_file="csv",
        release_id="r",
    )
    assert countries.codes[0].code == "JP"
    with pytest.raises(ValueError, match="header"):
        parse_iso3166("a,b\n1,2\n", notice=_NOTICE, source_file="csv", release_id="r")
    archive = tmp_path / "Loinc_2.83.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "Loinc.csv",
            "LOINC_NUM,LONG_COMMON_NAME,STATUS,COMPONENT\n8867-4,Heart rate,ACTIVE,Heart rate\n",
        )
    loinc = parse_loinc_zip(archive, notice=_NOTICE, source_file="zip", release_id="2.83")
    assert loinc.codes[0].code == "8867-4"
    assert loinc.codes[0].extra["COMPONENT"] == "Heart rate"


def test_edqm_terms_and_signature(tmp_path: Path):
    (tmp_path / "ROA.xml").write_text(
        "<terms><term class='ROA' id='20053000'><name lang='en'>Oral use</name></term></terms>",
        encoding="utf-8",
    )
    (tmp_path / "classes.json").write_text(
        '{"content":[{"code":"ROA","name":"Route"},{"code":"SOM","name":"Something"}]}',
        encoding="utf-8",
    )
    (tmp_path / "SOM.json").write_text(
        '{"content":[{"class":"SOM","code":"1","term":"A term","domain":"Human and veterinary",'
        '"status":"Current"}]}',
        encoding="utf-8",
    )
    release = edqm.parse_edqm_dir(tmp_path, notice=_NOTICE, source_file="edqm", release_id="2026")
    by_code = {row.code: row for row in release.codes}
    assert set(by_code) == {"ROA:20053000", "SOM:1"}
    assert by_code["ROA:20053000"].display == "Oral use"
    assert by_code["SOM:1"].extra["domain"] == "Human and veterinary"
    assert by_code["SOM:1"].status == "Current"
    assert edqm.class_data_uri("SOM").startswith(
        "/standardterms/api/v1/full_data_by_class/SOM/1/1?"
    )
    with pytest.raises(ValueError, match="path segment"):
        edqm.class_data_uri("../SOM")
    assert edqm.class_codes([{"code": "ROA"}, {"code": "BDF"}, {"code": "ROA"}]) == ["BDF", "ROA"]
    key = edqm.stapi_key(
        "test-login",
        "test-secret",
        verb="GET",
        uri="/standardterms/api/v1/versions",
        host="standardterms.edqm.eu",
        date="Sun, 15 Nov 2026 00:00:00 GMT",
    )
    assert key == "test-login|2fP1Qf1sQifnZLGwj1yQ=="


def test_missing_lists_are_skipped_and_a_bad_file_fails(tmp_path: Path):
    assert collect(tmp_path, ("iso3166",), required=False) == []
    folder = tmp_path / "refmeta" / "iso3166" / "r1"
    folder.mkdir(parents=True)
    (folder / "codes.csv").write_text("wrong\n", encoding="utf-8")
    (folder / "NOTICE.txt").write_text(_NOTICE, encoding="utf-8")
    with pytest.raises(ValueError, match="header"):
        collect(tmp_path, ("iso3166",), required=True)


def test_download_skips_edqm_without_keys_and_refuses_an_explicit_request(tmp_path, monkeypatch):
    called: list[str] = []
    monkeypatch.setattr(download, "_edqm_configured", lambda: False)
    monkeypatch.setattr(download, "_fetch", lambda system, raw, **kwargs: called.append(system))
    assert download.main(["--max-files", "1", "--raw-dir", str(tmp_path)]) == 0
    assert called == ["ucum"]
    assert download.main(["--system", "edqm", "--raw-dir", str(tmp_path)]) == 3


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _release(source_file: str, display: str = "Spontaneous Report") -> CodeRelease:
    return CodeRelease(
        system_key="ucum",
        release_id="abc123",
        licence_notice=_NOTICE,
        source_file=source_file,
        codes=(CodeRow(code="1", display=display),),
    )


def _marker(path: Path, **payload) -> Path:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_marker_counts_only_for_the_same_database_input_and_content(tmp_path: Path):
    from episteme.data.refmeta.load import marker_counts, release_digest

    rel = _release("refmeta/ucum/abc123/ucum-essence.xml")
    digest = release_digest(rel)
    m = _marker(
        tmp_path / "m.ok",
        database="episteme_test|::1|5433",
        source_file=rel.source_file,
        digest=digest,
    )
    assert marker_counts(
        m, database="episteme_test|::1|5433", source_file=rel.source_file, digest=digest
    )
    # A load into episteme_test must not make the production load skip.
    assert not marker_counts(
        m, database="episteme|::1|5433", source_file=rel.source_file, digest=digest
    )
    # A changed file in the same release folder loads again.
    changed = release_digest(_release(rel.source_file, display="Changed"))
    assert changed != digest
    assert not marker_counts(
        m, database="episteme_test|::1|5433", source_file=rel.source_file, digest=changed
    )
    # A marker from before this change (no database / digest) never counts.
    old = _marker(tmp_path / "old.ok", source_file=rel.source_file, rows=1)
    assert not marker_counts(
        old, database="episteme_test|::1|5433", source_file=rel.source_file, digest=digest
    )
    assert not marker_counts(tmp_path / "missing.ok", database="x", source_file="y", digest="z")
    bad = tmp_path / "bad.ok"
    bad.write_text("{not json", encoding="utf-8")
    assert not marker_counts(bad, database="x", source_file="y", digest="z")


def test_input_key_collision_two_inputs_never_share_a_marker_match(tmp_path: Path):
    """Project rule: the stored source_file is input_key(path, raw); two different
    inputs with the same file name (different release folders or systems) give
    different keys, and a marker of one never skips the other."""
    from episteme.data.checkpoint_markers import input_key
    from episteme.data.refmeta.load import marker_counts, release_digest

    raw = tmp_path / "raw"
    a = raw / "refmeta" / "ucum" / "aaa" / "ucum-essence.xml"
    b = raw / "refmeta" / "ucum" / "bbb" / "ucum-essence.xml"
    c = raw / "refmeta" / "iso639" / "aaa" / "ucum-essence.xml"
    for p in (a, b, c):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<root/>", encoding="utf-8")
    keys = {input_key(p, raw) for p in (a, b, c)}
    assert len(keys) == 3
    ka, kb = input_key(a, raw), input_key(b, raw)
    m = _marker(
        tmp_path / "m.ok", database="d", source_file=ka, digest=release_digest(_release(ka))
    )
    assert not marker_counts(m, database="d", source_file=kb, digest=release_digest(_release(kb)))


def test_edqm_get_signs_the_path_without_the_query(monkeypatch):
    """EDQM refuses a signature over path + query (403, 2026-11-16) and accepts the
    path-only signature with the query sent."""
    signed: list[str] = []
    sent: list[str] = []
    monkeypatch.setattr(
        download, "stapi_key", lambda login, secret, **kw: signed.append(kw["uri"]) or "x|y"
    )
    monkeypatch.setattr(download, "_http_get", lambda url, headers=None: sent.append(url) or b"{}")
    download._edqm_get("https://h.example", "/standardterms/api/v1/classes?limit=5&offset=0", "h")
    assert signed == ["/standardterms/api/v1/classes"]
    assert sent == ["https://h.example/standardterms/api/v1/classes?limit=5&offset=0"]


def test_iso3166_csv_from_pycountry_parses_back():
    version, body = download.iso3166_csv()
    assert version
    rel = parse_iso3166(body.decode("utf-8"), notice=_NOTICE, source_file="x", release_id="r")
    codes = [row.code for row in rel.codes]
    assert len(codes) >= 249 and len(codes) == len(set(codes))
    de = next(row for row in rel.codes if row.code == "DE")
    assert de.extra == {"alpha_3": "DEU", "numeric": "276"}
    assert de.display == "Germany"


def test_iso3166_download_writes_a_loadable_release(tmp_path: Path):
    download._fetch_iso3166(tmp_path, dry_run=False, force=False)
    folders = list((tmp_path / "refmeta" / "iso3166").iterdir())
    assert len(folders) == 1 and folders[0].name.startswith("pycountry-")
    assert {p.name for p in folders[0].iterdir()} == {"codes.csv", "NOTICE.txt", "PROVENANCE.txt"}
    releases = collect(tmp_path, ("iso3166",), required=True)
    assert releases[0].system_key == "iso3166" and len(releases[0].codes) >= 249
    # A second run with the same pycountry version is skipped.
    download._fetch_iso3166(tmp_path, dry_run=False, force=False)
    assert len(list((tmp_path / "refmeta" / "iso3166").iterdir())) == 1


def test_edqm_class_retries_without_tags_after_a_server_error(monkeypatch):
    import requests

    calls: list[str] = []

    def fake_get(base, uri, host):
        calls.append(uri)
        if "tags=1" in uri:
            resp = requests.Response()
            resp.status_code = 500
            raise requests.HTTPError(response=resp)
        return b'{"content": []}'

    monkeypatch.setattr(download, "_edqm_get", fake_get)
    assert download._paged_document("https://h", "h", "MAP") == b'{"content": []}'
    assert "tags=1" in calls[0] and "tags" not in calls[1]


def test_edqm_class_does_not_retry_a_client_error(monkeypatch):
    import requests

    def fake_get(base, uri, host):
        resp = requests.Response()
        resp.status_code = 403
        raise requests.HTTPError(response=resp)

    monkeypatch.setattr(download, "_edqm_get", fake_get)
    with pytest.raises(requests.HTTPError):
        download._paged_document("https://h", "h", "SOM")
