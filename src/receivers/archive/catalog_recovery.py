"""Catalog recovery preconditions shared by existing publication/reindex tools.

A snapshot is a reviewable action file, not authority or a publisher. File sets
stay standard SHA256 manifests. All endpoints are explicit; no dev fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from ..db.tx import read_only_cursor
from ..utils.canonical_key import canonical_key
from ..utils.content_hash import compressed_sha256, content_sha256
from .catalog import compression_suffix
from .path_parse import parse_archive_path
from .staged_manifest import MONTHS, StagedManifest, open_regular_nofollow

COLUMNS = (
    "storage_location",
    "station",
    "session_type",
    "file_category",
    "canonical_key",
    "file_date",
    "file_hour",
    "file_path",
    "compression",
    "file_size",
    "content_sha256",
    "compressed_sha256",
    "md5checksum",
    "md5uncompressed",
    "file_tracking_id",
)
TYPES = (
    "text",
    "text",
    "text",
    "text",
    "text",
    "date",
    "integer",
    "text",
    "text",
    "bigint",
    "text",
    "text",
    "text",
    "text",
    "bigint",
)
# Identifiers come exclusively from this immutable schema tuple, never a plan.
_SELECT = "SELECT " + ", ".join(COLUMNS) + " FROM archive_catalog "
_ROW_QUERY = (
    _SELECT
    + "WHERE storage_location=%s AND session_type=%s AND file_category=%s AND canonical_key=%s"
)
_LOCK_QUERY = _ROW_QUERY + " FOR UPDATE"
_SCOPE_QUERY = (
    _SELECT
    + "WHERE storage_location=%s AND session_type=%s AND file_category='rinex' AND canonical_key=ANY(%s)"
)


def normal(row):
    return (
        [v.isoformat() if isinstance(v, date) else v for v in row]
        if row is not None
        else None
    )


def deadline(value: str | None) -> float | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("owner deadline must include timezone")
    return parsed.astimezone(UTC).timestamp()


def live_check(until: float | None) -> None:
    if until is None or time.time() >= until:
        raise ValueError("fresh unexpired owner-coordination deadline required")


def explicit_hosts(hosts: list[str], location: str) -> None:
    if (
        not hosts
        or len(set(hosts)) != len(hosts)
        or any(
            not isinstance(h, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", h)
            for h in hosts
        )
    ):
        raise ValueError("distinct explicit catalog hosts required; no fallback")
    if (
        location == "imo_archive"
        and any(h in ("localhost", "127.0.0.1", "::1") for h in hosts)
        and socket.getfqdn() != "rek-d01.vedur.is"
    ):
        raise ValueError("production localhost requires verified rek-d01 executor")


def production_gateway(location, gateway):
    if location == "imo_archive" and gateway.rstrip("/") not in {
        "gpsops@rawdata:~/gpsdata",
        "gpsops@rawdata.vedur.is:~/gpsdata",
        "gpsops@rawdata:/mnt/gps/gpsdata",
        "gpsops@rawdata.vedur.is:/mnt/gps/gpsdata",
    }:
        raise ValueError("rawdata/gpsops is the sole production archive writer")


def _one(conn, location, relative, *, lock=False):
    # Shares the caller's writer transaction/row lock: never rollback here.
    p = parse_archive_path("/" + relative, "/")
    if p is None:
        raise ValueError("invalid catalog identity")
    params = (
        location,
        p.session_type,
        p.file_category,
        canonical_key(Path(relative).name),
    )
    with conn.cursor() as cur:
        if lock:
            cur.execute(
                """SELECT storage_location, station, session_type, file_category,
                canonical_key, file_date, file_hour, file_path, compression, file_size,
                content_sha256, compressed_sha256, md5checksum, md5uncompressed, file_tracking_id
                FROM archive_catalog WHERE storage_location=%s AND session_type=%s
                AND file_category=%s AND canonical_key=%s FOR UPDATE""",
                params,
            )
        else:
            cur.execute(
                """SELECT storage_location, station, session_type, file_category,
                canonical_key, file_date, file_hour, file_path, compression, file_size,
                content_sha256, compressed_sha256, md5checksum, md5uncompressed, file_tracking_id
                FROM archive_catalog WHERE storage_location=%s AND session_type=%s
                AND file_category=%s AND canonical_key=%s""",
                params,
            )
        return normal(cur.fetchone())


def read_scope(conn, location, scope):
    # One bounded indexed query, never a station-wide/history join or filesystem walk.
    identities = [parse_archive_path("/" + relative, "/") for relative in scope]
    sessions = {p.session_type for p in identities if p is not None}
    if len(sessions) != 1 or any(
        p is None
        or p.file_category != "rinex"
        or p.file_date is None
        or p.file_hour is not None
        or "24hr" not in p.session_type
        for p in identities
    ):
        raise ValueError("recovery catalogs require dateable daily RINEX scope")
    keys = [canonical_key(Path(r).name) for r in scope]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate logical catalog keys")
    with read_only_cursor(conn) as cur:
        cur.execute(_SCOPE_QUERY, (location, next(iter(sessions)), keys))
        rows = [normal(r) for r in cur.fetchall()]
    if any(r is None for r in rows):
        raise ValueError("invalid NULL catalog record")
    by_key = {r[4]: r for r in rows if r is not None}
    if len(by_key) != len(rows):
        raise ValueError("duplicate catalog rows")
    return {relative: by_key.get(key) for relative, key in zip(scope, keys)}


def shape(row, location, prefix, relative):
    if row is None:
        return
    p = parse_archive_path("/" + relative, "/")
    if p is None or p.file_date is None or len(row) != len(COLUMNS):
        raise ValueError("invalid catalog row shape")
    expected = [
        location,
        p.station,
        p.session_type,
        "rinex",
        canonical_key(Path(relative).name),
        p.file_date.isoformat(),
        None,
        prefix.rstrip("/") + "/" + relative,
        compression_suffix(Path(relative).name),
    ]
    if row[:9] != expected or row[12] is not None or row[13] is not None:
        raise ValueError(f"catalog identity/path/MD5/hour refused: {relative}")
    if not isinstance(row[9], int) or row[9] < 0:
        raise ValueError("invalid catalog size")


def archive_values(root: str, relative: str, expected: str):
    with open_regular_nofollow(Path(root).expanduser().absolute(), relative) as stream:
        path = f"/proc/{os.getpid()}/fd/{stream.fileno()}"
        content, blob = content_sha256(path), compressed_sha256(path)
        size = os.fstat(stream.fileno()).st_size
    if blob != expected:
        raise ValueError(f"archive read-back checksum mismatch: {relative}")
    return content, blob, size


@dataclass(frozen=True)
class CatalogPlan:
    data: dict
    sha256: str

    @classmethod
    def snapshot(
        cls,
        conns,
        scope: StagedManifest,
        selected: StagedManifest,
        *,
        root,
        location,
        prefix,
        gateway,
        publication=None,
    ):
        hosts = list(conns)
        explicit_hosts(hosts, location)
        production_gateway(location, gateway)
        if not set(selected.checksums) <= set(scope.checksums) or any(
            scope.checksums[r] != h for r, h in selected.products
        ):
            raise ValueError(
                "catalog selection must be an exact-hash subset of full scope"
            )
        rows = {h: read_scope(c, location, scope.checksums) for h, c in conns.items()}
        for relative in scope.checksums:
            prior = [rows[h][relative] for h in hosts]
            for row in prior:
                shape(row, location, prefix, relative)
            if any(row != prior[0] for row in prior[1:]):
                raise ValueError(f"catalog snapshot divergence: {relative}")
            if relative not in selected.checksums and (
                prior[0] is None
                or prior[0][11] != scope.checksums[relative]
                or not re.fullmatch(r"[0-9a-f]{64}", prior[0][10] or "")
            ):
                raise ValueError(
                    f"incomplete row outside approved repair selection: {relative}"
                )
        data = {
            "schema": "receivers.catalog-recovery.v1",
            "root": str(Path(root).expanduser().absolute()),
            "location": location,
            "prefix": prefix.rstrip("/"),
            "gateway": gateway,
            "publication": publication,
            "hosts": hosts,
            "scope": scope.checksums,
            "selected": selected.checksums,
            "rows": rows,
        }
        raw = json.dumps(data, sort_keys=True, indent=2).encode() + b"\n"
        return cls(data, hashlib.sha256(raw).hexdigest())

    def write(self, path):
        raw = json.dumps(self.data, sort_keys=True, indent=2).encode() + b"\n"
        with Path(path).open("xb") as stream:
            stream.write(raw)
        return hashlib.sha256(raw).hexdigest()

    @classmethod
    def load(cls, path, *, approved_sha=None):
        source = Path(path).expanduser().absolute()
        with open_regular_nofollow(source.parent, source.name) as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("catalog recovery JSON too large")
        sha = hashlib.sha256(raw).hexdigest()
        if approved_sha is not None and sha != approved_sha:
            raise ValueError("reviewed catalog plan checksum mismatch")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise ValueError("invalid catalog recovery JSON") from exc
        required = {
            "schema",
            "root",
            "location",
            "prefix",
            "gateway",
            "publication",
            "hosts",
            "scope",
            "selected",
            "rows",
        }
        if set(data) != required or data["schema"] != "receivers.catalog-recovery.v1":
            raise ValueError("unsupported catalog recovery plan")
        explicit_hosts(data["hosts"], data["location"])
        production_gateway(data["location"], data["gateway"])
        if (
            set(data["rows"]) != set(data["hosts"])
            or not data["selected"]
            or not data["scope"]
        ):
            raise ValueError("empty/inconsistent catalog plan")
        if not set(data["selected"]) <= set(data["scope"]):
            raise ValueError("catalog repair outside scope")
        for relative, digest in data["scope"].items():
            p = parse_archive_path("/" + relative, "/")
            if (
                p is None
                or len(relative.split("/")) != 6
                or any(s in ("", ".", "..") for s in relative.split("/"))
                or p.file_category != "rinex"
                or p.file_date is None
                or p.file_hour is not None
                or "24hr" not in p.session_type
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", relative)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ValueError("invalid catalog scope")
            if (
                not re.fullmatch(
                    r"[A-Za-z0-9]{4}\d{3}0\.\d{2}[dDoO]\.(?:Z|gz)", Path(relative).name
                )
                or Path(relative).name[:4].upper() != p.station
                or MONTHS[p.file_date.month] != relative.split("/")[1]
            ):
                raise ValueError(
                    "catalog scope must contain date-consistent short-name daily observations only"
                )
            for host in data["hosts"]:
                if set(data["rows"][host]) != set(data["scope"]):
                    raise ValueError("incomplete host snapshot")
                shape(
                    data["rows"][host][relative],
                    data["location"],
                    data["prefix"],
                    relative,
                )
            references = [data["rows"][h][relative] for h in data["hosts"]]
            if any(row != references[0] for row in references[1:]):
                raise ValueError("reviewed baseline snapshot lacks catalog parity")
            if relative not in data["selected"] and (
                references[0] is None
                or references[0][11] != digest
                or not re.fullmatch(r"[0-9a-f]{64}", references[0][10] or "")
            ):
                raise ValueError(
                    "incomplete baseline outside approved catalog repair set"
                )
        if len({tuple(r.split("/")[2:4]) for r in data["scope"]}) != 1 or len(
            {canonical_key(Path(r).name) for r in data["scope"]}
        ) != len(data["scope"]):
            raise ValueError("single-station scope with distinct logical keys required")
        if any(data["scope"][r] != h for r, h in data["selected"].items()):
            raise ValueError("catalog selection hash mismatch")
        return cls(data, sha)

    def bind(self, *, root, location, prefix, hosts, gateway):
        if (
            str(Path(root).expanduser().absolute()),
            location,
            prefix.rstrip("/"),
            list(hosts),
            gateway,
        ) != (
            self.data["root"],
            self.data["location"],
            self.data["prefix"],
            self.data["hosts"],
            self.data["gateway"],
        ):
            raise ValueError("catalog recovery roots/targets changed since preview")

    def desired(self, relative, values, host):
        content, blob, size = values
        baseline = self.data["rows"][host][relative]
        p = parse_archive_path("/" + relative, "/")
        if p is None or p.file_date is None:
            raise ValueError("invalid desired catalog date")
        return [
            self.data["location"],
            p.station,
            p.session_type,
            "rinex",
            canonical_key(Path(relative).name),
            p.file_date.isoformat(),
            None,
            self.data["prefix"] + "/" + relative,
            compression_suffix(Path(relative).name),
            size,
            content,
            blob,
            None,
            None,
            baseline[14] if baseline else None,
        ]

    def allowed(self, host, relative, row, *, values=None):
        shape(row, self.data["location"], self.data["prefix"], relative)
        if row == self.data["rows"][host][relative]:
            return
        if relative in self.data["selected"]:
            values = values or archive_values(
                self.data["root"], relative, self.data["scope"][relative]
            )
            if row == self.desired(relative, values, host):
                return
        raise ValueError(f"stale/unapproved catalog state on {host}: {relative}")

    def validate(self, conns, *, complete=False, verified_values=None):
        if list(conns) != self.data["hosts"]:
            raise ValueError("all planned catalog hosts required")
        rows = {
            h: read_scope(c, self.data["location"], self.data["scope"])
            for h, c in conns.items()
        }
        for relative, checksum in self.data["scope"].items():
            values = (
                archive_values(self.data["root"], relative, checksum)
                if complete
                else (verified_values or {}).get(relative)
            )
            for host in conns:
                self.allowed(host, relative, rows[host][relative], values=values)
                if complete and rows[host][relative] != self.desired(
                    relative, values, host
                ):
                    raise ValueError(
                        f"catalog completion not established: {host}: {relative}"
                    )
        return rows


class RowGuard:
    """Same-connection, row-locked CAS precondition and post-upsert proof."""

    def __init__(
        self, plan: CatalogPlan, host: str, until: float | None, global_check=None
    ):
        self.plan, self.host, self.until = plan, host, until
        self.global_check, self.count = global_check, 0

    def before(self, conn, relative, values, *, dry_run=False):
        if (
            relative not in self.plan.data["selected"]
            or values[1] != self.plan.data["scope"][relative]
        ):
            raise ValueError("catalog write outside exact approved repair set/hash")
        if not dry_run and self.global_check is not None and self.count % 50 == 0:
            self.global_check()
        self.count += 1
        row = _one(conn, self.plan.data["location"], relative, lock=not dry_run)
        self.plan.allowed(self.host, relative, row, values=values)
        if not dry_run:
            live_check(self.until)
        return {"row": row}

    def after(self, conn, relative, values):
        row = _one(conn, self.plan.data["location"], relative, lock=True)
        shape(row, self.plan.data["location"], self.plan.data["prefix"], relative)
        if row != self.plan.desired(relative, values, self.host):
            raise ValueError("catalog upsert did not produce the approved row")
        live_check(self.until)


def open_required(hosts):
    """Context manager for recovery: every explicit endpoint mandatory, no mirror."""
    from contextlib import contextmanager

    from ..db.connection import get_connection

    @contextmanager
    def opened():
        conns = {}
        try:
            for host in hosts:
                conns[host] = get_connection(host_override=host, single_host=True)
            yield conns
        finally:
            for conn in conns.values():
                conn.close()

    return opened()
