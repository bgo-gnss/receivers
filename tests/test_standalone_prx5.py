"""scripts/standalone_tools: prx5.py and prx5.sh against a fake receiver on loopback.

Both scripts run as subprocesses, so the no_network_plugin guard does not touch
them and nothing here can reach a real receiver. The property that matters most
is the login count: a few failed logins lock a 5.7.0 receiver for about 9 h.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, List, Optional

import pytest

TOOLS = Path(__file__).resolve().parent.parent / "scripts" / "standalone_tools"
USER, PASSWORD = "tester", "s3cret"
BOTH = ["py", "sh"]
GZ_BODY = bytes.fromhex("1f8b08000000000000034b4c4a0600c241243503000000")

spec = importlib.util.spec_from_file_location("prx5", TOOLS / "prx5.py")
assert spec and spec.loader
prx5 = importlib.util.module_from_spec(spec)
sys.modules["prx5"] = prx5
spec.loader.exec_module(prx5)

# ReceiverStatus2 from ENTC on 2026-11-03: RxError=32 (ANTENNA), RxState=640.
ENTC_RECEIVER_STATUS = bytes.fromhex(
    "24400f35ae2f3800ffffffffffff10008b00000080020000200000000604017e"
    "003c5b00013e5900053c5d00063d6200033a6200043a5d00"
)


# Real blocks from the archived GJAC files of 2026-10-01 (fw 5.7.0). Their decoded
# values were compared with bin2asc, so these check the offsets independently.
GJAC_PVT_GEODETIC = bytes.fromhex(
    "2440e53aa74f6000007099148609030027ee4809861af23fd40d0f85caacd3bf"
    "0020cb21ccc08c407c2c7042000000000000000000000000f90295d0ce9c8d91"
    "2df6d33f4fa1953800001500ffffffff0101000001000000b600ffffffff4200"
)
GJAC_RECEIVER_SETUP = bytes.fromhex(
    "24405f6e0e97a8010070991486090000474a4143000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000474a414300000000000000000000000000000000"
    "62676f2f686d6600000000000000000000000000494d4f000000000000000000"
    "0000000000000000000000000000000000000000000000000000000034313034"
    "3030350000000000000000000000000053535243370000000000000000000000"
    "00000000352e372e300000000000000000000000000000003530303031353435"
    "31000000000000000000000054524d35373937312e303020202020204e4f4e45"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "323032352e30382e342d67343161353464643238320000000000000000000000"
    "0000000000000000506f6c615278350000000000000000000000000000000000"
    "00000000000000000000000000000000fced4809861af23fd40d0f85caacd3bf"
    "61066644474a4143000000000000000000000000000000000000000000000000"
    "0000000000000000"
)


def healthy_status() -> bytes:
    block = bytearray(ENTC_RECEIVER_STATUS)
    block[20:24] = (2802).to_bytes(4, "little")
    block[24:28] = (0).to_bytes(4, "little")
    return bytes(block)


def sbf(block_id: int, body: bytes) -> bytes:
    return b"$@\0\0" + struct.pack("<HH", block_id, 8 + len(body)) + body


def power_block(volts: float = 13.5) -> bytes:
    return sbf(4101, b"\0" * 6 + struct.pack("<H", int(volts * 40) << 4 | 1))


def pvt_block() -> bytes:
    return GJAC_PVT_GEODETIC


def sats_block() -> bytes:
    svids = [5, 12, 40, 70, 210]
    body = (
        b"\0" * 6
        + bytes([len(svids), 4])
        + b"".join(bytes([s, 0, 0, 0]) for s in svids)
    )
    return sbf(4008, body)


def setup_block() -> bytes:
    return GJAC_RECEIVER_SETUP


def disk_block() -> bytes:
    sub = bytearray(16)
    sub[4], sub[5] = 1, 1
    sub[12:16] = (15000).to_bytes(4, "little")
    return sbf(4059, b"\0" * 6 + bytes([1, 16]) + bytes(sub))


class FakeReceiver:
    """The control port: prompt, login, esoc. Enforces fw 5.7.0 authorization."""

    def __init__(
        self, status_block: bytes = ENTC_RECEIVER_STATUS, locked: int = 0
    ) -> None:
        self.blocks = {
            "ReceiverSetup": setup_block(),
            "ReceiverStatus": status_block,
            "PowerStatus": power_block(),
            "PVTGeodetic": pvt_block(),
            "PVTSatCartesian": sats_block(),
            "DiskStatus": disk_block(),
        }
        self.locked = locked
        self.logins: List[str] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.wfile.write(b"IP14>")
                authed = False
                for raw in self.rfile:
                    line = raw.decode().strip()
                    if line.startswith("login,"):
                        given = [p.strip() for p in line.split(",")[1:]]
                        outer.logins.append(given[1])
                        if outer.locked:
                            reply = f"$R? LogIn: Too many failed login attempts! Next login possible in {outer.locked} sec\n"
                        elif given == [USER, PASSWORD]:
                            authed, reply = True, "$R! LogIn\n  OK\n"
                        else:
                            reply = "$R? LogIn: Wrong username or password!\n"
                        self.wfile.write(reply.encode() + b"IP14>")
                    elif line.startswith("esoc,"):
                        if not authed:
                            self.wfile.write(b"$E: Not authorized!\nIP14>")
                            continue
                        name = line.split(",")[2].strip()
                        block = outer.blocks[name]
                        self.wfile.write(b"$R: " + raw + b"IP14>")
                        # Split on purpose: a block must survive straddling reads.
                        self.wfile.write(block[:10])
                        self.wfile.flush()
                        self.wfile.write(block[10:])

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeFtp:
    """Just enough FTP for ftplib and curl, with passive transfers."""

    def __init__(self, files: Dict[str, bytes]) -> None:
        self.files = files
        self.logins: List[str] = []
        self.rests: List[int] = []
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, text: str) -> None:
                self.wfile.write(text.encode() + b"\r\n")

            def handle(self) -> None:
                self.reply("220 fake")
                rest = 0
                data_sock: Optional[socket.socket] = None
                user = ""
                for raw in self.rfile:
                    command, _, arg = raw.decode().strip().partition(" ")
                    command = command.upper()
                    if command == "USER":
                        user = arg
                        self.reply("331 password")
                    elif command == "PASS":
                        outer.logins.append(user)
                        if (user, arg) == (USER, PASSWORD):
                            self.reply("230 ok")
                        else:
                            self.reply("530 Login incorrect")
                    elif command == "PWD":
                        self.reply('257 "/"')
                    elif command == "TYPE":
                        self.reply("200 ok")
                    elif command == "SIZE":
                        if arg in outer.files:
                            self.reply(f"213 {len(outer.files[arg])}")
                        else:
                            self.reply("550 no file")
                    elif command == "MDTM":
                        self.reply("213 20261103000000")
                    elif command == "REST":
                        rest = int(arg)
                        outer.rests.append(rest)
                        self.reply("350 ok")
                    elif command == "PASV":
                        data_sock = socket.socket()
                        data_sock.bind(("127.0.0.1", 0))
                        data_sock.listen(1)
                        port = data_sock.getsockname()[1]
                        self.reply(f"227 Entering (127,0,0,1,{port >> 8},{port & 255})")
                    elif command == "RETR":
                        if arg not in outer.files or data_sock is None:
                            self.reply("550 no file")
                            continue
                        self.reply("150 sending")
                        conn, _ = data_sock.accept()
                        conn.sendall(outer.files[arg][rest:])
                        conn.close()
                        data_sock.close()
                        rest = 0
                        self.reply("226 done")
                    elif command == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("502 no")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def run(
    kind: str,
    *args: str,
    env: Optional[Dict[str, str]] = None,
    password: Optional[str] = PASSWORD,
) -> subprocess.CompletedProcess[str]:
    script = (
        [sys.executable, str(TOOLS / "prx5.py")]
        if kind == "py"
        else ["bash", str(TOOLS / "prx5.sh")]
    )
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("PRX5_")}
    full_env["XDG_CONFIG_HOME"] = (env or {}).get("XDG_CONFIG_HOME", "/nonexistent")
    if password is not None:
        full_env.update(PRX5_USER=USER, PRX5_PASS=password)
    return subprocess.run(
        script + list(args), env=full_env, capture_output=True, text=True, timeout=90
    )


@pytest.fixture
def receiver() -> Iterator[FakeReceiver]:
    fake = FakeReceiver()
    yield fake
    fake.stop()


REMOTE = "/DSK1/SSN/LOG1_15s_24hr/26307/ENTC3070.26_.gz"
LOCAL = "2026/nov/ENTC/15s_24hr/raw/ENTC202611030000a.sbf.gz"


@pytest.fixture
def ftp() -> Iterator[FakeFtp]:
    fake = FakeFtp({REMOTE: GZ_BODY})
    yield fake
    fake.stop()


# --- decoders --------------------------------------------------------------


def test_receiver_status_decodes_the_real_entc_block():
    decoded = prx5.decode_receiver_status(ENTC_RECEIVER_STATUS)
    assert decoded["error_flags"] == ["ANTENNA"]
    assert decoded["error_status"] == "critical"
    assert decoded["active_antenna"] is False
    assert decoded["uptime_seconds"] == 139


def test_find_block_waits_for_the_whole_block():
    block = power_block()
    assert prx5.find_block(block[:-1], 4101) is None
    assert prx5.find_block(b"IP14>" + pvt_block() + block + b"x", 4101) == block


def test_satellites_are_counted_by_constellation():
    decoded = prx5.decode_satellites(sats_block())
    assert decoded["total"] == 5
    assert decoded["by_constellation"] == {
        "GPS": 2,
        "GLONASS": 1,
        "Galileo": 1,
        "BeiDou": 1,
    }


def test_real_pvt_and_setup_match_bin2asc():
    pvt = prx5.decode_pvt(GJAC_PVT_GEODETIC)
    assert (pvt["mode"], pvt["satellites_used"]) == (3, 21)
    assert pvt["latitude"] == pytest.approx(64.82876981)
    assert pvt["longitude"] == pytest.approx(-17.61394482)
    setup = prx5.decode_setup(GJAC_RECEIVER_SETUP)
    assert (setup["firmware_version"], setup["serial_number"]) == ("5.7.0", "4104005")


def test_flat_quicklook_rinex_stays_inside_outdir(tmp_path, monkeypatch):
    local = tmp_path / "out" / "ENTC202611030000a.sbf.gz"
    local.parent.mkdir()
    local.write_bytes(GZ_BODY)
    monkeypatch.setattr(prx5.subprocess, "run", lambda *a, **k: None)
    target = prx5.Target(datetime(2026, 11, 3), "x", local)
    output = prx5.quicklook_rinex("sbf2rin", target, "ENTC", "1D", "304")
    assert Path(output).parent == local.parent / "rinex_quicklook"


@pytest.mark.parametrize("kind", BOTH)
@pytest.mark.parametrize("volts", [9.0, 24.0])
def test_voltage_is_reported_but_never_changes_the_status(kind, volts):
    fake = FakeReceiver(status_block=healthy_status())
    fake.blocks["PowerStatus"] = power_block(volts)
    try:
        out = run(
            kind,
            "health",
            "ENTC",
            f"127.0.0.1:{fake.port}",
            "--ftp-port",
            str(fake.port),
            "--http-port",
            str(fake.port),
            "--json",
        )
    finally:
        fake.stop()
    report = json.loads(out.stdout)
    assert out.returncode == 0, out.stdout + out.stderr
    assert report["power"]["voltage"] == volts
    assert "status" not in report["power"]


# --- time range and paths --------------------------------------------------


def test_end_is_inclusive_and_never_in_the_future():
    now = datetime(2026, 11, 5, 14, 30)
    days = prx5.period_times("1D", "2026-11-01", "2026-11-03", now, False)
    assert [d.day for d in days] == [1, 2, 3]
    assert len(prx5.period_times("1H", "2026-11-05", None, now, False)) == 15


def test_default_is_the_last_complete_period():
    now = datetime(2026, 11, 5, 14, 30)
    assert prx5.period_times("1D", None, None, now, False) == [datetime(2026, 11, 4)]
    assert prx5.period_times("1H", None, None, now, False) == [
        datetime(2026, 11, 5, 13)
    ]
    assert prx5.period_times("1H", None, None, now, True) == [datetime(2026, 11, 5, 14)]


@pytest.mark.parametrize("kind", BOTH)
@pytest.mark.parametrize(
    "session,start,expected",
    [
        ("15s_24hr", "2026-11-03", f"{REMOTE} -> out/{LOCAL}"),
        (
            "1Hz_1hr",
            "2026-11-03T14",
            "/DSK1/SSN/LOG2_1Hz_1hr/26307/ENTC307o.26_.gz -> "
            "out/2026/nov/ENTC/1Hz_1hr/raw/ENTC202611031400b.sbf.gz",
        ),
        (
            "status_1hr",
            "2026-01-01T00",
            "/DSK1/SSN/LOG5_status_1hr/26001/ENTC001a.26_.gz -> "
            "out/2026/jan/ENTC/status_1hr/raw/ENTC202601010000c.sbf.gz",
        ),
    ],
)
def test_paths_come_from_gtimes(kind, session, start, expected):
    if kind == "sh" and not shutil.which("timecalc"):
        pytest.skip("timecalc not on PATH")
    out = run(
        kind,
        "download",
        "ENTC",
        "--print-paths",
        "-s",
        session,
        "--start",
        start,
        "-o",
        "out",
        password=None,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[0].replace("out/./", "out/") == expected


# --- health ----------------------------------------------------------------


@pytest.mark.parametrize("kind", BOTH)
def test_health_reports_the_antenna_fault_as_critical(kind, receiver):
    out = run(
        kind,
        "health",
        "ENTC",
        f"127.0.0.1:{receiver.port}",
        "--ftp-port",
        str(receiver.port),
        "--http-port",
        str(receiver.port),
        "--json",
    )
    report = json.loads(out.stdout)
    assert out.returncode == 2, out.stdout + out.stderr
    assert report["status"] == "critical"
    assert report["identity"]["firmware_version"] == "5.7.0"
    assert report["identity"]["serial_number"] == "4104005"
    assert report["power"]["voltage"] == 13.5
    assert report["receiver"]["rx_error"] == 32
    assert report["position"]["satellites_used"] == 21
    assert receiver.logins == [PASSWORD]


@pytest.mark.parametrize("kind", BOTH)
def test_healthy_receiver_exits_zero(kind):
    fake = FakeReceiver(status_block=healthy_status())
    try:
        out = run(
            kind,
            "health",
            "ENTC",
            f"127.0.0.1:{fake.port}",
            "--ftp-port",
            str(fake.port),
            "--http-port",
            str(fake.port),
        )
    finally:
        fake.stop()
    assert out.returncode == 0, out.stdout + out.stderr
    assert "STATUS: OK" in out.stdout


@pytest.mark.parametrize("kind", BOTH)
def test_wrong_password_is_tried_exactly_once(kind, receiver):
    out = run(kind, "health", "ENTC", f"127.0.0.1:{receiver.port}", password="wrong")
    assert out.returncode == 3
    assert len(receiver.logins) == 1
    assert "NOT retried" in out.stdout + out.stderr
    assert "wrong" not in (out.stdout + out.stderr).replace("Wrong", "")


@pytest.mark.parametrize("kind", BOTH)
def test_lockout_countdown_is_reported(kind):
    fake = FakeReceiver(locked=33120)
    try:
        out = run(kind, "health", "ENTC", f"127.0.0.1:{fake.port}")
    finally:
        fake.stop()
    assert out.returncode == 3
    assert "33120" in out.stdout + out.stderr
    assert len(fake.logins) == 1


@pytest.mark.parametrize("kind", BOTH)
def test_no_credentials_sends_no_login(kind, receiver):
    out = run(kind, "health", "ENTC", f"127.0.0.1:{receiver.port}", password=None)
    assert receiver.logins == []
    assert out.returncode == 3
    assert "Not authorized" in out.stdout + out.stderr


def closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize("kind", BOTH)
def test_closed_control_port_is_unknown_not_a_crash(kind):
    out = run(
        kind,
        "health",
        "ENTC",
        f"127.0.0.1:{closed_port()}",
        "--ftp-port",
        str(closed_port()),
    )
    assert out.returncode == 3
    assert "FTP closed" in out.stdout
    assert "Traceback" not in out.stderr


# --- download --------------------------------------------------------------


def download_args(ftp: FakeFtp, out: Path) -> List[str]:
    return [
        "download",
        "ENTC",
        f"127.0.0.1:{ftp.port}",
        "--start",
        "2026-11-03",
        "-o",
        str(out),
        "--retries",
        "0",
    ]


@pytest.mark.parametrize("kind", BOTH)
def test_download_then_skip_when_present(kind, ftp, tmp_path):
    first = run(kind, *download_args(ftp, tmp_path))
    assert first.returncode == 0, first.stdout + first.stderr
    assert (tmp_path / LOCAL).read_bytes() == GZ_BODY
    assert not list(tmp_path.rglob("*.part"))
    second = run(kind, *download_args(ftp, tmp_path))
    assert "present" in second.stdout


@pytest.mark.parametrize("kind", BOTH)
def test_partial_file_resumes(kind, ftp, tmp_path):
    local = tmp_path / LOCAL
    local.parent.mkdir(parents=True)
    Path(f"{local}.part").write_bytes(GZ_BODY[:9])
    out = run(kind, *download_args(ftp, tmp_path))
    assert out.returncode == 0, out.stdout + out.stderr
    assert local.read_bytes() == GZ_BODY
    assert 9 in ftp.rests


@pytest.mark.parametrize("kind", BOTH)
def test_oversized_partial_restarts_from_zero(kind, ftp, tmp_path):
    local = tmp_path / LOCAL
    local.parent.mkdir(parents=True)
    Path(f"{local}.part").write_bytes(GZ_BODY + b"extra")
    out = run(kind, *download_args(ftp, tmp_path))
    assert out.returncode == 0, out.stdout + out.stderr
    assert local.read_bytes() == GZ_BODY


@pytest.mark.parametrize("kind", BOTH)
def test_corrupt_download_is_not_kept(kind, tmp_path):
    fake = FakeFtp({REMOTE: b"this is not gzip data"})
    try:
        out = run(kind, *download_args(fake, tmp_path))
    finally:
        fake.stop()
    assert out.returncode == 1
    assert not (tmp_path / LOCAL).exists()


@pytest.mark.parametrize("kind", BOTH)
def test_ftp_wrong_password_stops_the_whole_run(kind, ftp, tmp_path):
    args = download_args(ftp, tmp_path)
    out = run(kind, *args, "--end", "2026-11-04", "-s", "1Hz_1hr", password="wrong")
    assert out.returncode == 3, out.stdout + out.stderr
    assert len(ftp.logins) == 1
    assert "NOT retried" in out.stderr


@pytest.mark.parametrize("kind", BOTH)
def test_absent_and_unfinalised_files(kind, tmp_path):
    fake = FakeFtp({REMOTE[:-3]: b"growing raw data"})
    try:
        without = run(kind, *download_args(fake, tmp_path))
        assert without.returncode == 1
        assert "unfinalised" in without.stdout
        assert not (tmp_path / LOCAL).exists()
        with_flag = run(kind, *download_args(fake, tmp_path), "--unfinalised")
    finally:
        fake.stop()
    assert with_flag.returncode == 0, with_flag.stdout + with_flag.stderr
    assert (
        subprocess.run(
            ["gunzip", "-c", str(tmp_path / LOCAL)], capture_output=True
        ).stdout
        == b"growing raw data"
    )


@pytest.mark.parametrize("kind", BOTH)
def test_download_refuses_without_credentials(kind, ftp, tmp_path):
    out = run(kind, *download_args(ftp, tmp_path), password=None)
    assert out.returncode == 64
    assert ftp.logins == []


# --- stations file and credentials ------------------------------------------


@pytest.mark.parametrize("kind", BOTH)
def test_save_then_use_the_stored_address(kind, receiver, tmp_path):
    cfg = tmp_path / "stations.cfg"
    cfg.write_text("# keep this comment\n[OTHR]\nrouter_ip = 192.0.2.1\n")
    address = f"127.0.0.1:{receiver.port}"
    first = run(kind, "health", "ENTC", address, "--save", "--cfg", str(cfg), "--json")
    assert json.loads(first.stdout)["host"] == "127.0.0.1", first.stderr
    text = cfg.read_text()
    assert (
        "# keep this comment" in text
        and "[OTHR]" in text
        and f"receiver_controlport = {receiver.port}" in text
    )
    second = run(kind, "health", "ENTC", "--cfg", str(cfg), "--json")
    assert json.loads(second.stdout)["ports"]["control"]["port"] == receiver.port
    # A second save updates in place rather than duplicating the keys.
    run(kind, "health", "ENTC", address, "--save", "--cfg", str(cfg), "--json")
    assert cfg.read_text().count("router_ip = 127.0.0.1") == 1


@pytest.mark.parametrize("kind", BOTH)
def test_save_refuses_imo_config(kind, receiver, tmp_path):
    cfg = tmp_path / "gpsconfig" / "stations.cfg"
    cfg.parent.mkdir()
    cfg.write_text("[OTHR]\nrouter_ip = 192.0.2.1\n")
    out = run(
        kind,
        "health",
        "ENTC",
        f"127.0.0.1:{receiver.port}",
        "--save",
        "--cfg",
        str(cfg),
    )
    assert out.returncode == 64
    assert "gps-config-data" in out.stderr
    assert "ENTC" not in cfg.read_text()


@pytest.mark.parametrize("kind", BOTH)
def test_group_readable_credentials_are_refused(kind, receiver, tmp_path):
    creds = tmp_path / "credentials"
    creds.write_text(f"username={USER}\npassword={PASSWORD}\n")
    creds.chmod(0o640)
    out = run(
        kind,
        "health",
        "ENTC",
        f"127.0.0.1:{receiver.port}",
        "--cred-file",
        str(creds),
        password=None,
    )
    assert out.returncode == 64
    assert receiver.logins == []


@pytest.mark.parametrize("kind", BOTH)
def test_credentials_file_logs_in(kind, receiver, tmp_path):
    creds = tmp_path / "credentials"
    creds.write_text(f"# comment\nusername = {USER}\npassword = {PASSWORD}\n")
    creds.chmod(0o600)
    out = run(
        kind,
        "health",
        "ENTC",
        f"127.0.0.1:{receiver.port}",
        "--cred-file",
        str(creds),
        password=None,
    )
    assert receiver.logins == [PASSWORD]
    assert "Not authorized" not in out.stdout


@pytest.mark.parametrize("kind", BOTH)
def test_a_comma_in_the_password_is_refused_before_any_login(kind, receiver):
    out = run(kind, "health", "ENTC", f"127.0.0.1:{receiver.port}", password="a,b")
    assert out.returncode == 64
    assert receiver.logins == []


@pytest.mark.parametrize("kind", BOTH)
def test_usage_errors_exit_64_not_2(kind):
    assert run(kind, "health", "TOOLONG", "127.0.0.1").returncode == 64
    assert run(kind, "nonsense").returncode == 64
