"""AFST-shaped end-to-end workflow, entirely generated/local (no production I/O)."""

import argparse
import gzip
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from receivers.archive.access import WORKER
from receivers.archive.catalog_recovery import archive_values
from receivers.archive.path_parse import parse_archive_path
from receivers.cli import archive_access as access_cli
from receivers.cli import archive_sync as catalog_cli
from receivers.cli import arguments
from tests.test_catalog_recovery import Conn, Store

main = import_module("receivers.cli.main")
MONTHS = (
    "",
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
LOCATION = "fixture_archive"
PREFIX = "~/fixture-archive"


def blob(root, relative, payload, mode=0o644):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload, mtime=1))
    path.chmod(mode)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sums(path, rows):
    path.write_text("".join(f"{h}  {r}\n" for r, h in rows))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def alias(root, relative, label):
    value = f".incoming/{label}/{relative}"
    parent = root
    for part in value.split("/")[:-1]:
        parent = parent / part
        parent.mkdir(exist_ok=True)
        parent.chmod(0o700)
    os.link(root / relative, root / value)
    return value


def worker(packet, real_run):
    proc = real_run(
        [sys.executable, "-B", "-c", WORKER],
        input=json.dumps(packet),
        text=True,
        capture_output=True,
        timeout=90,
    )
    if proc.returncode:
        raise ValueError("local gateway worker refused: " + proc.stdout)
    return json.loads(proc.stdout)


