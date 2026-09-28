"""Configuring an NTRIP SBF feed: bind a stream, create a connection.

Fixtures are shaped from three PolaRx5 configs read live on 2026-09-28:

  SENG  Stream6 -> NTR2 -> mount SENG1, MeasEpoch+… @ sec1   (the model)
  GEVK  NTR2/GEVK1 exists in Server mode, NOTHING feeds it   (bind only)
  VOGC  only NTR1/VOGC0 — no SBF connection at all           (create + bind)

On all three, Stream1-7 were occupied and 8-10 free. That asymmetry is the whole
reason these builders refuse to pick a slot for you: Stream6 was free on SENG but
already driving IP10 on the other two, so a hard-coded index would have silently
killed a live output on two of three receivers.
"""

import pytest

from receivers.septentrio.ntrip import (
    build_ntrip_server_commands,
    build_sbf_bind_commands,
    free_sbf_streams,
    parse_ntrip_mounts,
    parse_sbf_destinations,
    redact_secrets,
)

RAW_BLOCKS = "MeasEpoch+GPSNav+GPSAlm+GPSIon+GPSUtc+GLONav+GALNav"

GEVK = """
setNtripSettings, NTR1, Server
setNtripSettings, NTR2, Server
setNtripSettings, NTR1, , "ntrcaster.vedur.is"
setNtripSettings, NTR2, , "ntrcaster.vedur.is"
setNtripSettings, NTR1, , , , "gpsops"
setNtripSettings, NTR2, , , , "gpsops"
setNtripSettings, NTR1, , , , , "SECRET1"
setNtripSettings, NTR2, , , , , "SECRET2"
setNtripSettings, NTR1, , , , , , "GEVK0"
setNtripSettings, NTR2, , , , , , "GEVK1"
setSBFOutput, Stream1, LOG1
setSBFOutput, Stream2, IPS1
setSBFOutput, Stream3, LOG2
setSBFOutput, Stream4, LOG3
setSBFOutput, Stream5, LOG4
setSBFOutput, Stream6, IP10
setSBFOutput, Stream7, LOG5
setSBFOutput, Stream1, , GPSNav+PVTGeodetic+Meas3Ranges
setSBFOutput, Stream1, , , sec15
setSBFOutput, Stream6, , PVTGeodetic
setSBFOutput, Stream6, , , sec15
"""

VOGC = "\n".join(
    ln for ln in GEVK.splitlines() if "NTR2" not in ln and "GEVK1" not in ln
).replace("GEVK0", "VOGC0")


class TestReadingTheReceiver:
    def test_destinations_are_only_the_wiring_lines(self):
        d = parse_sbf_destinations(GEVK)
        assert d["Stream1"] == "LOG1" and d["Stream6"] == "IP10"
        # block-list / interval lines must not register as destinations
        assert len(d) == 7

    def test_free_slots_match_what_the_fleet_showed(self):
        assert free_sbf_streams(GEVK) == ["Stream8", "Stream9", "Stream10"]


class TestBindRefusesToDestroyData:
    """Re-pointing an occupied stream stops that output with NO error."""

    @pytest.mark.parametrize(
        "occupied,dest", [("Stream1", "LOG1"), ("Stream6", "IP10")]
    )
    def test_occupied_stream_is_refused(self, occupied, dest):
        with pytest.raises(ValueError) as e:
            build_sbf_bind_commands(GEVK, occupied, "NTR2", RAW_BLOCKS, "sec1")
        assert dest in str(e.value)
        assert "Stream8" in str(e.value), "must name the free slots"

    def test_connection_without_a_mountpoint_is_refused(self):
        with pytest.raises(ValueError, match="no configured mountpoint"):
            build_sbf_bind_commands(VOGC, "Stream8", "NTR2", RAW_BLOCKS, "sec1")

    @pytest.mark.parametrize("blocks,interval", [("", "sec1"), (RAW_BLOCKS, "")])
    def test_empty_content_or_rate_is_refused(self, blocks, interval):
        with pytest.raises(ValueError):
            build_sbf_bind_commands(GEVK, "Stream8", "NTR2", blocks, interval)

    @pytest.mark.parametrize("bad", ["Stream", "stream8", "LOG1", "NTR2"])
    def test_malformed_stream_id_is_refused(self, bad):
        with pytest.raises(ValueError, match="invalid SBF stream"):
            build_sbf_bind_commands(GEVK, bad, "NTR2", RAW_BLOCKS, "sec1")


