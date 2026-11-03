"""reg_record loader, the part that needs no database: input discovery, markers and
resume, per-feed failure, the exit code of ``main``. ``load_release`` and the
connection are replaced by fakes."""

import contextlib
import json
from pathlib import Path

import pytest

from episteme.data.regulatory import load_reg
from episteme.data.regulatory.feeds import Feed

OK = {
    "status": "loaded",
    "rows": 1,
    "duplicates": 0,
    "conflicts": 0,
    "missing_key": 0,
    "bad_dates": 0,
}


def _feed(name: str) -> Feed:
    return Feed(
        name=name,
        url_key="K",
        format="ema_json",
        jurisdiction="EU",
        record_key=("url",),
        substance_fields=(),
        product_fields=(),
        date_field=None,
        cadence="monthly",
    )


class FakeConn:
    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    calls: list[str] = []
    failures: list[tuple[str, str]] = []

    def fake_load(conn, feed, paths, release_id, *, run_id, raw_dir=None):
        calls.append(feed.name)
        if feed.name in env.bad:
            return {**OK, "status": "failed", "reason": "short"}
        if feed.name in env.boom:
            raise RuntimeError("boom")
        return dict(OK)

    monkeypatch.setattr(load_reg, "load_release", fake_load)
    monkeypatch.setattr(
        load_reg,
        "record_failure",
        lambda src, stage, unit, err, run_id=None: failures.append((unit, err)),
    )
    monkeypatch.setattr(load_reg.audit_trail, "record", lambda *a, **k: "x")
    # The database seams: its name, whether it holds the release, the name resolution.
    monkeypatch.setattr(load_reg, "database_name", lambda conn: env.database)
    monkeypatch.setattr(
        load_reg, "release_loaded", lambda conn, dataset, release_id: env.in_db(dataset)
    )
    resolved: list[str] = []

    def fake_resolve(conn, feed, release_id, run_id):
        resolved.append(feed.name)
        return {"matched": 2, "unmatched": 1, "skipped_long": 0}

    monkeypatch.setattr(load_reg, "resolve_release_names", fake_resolve)

    class Env:
        pass

    env = Env()
    env.database = "episteme_test"
    env.in_db = lambda dataset: True
    env.resolved = resolved
    env.bad, env.boom = set(), set()
    env.raw, env.processed = raw, tmp_path / "processed"
    env.calls, env.failures = calls, failures
    return env


def _put(env, *names: str, text: str = "{}") -> None:
    for n in names:
        (env.raw / f"{n}.json").write_text(text, encoding="utf-8")


def _run(env, feeds, **kw):
    args = dict(release=None, force=False, reason=None, run_id="r")
    args.update(kw)
    return load_reg.run_feeds(FakeConn(), "ema", feeds, env.raw, env.processed, **args)


# --- feed_files ---------------------------------------------------------------
def test_feed_files_ema(tmp_path):
    (tmp_path / "ema_orphan.json").write_text("{}")
    assert load_reg.feed_files("ema", _feed("ema_orphan"), tmp_path) == [
        tmp_path / "ema_orphan.json"
    ]
    assert load_reg.feed_files("ema", _feed("ema_pip"), tmp_path) == []


def test_feed_files_openfda_reads_drug_directory_only(tmp_path):
    for sub in ("drug/enforcement", "device/enforcement", "food/enforcement", "drug/ndc"):
        d = tmp_path / sub
        d.mkdir(parents=True)
        (d / f"{sub.replace('/', '-')}-0001.json.zip").write_text("z")
    got = load_reg.feed_files("openfda", _feed("fda_enforcement"), tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in got] == [
        "drug/enforcement/drug-enforcement-0001.json.zip"
    ]
    assert load_reg.feed_files("openfda", _feed("fda_shortages"), tmp_path) == []


# --- marker name and NUL --------------------------------------------------------
def test_marker_name_is_safe_for_odd_release_ids(tmp_path):
    f = _feed("ema_orphan")
    for rel in ("a/b", 'x"; y', "..\\..\\z", "2026-11"):
        name = load_reg.marker_name_for(f, rel, tmp_path)
        assert name.startswith("ema_orphan__")
        assert not set(name) & set('/\\"')
    assert load_reg.marker_name_for(f, "a/b", tmp_path) != load_reg.marker_name_for(
        f, "a_b", tmp_path
    )


def test_dumps_strips_nul_values_but_not_a_literal_backslash_u0000():
    out = json.loads(load_reg._dumps({"a\x00": "x\x00y", "lit": "\\u0000", "n": [1, "\x00"]}))
    assert out == {"a": "xy", "lit": "\\u0000", "n": [1, ""]}
    assert "\x00" not in load_reg._dumps({"k": "v\x00"})


