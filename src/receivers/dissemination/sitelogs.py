"""IGS/M3G site-log generation for EPOS dissemination (C6/T7).

EPOS §3.2 makes the station site log the canonical metadata record, maintained
in the M3G portal (https://gnss-metadata.eu) within one business day of any TOS
change. This module is the dissemination-side wiring around the existing tostools
generator (``tos sitelog`` / :func:`tostools.core.site_log.generate_igs_site_log`):
it reads the station's TOS metadata, renders the IGS site log, and writes it to a
target directory (a ``gps-sitelogs`` repo working tree in production).

The repo-commit and M3G submission steps are deliberately split out (see
:func:`commit_site_log` / the M3G submitter stub) because they need the
``gps-sitelogs`` repo location and M3G credentials — open ops decision #3.
Generation itself is self-contained and testable offline with an injected client.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("receivers.dissemination.sitelogs")

# Where the gps-sitelogs clone lives when [paths] sitelogs_repo is unset.
DEFAULT_SITELOGS_REPO = "~/git/gps-sitelogs"


def resolve_sitelogs_repo(override: Optional[str] = None) -> Path:
    """Return the gps-sitelogs working-tree directory.

    Precedence: explicit ``override`` → receivers.cfg ``[paths] sitelogs_repo``
    → :data:`DEFAULT_SITELOGS_REPO` (``~/git/gps-sitelogs``). Mirrors
    :func:`receivers.cfg.global_sync.resolve_global_repo`, but does not validate
    the tree (callers create it / commit into it). The path is expanduser'd.
    """
    raw = override
    if not raw:
        try:
            from ..config.receivers_config import ReceiversConfig

            raw = ReceiversConfig().get_sitelogs_repo()
        except Exception:  # noqa: BLE001 — config absent/unreadable → default
            raw = None
    return Path(raw or DEFAULT_SITELOGS_REPO).expanduser()


def generate_site_log(
    station: str,
    out_dir: Path,
    *,
    client: Any = None,
    country_code: str = "ISL",
    monument_number: str = "00",
    include_date: bool = False,
    custom_date: Optional[str] = None,
    loglevel: int = logging.WARNING,
) -> Optional[Path]:
    """Render the IGS site log for ``station`` from TOS into ``out_dir``.

    Returns the written path, or None when TOS has no usable metadata (logged).
    ``client`` is an injectable ``TOSClient`` (defaults to a fresh one) so tests
    run offline. ``include_date``/``custom_date`` pick the dated M3G filename
    form (``<9char>_<YYYYMMDD>.log``) vs the plain ``<9char>.log``.
    """
    from tostools.core.site_log import (
        export_site_log_to_file,
        generate_igs_site_log,
    )
    from tostools.tosGPS import generate_igs_sitelog_filename

    sid = station.upper()
    if client is None:
        from tostools.api.tos_client import TOSClient

        client = TOSClient()

    try:
        meta = client.get_complete_station_metadata(sid)
    except Exception as exc:  # noqa: BLE001 - any TOS failure ⇒ skip (caller decides)
        logger.warning("site log: TOS lookup failed for %s: %s", sid, exc)
        return None
    if not meta:
        logger.warning("site log: no TOS metadata for %s", sid)
        return None

    device_sessions = meta.get("device_history", []) or []
    content = generate_igs_site_log(meta, device_sessions, loglevel=loglevel)
    if not content:
        logger.warning("site log: generator produced nothing for %s", sid)
        return None

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _subdir, filename = generate_igs_sitelog_filename(
        sid,
        country_code=country_code,
        monument_number=monument_number,
        include_date=include_date,
        custom_date=custom_date,
        create_station_subdir=False,
    )
    out_path = out_dir / filename
    if not export_site_log_to_file(content, str(out_path), sid, loglevel=loglevel):
        logger.warning("site log: export failed for %s → %s", sid, out_path)
        return None
    logger.info("site log written: %s", out_path)
    return out_path


def commit_site_log(repo_dir: Path, site_log: Path, message: str) -> bool:
    """Stage + commit ``site_log`` in the ``gps-sitelogs`` repo working tree.

    Returns True on a real commit, False when there was nothing to commit. Does
    NOT push (the sync/submission policy is decided per decision #3). Raises on a
    genuine git error so callers see a misconfigured repo rather than silent loss.
    """
    import subprocess

    repo_dir = Path(repo_dir)
    rel = site_log.relative_to(repo_dir)
    subprocess.run(["git", "-C", str(repo_dir), "add", str(rel)], check=True)
    status = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain", str(rel)],
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        logger.info("site log unchanged, nothing to commit: %s", rel)
        return False
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", message, "--", str(rel)],
        check=True,
    )
    return True


# M3G submission (decision #3 — needs the M3G account + API creds / upload path)
# is intentionally not implemented yet. The generated site log is the artifact M3G
# ingests; once the account exists this becomes either an M3G-API POST or a commit
# to the repo M3G pulls from. Tracked as C6 in docs/architecture/epos-dissemination-plan.md.
