"""Exact-file, metadata-only access restoration through the existing archive gateway.

The inline worker is Python 2.7/3 compatible (no installation on rawdata). It
uses Linux openat through stdlib ctypes, not pathname chmod. Owner coordination
must exclude data AND parent-namespace writers; checks are not distributed CAS.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

from .catalog_recovery import CatalogPlan, live_check
from .staged_manifest import file_checksum, open_regular_nofollow, read_staged_manifest

WORKER = r"""
from __future__ import print_function
import ctypes, hashlib, json, os, pwd, re, socket, stat, sys, time
libc = ctypes.CDLL(None, use_errno=True)
libc.openat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
libc.openat.restype = ctypes.c_int
RD = os.O_RDONLY | os.O_NOFOLLOW
DIR = RD | os.O_DIRECTORY
namespace = {}

def dirpin(fd):
    s = os.fstat(fd)
    return [s.st_dev, s.st_ino, s.st_uid, s.st_gid, stat.S_IMODE(s.st_mode)]

def check(ok, why):
    if not ok: raise ValueError(why)

def child(fd, name, flags):
    result = libc.openat(fd, name.encode('ascii'), flags, 0)
    if result < 0: raise ValueError('openat-refused')
    return result

def root_open(path):
    check(path.startswith('/') and all(p not in ('', '.', '..') for p in path.split('/')[1:]), 'absolute-root')
    fd = os.open('/', DIR)
    try:
        for part in path.split('/')[1:]:
            nxt = child(fd, part, DIR)
            os.close(fd); fd = nxt
        return fd
    except Exception:
        os.close(fd); raise

def opened(root, rel, private=False):
    parts = rel.split('/')
    check(all(p not in ('', '.', '..') and re.match(r'\A[A-Za-z0-9_.-]+\Z', p) for p in parts), 'relative-path')
    if private: check(parts[0].startswith('.'), 'private-alias-root')
    else: check(len(parts) == 6 and parts[4] in ('rinex', 'rinex_org') and re.match(r'\A[0-9]{4}\Z', parts[0]), 'public-layout')
    fd = os.dup(root)
    try:
        for index, part in enumerate(parts[:-1]):
            nxt = child(fd, part, DIR)
            os.close(fd); fd = nxt
            s = os.fstat(fd)
            key = '/'.join(parts[:index+1])
            value = dirpin(fd)
            if packet.get('execute') is True:
                check(value == packet['namespace'].get(key), 'parent-namespace-changed')
            namespace[key] = value
            if private: check(s.st_uid == os.geteuid() and stat.S_IMODE(s.st_mode) == 448, 'private-parent')
            else: check(stat.S_IMODE(s.st_mode) & 1, 'public-parent-not-traversable')
        return child(fd, parts[-1], RD | os.O_NONBLOCK)
    finally:
        os.close(fd)

def pin(fd):
    s = os.fstat(fd)
    check(stat.S_ISREG(s.st_mode) and s.st_uid == os.geteuid(), 'file-type-owner')
    return [s.st_dev, s.st_ino, s.st_uid, s.st_gid, s.st_nlink, s.st_size, s.st_mtime, stat.S_IMODE(s.st_mode), s.st_ctime]

def digest(fd):
    os.lseek(fd, 0, 0); h = hashlib.sha256()
    while True:
        part = os.read(fd, 1024*1024)
        if not part: return h.hexdigest()
        h.update(part)

def aliases(root, row, fd):
    p = pin(fd)
    check(p[4] == len(row['aliases']) + 1 and len(set(row['aliases'])) == len(row['aliases']), 'unknown-hardlink-alias')
    for rel in row['aliases']:
        check(rel.endswith('/' + row['relative']), 'alias-scope-mismatch')
        other = opened(root, rel, True)
        try: check(pin(other) == p, 'alias-object-mismatch')
        finally: os.close(other)

def reachable(root, row, fd, root_path):
    now = root_open(root_path)
    try: check(os.fstat(now).st_dev == os.fstat(root).st_dev and os.fstat(now).st_ino == os.fstat(root).st_ino, 'root-namespace-changed')
    finally: os.close(now)
    other = opened(root, row['relative'])
    try: check(pin(other) == pin(fd), 'public-namespace-changed')
    finally: os.close(other)
    aliases(root, row, fd)