def test_field_values_drop_nul():
    assert load_reg.field_values({"a": "x\x00y"}, "a") == ["xy"]


# --- run_feeds: resume ----------------------------------------------------------
def test_unchanged_input_is_skipped_changed_input_reloads_force_reloads(env):
    _put(env, "f1")
    feeds = [_feed("f1")]
    assert _run(env, feeds)["loaded"] == 1 and env.calls == ["f1"]
    res = _run(env, feeds)
    assert res["skipped"] == 1 and res["loaded"] == 0 and env.calls == ["f1"]
    _put(env, "f1", text='{"changed": true}')  # new size
    assert _run(env, feeds)["loaded"] == 1 and env.calls == ["f1", "f1"]
    assert _run(env, feeds, force=True, reason="why")["loaded"] == 1
    assert env.calls == ["f1", "f1", "f1"]


def test_failed_load_leaves_no_marker_so_the_next_run_retries(env):
    _put(env, "f1")
    env.bad.add("f1")
    assert _run(env, [_feed("f1")])["failed"] == ["f1"]
    env.bad.clear()
    assert _run(env, [_feed("f1")])["loaded"] == 1


def test_a_release_override_gets_its_own_marker(env):
    _put(env, "f1")
    assert _run(env, [_feed("f1")], release="2026-01")["loaded"] == 1
    assert _run(env, [_feed("f1")], release="2026-02")["loaded"] == 1
    assert _run(env, [_feed("f1")], release="2026-01")["skipped"] == 1


def test_a_marker_with_no_loaded_release_in_the_database_loads_again(env, capsys):
    _put(env, "f1")
    feeds = [_feed("f1")]
    assert _run(env, feeds)["loaded"] == 1
    env.in_db = lambda dataset: False  # the rows are gone (another database, a reset)
    res = _run(env, feeds)
    assert res["loaded"] == 1 and res["skipped"] == 0 and env.calls == ["f1", "f1"]
    assert "holds no loaded release" in capsys.readouterr().out


def test_a_marker_made_for_episteme_test_is_not_used_for_episteme(env):
    _put(env, "f1")
    feeds = [_feed("f1")]
    assert _run(env, feeds)["loaded"] == 1  # episteme_test
    env.database = "episteme"
    assert _run(env, feeds)["loaded"] == 1  # its own marker is missing: it loads
    assert _run(env, feeds)["skipped"] == 1
    env.database = "episteme_test"
    assert _run(env, feeds)["skipped"] == 1
    markers = sorted(p.name for p in Path(env.processed).rglob("*.ok"))
    assert len(markers) == 2 and markers[0] != markers[1]
    assert all("f1__" in m for m in markers)


def test_db_marker_name_differs_by_database():
    a = load_reg.db_marker_name("episteme_test", "k")
    b = load_reg.db_marker_name("episteme", "k")
    assert a != b and a.endswith("__k") and b.endswith("__k")
    assert not set(load_reg.db_marker_name('x/"y', "k")) & set('/\\"')


def test_an_ema_release_resolves_its_names_and_records_the_counts(env, capsys):
    _put(env, "f1")
    assert _run(env, [_feed("f1")])["loaded"] == 1
    assert env.resolved == ["f1"]
    assert "names_matched=2 names_unmatched=1" in capsys.readouterr().out
    marker = next(Path(env.processed).rglob("*.ok"))
    stats = json.loads(marker.read_text(encoding="utf-8"))["stats"]
    assert stats["names"] == {"matched": 2, "unmatched": 1, "skipped_long": 0}


def test_a_failed_ema_release_resolves_nothing(env):
    _put(env, "f1")
    env.bad.add("f1")
    _run(env, [_feed("f1")])
    assert env.resolved == []


def test_an_openfda_release_resolves_nothing(env):
    d = env.raw / "drug" / "ndc"
    d.mkdir(parents=True)
    (d / "drug-ndc-0001-of-0001.json.zip").write_text("z")
    feed = _feed("fda_ndc")
    out = load_reg.run_feeds(
        FakeConn(),
        "openfda",
        [feed],
        env.raw,
        env.processed,
        release=None,
        force=False,
        reason=None,
        run_id="r",
    )
    assert out["loaded"] == 1 and env.resolved == []


