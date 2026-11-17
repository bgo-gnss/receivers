"""Generated RINEX blobs + real SQL with a COALESCE/CAS-faithful transaction model."""

import copy
import gzip
import hashlib
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from receivers.archive import reindex as rx
from receivers.archive.catalog_recovery import CatalogPlan, RowGuard, archive_values
from receivers.archive.staged_manifest import StagedManifest, preserved_replacements

REL = "2026/jun/TEST/15s_24hr/rinex/TEST1720.26D.gz"
OTHER = REL.replace("1720", "1730")
LOCATION = "fixture_archive"
PREFIX = "~/archive"
KEY = (LOCATION, "15s_24hr", "rinex", "test1720.26d")


class Store:
    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows or {})


class Conn:
    def __init__(self, store):
        self.store, self.pending = store, None
        self.commits = 0
        self.dml = 0
        self.before_insert: Callable[[], None] | None = None
        self.statements = []

    def cursor(self):
        return Cursor(self)

    def commit(self):
        if self.pending is not None:
            self.store.rows = self.pending
        self.pending = None
        self.commits += 1

    def rollback(self):
        self.pending = None

    def close(self):
        self.rollback()


class Cursor:
    def __init__(self, conn):
        self.conn, self.result = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result[0] if self.result else None

    def execute(self, sql, params):
        self.conn.statements.append((sql, params))
        rows = (
            self.conn.pending if self.conn.pending is not None else self.conn.store.rows
        )
        if sql.lstrip().startswith("SELECT"):
            if "ANY(%s)" in sql:
                self.result = [
                    copy.deepcopy(r)
                    for k, r in rows.items()
                    if k[0] == params[0]
                    and k[1] == params[1]
                    and k[2] == "rinex"
                    and k[3] in params[2]
                ]
            else:
                row = rows.get(tuple(params))
                if row is None:
                    self.result = []
                elif sql.lstrip().startswith(
                    "SELECT content_sha256, compressed_sha256"
                ):
                    self.result = [(row[10], row[11])]
                else:
                    self.result = [copy.deepcopy(row)]
            return
        assert sql.lstrip().startswith("INSERT INTO archive_catalog")
        assert "WHERE (archive_catalog.file_path" in sql
        assert "COALESCE(EXCLUDED.md5checksum" in sql
        if self.conn.before_insert:
            callback, self.conn.before_insert = self.conn.before_insert, None
            callback()
        if self.conn.pending is None:
            self.conn.pending = copy.deepcopy(self.conn.store.rows)
        rows = self.conn.pending
        key = (params[0], params[4], params[5], params[6])
        prior = rows.get(key)
        if prior is not None:
            if "AND FALSE" in sql:
                return
            if len(params) > 15:
                assert "archive_catalog.md5checksum IS NOT DISTINCT FROM" in sql
                if prior != list(params[15:]):
                    return
        day = params[2].isoformat() if hasattr(params[2], "isoformat") else params[2]
        incoming = [
            params[0],
            params[1],
            params[4],
            params[5],
            params[6],
            day,
            params[3],
            params[7],
            params[8],
            params[9],
            params[10],
            params[11],
            params[12],
            params[13],
            params[14],
        ]
        if prior is not None:
            for index in (6, 10, 11, 12, 13, 14):
                if incoming[index] is None:
                    incoming[index] = prior[index]
        rows[key] = incoming
        self.conn.dml += 1


def make_row(relative, blob, content="b" * 64, size=1):
    return [
        LOCATION,
        "TEST",
        "15s_24hr",
        "rinex",
        Path(relative).name.removesuffix(".gz").lower(),
        "2026-06-21" if relative == REL else "2026-06-22",
        None,
        PREFIX + "/" + relative,
        ".gz",
        size,
        content,
        blob,
        None,
        None,
        None,
    ]


