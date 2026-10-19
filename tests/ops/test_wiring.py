from episteme.ops import profile as prof
from episteme.ops.hardware import Hardware


def _fake_profile(monkeypatch, **ov):
    p = prof.compute_profile(Hardware(8, 32 * 2**30, {"processed": "ssd"}), prof.Overrides(**ov))
    prof.get_profile.cache_clear()
    monkeypatch.setattr(prof, "get_profile", lambda: p)
    return p


def test_default_batch_rows_uses_the_override(monkeypatch):
    class S:
        batch_rows = 12_345

    monkeypatch.setattr("episteme.config.get_settings", lambda: S())
    assert prof.default_batch_rows() == 12_345


def test_default_batch_rows_rule_by_ram(monkeypatch):
    class S:
        batch_rows = None

    monkeypatch.setattr("episteme.config.get_settings", lambda: S())
    monkeypatch.setattr("episteme.ops.hardware.ram_bytes", lambda: 64 * 2**30)
    assert prof.default_batch_rows() == 100_000
    monkeypatch.setattr("episteme.ops.hardware.ram_bytes", lambda: 16 * 2**30)
    assert prof.default_batch_rows() == 50_000


def test_extract_workers_come_from_profile(monkeypatch):
    p = _fake_profile(monkeypatch)
    from episteme.data import parallel

    assert parallel.default_workers() == p.cpu_workers


def test_pmc_threads_default_from_profile(monkeypatch):
    _fake_profile(monkeypatch, pmc_threads=33)
    from episteme.data.pmc import download_pmc

    args = download_pmc.build_parser().parse_args([])
    assert args.threads is None
    assert download_pmc.resolve_threads(args) == 33 and args.threads == 33
    explicit = download_pmc.build_parser().parse_args(["--threads", "5"])
    assert download_pmc.resolve_threads(explicit) == 5


def test_pmc_build_parser_does_not_detect_hardware(monkeypatch):
    def boom():
        raise AssertionError("get_profile called while building the parser")

    monkeypatch.setattr(prof, "get_profile", boom)
    from episteme.data.pmc import download_pmc

    download_pmc.build_parser()
