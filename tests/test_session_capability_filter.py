"""Capability filtering for gap detection and the backfill enqueue.

Regression cover for the 2026-09-13 finding: only PolaRX5 has a ``status_1hr``
session, but gap detection and ``_enqueue_backfill`` scanned all 180 active
stations, so ~76 receivers with no status session contributed permanent phantom
gaps — re-queued every 6 h forever, and on NetRS the hour-00 request mapped to
the DAILY 15s raw and archived a duplicate under ``status_1hr/raw``.

The two traps these tests exist to pin:

* the filter MUST fail open (``mosaic-x5`` declares no session_map and would
  otherwise drop out of gap detection entirely — a station nothing scans is a
  station whose gaps are never repaired), and
* it MUST read ``receivers_config.get_supported_sessions`` and not
  ``receiver_registry.REGISTRY[...].sessions``. Those disagree in production
  (netr5: cfg says 15s only, registry also claims 1Hz), so filtering on the
  registry would manufacture the very phantom gaps this removes.
"""

import pytest

from receivers.config_utils import filter_stations_for_session, supports_session


class _Cfg:
    """Stand-in for receivers_config with an explicit session_map."""

    def __init__(self, mapping):
        self._mapping = mapping

    def get_supported_sessions(self, receiver_type):
        return list(self._mapping.get(receiver_type, []))


@pytest.fixture
def cfg(monkeypatch):
    """Patch the config the helper reads, mirroring production values."""
    fake = _Cfg(
        {
            "polarx5": ["15s_24hr", "1Hz_1hr", "status_1hr"],
            "netrs": ["15s_24hr", "1Hz_1hr"],
            "netr9": ["15s_24hr", "1Hz_1hr"],
            "netr5": ["15s_24hr"],  # NB: registry also claims 1Hz_1hr
            # 'mosaic-x5' deliberately absent -> [] -> fail open
        }
    )
    import receivers.config.receivers_config as rc

    monkeypatch.setattr(rc, "get_receivers_config", lambda: fake)
    return fake


@pytest.mark.parametrize(
    "receiver_type,session,expected",
    [
        ("polarx5", "status_1hr", True),
        ("netrs", "status_1hr", False),
        ("netr9", "status_1hr", False),
        ("g10", "status_1hr", True),  # unknown type -> fail open
        ("netr5", "1Hz_1hr", False),  # cfg wins over the registry
        ("netr5", "15s_24hr", True),
        ("mosaic-x5", "status_1hr", True),  # no session_map -> fail open
        (None, "status_1hr", True),
        ("", "status_1hr", True),
        ("POLARX5", "status_1hr", True),  # receiver-type casing
        ("polarx5", "STATUS_1HR", True),  # session casing
        ("netrs", "STATUS_1HR", False),
    ],
)
def test_supports_session(cfg, receiver_type, session, expected):
    assert supports_session(receiver_type, session) is expected


def test_registry_is_not_the_authority(cfg):
    """netr5 must be refused for 1Hz even though the REGISTRY allows it.

    This is the test that fails if someone 'simplifies' the helper to read
    receiver_registry — the mistake the original bug report recommended.
    """
    from receivers.config.receiver_registry import REGISTRY

    assert "1Hz_1hr" in REGISTRY["netr5"].sessions, "registry precondition"
    assert supports_session("netr5", "1Hz_1hr") is False


def test_filter_drops_only_incapable_and_preserves_order(cfg):
    configs = {
        "AAAA": {"receiver_type": "polarx5"},
        "BBBB": {"receiver_type": "netrs"},
        "CCCC": {"receiver_type": "polarx5"},
        "DDDD": {"receiver_type": "netr9"},
        "EEEE": {"receiver_type": "mosaic-x5"},
    }
    ids = ["AAAA", "BBBB", "CCCC", "DDDD", "EEEE"]
    assert filter_stations_for_session(configs, ids, "status_1hr") == [
        "AAAA",
        "CCCC",
        "EEEE",
    ]
    assert filter_stations_for_session(configs, ids, "15s_24hr") == ids


def test_filter_tolerates_unknown_and_configless_stations(cfg):
    """A station id with no config entry must not be dropped silently."""
    configs = {"AAAA": {"receiver_type": "netrs"}, "BBBB": {}}
    assert filter_stations_for_session(
        configs, ["AAAA", "BBBB", "ZZZZ"], "status_1hr"
    ) == [
        "BBBB",
        "ZZZZ",
    ]


def test_filter_is_empty_safe(cfg):
    assert filter_stations_for_session({}, [], "status_1hr") == []
