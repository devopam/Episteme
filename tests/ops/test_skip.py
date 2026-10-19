import argparse
from contextlib import contextmanager

import pytest

from episteme.ops import skip


class FakeConn:
    def __init__(self):
        self.log = []

    def commit(self):
        self.log.append("COMMIT")

    def rollback(self):
        self.log.append("ROLLBACK")


@pytest.fixture
def fake(monkeypatch):
    conn = FakeConn()
    rows = []

    @contextmanager
    def _connection():
        yield conn

    def _record(event, **kw):
        rows.append((event, kw["object"], kw["reason"], kw["run_id"]))
        return "h"

    monkeypatch.setattr(skip, "connection", _connection)
    monkeypatch.setattr(skip.audit_trail, "record", _record)
    return conn, rows


def test_add_skip_args_adds_reason_when_missing():
    ap = argparse.ArgumentParser()
    skip.add_skip_args(ap)
    a = ap.parse_args(["--skip-unit", "a", "--skip-unit", "b", "--reason", "r"])
    assert a.skip_unit == ["a", "b"] and a.reason == "r"


def test_add_skip_args_reuses_existing_reason():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", default="x")
    skip.add_skip_args(ap)  # a second --reason would raise ArgumentError
    assert ap.parse_args([]).skip_unit is None


def test_check_skip_args_requires_reason(capsys):
    ap = argparse.ArgumentParser()
    skip.add_skip_args(ap)
    with pytest.raises(SystemExit) as e:
        skip.check_skip_args(ap, ap.parse_args(["--skip-unit", "a"]))
    assert e.value.code == 2
    skip.check_skip_args(ap, ap.parse_args(["--skip-unit", "a", "--reason", "r"]))
    skip.check_skip_args(ap, ap.parse_args([]))


def test_apply_skips_none_touches_nothing(fake):
    conn, rows = fake
    assert skip.apply_skips("s", "load", ["a", "b"], None, None, run_id="r") == ["a", "b"]
    assert rows == [] and conn.log == []


def test_apply_skips_records_one_row_per_unit_and_commits(fake):
    conn, rows = fake
    left = skip.apply_skips("s", "load", ["a", "b", "c"], ["b", "b"], "why", run_id="r1")
    assert left == ["a", "c"]
    assert rows == [("unit_skip", "s b", "why", "r1")]
    assert conn.log == ["COMMIT"]


def test_apply_skips_unknown_name_exits_2_and_writes_nothing(fake, capsys):
    conn, rows = fake
    with pytest.raises(SystemExit) as e:
        skip.apply_skips("s", "load", ["a"], ["a", "zzz"], "why", run_id="r")
    assert e.value.code == 2
    assert "zzz" in capsys.readouterr().err
    assert rows == [] and conn.log == []
