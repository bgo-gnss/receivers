"""Re-rinex per-file converter dispatch.

The raw file's ACTUAL format (magic bytes + Trimble container extension) picks
the converter — not the station's current configured receiver — so a station
swapped to a new receiver still reconverts its historical raw (NYLA is a PolaRX5
today; its pre-2019 raw is NetRS .T00).
"""

import datetime
import re
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import receivers.cli.main  # noqa: F401 — ensure the submodule is in sys.modules
from receivers.cli.main import _receiver_type_for_raw

# receivers.cli.__init__ binds `main` to the main() function, shadowing the
# submodule attribute — so fetch the real module object from sys.modules.
m = sys.modules["receivers.cli.main"]


class TestReceiverTypeForRaw:
    def test_sbf_magic(self, tmp_path):
        p = tmp_path / "NYLA202305220000a.sbf"
        p.write_bytes(b"$@\x00\x00sbfbody")
        assert _receiver_type_for_raw(p) == "polarx"

    def test_trimble_t00_is_netrs(self, tmp_path):
        p = tmp_path / "NYLA200806090000a.T00"
        p.write_bytes(b"\x00\x01\x02trimble-container")
        assert _receiver_type_for_raw(p) == "netrs"

    def test_trimble_t02_is_netr9(self, tmp_path):
        p = tmp_path / "MANA201501010000a.T02"
        p.write_bytes(b"\x00\x01\x02trimble-container")
        assert _receiver_type_for_raw(p) == "netr9"

    def test_ashtech_u_magic(self, tmp_path):
        p = tmp_path / "RHOF201004020000a.atc"
        p.write_bytes(b"\x00\x00\x00\x00BHDRrest")  # BHDR at offset 4
        assert _receiver_type_for_raw(p) == "ashtech"

    def test_unknown_is_none(self, tmp_path):
        p = tmp_path / "README.txt"
        p.write_bytes(b"hello world, not a raw file")
        assert _receiver_type_for_raw(p) is None


def _archive(tmp_path):
    root = tmp_path / "archive"
    d = root / "2013" / "dec" / "NYLA" / "15s_24hr" / "raw"
    d.mkdir(parents=True)
    (d / "NYLA201312130000a.T00").write_bytes(b"trimble-netrs-data")  # -> netrs
    (d / "NYLA201312140000a.sbf").write_bytes(b"$@septentrio-data")  # -> polarx
    return root


def _args(root):
    return Namespace(
        session="15s_24hr",
        source_dir=str(root),  # re-rinex mode + source_root
        from_archive=False,
        ashtech=False,
        trimble=False,
        native_trimble=False,
        no_header_correction=True,  # bypass the 0-corrections re-rinex gate
        force=True,  # skip staged-skip
        backup_old=False,
        dry_run=False,
        work_dir=None,
        output_dir=None,
        loglevel=30,
        keep_intermediate=False,
        rinex_version=3,
    )


class TestPerFileDispatch:
    def test_mixed_raw_routed_by_format(self, tmp_path):
        root = _archive(tmp_path)
        made: dict = {}

        def fake_create(station_id, args, *a, receiver_type_override=None, **k):
            mc = made.get(receiver_type_override)
            if mc is None:
                mc = MagicMock(name=f"conv:{receiver_type_override}")
                mc.converted = []

                def _cv(raw_file, output_dir=None, force=False, _mc=mc):
                    _mc.converted.append(Path(raw_file).name)
                    rnx = Path(output_dir) / f"{Path(raw_file).stem}.rnx"
                    rnx.parent.mkdir(parents=True, exist_ok=True)
                    rnx.write_text("x")
                    return SimpleNamespace(
                        success=True,
                        header_corrections_applied=5,
                        rinex_file=rnx,
                        duration_seconds=0.1,
                    )

                def _dt(p):
                    g = re.match(r"NYLA(\d{4})(\d{2})(\d{2})", Path(p).name)
                    return (
                        datetime.datetime(int(g[1]), int(g[2]), int(g[3]))
                        if g
                        else None
                    )

                mc.convert_file.side_effect = _cv
                mc._extract_date_from_filename.side_effect = _dt
                made[receiver_type_override] = mc
            return mc, f".{receiver_type_override}", None

        bp = {
            "rinex_version": 3,
            "apply_hatanaka": False,
            "compression_format": None,
            "naming_convention": None,
            "observation_types": None,
            "rinex_config": None,
        }
        with (
            patch.object(m, "_create_rinex_converter", side_effect=fake_create),
            patch(
                "receivers.config.receivers_config.get_receivers_config",
                return_value=MagicMock(get_data_prepath=lambda: str(tmp_path)),
            ),
        ):
            converted, failed, skipped = m._rinex_convert_station_period(
                "NYLA",
                None,  # no single converter — per-file dispatch builds them
                ".sbf.gz",
                datetime.datetime(2013, 12, 13),
                datetime.datetime(2013, 12, 15),  # exclusive → 13th + 14th
                _args(root),
                MagicMock(),
                build_params=bp,
            )

        assert (converted, failed, skipped) == (2, 0, 0)
        # Each file routed to the converter for its OWN format.
        assert made["netrs"].converted == ["NYLA201312130000a.T00"]
        assert made["polarx"].converted == ["NYLA201312140000a.sbf"]

    def test_unrecognised_raw_skipped_not_misconverted(self, tmp_path):
        root = tmp_path / "archive"
        d = root / "2013" / "dec" / "NYLA" / "15s_24hr" / "raw"
        d.mkdir(parents=True)
        (d / "NYLA201312130000a.junk").write_bytes(b"not a known raw format")

        calls: list = []

        def fake_create(station_id, args, *a, receiver_type_override=None, **k):
            calls.append(receiver_type_override)
            return MagicMock(), f".{receiver_type_override}", None

        bp = {
            "rinex_version": 3,
            "apply_hatanaka": False,
            "compression_format": None,
            "naming_convention": None,
            "observation_types": None,
            "rinex_config": None,
        }
        with (
            patch.object(m, "_create_rinex_converter", side_effect=fake_create),
            patch(
                "receivers.config.receivers_config.get_receivers_config",
                return_value=MagicMock(get_data_prepath=lambda: str(tmp_path)),
            ),
        ):
            converted, failed, skipped = m._rinex_convert_station_period(
                "NYLA",
                None,
                ".sbf.gz",
                datetime.datetime(2013, 12, 13),
                datetime.datetime(2013, 12, 14),
                _args(root),
                MagicMock(),
                build_params=bp,
            )

        assert converted == 0 and skipped == 1  # skipped, never converted
        assert calls == []  # no converter built for an unrecognised format