root = None
try:
    packet = json.load(sys.stdin)
    execute = packet.get('execute') is True
    if packet.get('production') is True:
        check(socket.gethostname().split('.')[0] == 'rawdata' and pwd.getpwuid(os.geteuid()).pw_name == 'gpsops', 'production-gateway-role')
    if packet.get('capabilities_only') is True:
        print(json.dumps({'status': 'CAPABLE', 'python': list(sys.version_info[:3]), 'openat': True, 'nofollow': bool(os.O_NOFOLLOW), 'directory': bool(os.O_DIRECTORY), 'host': socket.gethostname(), 'user': pwd.getpwuid(os.geteuid()).pw_name, 'uid': os.geteuid(), 'gid': os.getegid(), 'server_time': time.time(), 'archive_opened': False}))
        sys.exit(0)
    rows = packet['rows']
    check(rows and len(set(r['relative'] for r in rows)) == len(rows), 'duplicate-empty-selection')
    if execute: check(len(rows) <= 50, 'max-batch-50')
    root = root_open(packet['root'])
    check(stat.S_IMODE(os.fstat(root).st_mode) & 1, 'public-root-not-traversable')
    if execute: check(dirpin(root) == packet['root_pin'], 'root-pin-changed')
    result = []
    for row in rows:
        check(re.match(r'\A[0-9a-f]{64}\Z', row['sha256']), 'sha256')
        fd = opened(root, row['relative'])
        try:
            before = pin(fd)
            check(before[7] in (384, 420), 'only-0600-or-0644')
            check(digest(fd) == row['sha256'], 'hash-mismatch')
            reachable(root, row, fd, packet['root'])
            check(pin(fd) == before, 'file-changed-during-hash')
            if execute:
                check(before == packet['pins'][row['relative']], 'stale-reviewed-pin')
                check(time.time() < packet['until'], 'owner-window-expired')
                if before[7] != 420:
                    # No blocking preparation between final clock check/fchmod.
                    os.fchmod(fd, 420)
                    os.fsync(fd)
                after = pin(fd)
                check(after[:7] == before[:7] and after[7] == 420, 'post-metadata')
                check(digest(fd) == row['sha256'], 'post-hash')
                reachable(root, row, fd, packet['root'])
            result.append({'relative': row['relative'], 'pin': pin(fd), 'sha256': row['sha256']})
        finally: os.close(fd)
    print(json.dumps({'status': 'VERIFIED', 'files': result, 'namespace': namespace, 'root_pin': dirpin(root)}, sort_keys=True))
except Exception as exc:
    # No raw error/log text, paths, credentials or connection details.
    print(json.dumps({'status': 'REFUSED_OR_PARTIAL', 'error_class': type(exc).__name__}))
    sys.exit(1)
finally:
    if root is not None: os.close(root)
