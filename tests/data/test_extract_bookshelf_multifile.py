"""Multi-file Bookshelf packages: one XML file per chapter."""

import io
import json
import tarfile

from episteme.data.bookshelf.extract_bookshelf import parse_book_archive

_LONG = (
    "Chapter body text that runs on long enough to be a real retrieval unit "
    "for the fixture, covering nothing anyone will read closely. "
) * 3

_PERMS = (
    "<permissions><license><license-p>Distributed under a Creative Commons "
    "Attribution 4.0 International licence (CC BY).</license-p></license></permissions>"
)


def _meta(title="Multi Book"):
    return (
        f"<book-meta><book-title-group><book-title>{title}</book-title></book-title-group>"
        f"<pub-date date-type='pub'><year>2021</year></pub-date>{_PERMS}</book-meta>"
    )


def _wrapper(inner, title="Multi Book"):
    return f"<book-part-wrapper>{_meta(title)}{inner}</book-part-wrapper>"


def _chapter(part_id, title, body=None):
    body = body or f"{_LONG} UNIQUE-{title.replace(' ', '-')}"
    idattr = f' id="{part_id}"' if part_id else ""
    return (
        f"<book-part{idattr}><book-part-meta><title-group><title>{title}</title>"
        f"</title-group></book-part-meta><body><p>{body}</p></body></book-part>"
    )


def _write_pkg(tmp_path, name, members):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(raw_dir / name, "w:gz") as tar:
        for member, xml in members:
            data = xml.encode("utf-8")
            info = tarfile.TarInfo(name=member)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw_dir


def _parse(tmp_path, name, members):
    raw_dir = _write_pkg(tmp_path, name, members)
    rows = parse_book_archive(raw_dir / name, raw_dir)
    book = [r for r in rows if r["container_id"] is None]
    parts = [r for r in rows if r["container_id"] is not None]
    return rows, book, parts


def test_multi_file_package(tmp_path):
    members = [
        (
            "NBK77/TOC.nxml",
            _wrapper("<toc><toc-entry>Contents list of the multi book</toc-entry></toc>"),
        ),
        ("NBK77/ch1.nxml", _wrapper(_chapter("ch1", "Chapter One"))),
        ("NBK77/ch2.nxml", _wrapper(_chapter("ch2", "Chapter Two"))),
        (
            "NBK77/app1.nxml",
            _wrapper(
                '<book-app id="app1"><book-app-meta><title-group><title>Appendix A</title>'
                "</title-group></book-app-meta><body><p>"
                f"{_LONG} UNIQUE-Appendix-A</p></body></book-app>"
            ),
        ),
        ("NBK77/ack.nxml", _wrapper('<book-part id="ack"><body/></book-part>')),
    ]
    rows, book, parts = _parse(tmp_path, "tr1_NBK77.tar.gz", members)
    assert len(book) == 1
    assert [p["source_record_id"] for p in parts] == ["ch1", "ch2", "app1"]
    assert [p["title"] for p in parts] == ["Chapter One", "Chapter Two", "Appendix A"]
    for p, marker in zip(parts, ["Chapter-One", "Chapter-Two", "Appendix-A"], strict=True):
        assert f"UNIQUE-{marker}" in p["text"]
        others = [m for m in ("Chapter-One", "Chapter-Two", "Appendix-A") if m != marker]
        assert not any(f"UNIQUE-{m}" in p["text"] for m in others)
    assert book[0]["title"] == "Multi Book"
    assert "Contents list" in book[0]["text"]
    assert book[0]["license"] == "CC BY"
    assert all(p["license"] == "CC BY" for p in parts)
    assert json.loads(book[0]["book_meta"])["n_parts"] == 3
    assert len({r["id"] for r in rows}) == len(rows)
    assert {r["source_file"] for r in rows} == {"tr1_NBK77.tar.gz"}


def test_repeated_part_id_across_members_kept(tmp_path):
    members = [
        ("NBK78/a.nxml", _wrapper(_chapter("sec1", "From A"))),
        ("NBK78/b.nxml", _wrapper(_chapter("sec1", "From B"))),
        ("NBK78/c.nxml", _wrapper(_chapter("sec1", "From C"))),
    ]
    rows, _book, parts = _parse(tmp_path, "tr2_NBK78.tar.gz", members)
    assert [p["title"] for p in parts] == ["From A", "From B", "From C"]
    assert [p["id"] for p in parts] == [
        "bookshelf:NBK78:sec1",
        "bookshelf:NBK78:sec1__b",
        "bookshelf:NBK78:sec1__c",
    ]
    assert [p["source_record_id"] for p in parts] == ["sec1", "sec1", "sec1"]
    assert len({r["id"] for r in rows}) == len(rows)


