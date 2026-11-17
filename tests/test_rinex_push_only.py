"""Staged-only CLI: generated products, fake DBs, local temporary rsync only."""

import argparse
import gzip
import hashlib
import logging
import shutil
import subprocess
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from receivers import archive
from receivers.archive import reindex as rx
from receivers.cli import arguments
from tests.test_reindex_dual_hash import KEY, REL, Connection

main = import_module("receivers.cli.main")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        arguments, "get_default_values", lambda: {"session": "15s_24hr", "days": 10}
    )
    parser = argparse.ArgumentParser()
    parser.set_defaults(loglevel="WARNING")
    arguments.setup_rinex_parser(parser.add_subparsers())
    work, read_root = tmp_path / "stage", tmp_path / "archive"
    read_root.mkdir()
    product = work / REL
    product.parent.mkdir(parents=True)
    product.write_bytes(gzip.compress(b"generated observations\n" * 10, mtime=1))
    product.chmod(0o600)
    checksum = hashlib.sha256(product.read_bytes()).hexdigest()
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_text(f"{checksum}  {REL}\n")
    argv = [
        "rinex",
        "TEST",
        "--push-only",
        "--manifest",
        str(manifest),
        "--work-dir",
        str(work),
        "--source-dir",
        str(read_root),
        "--catalog-prod",
    ]
    monkeypatch.setattr(
        main, "setup_logging", lambda *a, **k: logging.getLogger("staged-test")
    )
    monkeypatch.setattr(
        archive, "resolve_catalog_hosts", lambda *a, **k: ["db-a", "db-b"]
    )
    monkeypatch.setattr(
        main,
        "_resolve_archive_target",
        lambda: (str(read_root), KEY[0], "~/fixture-archive"),
    )
    raw_presence = import_module("receivers.rinex.raw_presence")
    monkeypatch.setattr(
        raw_presence,
        "check_regenerable",
        lambda *a, **k: SimpleNamespace(regenerable=True),
    )
    monkeypatch.setattr(
        main,
        "_rinex_convert_station_period",
        lambda *a, **k: pytest.fail("conversion entered"),
    )
    monkeypatch.setattr(
        Path, "rglob", lambda *a, **k: pytest.fail("recursive staging walk")
    )
    # An unrelated raw canary is deliberately present in the mixed staging tree.
    raw = work / "2026/jun/TEST/15s_24hr/raw/must-not-be-selected.sbf.gz"
    raw.parent.mkdir()
    raw.write_bytes(b"not a publication input")
    return SimpleNamespace(
        parser=parser,
        argv=argv,
        work=work,
        read_root=read_root,
        manifest=manifest,
        checksum=checksum,
        product=product,
    )


def test_default_is_dry_run_without_converter_or_catalog_access(
    setup, monkeypatch, capsys
):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("dry-run subprocess")
    )
    monkeypatch.setattr(
        archive,
        "reindex_files_multi",
        lambda *a, **k: pytest.fail("dry-run catalog access"),
    )
    assert main.cmd_rinex(setup.parser.parse_args(setup.argv)) == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output and REL in output and setup.checksum in output
    assert "db-a, db-b" in output and str(setup.read_root) in output
    assert not (setup.read_root / REL).exists()


def test_explicit_dry_run_overrides_push(setup, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("dry-run subprocess")
    )
    assert (
        main.cmd_rinex(setup.parser.parse_args(setup.argv + ["--push", "--dry-run"]))
        == 0
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--fix-headers"],
        ["--from-archive"],
        ["--cleanup"],
        ["--backup-old"],
        ["--dates", "20260621"],
        ["--days", "1"],
        ["--version", "3"],
    ],
)
def test_conflicting_modes_refuse_before_push(setup, monkeypatch, extra):
    monkeypatch.setattr(
        main,
        "_push_reconverted",
        lambda *a, **k: pytest.fail("conflicting mode reached push"),
    )
    assert main.cmd_rinex(setup.parser.parse_args(setup.argv + extra)) == 2


def test_missing_manifest_and_implicit_catalog_are_refused(setup):
    args = setup.parser.parse_args(setup.argv)
    args.manifest = None
    assert main.cmd_rinex(args) == 2
    args.manifest, args.catalog_prod = str(setup.manifest), False
    assert main.cmd_rinex(args) == 2


def test_checksum_change_prevents_backend_entry(setup, monkeypatch):
    setup.product.write_bytes(b"changed product")
    monkeypatch.setattr(
        main,
        "_push_reconverted",
        lambda *a, **k: pytest.fail("bad checksum reached backend"),
    )
    assert main.cmd_rinex(setup.parser.parse_args(setup.argv + ["--push"])) == 2


def test_manifest_without_push_only_is_refused_before_config(setup):
    args = setup.parser.parse_args(setup.argv)
    args.push_only = False
    assert main.cmd_rinex(args) == 2


