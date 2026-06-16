"""Tests for stream-capture config + BNC config generation."""

import stat

from receivers.streaming.bnc_config import (
    bnc_config_filename,
    build_bnc_config,
    write_bnc_config,
)
from receivers.streaming.config import (
    DEFAULT_CASTER_HOST,
    DEFAULT_CASTER_PORT,
    AcquisitionMode,
    StreamConfig,
    get_acquisition_mode,
)


class TestAcquisitionMode:
    def test_default_is_download(self):
        assert get_acquisition_mode({}) == AcquisitionMode.DOWNLOAD
        assert get_acquisition_mode({"station": {}}) == AcquisitionMode.DOWNLOAD

    def test_stream_selected(self):
        cfg = {"station": {"acquisition_mode": "stream"}}
        assert get_acquisition_mode(cfg) == AcquisitionMode.STREAM

    def test_case_insensitive(self):
        assert get_acquisition_mode({"acquisition_mode": "STREAM"}) == "stream"

    def test_unknown_falls_back_to_download(self):
        # Fail safe — never silently stream on a typo.
        assert get_acquisition_mode({"acquisition_mode": "bogus"}) == "download"


class TestStreamConfigFromStation:
    def test_mountpoint_defaults_to_sid0(self):
        sc = StreamConfig.from_station_config("GONH", {}, rnx_path="/x/GONH")
        assert sc.mountpoint == "GONH0"
        assert sc.caster_host == DEFAULT_CASTER_HOST
        assert sc.caster_port == DEFAULT_CASTER_PORT

    def test_reads_latlon_and_overrides(self):
        cfg = {
            "station": {
                "latitude": "63.885537",
                "longitude": "-22.270311",
                "stream_mountpoint": "GONHX",
            }
        }
        sc = StreamConfig.from_station_config("GONH", cfg, rnx_path="/x/GONH")
        assert sc.mountpoint == "GONHX"
        assert round(sc.latitude, 4) == 63.8855
        assert round(sc.longitude, 4) == -22.2703

    def test_mountpoint_suffix(self):
        sc = StreamConfig.from_station_config(
            "GONH", {}, rnx_path="/x/GONH", mountpoint_suffix="1"
        )
        assert sc.mountpoint == "GONH1"


class TestBuildBncConfig:
    def _cfg(self, **kw):
        base = dict(
            station_id="GONH",
            mountpoint="GONH0",
            caster_user="user",
            caster_password="secret",
            rnx_path="/home/gpsops/tmp/RT-rinex/GONH",
            latitude=63.92,
            longitude=-22.37,
        )
        base.update(kw)
        return StreamConfig(**base)

    def test_contains_core_keys(self):
        body = build_bnc_config(self._cfg())
        assert "[General]" in body and "[PPP]" in body
        assert "rnxIntr=1 hour" in body
        assert "rnxSampl=1" in body
        assert "rnxV3=2" in body  # default rnx_version=3 -> Qt-checked (2)
        assert "rnxPath=/home/gpsops/tmp/RT-rinex/GONH" in body

    def test_mountpoint_and_caster_with_credentials(self):
        body = build_bnc_config(self._cfg())
        assert (
            "mountPoints=//user:secret@ntrcaster.vedur.is:2101/GONH0 "
            "RTCM_3 ISL 63.92 -22.37 no 1" in body
        )
        assert "casterUrlList=http://user:secret@ntrcaster.vedur.is:2101" in body

    def test_no_credentials_renders_host_only(self):
        body = build_bnc_config(self._cfg(caster_user=None, caster_password=None))
        assert "casterUrlList=http://ntrcaster.vedur.is:2101" in body
        assert "@" not in body.split("casterUrlList=")[1].splitlines()[0]

    def test_rnx_version_3_is_qt_checked(self):
        # BNC booleans use Qt serialization: 2 = checked. rnxV3=1 silently left
        # BNC on RINEX 2 (the bug this fixed).
        assert "rnxV3=2" in build_bnc_config(self._cfg(rnx_version=3))

    def test_rnx_version_2_is_qt_unchecked(self):
        assert "rnxV3=0" in build_bnc_config(self._cfg(rnx_version=2))


class TestWriteBncConfig:
    def test_writes_file_0600_and_creates_rnx_path(self, tmp_path):
        rnx = tmp_path / "RT-rinex" / "GONH"
        sc = StreamConfig(
            station_id="GONH",
            mountpoint="GONH0",
            caster_user="u",
            caster_password="p",
            rnx_path=str(rnx),
        )
        out = write_bnc_config(sc, tmp_path / bnc_config_filename("GONH"))
        assert out.name == "rtcm2rinex-GONH.bnc"
        assert rnx.is_dir()  # output dir created
        mode = stat.S_IMODE(out.stat().st_mode)
        assert mode == 0o600  # credentials -> private perms