def test_repeated_id_inside_one_member_gets_counter(tmp_path):
    both = _chapter("x", "One") + _chapter("x", "Two")
    members = [
        ("NBK79/a.nxml", _wrapper(_chapter("x", "Zero"))),
        ("NBK79/b.nxml", _wrapper(both)),
    ]
    rows, _book, parts = _parse(tmp_path, "tr3_NBK79.tar.gz", members)
    assert len(parts) == 3
    assert len({r["id"] for r in rows}) == len(rows)


def test_positional_fallback_counts_across_members(tmp_path):
    members = [
        ("NBK80/a.nxml", _wrapper(_chapter(None, "A"))),
        ("NBK80/b.nxml", _wrapper(_chapter(None, "B"))),
    ]
    _rows, _book, parts = _parse(tmp_path, "tr4_NBK80.tar.gz", members)
    assert [p["source_record_id"] for p in parts] == ["part1", "part2"]


def test_accession_member_preferred_for_book_meta(tmp_path):
    members = [
        ("NBK81/TOC.nxml", _wrapper("<toc><toc-entry>toc text</toc-entry></toc>", "Toc Title")),
        (
            "NBK81/NBK81.nxml",
            _wrapper("<toc><toc-entry>main text</toc-entry></toc>", "Main Title"),
        ),
        ("NBK81/ch1.nxml", _wrapper(_chapter("ch1", "C1"), "Chapter Title")),
    ]
    _rows, book, _parts = _parse(tmp_path, "tr5_NBK81.tar.gz", members)
    assert book[0]["title"] == "Main Title"
    assert "main text" in book[0]["text"]


def test_first_member_when_no_toc_or_accession(tmp_path):
    members = [
        ("NBK82/zz.nxml", _wrapper(_chapter("c", "C"), "First Title")),
        ("NBK82/aa.nxml", _wrapper(_chapter("d", "D"), "Second Title")),
    ]
    _rows, book, _parts = _parse(tmp_path, "tr6_NBK82.tar.gz", members)
    assert book[0]["title"] == "First Title"


def test_members_parse_one_tree_at_a_time(tmp_path, monkeypatch):
    import gc
    import weakref

    from episteme.data.bookshelf import extract_bookshelf as mod

    members = [
        ("NBK90/ch1.nxml", _wrapper(_chapter("ch1", "C1"))),
        ("NBK90/ch2.nxml", _wrapper(_chapter("ch2", "C2"))),
        ("NBK90/TOC.nxml", _wrapper("<toc><toc-entry>t</toc-entry></toc>")),
    ]
    raw_dir = _write_pkg(tmp_path, "tr9_NBK90.tar.gz", members)

    real_parse = mod.ET.parse
    calls: list[str] = []
    live: list[weakref.ref] = []
    max_live = 0

    class _Tree:
        def __init__(self, tree):
            self._tree = tree

        def getroot(self):
            nonlocal max_live
            root = self._tree.getroot()
            live.append(weakref.ref(root))
            gc.collect()
            max_live = max(max_live, sum(1 for r in live if r() is not None))
            return root

    def spy(fh, *a, **k):
        tree = real_parse(fh, *a, **k)
        calls.append(str(len(calls)))
        return _Tree(tree)

    monkeypatch.setattr(mod.ET, "parse", spy)
    rows = mod.parse_book_archive(raw_dir / "tr9_NBK90.tar.gz", raw_dir)
    assert len(calls) == 3
    assert max_live == 1
    # output order is still archive order
    parts = [r for r in rows if r["container_id"] is not None]
    assert [p["source_record_id"] for p in parts] == ["ch1", "ch2"]


def test_preferred_member_last_each_member_opened_once(tmp_path, monkeypatch):
    members = [
        ("NBK91/ch1.nxml", _wrapper(_chapter("ch1", "C1"), "Chapter Title")),
        ("NBK91/ch2.nxml", _wrapper(_chapter("ch2", "C2"), "Chapter Title")),
        ("NBK91/NBK91.nxml", _wrapper("<toc><toc-entry>t</toc-entry></toc>", "Main Title")),
    ]
    raw_dir = _write_pkg(tmp_path, "tr10_NBK91.tar.gz", members)

    opened: list[str] = []
    real_extractfile = tarfile.TarFile.extractfile

    def spy(self, member):
        opened.append(member if isinstance(member, str) else member.name)
        return real_extractfile(self, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", spy)
    rows = parse_book_archive(raw_dir / "tr10_NBK91.tar.gz", raw_dir)
    assert opened == ["NBK91/ch1.nxml", "NBK91/ch2.nxml", "NBK91/NBK91.nxml"]
    book = [r for r in rows if r["container_id"] is None][0]
    assert book["title"] == "Main Title"
    assert [r["source_record_id"] for r in rows if r["container_id"]] == ["ch1", "ch2"]
