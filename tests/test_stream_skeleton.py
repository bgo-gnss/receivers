"""Tests for RINEX skeleton fill/refresh (validated vs the real GONH.SKL)."""

from receivers.streaming.skeleton import (
    SkeletonMetadata,
    build_skeleton,
    fill_skeleton,
    geodetic_to_ecef,
    metadata_from_tos,
    refresh_skeleton,
)


def _line(data: str, label: str) -> str:
    return f"{data:<60}{label}"


# Reconstructed GONH.SKL (label at col 61), the real stored skeleton on rek.
GONH_SKL = "\n".join(
    [
        _line("File configured from IMO rt streams", "COMMENT"),
        _line("GONH", "MARKER NAME"),
        _line("GONH", "MARKER NUMBER"),
        _line("HMF/BGO/HG          JH/IMO", "OBSERVER / AGENCY"),
        _line("3605273             SEPT MOSAIC-X5      4.8.0", "REC # / TYPE / VERS"),
        _line("60283B0038          TRM115000.10    NONE", "ANT # / TYPE"),
        _line("  2605201.0352 -1066895.4189  5704422.1172", "APPROX POSITION XYZ"),
        _line("        0.0000        0.0000        0.0000", "ANTENNA: DELTA H/E/N"),
        _line("     1     1", "WAVELENGTH FACT L1/2"),
        _line("", "END OF HEADER"),
    ]
) + "\n"


class TestFillReproducesGonh:
    def test_equipment_lines_match_real_skl(self):
        meta = SkeletonMetadata(
            marker_name="GONH",
            marker_number="GONH",
            rec_serial="3605273",
            rec_type="SEPT MOSAIC-X5",
            rec_version="4.8.0",
            ant_serial="60283B0038",
            ant_type="TRM115000.10",
            ant_radome="NONE",
            antenna_h=0.0,
            antenna_e=0.0,
            antenna_n=0.0,
        )
        out = fill_skeleton(GONH_SKL, meta)
        lines = {ln[60:].strip(): ln[:60] for ln in out.splitlines()}
        assert lines["REC # / TYPE / VERS"] == "3605273             SEPT MOSAIC-X5      4.8.0               "
        assert lines["ANT # / TYPE"] == "60283B0038          TRM115000.10    NONE                    "

    def test_static_lines_preserved(self):
        # No TOS values → everything stays identical to the template.
        out = fill_skeleton(GONH_SKL, SkeletonMetadata())
        assert out == GONH_SKL

    def test_position_never_touched(self):
        # Even a full refill leaves APPROX POSITION XYZ from the stored skeleton.
        meta = SkeletonMetadata(rec_serial="999", rec_type="SEPT POLARX5", rec_version="5.5.0")
        out = fill_skeleton(GONH_SKL, meta)
        pos = next(ln for ln in out.splitlines() if "APPROX POSITION" in ln)
        assert "2605201.0352 -1066895.4189  5704422.1172" in pos


class TestRefresh:
    def test_detects_equipment_change(self):
        new_meta = SkeletonMetadata(
            rec_serial="4001234", rec_type="SEPT POLARX5", rec_version="5.6.0"
        )
        updated, changed = refresh_skeleton(GONH_SKL, new_meta)
        assert changed is True
        rec = next(ln for ln in updated.splitlines() if "REC #" in ln)
        assert "4001234" in rec and "SEPT POLARX5" in rec

    def test_no_change_when_identical(self):
        meta = SkeletonMetadata(
            rec_serial="3605273", rec_type="SEPT MOSAIC-X5", rec_version="4.8.0"
        )
        _, changed = refresh_skeleton(GONH_SKL, meta)
        assert changed is False


class TestBuildSkeleton:
    def test_ecef_matches_gonh_survey(self):
        # cfg LLH -> ECEF within a couple metres of the surveyed GONH.SKL position
        x, y, z = geodetic_to_ecef(63.885537, -22.270311, 347.41)
        assert abs(x - 2605201.0352) < 3
        assert abs(y - -1066895.4189) < 3
        assert abs(z - 5704422.1172) < 3

    def test_build_full_header(self):
        meta = SkeletonMetadata(
            marker_name="GONH",
            marker_number="GONH",
            rec_serial="3605273",
            rec_type="SEPT MOSAIC-X5",
            rec_version="4.8.0",
            ant_serial="60283B0038",
            ant_type="TRM115000.10",
            ant_radome="NONE",
        )
        skl = build_skeleton(meta, latitude=63.885537, longitude=-22.270311, height=347.41)
        labels = [ln[60:].strip() for ln in skl.splitlines()]
        assert labels[0] == "COMMENT" and labels[-1] == "END OF HEADER"
        assert "APPROX POSITION XYZ" in labels and "REC # / TYPE / VERS" in labels
        # the freshly-built header round-trips through fill_skeleton unchanged
        assert fill_skeleton(skl, SkeletonMetadata()) == skl

    def test_build_is_refreshable(self):
        meta = SkeletonMetadata(marker_name="GONH", rec_serial="111")
        skl = build_skeleton(meta, latitude=63.9, longitude=-22.3, height=300.0)
        _, changed = refresh_skeleton(
            skl, SkeletonMetadata(rec_serial="222", rec_type="SEPT POLARX5")
        )
        assert changed is True


class TestMetadataFromTos:
    def test_maps_current_session_fields(self):
        # station dict shaped like TOSClient.get_complete_station_metadata
        station = {
            "device_history": [
                {
                    "time_to": None,  # open = current
                    "gnss_receiver": {
                        "model": "PolaRx5",
                        "serial_number": "4101636",
                        "firmware_version": "5.6.0",
                    },
                    "antenna": {"model": "TRM115000.10", "serial_number": "0001"},
                    "radome": {"model": "NONE"},
                }
            ]
        }
        meta = metadata_from_tos(station, station_id="HRSC")
        assert meta.marker_name == "HRSC"
        assert meta.rec_serial == "4101636" and meta.rec_version == "5.6.0"
        assert meta.rec_type == "SEPT POLARX5"  # IGS-standardised
        assert meta.ant_serial == "0001"
        # TRM115000.10 is not in the IGS table -> falls back to the raw TOS value
        assert meta.ant_type == "TRM115000.10"
        assert meta.ant_radome == "NONE"
