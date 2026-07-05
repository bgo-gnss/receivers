"""Rollout allowlist: sync.yaml `stations:` narrows the automated sweep to the
stations being onboarded (∩ in_epos, − exclude). Empty/absent = all."""

from receivers.dissemination.config import DisseminationTarget, _build_target


def _target(stations=(), exclude=()):
    return DisseminationTarget(
        name="epos",
        active=False,
        host="epos-portal.vedur.is",
        user="epos",
        dest="/mnt/epos_01/gps",
        source_root="/mnt_data/rawgpsdata",
        sessions=("15s_24hr",),
        exclude_stations=frozenset(exclude),
        stations=frozenset(stations),
    )


def test_allowlist_narrows_to_listed():
    t = _target(stations={"RHOF"})
    assert t.select_markers(["RHOF", "FIHO", "AKUR"]) == ["RHOF"]


def test_empty_allowlist_selects_all():
    t = _target()
    assert t.select_markers(["RHOF", "FIHO"]) == ["RHOF", "FIHO"]


def test_allowlist_case_insensitive_preserves_output():
    t = _target(stations={"RHOF"})
    # marker set may arrive lower-cased; match is case-insensitive, output verbatim
    assert t.select_markers(["rhof", "fiho"]) == ["rhof"]


def test_exclude_applies_alongside_allowlist():
    t = _target(stations={"RHOF", "FIHO"}, exclude={"FIHO"})
    assert t.select_markers(["RHOF", "FIHO"]) == ["RHOF"]


def test_exclude_applies_without_allowlist():
    t = _target(exclude={"FIHO"})
    assert t.select_markers(["RHOF", "FIHO", "AKUR"]) == ["RHOF", "AKUR"]


def test_typo_yields_empty_not_all():
    # A name not in the in_epos set → empty selection (loud no-op), never "all".
    t = _target(stations={"RHOOF"})
    assert t.select_markers(["RHOF", "FIHO"]) == []


def test_build_target_uppercases_and_defaults():
    t = _build_target(
        {
            "name": "epos",
            "dest": "/d",
            "source_root": "/s",
            "stations": ["rhof", "fiho"],
            "exclude_stations": ["dyna"],
        }
    )
    assert t.stations == frozenset({"RHOF", "FIHO"})
    assert t.exclude_stations == frozenset({"DYNA"})


def test_build_target_absent_stations_is_all():
    t = _build_target({"name": "epos", "dest": "/d", "source_root": "/s"})
    assert t.stations == frozenset()
    assert t.select_markers(["RHOF", "FIHO"]) == ["RHOF", "FIHO"]