def write(root, relative, payload):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload, mtime=1))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, *, absent=False):
    root = tmp_path / "archive"
    desired = write(root, REL, b"approved generated observations\n" * 10)
    other = write(root, OTHER, b"untouched generated observations\n" * 10)
    untouched = make_row(OTHER, other)
    untouched[9:12] = list(archive_values(str(root), OTHER, other))[2:] + [
        archive_values(str(root), OTHER, other)[0],
        other,
    ]
    rows = {(LOCATION, "15s_24hr", "rinex", untouched[4]): untouched}
    if not absent:
        rows[KEY] = make_row(REL, "a" * 64)
    conns = {h: Conn(Store(rows)) for h in ("db-a", "db-b")}
    scope = StagedManifest("generated", ((REL, desired), (OTHER, other)))
    selected = StagedManifest("generated", ((REL, desired),))
    plan = CatalogPlan.snapshot(
        conns,
        scope,
        selected,
        root=str(root),
        location=LOCATION,
        prefix=PREFIX,
        gateway="fixture-gateway",
        publication=None,
    )
    return root, desired, conns, scope, selected, plan


def reindex(conn, root, desired, plan, host="db-a", **kw):
    return rx.reindex_files(
        conn,
        [str(root / REL)],
        root=str(root),
        storage_location=LOCATION,
        dest_prefix=PREFIX,
        expected_checksums={REL: desired},
        recovery_guard=RowGuard(plan, host, time.time() + 300),
        **kw,
    )


def test_real_upsert_recovers_both_hashes_and_preserves_outside_scope(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path)
    before = copy.deepcopy(conns["db-a"].store.rows)
    for host, conn in conns.items():
        result = reindex(conn, root, desired, plan, host)
        assert result.updated == 1 and conn.dml == 1
        assert (
            conn.store.rows[KEY][10:12]
            == list(archive_values(str(root), REL, desired))[:2]
        )
        assert [r for k, r in conn.store.rows.items() if k != KEY] == [
            r for k, r in before.items() if k != KEY
        ]
    plan.validate(conns, complete=True)


def test_missing_row_is_inserted_with_null_md5_and_hour(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path, absent=True)
    result = reindex(conns["db-a"], root, desired, plan)
    assert result.inserted == 1
    row = conns["db-a"].store.rows[KEY]
    assert row[6] is None and row[12:14] == [None, None]


@pytest.mark.parametrize(
    "index,value",
    [(6, 0), (12, ""), (12, "a" * 32), (13, ""), (1, "FOREIGN"), (7, "/wrong/prefix")],
)
def test_global_guard_refuses_unsupported_fields_even_outside_repair(
    tmp_path, index, value
):
    root, _, conns, scope, selected, _ = fixture(tmp_path)
    for conn in conns.values():
        row = next(r for k, r in conn.store.rows.items() if k != KEY)
        row[index] = value
    with pytest.raises(ValueError):
        CatalogPlan.snapshot(
            conns,
            scope,
            selected,
            root=str(root),
            location=LOCATION,
            prefix=PREFIX,
            gateway="fixture-gateway",
        )
    assert all(c.dml == 0 for c in conns.values())


def test_parity_checked_before_any_write(tmp_path):
    root, _, conns, scope, selected, _ = fixture(tmp_path)
    conns["db-b"].store.rows[KEY][11] = "c" * 64
    with pytest.raises(ValueError, match="divergence"):
        CatalogPlan.snapshot(
            conns,
            scope,
            selected,
            root=str(root),
            location=LOCATION,
            prefix=PREFIX,
            gateway="fixture-gateway",
        )


def test_resume_accepts_only_known_poststate_not_foreign_mutation(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path)
    reindex(conns["db-a"], root, desired, plan)
    plan.validate(conns)  # known primary-done / secondary-baseline state
    conns["db-b"].store.rows[KEY][11] = "f" * 64
    with pytest.raises(ValueError, match="stale/unapproved"):
        plan.validate(conns)
    assert conns["db-b"].dml == 0


def test_racing_insert_is_not_overwritten_or_blessed(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path, absent=True)
    conn = conns["db-a"]
    foreign = make_row(REL, "f" * 64)
    foreign[12] = ""  # real COALESCE would retain this; never clear it

    def race() -> None:
        conn.store.rows[KEY] = foreign

    conn.before_insert = race
    with pytest.raises(ValueError):
        reindex(conn, root, desired, plan)
    conn.rollback()
    assert conn.dml == 0 and conn.commits == 0 and conn.store.rows[KEY] == foreign


def test_stale_present_row_is_not_rewritten(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path)
    conns["db-a"].store.rows[KEY][9] = 777
    with pytest.raises(ValueError):
        reindex(conns["db-a"], root, desired, plan)
    assert conns["db-a"].dml == 0


