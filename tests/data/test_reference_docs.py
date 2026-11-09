"""Reference document acquisition (ICH, MedDRA PtC, EMA guidance).

Fake sessions only: no test calls a server, and ``sleep`` and the clock are injected so no test
waits.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import pytest

from episteme.data.checkpoint_markers import input_key
from episteme.data.reference_docs import acquire
from episteme.data.reference_docs.acquire import DocRef, ZipSlipError, extract_zip, fetch

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "reference_docs"
REPO = Path(__file__).resolve().parents[2]
SOURCE = "ich"


class FakeResp:
    def __init__(self, status=200, body=b"", headers=None, payload=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {}
        self._payload = payload
        self.closed = False

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1 << 20):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """``routes`` maps a url to one response or a list of responses (the last one repeats)."""

    def __init__(self, routes):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in routes.items()}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        queue = self.routes[url]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    @property
    def urls(self):
        return [u for u, _ in self.calls]


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _ref(name="a.pdf", url=None, source=SOURCE, rel=None):
    return DocRef(
        source=source,
        url=url or f"https://files.test/{name}",
        rel_path=rel or name,
        title=name,
        version="Step 4",
        step_date="1 January 2020",
        licence="test licence",
    )


def _run(refs, raw, session, clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("min_interval_s", 0)
    kw.setdefault("budget_s", None)
    return fetch(refs, raw, session=session, clock=clock, sleep=clock.sleep, **kw), clock


def _manifest(raw, source=SOURCE):
    path = raw / source / "_manifest.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def _endpoints(monkeypatch):
    monkeypatch.setattr(
        acquire,
        "source_url",
        lambda name: {
            "ICH_API_BASE": "https://admin.ich.org/api/v1",
            "ICH_FILES_BASE": "https://database.ich.org",
            "MEDDRA_API_BASE": "https://admin.meddra.org/api",
            "MEDDRA_PTC_TAXONOMY": "tax-1",
            "EMA_BASE": "https://ema.test",
        }[name],
    )


# ----------------------------------------------------------------------------- planning


def test_plan_ich_reads_files_from_node_json():
    api = "https://admin.ich.org/api/v1/nodes?alias=/page/{}&loadEntities[]=paragraph"
    session = FakeSession(
        {
            api.format("efficacy-guidelines"): FakeResp(
                payload=_fixture("ich_efficacy_trimmed.json")
            ),
            api.format("e2br3-page"): FakeResp(payload=_fixture("ich_e2br3_trimmed.json")),
        }
    )
    refs = acquire.plan_ich(
        session, ["efficacy-guidelines", "e2br3-page"], pause_s=0, sleep=lambda s: None
    )
    by_url = {r.url: r for r in refs}
    e2a = by_url["https://database.ich.org/sites/default/files/E2A_Guideline.pdf"]
    assert e2a.source == "ich"
    assert e2a.rel_path == "E2A/E2A_Guideline.pdf"
    assert e2a.title == "ICH E2A Guideline"
    assert e2a.version == "Step 5"
    assert e2a.step_date == "27 October 1994"
    assert "ICH" in e2a.licence
    # video files are not documents
    assert not [r for r in refs if r.url.endswith(".mp4")]
    # a link inside page text is read, under a directory named after the page
    ig = [r for r in refs if r.url.endswith("IG_Complete_Package_v1_11_1.zip")]
    assert len(ig) == 1
    assert ig[0].rel_path.startswith("page_e2br3-page/")
    assert ig[0].url.startswith("https://admin.ich.org/")
    # every url once
    assert len({r.url for r in refs}) == len(refs)
    assert all(c["headers"]["User-Agent"] for _, c in session.calls)
    assert all("@" not in c["headers"]["User-Agent"] for _, c in session.calls)


def test_plan_meddra_lists_english_docs():
    url = "https://admin.meddra.org/api/guidances-page?taxonomy=tax-1&_format=json"
    session = FakeSession({url: FakeResp(payload=_fixture("meddra_guidances_trimmed.json"))})
    refs = acquire.plan_meddra(session)
    names = {r.rel_path for r in refs}
    assert "001329_termselptc_r4_26_mar2026.pdf" in names
    assert "001329_termselptc_r4_26_mar2026.docx" in names
    assert "000026_MedDRA_Best_Practices___2018.pdf" in names or any("Best" in n for n in names)
    ts = next(r for r in refs if r.rel_path == "001329_termselptc_r4_26_mar2026.pdf")
    assert ts.source == "meddra_ptc"
    assert ts.version == "4.26"
    assert ts.step_date == "2026-03-01"
    assert ts.title.startswith("MedDRA Term Selection")
    # the version item (release notes, guides) and the html renderings are left out
    assert not [r for r in refs if "intguide" in r.url or r.url.endswith(".html")]
    assert all(r.url.startswith("https://admin.meddra.org/") for r in refs)


def test_plan_ema_reads_paths_only_and_refuses_search(tmp_path):
    refs = acquire.plan_ema()
    assert len(refs) == 55
    assert all(r.url.startswith("https://ema.test/en/documents/") for r in refs)
    assert len({r.rel_path for r in refs}) == 55
    bad = tmp_path / "paths.txt"
    bad.write_text("/en/search?q=x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        acquire.plan_ema(bad)
    bad.write_text("/en/documents/x/y_en.pdf\nhttps://evil.test/a.pdf\n", encoding="utf-8")
    with pytest.raises(ValueError):
        acquire.plan_ema(bad)


# ----------------------------------------------------------------------------- fetch


def test_fetch_skips_unchanged_file(tmp_path):
    ref = _ref()
    session = FakeSession({ref.url: FakeResp(body=b"pdf one")})
    rc, _ = _run([ref], tmp_path, session)
    assert rc == 0
    assert (tmp_path / SOURCE / "a.pdf").read_bytes() == b"pdf one"
    assert len(session.calls) == 1
    # second run: the manifest and the file agree, so no request
    rc, _ = _run([ref], tmp_path, session)
    assert rc == 0
    assert len(session.calls) == 1
    assert len(_manifest(tmp_path)) == 1
    # a refresh asks again; the same bytes add nothing
    rc, _ = _run([ref], tmp_path, session, refresh=True)
    assert rc == 0
    assert len(session.calls) == 2
    assert len(_manifest(tmp_path)) == 1
    assert not list((tmp_path / SOURCE).glob("*.part"))


def test_fetch_keeps_changed_upstream_file_as_new_version(tmp_path, monkeypatch, caplog):
    ref = _ref()
    session = FakeSession({ref.url: [FakeResp(body=b"v1"), FakeResp(body=b"version two")]})
    _run([ref], tmp_path, session)
    monkeypatch.setattr(
        acquire, "_utcnow", lambda: acquire.datetime(2026, 11, 9, tzinfo=acquire.timezone.utc)
    )
    with caplog.at_level("WARNING"):
        rc, _ = _run([ref], tmp_path, session, refresh=True)
    assert rc == 0
    src = tmp_path / SOURCE
    assert (src / "a.pdf").read_bytes() == b"v1"  # never overwritten
    assert (src / "a.20261109.pdf").read_bytes() == b"version two"
    assert "version change" in caplog.text
    recs = _manifest(tmp_path)
    assert [r["rel_path"] for r in recs] == ["a.pdf", "a.20261109.pdf"]
    assert recs[1]["supersedes"] == "a.pdf"
    assert recs[1]["sha256"] == hashlib.sha256(b"version two").hexdigest()
    # the next run compares with the newest record and skips it
    rc, _ = _run([ref], tmp_path, FakeSession({ref.url: FakeResp(body=b"version two")}))
    assert rc == 0
    assert len(_manifest(tmp_path)) == 2


def test_429_with_retry_after_waits_then_stops_with_3(tmp_path):
    ref = _ref("b.pdf")
    other = _ref("c.pdf")
    session = FakeSession(
        {
            ref.url: FakeResp(status=429, headers={"Retry-After": "7"}),
            other.url: FakeResp(body=b"never reached"),
        }
    )
    rc, clock = _run([ref, other], tmp_path, session, min_interval_s=1)
    assert rc == 3
    assert clock.sleeps == [7.0]
    assert session.urls == [ref.url, ref.url]  # one retry after the wait, then stop
    assert not (tmp_path / SOURCE / "b.pdf").exists()
    assert not (tmp_path / SOURCE / "c.pdf").exists()


def test_429_then_success_goes_on(tmp_path):
    ref = _ref("b.pdf")
    session = FakeSession(
        {ref.url: [FakeResp(status=429, headers={"Retry-After": "3"}), FakeResp(body=b"ok")]}
    )
    rc, clock = _run([ref], tmp_path, session)
    assert rc == 0
    assert clock.sleeps == [3.0]
    assert (tmp_path / SOURCE / "b.pdf").read_bytes() == b"ok"


def test_retry_after_over_the_cap_stops_without_waiting(tmp_path):
    ref = _ref("b.pdf")
    session = FakeSession({ref.url: FakeResp(status=429, headers={"Retry-After": "5000"})})
    rc, clock = _run([ref], tmp_path, session)
    assert rc == 3
    assert clock.sleeps == []
    assert len(session.calls) == 1


def test_html_page_instead_of_document_stops_with_3(tmp_path):
    ref = _ref("d.pdf", source="ema_guidance")
    session = FakeSession(
        {ref.url: FakeResp(body=b"<html>verify</html>", headers={"Content-Type": "text/html"})}
    )
    rc, _ = _run([ref], tmp_path, session)
    assert rc == 3
    assert not (tmp_path / "ema_guidance" / "d.pdf").exists()


def test_missing_file_is_a_failure_but_the_rest_goes_on(tmp_path):
    bad, good = _ref("bad.pdf"), _ref("good.pdf")
    session = FakeSession({bad.url: FakeResp(status=404), good.url: FakeResp(body=b"x")})
    rc, _ = _run([bad, good], tmp_path, session)
    assert rc == 1
    assert (tmp_path / SOURCE / "good.pdf").exists()
    assert not (tmp_path / SOURCE / "bad.pdf").exists()


def test_min_interval_is_respected(tmp_path):
    refs = [_ref(f"f{i}.pdf") for i in range(3)]
    session = FakeSession({r.url: FakeResp(body=b"x") for r in refs})
    stamps: list[float] = []
    clock = Clock()
    orig = session.get

    def timed(url, **kw):
        stamps.append(clock())
        return orig(url, **kw)

    session.get = timed
    rc, _ = _run(refs, tmp_path, session, clock=clock, min_interval_s=60)
    assert rc == 0
    assert clock.sleeps == [60.0, 60.0]  # none before the first request
    assert [b - a for a, b in zip(stamps, stamps[1:], strict=False)] == [60.0, 60.0]


def test_budget_stops_with_3_and_resumes(tmp_path):
    refs = [_ref(f"f{i}.pdf") for i in range(3)]
    session = FakeSession({r.url: FakeResp(body=b"x") for r in refs})
    rc, _ = _run(refs, tmp_path, session, min_interval_s=60, budget_s=100)
    assert rc == 3
    assert len(session.calls) == 2  # the third request would end past the budget
    rc, _ = _run(refs, tmp_path, session, min_interval_s=60, budget_s=100)
    assert rc == 0
    assert len(session.calls) == 3
    assert len(_manifest(tmp_path)) == 3


# ----------------------------------------------------------------------------- zip


def _zip_bytes(members: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777) << 16
            zf.writestr(info, target)
    return buf.getvalue()


def test_zip_is_unpacked_to_a_sibling_directory(tmp_path):
    ref = _ref("pack.zip", rel="G/pack.zip")
    body = _zip_bytes({"a.txt": b"A", "sub/b.txt": b"B"})
    rc, _ = _run([ref], tmp_path, FakeSession({ref.url: FakeResp(body=body)}))
    assert rc == 0
    assert (tmp_path / SOURCE / "G" / "pack" / "sub" / "b.txt").read_bytes() == b"B"
    assert _manifest(tmp_path)[0]["members"] == 2


def test_zip_slip_member_is_refused(tmp_path):
    with pytest.raises(ZipSlipError):
        extract_zip_from(tmp_path, {"../evil.txt": b"x"})
    with pytest.raises(ZipSlipError):
        extract_zip_from(tmp_path, {"ok.txt": b"x", "a/../../evil.txt": b"x"})
    with pytest.raises(ZipSlipError):
        extract_zip_from(tmp_path, {"/abs.txt": b"x"})
    with pytest.raises(ZipSlipError):
        extract_zip_from(tmp_path, {"..\\evil.txt": b"x"})
    with pytest.raises(ZipSlipError):
        extract_zip_from(tmp_path, {"C:/evil.txt": b"x"})
    with pytest.raises(ZipSlipError):
        extract_zip_from(tmp_path, {"ok.txt": b"x"}, links={"lnk": "../../etc"})
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path / "out" / "ok.txt").exists()  # nothing written before the check passed


def extract_zip_from(tmp_path, members, links=None):
    z = tmp_path / "bad.zip"
    z.write_bytes(_zip_bytes(members, links))
    return extract_zip(z, tmp_path / "out")


def test_fetch_refuses_a_slip_zip_and_keeps_no_manifest_line(tmp_path):
    ref = _ref("evil.zip")
    ok = _ref("fine.pdf")
    session = FakeSession(
        {
            ref.url: FakeResp(body=_zip_bytes({"../x.txt": b"x"})),
            ok.url: FakeResp(body=b"fine"),
        }
    )
    rc, _ = _run([ref, ok], tmp_path, session)
    assert rc == 1
    assert not (tmp_path / SOURCE / "evil.zip").exists()
    assert [r["rel_path"] for r in _manifest(tmp_path)] == ["fine.pdf"]
    assert not (tmp_path / "x.txt").exists()


# ----------------------------------------------------------------------------- manifest


def test_manifest_keys_on_input_key(tmp_path):
    ref = _ref("a.pdf", rel="E2A/a.pdf")
    body = b"content"
    rc, _ = _run([ref], tmp_path, FakeSession({ref.url: FakeResp(body=body)}))
    assert rc == 0
    rec = _manifest(tmp_path)[0]
    dest = tmp_path / SOURCE / "E2A" / "a.pdf"
    assert rec["input_key"] == input_key(dest, tmp_path) == "ich__E2A__a.pdf"
    assert rec["sha256"] == hashlib.sha256(body).hexdigest()
    assert rec["bytes"] == len(body)
    assert rec["url"] == ref.url
    assert rec["rel_path"] == "E2A/a.pdf"
    assert rec["version"] == "Step 4"
    assert rec["step_date"] == "1 January 2020"
    assert rec["licence"] == "test licence"
    assert rec["source"] == SOURCE
    assert rec["fetched_at"].endswith("Z")


def test_input_key_collision_is_refused(tmp_path):
    a = _ref("x", url="https://files.test/1", rel="a/b__c")
    b = _ref("y", url="https://files.test/2", rel="a__b/c")
    with pytest.raises(ValueError, match="collision"):
        fetch([a, b], tmp_path, session=FakeSession({}), min_interval_s=0, budget_s=None)


def test_rel_path_cannot_leave_the_source_directory(tmp_path):
    ref = _ref("x", rel="../escape.pdf")
    with pytest.raises(ValueError):
        fetch([ref], tmp_path, session=FakeSession({}), min_interval_s=0, budget_s=None)


# ----------------------------------------------------------------------------- guard


def test_no_url_literal_outside_sources_env():
    """The endpoint guard of CLAUDE.md, in Python: no host or URL under scripts/data/ except
    in _lib/sources.env (comment lines are allowed). None in the new module either."""
    pattern = re.compile(r"ftp\.|s3://|https?://")
    hits = []
    for path in (REPO / "scripts" / "data").rglob("*"):
        if not path.is_file() or path.as_posix().endswith("_lib/sources.env"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line) and not line.lstrip().startswith("#"):
                hits.append(f"{path.relative_to(REPO)}:{number}")
    assert hits == []
    for path in (REPO / "src" / "episteme" / "data" / "reference_docs").glob("*"):
        if path.suffix in (".py", ".txt"):
            text = path.read_text(encoding="utf-8")
            assert "http://" not in text and "https://" not in text, path.name


def test_file_on_disk_without_manifest_is_adopted_without_a_request(tmp_path):
    ref = _ref("a.pdf", rel="E2A/a.pdf")
    dest = tmp_path / SOURCE / "E2A" / "a.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"by hand")
    session = FakeSession({ref.url: FakeResp(body=b"must not be requested")})
    rc, _ = _run([ref], tmp_path, session)
    assert rc == 0
    assert session.calls == []
    assert dest.read_bytes() == b"by hand"
    rec = _manifest(tmp_path)[0]
    assert rec["adopted"] is True
    assert rec["sha256"] == hashlib.sha256(b"by hand").hexdigest()
    assert rec["input_key"] == "ich__E2A__a.pdf"
    # the next run skips it through the manifest
    rc, _ = _run([ref], tmp_path, session)
    assert rc == 0 and session.calls == []
    assert len(_manifest(tmp_path)) == 1


def test_a_refresh_after_adoption_keeps_a_changed_upstream_file_apart(tmp_path):
    ref = _ref("a.pdf")
    dest = tmp_path / SOURCE / "a.pdf"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"old")
    _run([ref], tmp_path, FakeSession({}))
    session = FakeSession({ref.url: FakeResp(body=b"new upstream")})
    rc, _ = _run([ref], tmp_path, session, refresh=True)
    assert rc == 0
    assert dest.read_bytes() == b"old"
    assert len(_manifest(tmp_path)) == 2


# ----------------------------------------------------------------------------- fix round 1


def _node(links_html: str, files=()):
    return {
        "items": [
            {
                "mainWidgets": {
                    "items": [
                        {
                            "text": links_html,
                            "entries": [
                                {
                                    "code": "E9",
                                    "status": "Step 4",
                                    "details": {"stepDate": "1 May 2000"},
                                    "fileGroups": [
                                        {"files": [{"title": "t", "uri": u} for u in files]}
                                    ],
                                }
                            ],
                        }
                    ]
                }
            }
        ]
    }


def test_plan_ich_drops_off_host_and_search_links():
    url = "https://admin.ich.org/api/v1/nodes?alias=/page/p&loadEntities[]=paragraph"
    html_text = (
        '<a href="https://admin.ich.org/x/ok.pdf">ok</a>'
        '<a href="https://www.ema.europa.eu/en/documents/a_en.pdf">ema</a>'
        '<a href="https://admin.ich.org/search/find.pdf">search</a>'
    )
    node = _node(
        html_text,
        files=[
            "/sites/default/files/rel.pdf",
            "https://evil.test/x.pdf",
            "https://database.ich.org/search/y.pdf",
        ],
    )
    refs = acquire.plan_ich(
        FakeSession({url: FakeResp(payload=node)}), ["p"], pause_s=0, sleep=lambda s: None
    )
    assert sorted(r.url for r in refs) == [
        "https://admin.ich.org/x/ok.pdf",
        "https://database.ich.org/sites/default/files/rel.pdf",
    ]


def test_plan_ich_pause_comes_from_settings():
    base = "https://admin.ich.org/api/v1/nodes?alias=/page/{}&loadEntities[]=paragraph"
    session = FakeSession(
        {base.format("a"): FakeResp(payload={"items": []}), base.format("b"): FakeResp(payload={})}
    )
    sleeps: list[float] = []
    acquire.plan_ich(session, ["a", "b"], sleep=sleeps.append)
    assert sleeps == [acquire.get_settings().reference_docs_api_pause_s]
    assert sleeps == [1.0]


def test_plan_meddra_drops_off_host_urls():
    url = "https://admin.meddra.org/api/guidances-page?taxonomy=tax-1&_format=json"
    payload = [
        {
            "language": "English",
            "items": [
                {
                    "title": "Points to Consider",
                    "documentCollection": [
                        {
                            "documentTitle": "X Release 1.0",
                            "guidanceFilePdf": {"url": "https://admin.meddra.org/f/a.pdf"},
                            "guidanceFileDoc": {
                                "url": "https://www.ema.europa.eu/en/documents/a.docx"
                            },
                            "guidanceFileSpreadsheet": {
                                "url": "https://admin.meddra.org/search/b.xlsx"
                            },
                        }
                    ],
                }
            ],
        }
    ]
    refs = acquire.plan_meddra(FakeSession({url: FakeResp(payload=payload)}))
    assert [r.url for r in refs] == ["https://admin.meddra.org/f/a.pdf"]


def test_fetch_refuses_a_search_url(tmp_path):
    ref = _ref("s.pdf", url="https://ema.test/en/search/s.pdf")
    with pytest.raises(ValueError, match="search"):
        fetch([ref], tmp_path, session=FakeSession({}), min_interval_s=0, budget_s=None)


def test_clashing_file_names_get_distinct_names():
    url = "https://admin.ich.org/api/v1/nodes?alias=/page/p&loadEntities[]=paragraph"
    node = _node(
        "", files=["https://database.ich.org/a/same.pdf", "https://database.ich.org/b/same.pdf"]
    )
    refs = acquire.plan_ich(
        FakeSession({url: FakeResp(payload=node)}), ["p"], pause_s=0, sleep=lambda s: None
    )
    names = [r.rel_path for r in refs]
    assert len(names) == 2 and len(set(names)) == 2
    assert names[0] == "E9/same.pdf" and names[1].startswith("E9/same__")
    assert names[1].endswith(".pdf")
    again = acquire.plan_ich(
        FakeSession({url: FakeResp(payload=node)}), ["p"], pause_s=0, sleep=lambda s: None
    )
    assert [r.rel_path for r in again] == names


def test_ema_names_that_clash_are_made_distinct(tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("/en/documents/a/x_en.pdf\n/en/documents/b/x_en.pdf\n", encoding="utf-8")
    refs = acquire.plan_ema(f)
    assert len({r.rel_path for r in refs}) == 2


def test_content_encoded_download_is_not_a_short_read(tmp_path):
    ref = _ref("c.xml")
    body = b"<a>decoded body, longer than the encoded size</a>"
    resp = FakeResp(body=body, headers={"Content-Length": "10", "Content-Encoding": "gzip"})
    rc, _ = _run([ref], tmp_path, FakeSession({ref.url: resp}))
    assert rc == 0
    assert (tmp_path / SOURCE / "c.xml").read_bytes() == body


def test_short_read_without_encoding_is_a_failure(tmp_path):
    ref = _ref("c.pdf")
    resp = FakeResp(body=b"abc", headers={"Content-Length": "10"})
    rc, _ = _run([ref], tmp_path, FakeSession({ref.url: resp}))
    assert rc == 1
    assert not (tmp_path / SOURCE / "c.pdf").exists()
    assert not list((tmp_path / SOURCE).glob("*.part"))


class RaisingSession(FakeSession):
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        raise acquire.requests.ConnectionError("boom")


def test_connection_error_fails_the_file_for_ich_and_continues(tmp_path):
    a, b = _ref("a.pdf"), _ref("b.pdf")

    class Mixed(FakeSession):
        def get(self, url, **kwargs):
            if url == a.url:
                self.calls.append((url, kwargs))
                raise acquire.requests.Timeout("slow")
            return super().get(url, **kwargs)

    rc, _ = _run([a, b], tmp_path, Mixed({b.url: FakeResp(body=b"b")}))
    assert rc == 1
    assert (tmp_path / SOURCE / "b.pdf").exists()


def test_connection_error_stops_ema_with_3(tmp_path):
    a = _ref("a.pdf", source="ema_guidance")
    b = _ref("b.pdf", source="ema_guidance")
    session = RaisingSession({})
    rc, _ = _run([a, b], tmp_path, session)
    assert rc == 3
    assert len(session.calls) == 1


def test_html_answer_is_a_failed_file_for_ich_but_a_stop_for_ema(tmp_path):
    html_resp = {"Content-Type": "text/html; charset=utf-8"}
    a, b = _ref("a.pdf"), _ref("b.pdf")
    session = FakeSession(
        {a.url: FakeResp(body=b"<html></html>", headers=html_resp), b.url: FakeResp(body=b"ok")}
    )
    rc, _ = _run([a, b], tmp_path, session)
    assert rc == 1
    assert (tmp_path / SOURCE / "b.pdf").exists()
    e = _ref("e.pdf", source="ema_guidance")
    f = _ref("f.pdf", source="ema_guidance")
    session = FakeSession(
        {e.url: FakeResp(body=b"<html></html>", headers=html_resp), f.url: FakeResp(body=b"ok")}
    )
    rc, _ = _run([e, f], tmp_path, session)
    assert rc == 3
    assert len(session.calls) == 1


def test_403_stops_ema_but_is_a_failed_file_for_ich(tmp_path):
    e = _ref("e.pdf", source="ema_guidance")
    f = _ref("f.pdf", source="ema_guidance")
    session = FakeSession({e.url: FakeResp(status=403), f.url: FakeResp(body=b"ok")})
    rc, _ = _run([e, f], tmp_path, session)
    assert rc == 3
    assert len(session.calls) == 1
    a, b = _ref("a.pdf"), _ref("b.pdf")
    session = FakeSession({a.url: FakeResp(status=403), b.url: FakeResp(body=b"ok")})
    rc, _ = _run([a, b], tmp_path, session)
    assert rc == 1
    assert len(session.calls) == 2


def test_damaged_stored_file_is_restored_when_upstream_is_unchanged(tmp_path):
    ref = _ref("a.pdf")
    _run([ref], tmp_path, FakeSession({ref.url: FakeResp(body=b"whole file")}))
    stored = tmp_path / SOURCE / "a.pdf"
    stored.write_bytes(b"damaged")
    session = FakeSession({ref.url: FakeResp(body=b"whole file")})
    rc, _ = _run([ref], tmp_path, session)
    assert rc == 0
    assert stored.read_bytes() == b"whole file"
    assert len(_manifest(tmp_path)) == 1


def test_deleted_unpack_folder_is_made_again_without_a_request(tmp_path):
    import shutil

    ref = _ref("pack.zip")
    body = _zip_bytes({"a.txt": b"A"})
    _run([ref], tmp_path, FakeSession({ref.url: FakeResp(body=body)}))
    folder = tmp_path / SOURCE / "pack"
    shutil.rmtree(folder)
    session = FakeSession({})
    rc, _ = _run([ref], tmp_path, session)
    assert rc == 0
    assert session.calls == []
    assert (folder / "a.txt").read_bytes() == b"A"
