#!/usr/bin/env python3
"""prx5.py - download data from, and check the health of, one PolaRx5 (fw 5.7.0).

Stand-alone: Python standard library plus gtimes (download only). RxTools is
optional and adds the disk check (bin2asc) and `--rinex` (sbf2rin). It does not
import the `receivers` package and needs no database. Read README.md first.

    prx5.py health   ENTC 192.0.2.10
    prx5.py download ENTC 192.0.2.10:2160 --start 2026-11-01 --end 2026-11-03
    prx5.py health   ENTC 192.0.2.10 --save   # remember the address
    prx5.py health   ENTC                    # from then on

Exit codes: 0 ok; 1 warning, or some files not fetched; 2 critical;
3 unreachable, login refused or unknown; 64 bad usage.

LOCKOUT: a few failed logins on fw 5.7.0 lock EVERY account on the receiver for
about nine hours, including the production scheduler's. So this tool logs in
once per run, on one connection, and stops at the first refusal. Never add a
retry around a login here.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import ftplib
import getpass
import gzip
import json
import math
import os
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

EXIT_OK, EXIT_WARNING, EXIT_CRITICAL, EXIT_UNKNOWN, EXIT_USAGE = 0, 1, 2, 3, 64
RANK = {"ok": 0, "warning": 1, "critical": 2}

DEFAULT_FTP_PORT = 2160
DEFAULT_HTTP_PORT = 8060
DEFAULT_CONTROL_PORT = 28784
SECURE_CONTROL_PORT = 28783
DEFAULT_BASE_PATH = "/DSK1/SSN/"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
DEFAULT_CFG = CONFIG_HOME / "polarx5-tools" / "stations.cfg"
DEFAULT_CRED = CONFIG_HOME / "polarx5-tools" / "credentials"
# datetime.UTC needs Python 3.11; this tool runs on 3.9.
UTC = timezone.utc  # noqa: UP017
LOCAL_TEMPLATE = "%Y/#b/{station}/{session}/raw/{name}"


@dataclass(frozen=True)
class Session:
    letter: str
    remote_dir: str
    freq: str  # gtimes frequency: picks the #Rin2 letter (1D -> 0, 1H -> a..x)


SESSIONS = {
    "15s_24hr": Session("a", "LOG1_15s_24hr", "1D"),
    "1Hz_1hr": Session("b", "LOG2_1Hz_1hr", "1H"),
    "status_1hr": Session("c", "LOG5_status_1hr", "1H"),
}

# SBF block IDs and the names `esoc` knows them by.
BLOCK_POWER = ("PowerStatus", 4101)
BLOCK_RECEIVER = ("ReceiverStatus", 4014)
BLOCK_PVT = ("PVTGeodetic", 4007)
BLOCK_SATS = ("PVTSatCartesian", 4008)
BLOCK_DISK = ("DiskStatus", 4059)
BLOCK_SETUP = ("ReceiverSetup", 5902)

# ReceiverStatus2 bit names, as in receivers/health/polarx5_tcp_extractor.py.
RX_ERROR_BITS = {
    3: "SOFTWARE",
    4: "WATCHDOG",
    5: "ANTENNA",
    6: "CONGESTION",
    8: "MISSEDEVENT",
    9: "CPUOVERLOAD",
    10: "INVALIDCONFIG",
    11: "OUTOFGEOFENCE",
}
EXT_ERROR_BITS = {
    0: "SISERROR",
    1: "DIFFCORRERROR",
    2: "EXTSENSORERROR",
    3: "SETUPERROR",
}

# Defaults of receivers/health/metrics.py ThresholdConfig. Voltage is reported
# only: its limits depend on the power type, which this tool does not know.
TEMPERATURE_BAND = (-20.0, -10.0, 50.0, 60.0)
CPU_WARN, CPU_CRIT = 75, 90
DISK_WARN, DISK_CRIT = 90.0, 97.0


class ToolError(Exception):
    """A failure to report in one line and exit with `code`."""

    def __init__(self, message: str, code: int = EXIT_UNKNOWN) -> None:
        super().__init__(message)
        self.code = code


def worst(statuses: List[str]) -> str:
    return max(statuses, key=lambda s: RANK.get(s, 1), default="ok")


def band_status(value: float, band: Tuple[float, float, float, float]) -> str:
    crit_low, warn_low, warn_high, crit_high = band
    if value < crit_low or value > crit_high:
        return "critical"
    if value < warn_low or value > warn_high:
        return "warning"
    return "ok"


# --- configuration and credentials -------------------------------------------


def cfg_path(args: argparse.Namespace) -> Path:
    if args.cfg:
        return Path(args.cfg).expanduser()
    env = os.environ.get("PRX5_STATIONS_CFG")
    return Path(env).expanduser() if env else DEFAULT_CFG


def read_station(path: Path, station: str) -> Dict[str, str]:
    """The station's section; key names match IMO's stations.cfg on purpose."""
    if not path.exists():
        return {}
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path)
    except configparser.Error as exc:
        raise ToolError(f"cannot parse {path}: {exc}", EXIT_USAGE) from exc
    return dict(parser[station]) if parser.has_section(station) else {}


def save_station(path: Path, station: str, values: Dict[str, str]) -> None:
    """Add or update the keys in one section, leaving every other line alone."""
    if "gpsconfig" in path.resolve().parts:
        raise ToolError(
            f"refusing to write {path}: IMO's config is changed in gps-config-data, "
            "not by this tool",
            EXIT_USAGE,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == f"[{station}]"), None
    )
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{station}]"] + [f"{k} = {v}" for k, v in values.items()]
    else:
        end = next(
            (
                i
                for i in range(start + 1, len(lines))
                if lines[i].lstrip().startswith("[")
            ),
            len(lines),
        )
        pending = dict(values)
        for i in range(start + 1, end):
            key = lines[i].split("=", 1)[0].strip()
            if "=" in lines[i] and key in pending:
                lines[i] = f"{key} = {pending.pop(key)}"
        insert = [f"{k} = {v}" for k, v in pending.items()]
        at = start + 1
        lines[at:at] = insert
    path.write_text("\n".join(lines) + "\n")


def parse_address(address: Optional[str]) -> Tuple[Optional[str], Optional[int]]:
    if not address:
        return None, None
    host, sep, port = address.partition(":")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", host) or (sep and not port.isdigit()):
        raise ToolError(
            f"bad address {address!r}: expected HOST or HOST:PORT", EXIT_USAGE
        )
    return host, int(port) if sep else None


def resolve_port(
    cli: Optional[int], cfg: Dict[str, str], key: str, default: int
) -> int:
    if cli is not None:
        return cli
    value = cfg.get(key, "").strip()
    return int(value) if value.isdigit() else default


def resolve_host(
    host: Optional[str], cfg: Dict[str, str], station: str, path: Path
) -> str:
    resolved = host or cfg.get("router_ip", "").strip()
    if not resolved:
        raise ToolError(
            f"no address for {station}: give HOST[:PORT] or add it to {path}",
            EXIT_USAGE,
        )
    return resolved


def read_cred_file(path: Path) -> Tuple[str, str]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise ToolError(f"{path} is mode {mode:o}: run chmod 600 on it", EXIT_USAGE)
    found: Dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            found[key.strip()] = value.strip()
    if not found.get("username") or not found.get("password"):
        raise ToolError(f"{path} needs username= and password= lines", EXIT_USAGE)
    return found["username"], found["password"]


def load_credentials(args: argparse.Namespace) -> Optional[Tuple[str, str]]:
    """--user/--password, then the environment, --cred-file, the default file."""
    user, password = os.environ.get("PRX5_USER"), os.environ.get("PRX5_PASS")
    if args.password and not args.user:
        raise ToolError("--password needs --user", EXIT_USAGE)
    if bool(user) != bool(password) and not args.user:
        raise ToolError("set both PRX5_USER and PRX5_PASS, or neither", EXIT_USAGE)
    if args.user:
        typed = args.password
        if not typed:
            if not sys.stdin.isatty():
                raise ToolError(
                    "--user needs --password when not at a terminal", EXIT_USAGE
                )
            typed = getpass.getpass(f"Password for {args.user}: ")
        if not typed:
            raise ToolError("empty password", EXIT_USAGE)
        creds = (args.user, typed)
    elif user and password:
        creds = (user, password)
    else:
        path = Path(args.cred_file).expanduser() if args.cred_file else DEFAULT_CRED
        if not path.exists():
            if args.cred_file:
                raise ToolError(f"no credentials file at {path}", EXIT_USAGE)
            return None
        creds = read_cred_file(path)
    # Septentrio commands are comma separated, so these could never log in, and
    # each try would count towards the lockout.
    if any(re.search(r"[,\s]", value) for value in creds):
        raise ToolError(
            "username/password must not contain commas or whitespace", EXIT_USAGE
        )
    return creds


# --- SBF decoding (pure functions; offsets as in polarx5_tcp_extractor.py) -----


def find_block(data: bytes, block_id: int) -> Optional[bytes]:
    """The whole block, or None. A block split across reads is found on a later pass."""
    pos = 0
    while True:
        sync = data.find(b"$@", pos)
        if sync < 0 or sync + 8 > len(data):
            return None
        found = struct.unpack_from("<H", data, sync + 4)[0] & 0x1FFF
        length = struct.unpack_from("<H", data, sync + 6)[0]
        if found == block_id:
            return data[sync : sync + length] if sync + length <= len(data) else None
        pos = sync + max(length, 2)


def error_flags(rx_error: int, ext_error: int) -> List[str]:
    flags = [RX_ERROR_BITS.get(b, f"BIT{b}") for b in range(32) if rx_error & (1 << b)]
    flags += [
        EXT_ERROR_BITS.get(b, f"EXT_BIT{b}") for b in range(8) if ext_error & (1 << b)
    ]
    return flags


def decode_receiver_status(block: bytes) -> Dict[str, Any]:
    if len(block) < 32:
        raise ValueError("ReceiverStatus too short")
    rx_status = struct.unpack_from("<I", block, 20)[0]
    rx_error = struct.unpack_from("<I", block, 24)[0]
    ext_error = block[15]
    flags = error_flags(rx_error, ext_error)
    cpu = block[14]
    temperature = block[31] - 100
    return {
        "cpu_percent": cpu,
        "cpu_status": (
            "critical" if cpu >= CPU_CRIT else "warning" if cpu >= CPU_WARN else "ok"
        ),
        "temperature_c": temperature,
        "temperature_status": band_status(temperature, TEMPERATURE_BAND),
        "uptime_seconds": struct.unpack_from("<I", block, 16)[0],
        "rx_error": rx_error,
        "ext_error": ext_error,
        "error_flags": flags,
        # An antenna overcurrent cuts antenna power: no observations at all.
        "error_status": (
            "critical" if "ANTENNA" in flags else "warning" if flags else "ok"
        ),
        "active_antenna": bool(rx_status & 2),
    }


def decode_power(block: bytes) -> Dict[str, Any]:
    if len(block) < 16:
        raise ValueError("PowerStatus too short")
    voltage = round((struct.unpack_from("<H", block, 14)[0] >> 4) / 40.0, 2)
    return {"voltage": voltage}


def decode_pvt(block: bytes) -> Dict[str, Any]:
    if len(block) < 94:
        raise ValueError("PVTGeodetic too short")
    mode = block[14] & 0x0F
    lat = math.degrees(struct.unpack_from("<d", block, 16)[0])
    lon = math.degrees(struct.unpack_from("<d", block, 24)[0])
    height = struct.unpack_from("<d", block, 32)[0]
    result: Dict[str, Any] = {
        "mode": mode,
        "fix": mode > 0,
        "satellites_used": block[74],
        "status": "ok" if mode > 0 else "warning",
    }
    # SBF marks an absent value with a huge negative "do not use" number.
    if not math.isnan(lat) and abs(lat) <= 90 and abs(lon) <= 180:
        result.update(
            latitude=round(lat, 8), longitude=round(lon, 8), height=round(height, 3)
        )
    return result


def constellation(svid: int) -> Optional[str]:
    ranges = (
        (1, 37, "GPS"),
        (38, 62, "GLONASS"),
        (63, 106, "Galileo"),
        (120, 140, "SBAS"),
        (141, 180, "BeiDou"),
        (181, 187, "QZSS"),
        (191, 197, "IRNSS"),
        (201, 263, "BeiDou"),
    )
    return next((name for low, high, name in ranges if low <= svid <= high), None)


def decode_satellites(block: bytes) -> Dict[str, Any]:
    if len(block) < 16:
        raise ValueError("PVTSatCartesian too short")
    total, size = block[14], block[15]
    counts: Dict[str, int] = {}
    for i in range(total):
        offset = 16 + i * size
        if offset >= len(block):
            break
        name = constellation(block[offset])
        if name:
            counts[name] = counts.get(name, 0) + 1
    return {
        "total": total,
        "by_constellation": counts,
        "status": "ok" if total >= 4 else "warning",
    }


def decode_setup(block: bytes) -> Dict[str, Any]:
    if len(block) < 216:
        raise ValueError("ReceiverSetup too short")

    def text(start: int, size: int) -> str:
        return (
            block[start : start + size]
            .split(b"\0", 1)[0]
            .decode("ascii", "ignore")
            .strip()
        )

    return {
        "marker_name": text(16, 60),
        "marker_number": text(76, 20),
        "serial_number": text(156, 20),
        "receiver_name": text(176, 20),
        "firmware_version": text(196, 20),
    }


def _number(value: Optional[str]) -> float:
    try:
        return float(value or 0)
    except ValueError:
        return 0.0


def disk_status(disks: List[Dict[str, Any]]) -> str:
    mounted = [d for d in disks if d.get("mounted")]
    if not mounted:
        return "critical"
    usage = max(float(d.get("usage_percent", 0)) for d in mounted)
    if usage >= DISK_CRIT or any(d.get("full") for d in mounted):
        return "critical"
    return (
        "warning" if usage >= DISK_WARN or any(d.get("error") for d in disks) else "ok"
    )


def find_rxtool(name: str) -> Optional[str]:
    return shutil.which(name) or next(
        (p for p in (f"/usr/local/rxtools/bin/{name}",) if os.access(p, os.X_OK)), None
    )


def decode_disk(block: bytes) -> Dict[str, Any]:
    """Via bin2asc: the sub-block layout varies by firmware revision."""
    bin2asc = find_rxtool("bin2asc")
    if bin2asc:
        with tempfile.TemporaryDirectory(prefix="prx5-") as tmp:
            sbf = Path(tmp) / "disk.sbf"
            sbf.write_bytes(block)
            subprocess.run(
                [bin2asc, "-f", str(sbf), "-m", "DiskStatus", "-t", "-p", tmp],
                check=True,
                capture_output=True,
                timeout=30,
            )
            outputs = list(Path(tmp).glob("*DiskStatus*"))
            if outputs:
                with outputs[0].open(newline="") as handle:
                    rows = [
                        r
                        for r in csv.DictReader(handle)
                        if not any(str(v).startswith("---") for v in r.values())
                    ]
                disks: List[Dict[str, Any]] = [
                    {
                        "disk_id": int(_number(r.get("DiskID"))),
                        "mounted": _number(r.get("DISK_MOUNTED")) == 1,
                        "full": _number(r.get("DISK_FULL")) == 1,
                        "error": r.get("Error", "") not in ("", "No error"),
                        "total_mb": _number(r.get("DiskSize [MB]")),
                        "usage_percent": _number(r.get("DiskUsagePercent [%]")),
                    }
                    for r in rows
                ]
                return {"disks": disks, "status": disk_status(disks), "limited": False}
    # Without bin2asc only the stable header fields can be read.
    count, size = block[14], block[15]
    disks = []
    for i in range(count):
        offset = 16 + i * size
        if size >= 16 and offset + 16 <= len(block):
            disks.append(
                {
                    "disk_id": block[offset + 4],
                    "mounted": bool(block[offset + 5] & 1),
                    "total_mb": float(struct.unpack_from("<I", block, offset + 12)[0]),
                }
            )
    return {"disks": disks, "status": disk_status(disks), "limited": True}


# --- control connection --------------------------------------------------------


class ControlSession:
    """One TCP command connection, reused for every block."""

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        reply = self._read_until(
            lambda data: re.search(rb"IP\w*>", data) is not None, timeout
        )
        match = re.search(rb"(IP\w*)>", reply)
        self.connection_id = match.group(1).decode() if match else "IP11"

    def close(self) -> None:
        self.sock.close()

    def _read_until(self, done: Callable[[bytes], bool], wait: float) -> bytes:
        data = b""
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not done(data):
            self.sock.settimeout(max(0.05, min(0.5, deadline - time.monotonic())))
            try:
                chunk = self.sock.recv(8192)
            except socket.timeout:  # noqa: UP041 - differs from TimeoutError on 3.9
                continue
            if not chunk:
                break
            data += chunk
        return data

    def login(self, user: str, password: str) -> None:
        """One attempt, ever. Raises ToolError on refusal; never retries."""
        self.sock.sendall(f"login, {user}, {password}\n".encode())

        def answered(data: bytes) -> bool:
            return re.search(rb"(\$R|\$E)[^\n]*\n(.|\n)*IP\w*>", data) is not None

        text = self._read_until(answered, self.timeout).decode("ascii", "ignore")
        locked = re.search(r"Too many failed login.*?(\d+) sec", text)
        if locked:
            seconds = int(locked.group(1))
            raise ToolError(
                f"receiver locked by failed logins for another {seconds} s "
                f"(~{seconds / 3600:.1f} h). It counts down whatever you do: wait it out."
            )
        if "$E" in text and "Invalid command" in text:
            return  # firmware <= 5.5 has no login command and needs none
        if "$R?" in text:
            detail = next((ln for ln in text.splitlines() if "$R?" in ln), "").strip()
            raise ToolError(
                f"login refused and NOT retried ({detail}). Fix the credentials before "
                "running again: repeated failures lock the receiver for about 9 h."
            )

    def request_block(
        self, name: str, block_id: int, wait: float = 3.0
    ) -> Optional[bytes]:
        self.sock.sendall(f"esoc, {self.connection_id}, {name}\n".encode())

        def done(data: bytes) -> bool:
            return find_block(data, block_id) is not None or b"Not authorized" in data

        data = self._read_until(done, wait)
        if b"Not authorized" in data:
            raise ToolError(
                "receiver answered 'Not authorized': fw 5.7.0 needs credentials "
                "(see README.md)"
            )
        return find_block(data, block_id)


# --- health ------------------------------------------------------------------


def probe_port(host: str, port: int) -> str:
    """'open', 'refused' or 'timeout'. Retried once: 3G/4G links lose packets."""
    state = "timeout"
    for _ in range(2):
        try:
            socket.create_connection((host, port), timeout=5).close()
            return "open"
        except ConnectionRefusedError:
            state = "refused"
        except OSError:
            state = "timeout"
    return state


def check_ports(host: str, ports: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
    return {
        name: {"port": p, "state": probe_port(host, p)} for name, p in ports.items()
    }


def port_hints(ports: Dict[str, Dict[str, Any]], host: str) -> List[str]:
    hints = []
    if ports["ftp"]["state"] != "open":
        hints.append(
            "FTP closed: rxupgrade to 5.7.0 disables it. `receivers rec-provision <SID>` "
            "reopens it, and until then no data can be downloaded."
        )
    if (
        ports["control"]["state"] != "open"
        and probe_port(host, SECURE_CONTROL_PORT) == "open"
    ):
        hints.append(
            f"Control port closed but TLS port {SECURE_CONTROL_PORT} open: the receiver "
            "is in `sis, secure`, as it is left after a firmware upgrade."
        )
    return hints


def collect_health(session: ControlSession) -> Dict[str, Any]:
    decoders: List[Tuple[str, Tuple[str, int], Callable[[bytes], Dict[str, Any]]]] = [
        ("identity", BLOCK_SETUP, decode_setup),
        ("receiver", BLOCK_RECEIVER, decode_receiver_status),
        ("power", BLOCK_POWER, decode_power),
        ("position", BLOCK_PVT, decode_pvt),
        ("satellites", BLOCK_SATS, decode_satellites),
        ("disk", BLOCK_DISK, decode_disk),
    ]
    result: Dict[str, Any] = {}
    for key, (name, block_id), decode in decoders:
        block = session.request_block(name, block_id)
        if block is None:
            result[key] = None
            continue
        try:
            result[key] = decode(block)
        except (ValueError, struct.error, OSError, subprocess.SubprocessError) as exc:
            result[key] = {"status": "warning", "decode_error": str(exc)}
    return result


def overall_status(report: Dict[str, Any]) -> str:
    statuses = [
        "ok" if p["state"] == "open" else "warning" for p in report["ports"].values()
    ]
    receiver = report.get("receiver")
    if receiver:
        statuses += [
            receiver.get(k, "ok")
            for k in ("cpu_status", "temperature_status", "error_status")
        ]
        statuses += [receiver["status"]] if "decode_error" in receiver else []
    for key in ("power", "position", "satellites", "disk"):
        part = report.get(key)
        statuses.append(part.get("status", "ok") if part else "warning")
    if report.get("receiver") is None:
        statuses.append("warning")
    return worst(statuses)


def format_uptime(seconds: int) -> str:
    days, rest = divmod(seconds, 86400)
    return f"{days}d {rest // 3600:02d}h {rest % 3600 // 60:02d}m"


def print_health(report: Dict[str, Any]) -> None:
    print(f"{report['station']} @ {report['host']}  {report['checked_at']}")
    for name, port in report["ports"].items():
        print(f"  {name + ' port':16} {port['port']} {port['state']}")
    identity = report.get("identity") or {}
    if identity.get("firmware_version"):
        print(
            f"  {'receiver':16} {identity.get('receiver_name', '?')} "
            f"fw {identity['firmware_version']} serial {identity.get('serial_number', '?')} "
            f"marker {identity.get('marker_name', '?')}"
        )
    receiver = report.get("receiver")
    if receiver and "decode_error" not in receiver:
        print(f"  {'cpu':16} {receiver['cpu_percent']} %  [{receiver['cpu_status']}]")
        print(
            f"  {'temperature':16} {receiver['temperature_c']} C  [{receiver['temperature_status']}]"
        )
        print(f"  {'uptime':16} {format_uptime(receiver['uptime_seconds'])}")
        print(f"  {'antenna power':16} {'on' if receiver['active_antenna'] else 'OFF'}")
        flags = ", ".join(receiver["error_flags"]) or "none"
        print(f"  {'errors':16} {flags}  [{receiver['error_status']}]")
    power = report.get("power")
    if power and "voltage" in power:
        print(f"  {'voltage':16} {power['voltage']} V")
    position = report.get("position")
    if position and "mode" in position:
        where = (
            f"{position['latitude']:.6f} {position['longitude']:.6f} {position['height']:.1f} m"
            if "latitude" in position
            else "no position"
        )
        print(
            f"  {'position':16} mode {position['mode']}, {where}  [{position['status']}]"
        )
    satellites = report.get("satellites")
    if satellites and "total" in satellites:
        parts = " ".join(
            f"{k}:{v}" for k, v in sorted(satellites["by_constellation"].items())
        )
        print(
            f"  {'satellites used':16} {satellites['total']} {parts}  [{satellites['status']}]"
        )
    disk = report.get("disk")
    if disk and "disks" in disk:
        for item in disk["disks"]:
            state = "mounted" if item.get("mounted") else "not mounted"
            usage = (
                f"{item['usage_percent']:.1f} % of " if "usage_percent" in item else ""
            )
            print(
                f"  {'disk ' + str(item['disk_id']):16} {state}, {usage}{item['total_mb']:.0f} MB"
            )
        note = (
            "  (no bin2asc: usage and full state unknown)"
            if disk.get("limited")
            else ""
        )
        print(f"  {'disk status':16} [{disk['status']}]{note}")
    for key in ("identity", "receiver", "power", "position", "satellites", "disk"):
        if report.get(key) is None and key in report:
            print(f"  {key:16} no reply")
    for hint in report.get("hints", []):
        print(f"  hint: {hint}")
    if report.get("error"):
        print(f"  error: {report['error']}")
    print(f"STATUS: {report['status'].upper()}")


def command_health(args: argparse.Namespace) -> int:
    path = cfg_path(args)
    cfg = read_station(path, args.station)
    host_arg, port_arg = parse_address(args.address)
    host = resolve_host(host_arg, cfg, args.station, path)
    ports = {
        "ftp": resolve_port(args.ftp_port, cfg, "receiver_ftpport", DEFAULT_FTP_PORT),
        "http": resolve_port(
            args.http_port, cfg, "receiver_httpport", DEFAULT_HTTP_PORT
        ),
        "control": resolve_port(
            port_arg, cfg, "receiver_controlport", DEFAULT_CONTROL_PORT
        ),
    }
    creds = None if args.no_login else load_credentials(args)
    report: Dict[str, Any] = {
        "station": args.station,
        "host": host,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ports": check_ports(host, ports),
    }
    report["hints"] = port_hints(report["ports"], host)
    code = None
    if report["ports"]["control"]["state"] != "open":
        report["status"] = "unknown"
        report["error"] = (
            f"control port {ports['control']} {report['ports']['control']['state']}"
        )
        code = EXIT_UNKNOWN
    else:
        session = None
        try:
            session = ControlSession(host, ports["control"], args.timeout)
            if creds:
                session.login(*creds)
            report.update(collect_health(session))
            report["status"] = overall_status(report)
        except ToolError as exc:
            report["status"], report["error"], code = "unknown", str(exc), exc.code
        except OSError as exc:
            report["status"], report["error"], code = "unknown", str(exc), EXIT_UNKNOWN
        finally:
            if session:
                session.close()
    if args.save and host_arg:
        save_station(
            path,
            args.station,
            {"router_ip": host, "receiver_controlport": str(ports["control"])},
        )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_health(report)
    return code if code is not None else RANK[report["status"]]


def command_ports(args: argparse.Namespace) -> int:
    path = cfg_path(args)
    cfg = read_station(path, args.station)
    host_arg, _ = parse_address(args.address)
    host = resolve_host(host_arg, cfg, args.station, path)
    ports = {
        "ftp": resolve_port(args.ftp_port, cfg, "receiver_ftpport", DEFAULT_FTP_PORT),
        "http": resolve_port(
            args.http_port, cfg, "receiver_httpport", DEFAULT_HTTP_PORT
        ),
        "control": resolve_port(
            args.control_port, cfg, "receiver_controlport", DEFAULT_CONTROL_PORT
        ),
    }
    states = check_ports(host, ports)
    if args.json:
        print(
            json.dumps(
                {"station": args.station, "host": host, "ports": states}, indent=2
            )
        )
    else:
        for name, port in states.items():
            print(f"{args.station} {name:8} {host}:{port['port']} {port['state']}")
        for hint in port_hints(states, host):
            print(f"hint: {hint}")
    return (
        EXIT_OK if all(p["state"] == "open" for p in states.values()) else EXIT_WARNING
    )


def command_stations(args: argparse.Namespace) -> int:
    path = cfg_path(args)
    if not path.exists():
        print(f"no stations file at {path}")
        return EXIT_OK
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path)
    print(f"# {path}")
    for name in parser.sections():
        section = parser[name]
        print(
            f"{name:6} {section.get('router_ip', '-'):28} "
            f"ftp={section.get('receiver_ftpport', '-')} "
            f"control={section.get('receiver_controlport', '-')}"
        )
    return EXIT_OK


# --- download: the time range and the paths ------------------------------------


def parse_when(text: str) -> Tuple[datetime, bool]:
    """UTC. Returns the moment and whether an hour was given."""
    for fmt, has_hour in (
        ("%Y-%m-%d", False),
        ("%Y%m%d", False),
        ("%Y-%m-%dT%H", True),
        ("%Y-%m-%d %H", True),
        ("%Y%m%d%H", True),
    ):
        try:
            return datetime.strptime(text, fmt), has_hour
        except ValueError:
            pass
    raise ToolError(
        f"bad date {text!r}: use YYYY-MM-DD or YYYY-MM-DDTHH (UTC)", EXIT_USAGE
    )


def period_times(
    freq: str,
    start: Optional[str],
    end: Optional[str],
    now: datetime,
    unfinalised: bool,
    back: Optional[int] = None,
) -> List[datetime]:
    """Period starts from start to end, BOTH INCLUSIVE, never in the future.

    With no dates: the last complete period, or the running one with `unfinalised`.
    With `back`: the last N complete periods, plus the running one with `unfinalised`.
    """
    step = timedelta(days=1) if freq == "1D" else timedelta(hours=1)
    current = (
        now.replace(hour=0, minute=0, second=0, microsecond=0)
        if freq == "1D"
        else now.replace(minute=0, second=0, microsecond=0)
    )

    def first(value: datetime) -> datetime:
        return value.replace(hour=0) if freq == "1D" else value

    if back is not None:
        if start is not None or end is not None:
            raise ToolError("-D cannot be combined with --start or --end", EXIT_USAGE)
        times = [current - step * n for n in range(back, 0, -1)]
        return times + [current] if unfinalised else times
    if start is None and end is None:
        return [current if unfinalised else current - step]
    if start is None:
        raise ToolError("--end needs --start", EXIT_USAGE)
    begin, begin_has_hour = parse_when(start)
    if end is None:
        last = begin if (begin_has_hour or freq == "1D") else begin.replace(hour=23)
    else:
        last, end_has_hour = parse_when(end)
        if freq == "1H" and not end_has_hour:
            last = last.replace(hour=23)
    begin, last = first(begin), first(last)
    if last < begin:
        raise ToolError("--end is before --start", EXIT_USAGE)
    times = []
    moment = begin
    while moment <= last and moment <= current:
        times.append(moment)
        moment += step
    return times


def gtimes_path(template: str, freq: str, moment: datetime) -> str:
    try:
        from gtimes.timefunc import datepathlist
    except ImportError as exc:
        raise ToolError(
            "gtimes is required for download: pip install gtimes", EXIT_USAGE
        ) from exc
    step = timedelta(days=1) if freq == "1D" else timedelta(hours=1)
    return str(datepathlist(template, freq, moment, moment + step)[0])


@dataclass
class Target:
    moment: datetime
    remote: str
    local: Path


def build_target(
    station: str,
    session_name: str,
    moment: datetime,
    base: str,
    outdir: Path,
    flat: bool,
) -> Target:
    session = SESSIONS[session_name]
    remote = gtimes_path(
        f"{base.rstrip('/')}/{session.remote_dir}/%y%j/{station}#Rin2_.gz",
        session.freq,
        moment,
    )
    name = f"{station}%Y%m%d%H00{session.letter}.sbf.gz"
    template = (
        name
        if flat
        else LOCAL_TEMPLATE.format(station=station, session=session_name, name=name)
    )
    return Target(moment, remote, outdir / gtimes_path(template, session.freq, moment))


def valid_gzip(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as handle:
            while handle.read(1 << 20):
                pass
        return True
    except (OSError, EOFError, zlib.error):
        return False


# --- download: FTP --------------------------------------------------------------


class Receiver:
    """A lazily opened FTP connection. A refused login ends the whole run."""

    def __init__(
        self,
        host: str,
        port: int,
        creds: Optional[Tuple[str, str]],
        timeout: float,
        passive: bool,
    ) -> None:
        self.host, self.port, self.creds = host, port, creds
        self.timeout, self.passive = timeout, passive
        self.ftp: Optional[ftplib.FTP] = None

    def connect(self) -> ftplib.FTP:
        if self.ftp is not None:
            return self.ftp
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=self.timeout)
        try:
            ftp.login(*(self.creds or ("anonymous", "")))
        except ftplib.error_perm as exc:
            ftp.close()
            raise ToolError(
                f"FTP login refused and NOT retried ({exc}). Fix the credentials before "
                "running again: repeated failures lock the receiver for about 9 h."
            ) from exc
        ftp.set_pasv(self.passive)
        ftp.voidcmd("TYPE I")
        self.ftp = ftp
        return ftp

    def drop(self) -> None:
        if self.ftp is not None:
            self.ftp.close()
            self.ftp = None

    def close(self) -> None:
        if self.ftp is not None:
            try:
                self.ftp.quit()
            except (OSError, EOFError, ftplib.Error):
                self.ftp.close()
            self.ftp = None

    def size(self, remote: str) -> Optional[int]:
        try:
            return self.connect().size(remote)
        except ftplib.error_perm:
            return None


def fetch(receiver: Receiver, remote: str, part: Path, size: Optional[int]) -> None:
    """RETR into `part`, resuming when the partial file can still be valid."""
    offset = part.stat().st_size if part.exists() else 0
    if size is None or offset > size:
        # The server answers 554 to a REST beyond the file, for ever.
        part.unlink(missing_ok=True)
        offset = 0
    if size is not None and offset == size:
        return
    ftp = receiver.connect()
    with part.open("ab") as handle:
        ftp.retrbinary(f"RETR {remote}", handle.write, rest=offset or None)


def fetch_target(
    receiver: Receiver, target: Target, unfinalised: bool, force: bool, retries: int
) -> Tuple[str, int]:
    """Returns (outcome, bytes on disk). Raises ToolError only for a refused login."""
    part = target.local.with_name(target.local.name + ".part")
    target.local.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(min(30, 5 * attempt))
        try:
            size = receiver.size(target.remote)
            if size is None:
                bare = target.remote[:-3]
                bare_size = receiver.size(bare)
                if not bare_size:
                    return "absent", 0
                if not unfinalised:
                    return "unfinalised", 0
                # Still being written, so it grows: no resume, and gzip it here.
                raw = target.local.with_name(target.local.name + ".raw")
                raw.unlink(missing_ok=True)
                fetch(receiver, bare, raw, None)
                if raw.stat().st_size < bare_size:
                    raise OSError(f"short read of {bare}")
                with raw.open("rb") as source, gzip.open(part, "wb") as sink:
                    shutil.copyfileobj(source, sink)
                raw.unlink()
                part.replace(target.local)
                return "fetched-unfinalised", target.local.stat().st_size
            if (
                target.local.exists()
                and target.local.stat().st_size == size
                and not force
            ):
                return "present", size
            fetch(receiver, target.remote, part, size)
            if part.stat().st_size != size:
                raise OSError(f"got {part.stat().st_size} of {size} bytes")
            if not valid_gzip(part):
                part.unlink()
                raise OSError("downloaded file is not valid gzip")
            part.replace(target.local)
            return "fetched", size
        except ftplib.error_perm as exc:
            if str(exc).startswith("554"):
                part.unlink(missing_ok=True)
            last_error = str(exc)
            receiver.drop()
        except (OSError, EOFError, ftplib.Error) as exc:
            last_error = str(exc) or type(exc).__name__
            receiver.drop()
    return f"failed: {last_error}", 0


def quicklook_rinex(
    sbf2rin: str, target: Target, station: str, freq: str, version: str
) -> str:
    """Uncorrected RINEX beside the raw file. Headers come from the receiver, not TOS."""
    # Beside raw/ in the date tree; inside --outdir with --flat.
    raw_dir = target.local.parent
    base = raw_dir.parent if raw_dir.name == "raw" else raw_dir
    outdir = base / "rinex_quicklook"
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / gtimes_path(f"{station}#Rin2o", freq, target.moment)
    with tempfile.TemporaryDirectory(prefix="prx5-") as tmp:
        sbf = Path(tmp) / target.local.name[:-3]
        with gzip.open(target.local, "rb") as source, sbf.open("wb") as sink:
            shutil.copyfileobj(source, sink)
        subprocess.run(
            [sbf2rin, "-f", str(sbf), "-o", str(output), f"-R{version}"],
            check=True,
            capture_output=True,
            timeout=1800,
        )
    return str(output)


def command_download(args: argparse.Namespace) -> int:
    path = cfg_path(args)
    cfg = read_station(path, args.station)
    host_arg, port_arg = parse_address(args.address)
    now = datetime.now(UTC).replace(tzinfo=None)
    session = SESSIONS[args.session]
    times = period_times(
        session.freq, args.start, args.end, now, args.unfinalised, args.sessions_back
    )
    base = args.base_path or cfg.get("base_path", "").strip() or DEFAULT_BASE_PATH
    outdir = Path(args.outdir).expanduser()
    targets = [
        build_target(args.station, args.session, t, base, outdir, args.flat)
        for t in times
    ]
    if args.print_paths:
        for target in targets:
            print(f"{target.remote} -> {target.local}")
        return EXIT_OK

    host = resolve_host(host_arg, cfg, args.station, path)
    port = resolve_port(port_arg, cfg, "receiver_ftpport", DEFAULT_FTP_PORT)
    creds = None if args.anonymous else load_credentials(args)
    if creds is None and not args.anonymous:
        raise ToolError(
            "no credentials: fw 5.7.0 refuses anonymous FTP. Set PRX5_USER and PRX5_PASS "
            f"or create {DEFAULT_CRED} (see README.md); use --anonymous for older firmware.",
            EXIT_USAGE,
        )
    sbf2rin = None
    if args.rinex:
        sbf2rin = find_rxtool("sbf2rin")
        if not sbf2rin:
            raise ToolError(
                "--rinex needs sbf2rin from RxTools (see README.md)", EXIT_USAGE
            )
    state = probe_port(host, port)
    if state != "open":
        raise ToolError(
            f"FTP port {host}:{port} {state}. After a 5.7.0 upgrade FTP is off until "
            "`receivers rec-provision <SID>`; `prx5.py health` shows the other ports."
        )
    if args.save and host_arg:
        save_station(
            path, args.station, {"router_ip": host, "receiver_ftpport": str(port)}
        )

    receiver = Receiver(host, port, creds, args.timeout, not args.active)
    results: List[Dict[str, Any]] = []
    try:
        for target in targets:
            if args.dry_run:
                size = receiver.size(target.remote)
                outcome, count = (
                    ("available", size) if size is not None else ("absent", 0)
                )
                if size is None and receiver.size(target.remote[:-3]):
                    outcome = "unfinalised"
            else:
                outcome, count = fetch_target(
                    receiver, target, args.unfinalised, args.force, args.retries
                )
            item: Dict[str, Any] = {
                "remote": target.remote,
                "local": str(target.local),
                "outcome": outcome,
                "bytes": count,
            }
            if sbf2rin and outcome.startswith("fetched"):
                try:
                    item["rinex"] = quicklook_rinex(
                        sbf2rin, target, args.station, session.freq, args.rinex
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    item["rinex_error"] = str(exc)
            results.append(item)
            if not args.json:
                note = f" {item['rinex']}" if "rinex" in item else ""
                print(f"{outcome:20} {count:>12} {target.local}{note}")
    finally:
        receiver.close()

    bad = [
        r
        for r in results
        if not r["outcome"].startswith(("fetched", "present", "available"))
    ]
    if args.json:
        print(
            json.dumps(
                {"station": args.station, "host": host, "files": results}, indent=2
            )
        )
    else:
        label = "available on the receiver" if args.dry_run else "in place"
        print(f"{len(results) - len(bad)}/{len(results)} files {label}")
        if any(r["outcome"] == "unfinalised" for r in results):
            print(
                "hint: unfinalised = still being written; --unfinalised fetches it as it stands"
            )
    return EXIT_WARNING if bad or any("rinex_error" in r for r in results) else EXIT_OK


# --- command line ---------------------------------------------------------------


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # exit 2 means "critical" here
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def positive_int(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a whole number of at least 1"
        )
    return int(text)


def station_id(text: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{4}", text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a four-character station ID")
    return text.upper()


def build_parser() -> argparse.ArgumentParser:
    common = Parser(add_help=False)
    common.add_argument(
        "--cfg", help=f"stations file (default $PRX5_STATIONS_CFG or {DEFAULT_CFG})"
    )
    common.add_argument(
        "--timeout", type=float, default=30.0, help="network timeout, s (default 30)"
    )
    common.add_argument("--json", action="store_true", help="JSON on stdout")

    target = Parser(add_help=False)
    target.add_argument("station", type=station_id, metavar="STA")
    target.add_argument("address", nargs="?", metavar="HOST[:PORT]")

    auth = Parser(add_help=False)
    auth.add_argument(
        "-u", "--user", help="login user; asks for the password if -p is left out"
    )
    auth.add_argument(
        "-p", "--password", help="login password; visible in ps and shell history"
    )
    auth.add_argument(
        "--cred-file", help=f"credentials file, mode 600 (default {DEFAULT_CRED})"
    )
    auth.add_argument(
        "--save", action="store_true", help="store the address in the stations file"
    )

    parser = Parser(
        prog="prx5.py", description="PolaRx5 (fw 5.7.0) download and health tool."
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)

    download = commands.add_parser(
        "download",
        parents=[target, common, auth],
        help="fetch files over FTP; PORT is the FTP port (default 2160)",
    )
    download.add_argument("-s", "--session", choices=SESSIONS, default="15s_24hr")
    download.add_argument(
        "-D",
        "--sessions-back",
        type=positive_int,
        metavar="N",
        help="the last N complete sessions: days for 15s_24hr, hours for the 1hr sessions",
    )
    download.add_argument(
        "--start", help="first period, UTC: YYYY-MM-DD or YYYY-MM-DDTHH"
    )
    download.add_argument(
        "--end", help="last period, INCLUSIVE (default: same as --start)"
    )
    download.add_argument(
        "-o", "--outdir", default="gpsdata", help="root of the tree (default ./gpsdata)"
    )
    download.add_argument(
        "--flat", action="store_true", help="no date tree, all files in --outdir"
    )
    download.add_argument(
        "--base-path", help=f"on the receiver (default {DEFAULT_BASE_PATH})"
    )
    download.add_argument(
        "--dry-run", action="store_true", help="ask the receiver, write nothing"
    )
    download.add_argument(
        "--print-paths", action="store_true", help="print paths, no network"
    )
    download.add_argument(
        "--unfinalised",
        action="store_true",
        help="also fetch a file still being written",
    )
    download.add_argument(
        "--force", action="store_true", help="fetch even if the local size matches"
    )
    download.add_argument(
        "--retries", type=int, default=2, help="after network errors (default 2)"
    )
    download.add_argument(
        "--active", action="store_true", help="active FTP (default passive)"
    )
    download.add_argument(
        "--anonymous", action="store_true", help="no login, for firmware before 5.7"
    )
    download.add_argument(
        "--rinex",
        nargs="?",
        const="304",
        choices=["211", "304", "305", "400"],
        help="quick-look RINEX with sbf2rin (default 304); uncorrected headers",
    )
    download.set_defaults(func=command_download)

    health = commands.add_parser(
        "health",
        parents=[target, common, auth],
        help="query the receiver; PORT is the control port (default 28784)",
    )
    health.add_argument("--ftp-port", type=int)
    health.add_argument("--http-port", type=int)
    health.add_argument("--no-login", action="store_true", help="do not send a login")
    health.set_defaults(func=command_health)

    ports = commands.add_parser(
        "ports", parents=[target, common], help="TCP check only: no login, nothing sent"
    )
    ports.add_argument("--ftp-port", type=int)
    ports.add_argument("--http-port", type=int)
    ports.add_argument("--control-port", type=int)
    ports.set_defaults(func=command_ports)

    stations = commands.add_parser(
        "stations", parents=[common], help="list the stations file"
    )
    stations.set_defaults(func=command_stations)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ToolError as exc:
        print(f"prx5.py: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