@pytest.mark.parametrize("index,value", [(6, 0), (12, ""), (13, "")])
def test_populated_fields_after_preview_refuse_without_dml(tmp_path, index, value):
    root, desired, conns, _, _, plan = fixture(tmp_path)
    conns["db-a"].store.rows[KEY][index] = value
    with pytest.raises(ValueError):
        reindex(conns["db-a"], root, desired, plan)
    assert conns["db-a"].dml == 0 and conns["db-a"].store.rows[KEY][index] == value


def test_recovery_rejects_staging_as_archive_readback(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path)
    stage = tmp_path / "stage"
    write(stage, REL, b"approved generated observations\\n" * 10)
    with pytest.raises(ValueError, match="actual archive read-back root"):
        reindex(conns["db-a"], stage, desired, plan)
    assert conns["db-a"].dml == 0


def test_expiry_after_hashing_prevents_catalog_effect(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path)
    with pytest.raises(ValueError, match="unexpired"):
        rx.reindex_files(
            conns["db-a"],
            [str(root / REL)],
            root=str(root),
            storage_location=LOCATION,
            dest_prefix=PREFIX,
            expected_checksums={REL: desired},
            recovery_guard=RowGuard(plan, "db-a", time.time() - 1),
        )
    assert conns["db-a"].dml == 0 and conns["db-a"].commits == 0


def test_dry_run_no_dml_or_commit(tmp_path):
    root, desired, conns, _, _, plan = fixture(tmp_path)
    stats = reindex(conns["db-a"], root, desired, plan, dry_run=True)
    assert stats.updated == 1 and conns["db-a"].dml == conns["db-a"].commits == 0


def test_snapshot_sha_bindings_and_no_overwrite(tmp_path):
    root, _, conns, _, _, plan = fixture(tmp_path)
    path = tmp_path / "catalog-plan.json"
    sha = plan.write(path)
    loaded = CatalogPlan.load(path, approved_sha=sha)
    loaded.bind(
        root=str(root),
        location=LOCATION,
        prefix=PREFIX,
        hosts=list(conns),
        gateway="fixture-gateway",
    )
    with pytest.raises(ValueError):
        loaded.bind(
            root=str(tmp_path),
            location=LOCATION,
            prefix=PREFIX,
            hosts=list(conns),
            gateway="fixture-gateway",
        )
    with pytest.raises(FileExistsError):
        plan.write(path)
    with pytest.raises(ValueError):
        CatalogPlan.load(path, approved_sha="0" * 64)


@pytest.mark.parametrize("change", ["file", "parent", "owner"])
def test_data_namespace_changes_refuse_even_with_identical_bytes(tmp_path, change):
    import os

    from receivers.archive.staged_manifest import namespace_snapshot, require_namespace

    root = tmp_path / "archive"
    write(root, REL, b"baseline bytes")
    path = root / REL
    path.chmod(0o644)
    expected = namespace_snapshot(root, [REL], owner_uid=os.getuid())
    if change == "file":
        previous = path.with_name("previous")
        path.rename(previous)
        path.write_bytes(previous.read_bytes())
    elif change == "parent":
        previous = path.parent.with_name("previous-dir")
        path.parent.rename(previous)
        path.parent.mkdir()
        (previous / path.name).rename(path)
    else:
        expected["owner_uid"] = os.getuid() + 1
    with pytest.raises(ValueError):
        require_namespace(root, [REL], expected)


def test_original_copy_proof_and_completed_subset_filter(tmp_path):
    stage, originals, archive = (
        tmp_path / n for n in ("stage", "originals", "archive")
    )
    baseline = write(originals, REL, b"original")
    desired = write(stage, REL, b"approved candidate")
    write(archive, REL, b"original")
    products, protected = (
        StagedManifest("x", ((REL, desired),)),
        StagedManifest("y", ((REL, baseline),)),
    )
    assert preserved_replacements(
        products, protected, original_root=originals, archive_root=archive
    ) == [REL]
    write(archive, REL, b"approved candidate")
    assert (
        preserved_replacements(
            products, protected, original_root=originals, archive_root=archive
        )
        == []
    )
    write(archive, REL, b"foreign bytes")
    with pytest.raises(ValueError):
        preserved_replacements(
            products, protected, original_root=originals, archive_root=archive
        )
