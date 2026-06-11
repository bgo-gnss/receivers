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
from typing import Any, Dict, List, Optional, Tuple

from .operations import CfgOperationError

# Where the gps-config-data clone lives when GPS_CONFIG_DATA_REPO is unset.
DEFAULT_GLOBAL_REPO = "~/git/gps-config-data"

_COAUTHOR_TRAILER = (
    "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
)


def _repo_from_cfg() -> Optional[str]:
    """Read ``[paths] gps_config_data_repo`` from receivers.cfg, or None."""
    try:
        from ..config.receivers_config import ReceiversConfig

        return ReceiversConfig().get_gps_config_data_repo()
    except Exception:  # noqa: BLE001 — config absent/unreadable → fall through
        return None


def resolve_global_repo(repo: Optional[str] = None) -> Path:
    """Return the gps-config-data clone directory, validated.

    Precedence: explicit ``repo`` → ``$GPS_CONFIG_DATA_REPO`` → receivers.cfg
    ``[paths] gps_config_data_repo`` → :data:`DEFAULT_GLOBAL_REPO`
    (``~/git/gps-config-data``). The chosen path must be a git work-tree
    (contains ``.git``) and hold a ``stations.cfg``.

    Raises :class:`CfgOperationError` with an actionable message otherwise.
    """
    raw = (
        repo
        or os.environ.get("GPS_CONFIG_DATA_REPO")
        or _repo_from_cfg()
        or DEFAULT_GLOBAL_REPO
    )
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


def _git(
    repo: Path, *args: str, check: bool = True, timeout: Optional[int] = None
) -> subprocess.CompletedProcess:
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
        timeout=timeout,
    )


def _origin_state(repo: Path) -> Optional[Tuple[int, int]]:
    """Return ``(behind, ahead)`` of HEAD vs its upstream, or ``None``.

    ``None`` means the branch has no upstream — a local-only clone that cannot
    diverge from any origin (e.g. a fresh ``git init`` in tests). When an
    upstream exists, a best-effort ``git fetch`` refreshes it first so the
    counts reflect what origin actually holds.
    """
    upstream = _git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        check=False,
    )
    if upstream.returncode != 0:
        return None  # no upstream → local-only, nothing to diverge from

    # Refresh origin so behind/ahead is accurate. Best-effort: a network blip
    # falls back to the last-known ref (the require-push + push step is the final
    # arbiter — a non-ff push is rejected).
    try:
        _git(repo, "fetch", "--quiet", check=False, timeout=30)
    except subprocess.TimeoutExpired:
        pass

    counts = _git(
        repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD", check=False
    )
    if counts.returncode != 0:
        return None
    try:
        behind_s, ahead_s = counts.stdout.split()
        return int(behind_s), int(ahead_s)
    except ValueError:
        return None


def assert_committable(repo: Path, *, push: bool) -> None:
    """Raise unless a ``--global`` commit on ``repo`` is safe right now.

    The divergence guardrail. ``--global`` is a laptop-side finalize: edit →
    commit → push → the rek-d01 config-sync timer ``git pull --ff-only``-s the
    new commit. A local commit that is *not* pushed leaves the clone ahead of
    origin, so the server's ff-only pull fails and config propagation silently
    stops. To keep every clone linear with origin:

      * a commit on a **remote-tracked** clone **requires ``push=True``**; and
      * the clone must be **even with origin** (not behind/ahead).

    A **local-only** clone (no upstream — e.g. a test repo) cannot diverge from
    any origin, so the guardrail is a no-op there.

    Call this BEFORE writing the cfg file so a refusal leaves no dirty work-tree.
    """
    state = _origin_state(repo)
    if state is None:
        return  # local-only clone — nothing to diverge from
    behind, ahead = state
    if not push:
        raise CfgOperationError(
            f"--global commit on a remote-tracked clone ({repo.name}) requires "
            f"--push: committing without pushing leaves the clone ahead of "
            f"origin and breaks the rek-d01 config-sync `git pull --ff-only`. "
            f"Re-run with --push, or use --dry-run to preview."
        )
    if behind or ahead:
        raise CfgOperationError(
            f"{repo.name} is not even with origin (behind {behind}, ahead "
            f"{ahead}) — committing now would diverge it and break the "
            f"config-sync ff-only pull. Run `git -C {repo} pull --ff-only` "
            f"(and push any local commits) first, then retry."
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
      * **divergence guardrail** (when the clone is remote-tracked): the commit
        **requires ``push=True``** and refuses when the clone is not even with
        origin. A local commit that isn't pushed leaves the clone *ahead* of
        origin, which breaks the rek-d01 config-sync timer's ``git pull
        --ff-only`` — silently halting config propagation. ``--global`` is a
        laptop-side finalize (edit → commit → push → sync); keeping every commit
        pushed keeps the clone linear so the server only ever fast-forwards.
        A local-only clone (no upstream — e.g. tests) skips the guardrail.
      * **no-op** when nothing is staged (returns ``committed=False``).

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

    assert_committable(repo, push=push)

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
