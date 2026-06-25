"""Tests for receivers.septentrio.ntrip — identity-safe NTRIP-stream control."""

import pytest

from receivers.septentrio.ntrip import (
    build_ntrip_stream_commands,
    normalize_ntrip_state,
    parse_ntrip_mounts,
    sbf_streams_for_conn,
)

# Trimmed from a live HRIC extract: NTR1→HRIC0 (RTCM), NTR2→HRIC1 (SBF Stream6).
HRIC_CONFIG = """\
setDataInOut, NTR1, , RTCMv3
setSBFOutput, Stream1, LOG1
setSBFOutput, Stream6, NTR2
setSBFOutput, Stream1, , GPSNav+GPSIon
setSBFOutput, Stream6, , MeasEpoch+GPSNav
setSBFOutput, Stream1, , , sec15
setSBFOutput, Stream6, , , sec1
setRTCMv3Output, NTR1, RTCM1004+RTCM1006
setRTCMv3Output, NTR2, RTCM1004+RTCM1006
setNtripSettings, NTR1, Server
setNtripSettings, NTR2, Server
setNtripSettings, NTR1, , "ntrcaster.vedur.is"
setNtripSettings, NTR2, , "ntrcaster.vedur.is"
setNtripSettings, NTR1, , , , "gpsops"
setNtripSettings, NTR2, , , , "gpsops"
setNtripSettings, NTR1, , , , , , "HRIC0"
setNtripSettings, NTR2, , , , , , "HRIC1"
"""


@pytest.mark.parametrize(
    "state, expected",
    [
        ("off", "off"),
        ("OFF", "off"),
        ("disable", "off"),
        ("on", "Server"),
        ("server", "Server"),
        ("enable", "Server"),
        ("client", "Client"),
    ],
)
def test_normalize_state(state, expected):
    assert normalize_ntrip_state(state) == expected


def test_normalize_state_rejects_unknown():
    with pytest.raises(ValueError, match="unknown NTRIP state"):
        normalize_ntrip_state("bogus")


def test_parse_ntrip_mounts_maps_mountpoint_to_conn():
    assert parse_ntrip_mounts(HRIC_CONFIG) == {"HRIC0": "NTR1", "HRIC1": "NTR2"}


def test_parse_ntrip_mounts_empty_when_none():
    assert parse_ntrip_mounts("setDataInOut, NTR1, , RTCMv3\n") == {}


def test_sbf_streams_for_conn_finds_only_destination_assignment():
    # Stream6's destination is NTR2; its content/interval lines must not match.
    assert sbf_streams_for_conn(HRIC_CONFIG, "NTR2") == ["Stream6"]


def test_sbf_streams_for_conn_none_for_log_only_conn():
    assert sbf_streams_for_conn(HRIC_CONFIG, "NTR1") == []  # NTR1 fed by RTCM, not SBF


def test_build_commands_basic_off():
    assert build_ntrip_stream_commands("NTR2", "off") == [
        "setNtripSettings, NTR2, off",
        "eccf, Current, Boot",
    ]


def test_build_commands_with_sbf_drop():
    assert build_ntrip_stream_commands("NTR2", "off", drop_sbf_streams=["Stream6"]) == [
        "setNtripSettings, NTR2, off",
        "setSBFOutput, Stream6, none",
        "eccf, Current, Boot",
    ]


def test_build_commands_on_maps_to_server():
    assert (
        build_ntrip_stream_commands("NTR2", "on")[0] == "setNtripSettings, NTR2, Server"
    )


def test_build_commands_rejects_bad_conn():
    with pytest.raises(ValueError, match="invalid NTRIP connection"):
        build_ntrip_stream_commands("HRIC1", "off")


def test_disable_mount_end_to_end_resolution():
    """The --disable-mount path: mount → conn → commands (+ optional sbf drop)."""
    conn = parse_ntrip_mounts(HRIC_CONFIG)["HRIC1"]
    streams = sbf_streams_for_conn(HRIC_CONFIG, conn)
    assert build_ntrip_stream_commands(conn, "off", drop_sbf_streams=streams) == [
        "setNtripSettings, NTR2, off",
        "setSBFOutput, Stream6, none",
        "eccf, Current, Boot",
    ]