def test_full_1745_145_195_1861_recovery_without_raw_qc_or_replay(
    tmp_path, monkeypatch, capsys
):
    if shutil.which("rsync") is None:
        pytest.skip("local rsync unavailable")
    archive = tmp_path / "archive"
    archive.mkdir(mode=0o755)
    stage = tmp_path / "publish"
    original_roots = [tmp_path / n for n in ("backup-r2", "fixed", "mirror-hdr")]
    all_products, publications, originals, orgs = [], [], [], []
    protected_groups: list[list[tuple[str, str]]] = [[], [], []]
    aliases = {}
    rows = {}
    for index in range(1745):
        day = date(2021, 3, 4) + timedelta(days=index)
        relative = f"{day.year}/{MONTHS[day.month]}/TEST/15s_24hr/rinex/TEST{day:%j}0.{day:%y}D.gz"
        desired = blob(stage, relative, f"accepted candidate {index}\n".encode() * 3)
        group_index = 0 if index < 1482 else (1 if index < 1598 else 2)
        original_relative = (
            relative.replace("/rinex/", "/rinex_org/") if group_index == 1 else relative
        )
        baseline = blob(
            original_roots[group_index],
            original_relative,
            f"original {index}\n".encode() * 3,
            0o664 if group_index == 0 else 0o644,
        )
        protected_groups[group_index].append((original_relative, baseline))
        if index < 1600:
            blob(archive, relative, f"accepted candidate {index}\n".encode() * 3, 0o600)
            aliases[relative] = [alias(archive, relative, "candidates-001")]
        else:
            blob(archive, relative, f"original {index}\n".encode() * 3)
            publications.append((relative, desired))
            originals.append((relative, baseline))
            aliases[relative] = []
        all_products.append((relative, desired))
        if group_index == 1:
            org = relative.replace("/rinex/", "/rinex_org/")
            blob(archive, org, f"original {index}\n".encode() * 3, 0o600)
            aliases[org] = [alias(archive, org, "originals-001")]
            orgs.append((org, baseline))
        if index < 1697:
            identity = parse_archive_path(str(archive / relative), str(archive))
            assert identity is not None and identity.file_date is not None
            if index < 1550:
                content, compressed, size = archive_values(
                    str(archive), relative, desired
                )
            else:
                content, compressed, size = "a" * 64, "b" * 64, 1
            key = Path(relative).name.removesuffix(".gz").lower()
            rows[(LOCATION, "15s_24hr", "rinex", key)] = [
                LOCATION,
                "TEST",
                "15s_24hr",
                "rinex",
                key,
                day.isoformat(),
                None,
                PREFIX + "/" + relative,
                ".gz",
                size,
                content,
                compressed,
                None,
                None,
                None,
            ]
    assert len(publications) == 145 and len(orgs) == 116 and len(rows) == 1697
    scope_file, selected_file, publish_file, originals_file = [
        tmp_path / n
        for n in (
            "scope.sha256",
            "catalog.sha256",
            "publish.sha256",
            "originals.sha256",
        )
    ]
    sums(scope_file, all_products)
    sums(selected_file, all_products[1550:])
    sums(publish_file, publications)
    sums(originals_file, originals)
    access_manifest = tmp_path / "access.sha256"
    sums(access_manifest, all_products + orgs)
    protected = []
    for index, group in enumerate(protected_groups):
        mf = tmp_path / f"protected-{index}.sha256"
        sha = sums(mf, group)
        protected.append(
            {
                "root": str(original_roots[index]),
                "manifest": str(mf),
                "manifest_sha256": sha,
                "mode": 0o664 if index == 0 else 0o644,
            }
        )
    protected_file, alias_file = tmp_path / "protected.json", tmp_path / "aliases.json"
    protected_file.write_text(json.dumps(protected))
    alias_file.write_text(json.dumps(aliases))
    # A mixed-tree raw canary must not be discovered/opened by any phase.
    canary = original_roots[2] / "must-not-open.sbf.gz"
    canary.write_bytes(b"generated raw canary, not an input")
    canary_before = canary.stat()
    monkeypatch.setattr(
        Path, "rglob", lambda *a, **k: pytest.fail("recursive mixed-tree walk")
    )
    monkeypatch.setattr(
        os, "walk", lambda *a, **k: pytest.fail("recursive mixed-tree walk")
    )
    raw_presence = import_module("receivers.rinex.raw_presence")
    monkeypatch.setattr(
        raw_presence, "check_regenerable", lambda *a, **k: pytest.fail("raw discovery")
    )
    monkeypatch.setattr(
        main,
        "_rinex_convert_station_period",
        lambda *a, **k: pytest.fail("scientific conversion/QC rerun"),
    )
    monkeypatch.setattr(
        main, "setup_logging", lambda *a, **k: logging.getLogger("recovery-test")
    )
    monkeypatch.setattr(
        arguments, "get_default_values", lambda: {"days": 10, "session": "15s_24hr"}
    )
    target = SimpleNamespace(
        name=LOCATION,
        dest=PREFIX,
        user="fixture",
        host="fixture-gateway",
        tier="archive",
        active=True,
    )
    remote = f"{target.user}@{target.host}:{target.dest}"
    monkeypatch.setattr(
        main, "_resolve_archive_target", lambda: (remote, LOCATION, PREFIX)
    )
    monkeypatch.setattr(
        catalog_cli, "load_sync_config", lambda *a: [target], raising=False
    )
    import receivers.archive as public_archive

    monkeypatch.setattr(public_archive, "load_sync_config", lambda *a: [target])
    monkeypatch.setattr(
        public_archive, "resolve_catalog_hosts", lambda *a, **k: ["db-a", "db-b"]
    )
    monkeypatch.setattr(access_cli, "load_sync_config", lambda *a: [target])
    monkeypatch.setattr(
        access_cli, "resolve_catalog_hosts", lambda *a, **k: ["db-a", "db-b"]
    )
    stores = {h: Store(rows) for h in ("db-a", "db-b")}
    connections = []

    def connect(*, host_override=None, single_host=False):
        assert host_override in stores and single_host is True
        conn = Conn(stores[host_override])
        connections.append(conn)
        return conn

    import receivers.db.connection as db

    monkeypatch.setattr(db, "get_connection", connect)
    real_run = subprocess.run
    transfers, access_batches = [], []

    def local_only(argv, **kwargs):
        assert argv[0] == "rsync", (
            "unexpected subprocess (no SSH or raw tools permitted)"
        )
        assert argv[-1] == remote + "/"
        listed = Path(argv[argv.index("--files-from") + 1]).read_text().splitlines()
        assert len(listed) <= 50 and all(r in dict(publications) for r in listed)
        assert "--checksum" in argv and "--chmod=D755,F644" in argv
        assert "--no-implied-dirs" in argv  # keep reviewed parent modes/identity
        transfers.append(listed)
        actual = list(argv)
        actual[-1] = str(archive) + "/"
        return real_run(actual, **kwargs)

    monkeypatch.setattr(subprocess, "run", local_only)

    def local_gateway(ssh, packet):
        assert ssh == "fixture@fixture-gateway"
        if packet.get("execute"):
            access_batches.append([r["relative"] for r in packet["rows"]])
        return worker(packet, real_run)

    monkeypatch.setattr(access_cli, "gateway_request", local_gateway)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    arguments.setup_rinex_parser(sub)
    catalog_cli.create_archive_reindex_parser(sub)
    access_cli.create_archive_access_parser(sub)
    catalog_plan = tmp_path / "catalog-plan.json"
    cat_base = [
        "archive-reindex",
        "--dir",
        str(archive),
        "--storage-location",
        LOCATION,
        "--catalog-host",
        "db-a,db-b",
    ]
    create = cat_base + [
        "--write-plan",
        str(catalog_plan),
        "--station",
        "TEST",
        "--manifest",
        str(selected_file),
        "--scope-manifest",
        str(scope_file),
        "--publish-manifest",
        str(publish_file),
        "--publish-root",
        str(stage),
        "--originals-manifest",
        str(originals_file),
        "--originals-root",
        str(original_roots[2]),
    ]
    assert catalog_cli.cmd_archive_reindex(parser.parse_args(create)) == 0
    sha = hashlib.sha256(catalog_plan.read_bytes()).hexdigest()
    assert sum(c.dml for c in connections) == 0
    blocked_mode_plan = tmp_path / "blocked-access.json"
    blocked_access = [
        "archive-access",
        "--catalog-plan",
        str(catalog_plan),
        "--read-root",
        str(archive),
        "--gateway-root",
        str(archive),
        "--catalog-host",
        "db-a,db-b",
        "--manifest",
        str(access_manifest),
        "--aliases",
        str(alias_file),
        "--protected-originals",
        str(protected_file),
        "--write-plan",
        str(blocked_mode_plan),
    ]
    assert access_cli.cmd_archive_access(parser.parse_args(blocked_access)) != 0
    assert not blocked_mode_plan.exists() and not access_batches
    # No catalog prefix may be written while any selected archive file is baseline.
    until = datetime.fromtimestamp(time.time() + 600, UTC).isoformat()
    execute_catalog = cat_base + [
        "--plan",
        str(catalog_plan),
        "--plan-sha256",
        sha,
        "--execute",
        "--owner-until",
        until,
    ]
    assert catalog_cli.cmd_archive_reindex(parser.parse_args(execute_catalog)) != 0
    assert sum(c.dml for c in connections) == 0
    pub = [
        "rinex",
        "TEST",
        "--push-only",
        "--manifest",
        str(publish_file),
        "--work-dir",
        str(stage),
        "--source-dir",
        str(archive),
        "--catalog-host",
        "db-a,db-b",
        "--recovery-plan",
        str(catalog_plan),
        "--plan-sha256",
        sha,
        "--originals-manifest",
        str(originals_file),
        "--originals-root",
        str(original_roots[2]),
        "--owner-until",
        until,
    ]
    assert main.cmd_rinex(parser.parse_args(pub)) == 0
    assert not transfers and sum(c.dml for c in connections) == 0
    assert main.cmd_rinex(parser.parse_args(pub + ["--push"])) == 0
    assert [len(b) for b in transfers] == [1, 50, 50, 44]
    assert sum(c.dml for c in connections) == 0  # explicit catalog-only phase
    assert main.cmd_rinex(parser.parse_args(pub + ["--push"])) == 0
    assert len(transfers) == 4  # no completed-data replay
    assert catalog_cli.cmd_archive_reindex(parser.parse_args(execute_catalog)) == 0
    assert sum(c.dml for c in connections) == 390  # exactly 195 per host
    mode_plan = tmp_path / "access-plan.json"
    access_base = [
        "archive-access",
        "--catalog-plan",
        str(catalog_plan),
        "--read-root",
        str(archive),
        "--gateway-root",
        str(archive),
        "--catalog-host",
        "db-a,db-b",
    ]
    preview = access_base + [
        "--manifest",
        str(access_manifest),
        "--aliases",
        str(alias_file),
        "--protected-originals",
        str(protected_file),
        "--write-plan",
        str(mode_plan),
    ]
    assert access_cli.cmd_archive_access(parser.parse_args(preview)) == 0
    before = json.loads(mode_plan.read_text())
    assert len(before["pins"]) == 1861
    assert sum(pin[7] == 0o600 for pin in before["pins"].values()) == 1716
    mode_sha = hashlib.sha256(mode_plan.read_bytes()).hexdigest()
    execute_access = access_base + [
        "--plan",
        str(mode_plan),
        "--plan-sha256",
        mode_sha,
        "--yes",
        "--owner-until",
        until,
    ]
    assert access_cli.cmd_archive_access(parser.parse_args(execute_access)) == 0
    assert len(access_batches[0]) == 1 and max(map(len, access_batches)) <= 50
    assert sum(map(len, access_batches)) == 1716
    for relative, checksum in all_products + orgs:
        path = archive / relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    for index, group in enumerate(protected_groups):
        for relative, checksum in group:
            path = original_roots[index] / relative
            assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
            assert stat.S_IMODE(path.stat().st_mode) == (0o664 if index == 0 else 0o644)
    assert sum(c.dml for c in connections) == 390
    assert main.cmd_rinex(parser.parse_args(pub + ["--push"])) == 0
    assert len(transfers) == 4  # no byte publisher replay after access restoration
    old_batch_count = len(access_batches)
    assert access_cli.cmd_archive_access(parser.parse_args(execute_access)) != 0
    assert len(access_batches) == old_batch_count  # stale mode plan requires re-review
    canary_after = canary.stat()
    assert (canary_after.st_size, canary_after.st_mtime_ns) == (
        canary_before.st_size,
        canary_before.st_mtime_ns,
    )
    assert "access-verified" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad", ["unknown-link", "symlink", "stale-parent", "expired", "wrong-hash"]
)
def test_gateway_metadata_refusals_have_no_effect(tmp_path, bad):
    root = tmp_path / "gateway"
    root.mkdir(mode=0o755)
    relative = "2026/jun/TEST/15s_24hr/rinex/TEST1720.26D.gz"
    checksum = blob(root, relative, b"generated", 0o600)
    rows = [{"relative": relative, "sha256": checksum, "aliases": []}]
    real_run = subprocess.run
    inspection = worker({"root": str(root), "rows": rows, "execute": False}, real_run)
    packet = {
        "root": str(root),
        "rows": rows,
        "execute": True,
        "until": time.time() + 300,
        "pins": {r["relative"]: r["pin"] for r in inspection["files"]},
        "root_pin": inspection["root_pin"],
        "namespace": inspection["namespace"],
    }
    path = root / relative
    if bad == "unknown-link":
        os.link(path, tmp_path / "foreign-alias")
    elif bad == "symlink":
        real = tmp_path / "real"
        path.rename(real)
        path.symlink_to(real)
    elif bad == "stale-parent":
        old = path.parent.with_name("old-rinex")
        path.parent.rename(old)
        path.parent.mkdir(mode=0o755)
        (old / path.name).rename(path)
    elif bad == "expired":
        packet["until"] = time.time() - 1
    else:
        rows[0]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        worker(packet, real_run)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
