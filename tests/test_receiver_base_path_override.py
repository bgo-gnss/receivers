"""Per-station storage-root (base_path) override for Trimble receivers.

A station that logs to a non-default disk (e.g. AKUR on ``/External/`` instead
of the NetR9 default ``/Internal/``) sets ``receiver_base_path`` in stations.cfg.
The path builder honours it via ``_resolve_base_path()``; the global
receiver-type default applies otherwise.

The NetR5 CACHEDIR download prefix is a SEPARATE concern, keyed ``cachedir_prefix``
and resolved independently in the HTTP client — these tests pin that the two
never collide (a storage-root ``base_path`` must not be read as a CACHEDIR prefix).
"""

from __future__ import annotations

from receivers.trimble.http_download_client import NetR9HTTPDownloader
from receivers.trimble.netr9 import NetR9
from receivers.trimble.netrs import NetRS


def _resolve(cls, station_base_path, global_default_key, global_value):
    """Build a bare instance and exercise _resolve_base_path in isolation."""
    obj = object.__new__(cls)
    obj.station_info = (
        {"receiver": {"base_path": station_base_path}}
        if station_base_path
        else {"receiver": {}}
    )
    setattr(
        obj, global_default_key, {"base_path": global_value} if global_value else {}
    )
    return obj._resolve_base_path()


class TestNetR9ResolveBasePath:
    def test_per_station_override_wins(self):
        # AKUR-style: station logs to /External/, overriding the global default.
        assert (
            _resolve(NetR9, "/External/", "netr9_config", "/Internal/") == "/External/"
        )

    def test_falls_back_to_global_default(self):
        assert _resolve(NetR9, None, "netr9_config", "/Internal/") == "/Internal/"

    def test_hardcoded_default_when_global_unset(self):
        # No station override, no global value → built-in /Internal/.
        assert _resolve(NetR9, None, "netr9_config", None) == "/Internal/"


class TestNetRSResolveBasePath:
    def test_per_station_override_wins(self):
        assert (
            _resolve(NetRS, "/External/", "netrs_config", "/download/") == "/External/"
        )

    def test_hardcoded_default_when_global_unset(self):
        assert _resolve(NetRS, None, "netrs_config", None) == "/download/"


class TestCachedirPrefixIsSeparateKey:
    """The HTTP client reads cachedir_prefix, never base_path."""

    def test_explicit_cachedir_prefix_honoured(self):
        assert (
            NetR9HTTPDownloader._explicit_cachedir_prefix(
                {"cachedir_prefix": "/CACHEDIR9/download"}
            )
            == "/CACHEDIR9/download"
        )

    def test_base_path_is_NOT_read_as_cachedir_prefix(self):
        # The collision guard: a storage-root base_path must not become the
        # CACHEDIR prefix (which would build /External//Internal/... and 404).
        assert (
            NetR9HTTPDownloader._explicit_cachedir_prefix({"base_path": "/External/"})
            is None
        )

    def test_empty_means_autodiscover(self):
        assert NetR9HTTPDownloader._explicit_cachedir_prefix({}) is None
        assert (
            NetR9HTTPDownloader._explicit_cachedir_prefix({"cachedir_prefix": ""})
            is None
        )