"""


def gateway_request(target: str, packet: dict) -> dict:
    timeout = 20.0 if packet.get("capabilities_only") else 1800.0
    if packet.get("execute"):
        live_check(packet.get("until"))
        timeout = min(timeout, max(1.0, packet["until"] - time.time()))
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            target,
            "python -c " + shlex.quote(WORKER),
        ],
        input=json.dumps(packet),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise ValueError(
            "gateway refused/partial; reconcile before retry; timeout is not cancellation proof"
        )
    try:
        response = json.loads(proc.stdout)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("gateway returned invalid JSON; state unconfirmed") from exc
    if not isinstance(response, dict):
        raise ValueError("gateway returned invalid response shape")
    if packet.get("capabilities_only"):
        if (
            response.get("status") != "CAPABLE"
            or response.get("archive_opened") is not False
            or response.get("python", [0])[0] not in (2, 3)
            or not all(response.get(k) for k in ("openat", "nofollow", "directory"))
        ):
            raise ValueError("gateway runtime lacks required FD capabilities")
        return response
    if (
        response.get("status") != "VERIFIED"
        or len(response["files"]) != len(packet["rows"])
        or {r["relative"] for r in response["files"]}
        != {r["relative"] for r in packet["rows"]}
    ):
        raise ValueError("gateway did not verify exact requested file set")
    return response


def read_json(path):
    source = Path(path).expanduser().absolute()
    with open_regular_nofollow(source.parent, source.name) as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("action JSON too large")
    try:
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid action JSON") from exc


def protected_originals(path, catalog: CatalogPlan, *, verify=True):
    """Exact original-copy manifests only; reject raw before opening any product."""
    groups, spec_sha = read_json(path)
    if not isinstance(groups, list) or not groups:
        raise ValueError("explicit protected-original groups required")
    prepared, coverage = [], set()
    for group in groups:
        if set(group) != {"root", "manifest", "manifest_sha256", "mode"} or group[
            "mode"
        ] not in (0o644, 0o664):
            raise ValueError("invalid protected-original group")
        manifest = read_staged_manifest(
            group["manifest"],
            work_dir=group["root"],
            stations=list({r.split("/")[2] for r in catalog.data["scope"]}),
            session=next(iter(catalog.data["scope"])).split("/")[3],
            validate_files=False,
            categories=("rinex", "rinex_org"),
        )
        if manifest.sha256 != group["manifest_sha256"]:
            raise ValueError("protected-original manifest changed")
        for relative, checksum in manifest.products:
            logical = relative.replace("/rinex_org/", "/rinex/")
            if logical not in catalog.data["scope"] or logical in coverage:
                raise ValueError(
                    "protected originals must cover full scope exactly once"
                )
            coverage.add(logical)
            prepared.append(
                (
                    Path(group["root"]).expanduser().absolute(),
                    relative,
                    checksum,
                    group["mode"],
                )
            )
    if coverage != set(catalog.data["scope"]):
        raise ValueError("incomplete protected-original coverage")
    if verify:
        for root, relative, checksum, mode in prepared:
            if file_checksum(root, relative) != checksum:
                raise ValueError("protected original checksum mismatch")
            with open_regular_nofollow(root, relative) as stream:
                protected_stat = os.fstat(stream.fileno())
                if stat.S_IMODE(protected_stat.st_mode) != mode:
                    raise ValueError("protected original mode changed")
                logical = relative.replace("/rinex_org/", "/rinex/")
                with open_regular_nofollow(Path(catalog.data["root"]), logical) as live:
                    live_stat = os.fstat(live.fileno())
                    if (live_stat.st_dev, live_stat.st_ino) == (
                        protected_stat.st_dev,
                        protected_stat.st_ino,
                    ):
                        raise ValueError(
                            "protected original is the live candidate object, not a separate copy"
                        )
    return prepared, spec_sha


def access_rows(manifest, aliases, catalog: CatalogPlan, protected):
    if set(aliases) != set(manifest.checksums) or any(
        not isinstance(v, list) for v in aliases.values()
    ):
        raise ValueError("exact explicit alias list required for every final name")
    live = {r: h for r, h in manifest.products if "/rinex/" in r}
    if live != catalog.data["scope"]:
        raise ValueError("access selection must include the complete candidate scope")
    originals = {
        relative: checksum
        for _, relative, checksum, _ in protected
        if "/rinex_org/" in relative
    }
    org = {r: h for r, h in manifest.products if "/rinex_org/" in r}
    if org != originals:
        raise ValueError("access rinex_org selection must match protected original set")
    return [
        {"relative": r, "sha256": h, "aliases": aliases[r]}
        for r, h in manifest.products
    ]


def readback(root, rows, *, final=False):
    observed = {}
    for row in rows:
        relative = row["relative"]
        if file_checksum(root, relative) != row["sha256"]:
            raise ValueError("authoritative archive hash mismatch")
        with open_regular_nofollow(root, relative) as stream:
            mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
        if mode not in ((0o644,) if final else (0o600, 0o644)):
            raise ValueError("authoritative archive mode mismatch")
        observed[relative] = mode
    return observed