def test_resolve_release_names_reads_the_release_and_audits_the_counts(monkeypatch):
    from episteme.data.regulatory import load_substance

    seen = {}

    def fake_resolve(conn, names, source):
        seen["names"], seen["source"] = list(names), source
        return {"matched": 1, "unmatched": 1, "skipped_long": 0}

    audits = []
    monkeypatch.setattr(load_substance, "resolve_names", fake_resolve)
    monkeypatch.setattr(load_reg, "release_names", lambda conn, d, r: ["metformin", "nope"])
    monkeypatch.setattr(load_reg.audit_trail, "record", lambda ev, **k: audits.append((ev, k)))
    out = load_reg.resolve_release_names(FakeConn(), _feed("ema_medicines"), "2026-11", "r")
    assert out == {"matched": 1, "unmatched": 1, "skipped_long": 0}
    assert seen == {"names": ["metformin", "nope"], "source": "ema"}
    ((event, kw),) = audits
    assert event == "load_commit" and kw["new_value"] == out
    assert "ema_medicines 2026-11" in kw["object"]


# --- RowPreparer: values too long to index ----------------------------------------
def test_names_and_product_keys_too_long_to_index_are_skipped_and_counted():
    from episteme.data.regulatory.normalize import MAX_INDEX_TEXT_BYTES

    feed = Feed(
        name="t",
        url_key="K",
        format="ema_json",
        jurisdiction="EU",
        record_key=("id",),
        substance_fields=("sub",),
        product_fields=("prod",),
        date_field=None,
        cadence="monthly",
    )
    long = "x" * (MAX_INDEX_TEXT_BYTES + 1)
    rows, stats = load_reg.prepare_rows(
        feed, [{"id": "1", "sub": f"aspirin | {long}", "prod": ["P1", long]}], "f"
    )
    assert rows[0]["substance_names"] == ["aspirin"]
    assert rows[0]["product_keys"] == ["P1"]
    assert stats["long_values"] == 2
    assert rows[0]["payload"]["prod"][1] == long  # the value stays in the payload


# --- run_feeds: failures ----------------------------------------------------------
def test_missing_input_is_a_failed_feed_and_others_still_run(env):
    _put(env, "f2")
    res = _run(env, [_feed("f1"), _feed("f2")])
    assert res["failed"] == ["f1"] and res["loaded"] == 1
    assert env.calls == ["f2"]
    assert [u for u, _ in env.failures] == ["f1"]


def test_one_failed_or_raising_feed_does_not_stop_the_others(env):
    _put(env, "f1", "f2", "f3")
    env.bad.add("f1")
    env.boom.add("f2")
    res = _run(env, [_feed("f1"), _feed("f2"), _feed("f3")])
    assert res["failed"] == ["f1", "f2"] and res["loaded"] == 1
    assert env.calls == ["f1", "f2", "f3"]
    assert {u for u, _ in env.failures} == {"f1", "f2"}


