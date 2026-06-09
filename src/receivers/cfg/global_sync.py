"""Write a cfg change to the gps-config-data git repo (source of truth) + commit.

The ``cfg`` verbs write the **local/deployed** ``stations.cfg`` by default
(``~/.config/gpsconfig/`` via gps_parser). The ``--global`` flag instead targets
the **gps-config-data** git clone — the source of truth that the production
``gps-config-sync`` timer propagates to rek-d01 — and commits the change there.

This module provides the two net-new pieces that backing flag needs:

  * :func:`resolve_global_repo` — locate the gps-config-data clone.
  * :func:`git_commit_cfg` — the package's only git automation: stage + commit
    (and optionally push) a cfg edit. Deliberately narrow and safe — no merge,
    no force, push opt-in, no-op when nothing changed, refuses a detached HEAD.

Keeping the two config layers separate is intentional: ``--global`` never
touches the local/deployed config, so finalizing both is two runs by design.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .operations import CfgOperationError

# Where the gps-config-data clone lives when GPS_CONFIG_DATA_REPO is unset.
DEFAULT_GLOBAL_REPO = "~/git/gps-config-data"

_COAUTHOR_TRAILER = (
    "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
)


def resolve_global_repo(repo: Optional[str] = None) -> Path:
    """Return the gps-config-data clone directory, validated.

    Precedence: explicit ``repo`` → ``$GPS_CONFIG_DATA_REPO`` →
    :data:`DEFAULT_GLOBAL_REPO` (``~/git/gps-config-data``). The chosen path must
    be a git work-tree (contains ``.git``) and hold a ``stations.cfg``.

    Raises :class:`CfgOperationError` with an actionable message otherwise.
    """
    raw = repo or os.environ.get("GPS_CONFIG_DATA_REPO") or DEFAULT_GLOBAL_REPO
    path = Path(raw).expanduser()
    if not path.exists():
        raise CfgOperationError(
            f"gps-config-data repo not found at {path} (set GPS_CONFIG_DATA_REPO "
            f"or clone it to {DEFAULT_GLOBAL_REPO})"
        )
    if not (path / ".git").exists():
        raise CfgOperationError(f"{path} is not a git work-tree (no .git)")
    if not (path / "stations.cfg").exists():
        raise CfgOperationError(f"{path} has no stations.cfg")
    return path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in ``repo`` with a deterministic identity.

    The ``-c user.*`` overrides make commits work even where the repo/CI has no
    configured identity (e.g. a fresh ``git init`` in tests).
    """
    cmd = [
        "git",
        "-c",
        "user.name=GPS Receivers cfg",
        "-c",
        "user.email=gps@vedur.is",
        *args,
    ]
    return subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=check,
    )


def git_commit_cfg(
    repo: Path,
    rel_paths: List[str],
    message: str,
    *,
    push: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Stage ``rel_paths`` in ``repo``, commit with ``message``, optionally push.

    Safe by construction:
      * **dry-run** returns the working-tree diff for ``rel_paths`` and the
        planned message, writing nothing.
      * refuses a **detached HEAD** (no branch to commit onto sensibly).
      * **no-op** when nothing is staged (returns ``committed=False``).
      * **push is opt-in**; a push failure is reported but leaves the local
        commit intact (the operator can push by hand) rather than raising.

    Returns ``{"committed", "pushed", "commit", "message", "diff"?, "push_error"?}``.
    """
    diff = _git(repo, "--no-pager", "diff", "--", *rel_paths).stdout

    if dry_run:
        return {
            "committed": False,
            "pushed": False,
            "commit": None,
            "message": message,
            "diff": diff,
            "dry_run": True,
        }

    # A detached HEAD has no symbolic branch ref.
    head = _git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    if head.returncode != 0:
        raise CfgOperationError(
            f"{repo} is in a detached-HEAD state — checkout a branch before "
            f"committing with --global"
        )

    _git(repo, "add", "--", *rel_paths)

    # Nothing staged → nothing to do.
    staged = _git(repo, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return {
            "committed": False,
            "pushed": False,
            "commit": None,
            "message": message,
            "reason": "no changes",
        }

    full_message = f"{message}\n\n{_COAUTHOR_TRAILER}"
    _git(repo, "commit", "-m", full_message)
    sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

    result: Dict[str, Any] = {
        "committed": True,
        "pushed": False,
        "commit": sha,
        "message": message,
    }

    if push:
        pushed = _git(repo, "push", check=False)
        if pushed.returncode == 0:
            result["pushed"] = True
        else:
            result["push_error"] = (pushed.stderr or pushed.stdout).strip()

    return result