def test_real_local_rsync_and_both_host_hashes_use_archive_readback(setup, monkeypatch):
    if shutil.which("rsync") is None:
        pytest.skip("local rsync executable unavailable")
    conns = {host: Connection() for host in ("db-a", "db-b")}
    real_run = subprocess.run
    commands = []

    def local_only(argv, **kwargs):
        assert argv[0] == "rsync", "no unrelated subprocess permitted"
        assert argv[-2:] == [str(setup.work) + "/", str(setup.read_root) + "/"]
        assert "--chmod=D755,F644" in argv and "--delete" not in argv
        files = Path(argv[argv.index("--files-from") + 1]).read_text().splitlines()
        assert files == [REL]
        commands.append(argv)
        return real_run(argv, **kwargs)

    def reindex(hosts, files, **kwargs):
        assert hosts == ["db-a", "db-b"]
        assert files == [str(setup.read_root / REL)]
        assert kwargs["root"] == str(setup.read_root)
        assert kwargs["expected_checksums"] == {REL: setup.checksum}
        return {host: rx.reindex_files(conns[host], files, **kwargs) for host in hosts}

    monkeypatch.setattr(subprocess, "run", local_only)
    monkeypatch.setattr(archive, "reindex_files_multi", reindex)
    assert main.cmd_rinex(setup.parser.parse_args(setup.argv + ["--push"])) == 0
    assert len(commands) == 1
    published = setup.read_root / REL
    assert published.read_bytes() == setup.product.read_bytes()
    assert published.stat().st_mode & 0o777 == 0o644
    expected = (
        hashlib.sha256(gzip.decompress(published.read_bytes())).hexdigest(),
        setup.checksum,
    )
    for conn in conns.values():
        assert conn.rows[KEY] == expected and conn.commits == 1
    assert setup.product.exists()  # no cleanup
    assert not list(setup.read_root.glob("**/*.sbf.gz"))


@pytest.mark.parametrize("failure", ["different-bytes", "host-failure"])
def test_catalog_mismatch_or_partial_host_never_reports_success(
    setup, monkeypatch, failure
):
    conns = {host: Connection() for host in ("db-a", "db-b")}

    def simulated_transfer(argv, **kwargs):
        assert argv[0] == "rsync"
        dest = setup.read_root / REL
        dest.parent.mkdir(parents=True)
        dest.write_bytes(
            gzip.compress(b"wrong bytes", mtime=1)
            if failure == "different-bytes"
            else setup.product.read_bytes()
        )
        return SimpleNamespace(returncode=0, stdout=REL + "\n", stderr="")

    def reindex(hosts, files, **kwargs):
        return {
            host: (
                None
                if failure == "host-failure" and host == "db-b"
                else rx.reindex_files(conns[host], files, **kwargs)
            )
            for host in hosts
        }

    monkeypatch.setattr(subprocess, "run", simulated_transfer)
    monkeypatch.setattr(archive, "reindex_files_multi", reindex)
    assert main.cmd_rinex(setup.parser.parse_args(setup.argv + ["--push"])) == 1
    if failure == "different-bytes":
        assert all(not conn.rows and conn.commits == 0 for conn in conns.values())
    else:
        assert conns["db-a"].commits == 1 and conns["db-b"].commits == 0


def test_guard_refusal_never_transfers_a_smaller_subset(setup, monkeypatch):
    original = setup.read_root / REL
    original.parent.mkdir(parents=True)
    original.write_bytes(setup.product.read_bytes())
    second = REL.replace("1720", "1730")
    (setup.work / second).write_bytes(setup.product.read_bytes())
    setup.manifest.write_text(f"{setup.checksum}  {REL}\n{setup.checksum}  {second}\n")
    raw_presence = import_module("receivers.rinex.raw_presence")
    monkeypatch.setattr(
        raw_presence,
        "check_regenerable",
        lambda *a, **k: SimpleNamespace(regenerable=False),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: pytest.fail("guard failure transferred a subset"),
    )
    assert main.cmd_rinex(setup.parser.parse_args(setup.argv + ["--push"])) == 1
    assert original.read_bytes() == setup.product.read_bytes()
    assert not (setup.read_root / second).exists()


def test_archive_readback_symlink_is_not_cataloged(setup, monkeypatch):
    destination = setup.read_root / REL
    destination.parent.mkdir(parents=True)
    destination.symlink_to(setup.product)
    conn = Connection()
    stats = rx.reindex_files(
        conn,
        [str(destination)],
        root=str(setup.read_root),
        storage_location=KEY[0],
        dest_prefix="~/fixture",
        expected_checksums={REL: setup.checksum},
    )
    assert stats.errors and conn.commits == 0 and not conn.statements


@pytest.mark.parametrize("outcome", ["timeout", "nonzero", "missing"])
def test_recovery_transport_failure_has_no_durability_or_retry_claim(
    setup, monkeypatch, capsys, outcome
):
    marker = "DO-NOT-PRINT-TRANSPORT-TEXT"
    checks: list[bool] = []

    def transport(argv, **kwargs):
        assert "--no-implied-dirs" in argv
        assert checks == [True]
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(argv, 1, output=f"{REL}\n{marker}")
        if outcome == "missing":
            raise FileNotFoundError(marker)
        return SimpleNamespace(returncode=23, stdout=f"{REL}\n", stderr=marker)

    monkeypatch.setattr(subprocess, "run", transport)
    monkeypatch.setattr(
        main, "_push_reindex", lambda *a, **k: pytest.fail("recovery catalog write")
    )
    args = setup.parser.parse_args(setup.argv + ["--push"])
    result = main._push_reconverted(
        setup.work,
        args,
        logging.getLogger("recovery-failure-test"),
        only_rel=[REL],
        preserved_replacements=[REL],
        before_transfer=lambda: checks.append(True),
        defer_catalog=True,
    )
    assert result["rc"] != 0 and result["pushed"] == 0
    output = capsys.readouterr().out
    assert marker not in output
    assert "durable" not in output and "re-run the push" not in output
    if outcome != "missing":
        assert "UNKNOWN" in output and "reconcile" in output.lower()