def test_odd_release_id_and_marker_write_failure_stay_inside_the_feed(env, monkeypatch):
    _put(env, "f1", "f2")
    res = _run(env, [_feed("f1")], release='a/b"c')
    assert res["loaded"] == 1 and res["failed"] == []

    def bad_write(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(load_reg.checkpoint_markers, "write_marker", bad_write)
    res = _run(env, [_feed("f1"), _feed("f2")], release="2026-12")
    assert res["failed"] == ["f1", "f2"]  # each failed on its own; the loop went on
    assert env.calls.count("f2") == 1


# --- main -------------------------------------------------------------------------
def test_only_selects_and_rejects_unknown_feed_names():
    feeds = load_reg.select_feeds("ema", ["ema_orphan"])
    assert [f.name for f in feeds] == ["ema_orphan"]
    assert len(load_reg.select_feeds("ema", None)) > 1
    with pytest.raises(ValueError, match="nope"):
        load_reg.select_feeds("ema", ["nope"])
    assert load_reg.main(["--source", "ema", "--only", "nope"]) == 2


class _Handle:
    def exit_code(self, rc):
        return rc


@pytest.fixture
def main_env(env, monkeypatch, tmp_path):
    import episteme.config as cfg
    import episteme.ops.status as status
    import episteme.ops.stop as stop
    from episteme.data.db import connection as db_connection

    monkeypatch.setenv("EPISTEME_RAW_ROOT", str(tmp_path / "rawroot"))
    monkeypatch.setenv("EPISTEME_PROCESSED_ROOT", str(env.processed))
    cfg.get_settings.cache_clear()
    monkeypatch.setattr(db_connection, "admin_dsn_from_settings", lambda: "dsn")

    class Ctx(contextlib.AbstractContextManager):
        def __enter__(self):
            return FakeConn()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(load_reg.psycopg, "connect", lambda *a, **k: Ctx())
    monkeypatch.setattr(stop, "stage_guard", lambda *a, **k: contextlib.nullcontext(_Handle()))

    class _Cur:
        def update(self, **k):
            pass

    monkeypatch.setattr(status, "current", lambda: _Cur())
    (tmp_path / "rawroot" / "ema").mkdir(parents=True)
    yield tmp_path / "rawroot" / "ema"
    cfg.get_settings.cache_clear()


def test_main_exits_non_zero_when_a_feed_has_no_input(main_env, capsys):
    assert load_reg.main(["--source", "ema", "--only", "ema_orphan"]) == 1
    assert "no input files" in capsys.readouterr().out


def test_main_exits_zero_when_every_selected_feed_loads(main_env, env):
    (main_env / "ema_orphan.json").write_text("{}")
    assert load_reg.main(["--source", "ema", "--only", "ema_orphan"]) == 0
    assert env.calls == ["ema_orphan"]
    # a second run skips the unchanged feed and still succeeds
    assert load_reg.main(["--source", "ema", "--only", "ema_orphan"]) == 0
    assert env.calls == ["ema_orphan"]


def _recall_run(monkeypatch, *, failed=(), feed="fda_enforcement", build=None):
    from episteme.data.regulatory import recall_lots

    built: list[str] = []

    def fake_build(conn, release_id, *, run_id=None):
        built.append(release_id)
        if build is not None:
            build()
        return {"recalls": 1}

    monkeypatch.setattr(recall_lots, "build_recall_lots", fake_build)
    monkeypatch.setattr(load_reg, "latest_release", lambda conn, dataset: "r1")
    monkeypatch.setattr(load_reg.audit_trail, "commit", lambda conn: conn.commit())
    outcome = {"loaded": 1, "skipped": 0, "failed": list(failed)}
    load_reg._refresh_recall_lots(FakeConn(), "openfda", [_feed(feed)], outcome, "r")
    return built, outcome


def test_a_good_enforcement_release_builds_its_recall_lots(env, monkeypatch):
    built, outcome = _recall_run(monkeypatch)
    assert built == ["r1"] and outcome["failed"] == []


def test_a_failed_enforcement_release_builds_nothing(env, monkeypatch):
    built, _ = _recall_run(monkeypatch, failed=["fda_enforcement"])
    assert built == []


def test_another_feed_builds_nothing(env, monkeypatch):
    built, _ = _recall_run(monkeypatch, feed="fda_ndc")
    assert built == []


def test_a_failed_recall_lot_build_fails_the_run(env, monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(load_reg.audit_trail, "rollback", lambda conn: conn.rollback())
    built, outcome = _recall_run(monkeypatch, build=boom)
    assert outcome["failed"] == ["recall_lot"]
    assert env.failures[-1][0] == "recall_lot"


def test_main_force_needs_a_reason():
    assert load_reg.main(["--source", "ema", "--force"]) == 2


def test_markers_live_under_the_processed_root(main_env, env):
    (main_env / "ema_orphan.json").write_text("{}")
    load_reg.main(["--source", "ema", "--only", "ema_orphan"])
    found = list(Path(env.processed).rglob("*.json")) + list(Path(env.processed).rglob("*.ok"))
    assert any("ema_orphan__" in str(p) for p in found)


def test_reg_lock_timeout_setting(monkeypatch):
    import episteme.config as cfg

    monkeypatch.delenv("EPISTEME_REG_LOCK_TIMEOUT_S", raising=False)
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().reg_lock_timeout_s == 300
    monkeypatch.setenv("EPISTEME_REG_LOCK_TIMEOUT_S", "7")
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().reg_lock_timeout_s == 7
    monkeypatch.setenv("EPISTEME_REG_LOCK_TIMEOUT_S", "0")  # invalid: back to the default
    cfg.get_settings.cache_clear()
    assert cfg.get_settings().reg_lock_timeout_s == 300
    cfg.get_settings.cache_clear()


def test_lock_timeout_wraps_only_the_partition_ddl_and_turns_into_a_failed_reason(monkeypatch):
    import psycopg.errors

    seen = []

    class Cur:
        def __init__(self):
            self.row = None

        def execute(self, q, params=None):
            seen.append((q, params))
            self.row = ("0",)

        def fetchone(self):
            return self.row

    def blocked(cur, dataset, release_id):
        raise psycopg.errors.LockNotAvailable("x")

    monkeypatch.setattr(load_reg, "_create_partitions", blocked)
    with pytest.raises(load_reg._Invalid, match="EPISTEME_REG_LOCK_TIMEOUT_S"):
        load_reg._create_partitions_with_timeout(Cur(), "d", "r")
    assert any("set_config" in q and p == ("300s",) for q, p in seen)
