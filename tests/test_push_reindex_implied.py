"""A catalog target implies the post-push reindex (so it can't be forgotten).

`receivers rinex … --push --catalog-prod` should push AND reindex the production
catalog in one command — passing --catalog-prod (or --catalog-host) means "reindex
there", without also needing --reindex. Bare --push still only prints the hint.
"""

import importlib
from argparse import Namespace
from unittest.mock import patch

m = importlib.import_module("receivers.cli.main")


def _args(**kw):
    base = dict(reindex=False, catalog_prod=False, catalog_host=None)
    base.update(kw)
    return Namespace(**base)


def _call(args):
    """Run _push_reindex with the real reindex/resolve stubbed; return whether the
    reindex actually fired."""
    with (
        patch("receivers.archive.reindex_files_multi", return_value=[]) as mock_reindex,
        patch("receivers.archive.resolve_catalog_hosts", return_value=["pgdev"]),
    ):
        m._push_reindex(
            args,
            ["/w/f.d.Z"],
            root="/w",
            storage_location="imo_archive",
            dest_prefix="~/gpsdata",
        )
    return mock_reindex.called


def test_catalog_prod_implies_reindex():
    assert _call(_args(catalog_prod=True)) is True


def test_catalog_host_implies_reindex():
    assert _call(_args(catalog_host="localhost")) is True


def test_explicit_reindex_still_works():
    assert _call(_args(reindex=True)) is True


def test_bare_push_does_not_reindex():
    assert _call(_args()) is False