class TestTheGevkCase:
    """NTR2/GEVK1 already exists in Server mode — it just needs feeding."""

    def test_binds_a_free_stream_and_persists(self):
        cmds = build_sbf_bind_commands(GEVK, "Stream8", "NTR2", RAW_BLOCKS, "sec1")
        assert cmds == [
            "setSBFOutput, Stream8, NTR2",
            f"setSBFOutput, Stream8, , {RAW_BLOCKS}",
            "setSBFOutput, Stream8, , , sec1",
            "eccf, Current, Boot",
        ]

    def test_the_result_reads_back_as_wired(self):
        cmds = build_sbf_bind_commands(GEVK, "Stream8", "NTR2", RAW_BLOCKS, "sec1")
        assert parse_sbf_destinations("\n".join(cmds))["Stream8"] == "NTR2"

    def test_it_touches_nothing_else(self):
        cmds = build_sbf_bind_commands(GEVK, "Stream8", "NTR2", RAW_BLOCKS, "sec1")
        body = "\n".join(cmds)
        assert "setNtripSettings" not in body, "identity-safe: no connection changes"
        for other in ("Stream1", "Stream6", "LOG1", "IP10"):
            assert other not in body


class TestTheVogcCase:
    """No NTR2 at all — the connection has to be created first."""

    def test_creates_the_connection_and_reads_back(self):
        cmds = build_ntrip_server_commands(
            VOGC,
            "NTR2",
            caster="ntrcaster.vedur.is",
            mountpoint="VOGC1",
            user="gpsops",
            password="hunter2",
        )
        assert cmds[0] == "setNtripSettings, NTR2, Server"
        assert cmds[-1] == "eccf, Current, Boot"
        # the round trip that matters: the receiver will report it this way
        assert parse_ntrip_mounts("\n".join(cmds)) == {"VOGC1": "NTR2"}

    def test_then_the_bind_becomes_legal(self):
        created = build_ntrip_server_commands(
            VOGC, "NTR2", caster="c", mountpoint="VOGC1", user="u", password="p"
        )
        after = VOGC + "\n" + "\n".join(created)
        cmds = build_sbf_bind_commands(after, "Stream8", "NTR2", RAW_BLOCKS, "sec1")
        assert cmds[0] == "setSBFOutput, Stream8, NTR2"

    def test_overwriting_a_live_connection_is_refused(self):
        with pytest.raises(ValueError, match="already serves mountpoint"):
            build_ntrip_server_commands(
                GEVK, "NTR2", caster="c", mountpoint="X", user="u", password="p"
            )

    @pytest.mark.parametrize("field", ["caster", "mountpoint", "user", "password"])
    def test_empty_required_field_is_refused(self, field):
        kw = dict(caster="c", mountpoint="m", user="u", password="p")
        kw[field] = "  "
        with pytest.raises(ValueError, match=field):
            build_ntrip_server_commands(VOGC, "NTR2", **kw)

    def test_port_is_optional_and_range_checked(self):
        with_port = build_ntrip_server_commands(
            VOGC, "NTR2", caster="c", mountpoint="m", user="u", password="p", port=2101
        )
        assert "setNtripSettings, NTR2, , , 2101" in with_port
        with pytest.raises(ValueError, match="out of range"):
            build_ntrip_server_commands(
                VOGC,
                "NTR2",
                caster="c",
                mountpoint="m",
                user="u",
                password="p",
                port=99999,
            )


class TestTheSecretNeverReachesAnOperator:
    def test_password_is_masked_but_the_rest_survives(self):
        cmds = build_ntrip_server_commands(
            VOGC,
            "NTR2",
            caster="ntrcaster.vedur.is",
            mountpoint="VOGC1",
            user="gpsops",
            password="TOPSECRET",
        )
        assert any("TOPSECRET" in c for c in cmds), "the real command must carry it"
        safe = redact_secrets(cmds)
        assert not any("TOPSECRET" in c for c in safe)
        assert any('"********"' in c for c in safe)
        # everything else still legible for a dry-run diff
        assert any("VOGC1" in c for c in safe) and any("gpsops" in c for c in safe)

    def test_redaction_leaves_unrelated_commands_alone(self):
        assert redact_secrets(["eccf, Current, Boot"]) == ["eccf, Current, Boot"]
