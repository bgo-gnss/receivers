"""`epos-disseminate --publish-m3g --dry-run` is validate-only.

--dry-run turns the site-log/M3G path into a pure preview: the log is still
rendered (needed to validate), but nothing persistent or outward happens — no
git commit, no M3G PUT. Without it, a changed log is committed and published.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from receivers.cli.epos_disseminate import cmd_epos_disseminate


def _target():
    t = SimpleNamespace()
    t.name = "epos"
    t.format = SimpleNamespace(country_code="ISL", monument_number="00")
    return t


def _args(**over):
    base = dict(
        config=None,
        target=None,
        list_stations=False,
        refresh_metadata=False,
        reactive=False,
        station="NYLA",
        sitelog=False,
        publish_m3g=True,
        sitelog_plain=False,
        sitelog_dir=None,
        dry_run=False,
        m3g_network="EPOS",
        m3g_endpoint=None,
        json=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_publish_m3g_dry_run_validates_no_commit_no_put():
    gate = SimpleNamespace(path=Path("/tmp/nyla00isl_20260711.log"), changed=True)
    # dry-run: submit_to_m3g returns a validated result with no upload (no PUT).
    result = SimpleNamespace(validation=None, validated=True, upload=None, skipped=None)

    with (
        patch(
            "receivers.dissemination.load_dissemination_config",
            return_value=[_target()],
        ),
        patch(
            "receivers.dissemination.sitelogs.resolve_sitelogs_repo",
            return_value=Path("/tmp"),
        ),
        patch(
            "receivers.dissemination.sitelogs.generate_site_log_if_changed",
            return_value=gate,
        ),
        patch("receivers.dissemination.sitelogs.commit_site_log") as mock_commit,
        patch(
            "receivers.dissemination.sitelogs.submit_to_m3g", return_value=result
        ) as mock_submit,
    ):
        rc = cmd_epos_disseminate(_args(dry_run=True))

    assert rc == 0
    # --dry-run on a CHANGED log must NOT commit locally...
    mock_commit.assert_not_called()
    # ...and must call M3G with dry_run=True (validate-only, no PUT).
    assert mock_submit.call_count == 1
    assert mock_submit.call_args.kwargs["dry_run"] is True


def test_publish_m3g_without_dry_run_commits_and_publishes():
    gate = SimpleNamespace(path=Path("/tmp/nyla00isl_20260711.log"), changed=True)
    upload = SimpleNamespace(
        dry_run=False, ok=True, sitelog_name="nyla00isl.log", draft_url="http://m3g/x"
    )
    result = SimpleNamespace(
        validation=None, validated=True, upload=upload, skipped=None
    )

    with (
        patch(
            "receivers.dissemination.load_dissemination_config",
            return_value=[_target()],
        ),
        patch(
            "receivers.dissemination.sitelogs.resolve_sitelogs_repo",
            return_value=Path("/tmp"),
        ),
        patch(
            "receivers.dissemination.sitelogs.generate_site_log_if_changed",
            return_value=gate,
        ),
        patch(
            "receivers.dissemination.sitelogs.commit_site_log", return_value=True
        ) as mock_commit,
        patch(
            "receivers.dissemination.sitelogs.submit_to_m3g", return_value=result
        ) as mock_submit,
    ):
        rc = cmd_epos_disseminate(_args(dry_run=False))

    assert rc == 0
    # Real run on a CHANGED log commits locally and publishes (dry_run=False).
    mock_commit.assert_called_once()
    assert mock_submit.call_args.kwargs["dry_run"] is False
