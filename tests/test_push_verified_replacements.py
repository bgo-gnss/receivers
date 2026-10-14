"""``verified_replacements`` lets stream-splice past the two push gates — and nothing else.

A truncated BNC daily has no 15 s raw, so the regenerability gate calls it
irreplaceable although its archived 1 Hz hours regenerate it; and an
R3 -> R2 rebuild shrinks the Hatanaka layer the degradation gate measures.
stream-splice proves the replacement by a stronger test (stream product,
epochs with satellites >= old) and names those paths. The gates must still
refuse every path it did NOT name.
"""

from __future__ import annotations

import gzip
import importlib
import logging
import subprocess
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

m = importlib.import_module("receivers.cli.main")

REL = "2026/jul/SEY9/15s_24hr/rinex/SEY91820.26D.Z"
_real_run = subprocess.run


def _write(path: Path, nbytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(b"x" * nbytes))


def _push(tmp_path: Path, *, staged: int, archived: int, raw: bool, verified):
    work, arch = tmp_path / "work", tmp_path / "arch"
    _write(work / REL, staged)
    _write(arch / REL, archived)
    if raw:
        _write(arch / "2026/jul/SEY9/15s_24hr/raw/SEY9202607010000a.sbf.gz", 10)
    sent: list[list[str]] = []

    def run(cmd, *a, **k):
        if cmd[0] == "rsync":
            listed = Path(cmd[cmd.index("--files-from") + 1]).read_text().split()
            sent.append(listed)
            return subprocess.CompletedProcess(cmd, 0, "\n".join(listed) + "\n", "")
        return _real_run(cmd, *a, **k)  # the degradation gate's gzip -dc

    args = Namespace(
        dry_run=False,
        fix_headers=False,
        from_archive=False,
        source_dir=str(arch),
        backup_old=False,
        reindex=False,
        catalog_prod=False,
        catalog_host=None,
    )
    with (
        patch.object(m, "_push_reindex"),
        patch.object(
            m, "_resolve_archive_target", return_value=("gpsops@x:~/g", "a", "~/g")
        ),
        patch(
            "receivers.archive.format_guard.split_bad_z", lambda rel, *a, **k: (rel, [])
        ),
        patch("subprocess.run", side_effect=run),
    ):
        stats = m._push_reconverted(
            work,
            args,
            logging.getLogger("t"),
            only_rel=[REL],
            verified_replacements=verified,
        )
    return stats, (sent[0] if sent else [])


class TestRegenerabilityGate:
    def test_rawless_replacement_refused_without_proof(self, tmp_path):
        stats, sent = _push(
            tmp_path, staged=5000, archived=1000, raw=False, verified=None
        )
        assert sent == []
        assert stats["refused_unregenerable"] == 1

    def test_rawless_replacement_pushed_when_proven(self, tmp_path):
        stats, sent = _push(
            tmp_path, staged=5000, archived=1000, raw=False, verified={REL}
        )
        assert sent == [REL]
        assert "refused_unregenerable" not in stats

    def test_proof_for_another_path_does_not_leak(self, tmp_path):
        other = REL.replace("1820", "1830")
        stats, sent = _push(
            tmp_path, staged=5000, archived=1000, raw=False, verified={other}
        )
        assert sent == []


class TestDegradationGate:
    def test_smaller_replacement_refused_without_proof(self, tmp_path):
        stats, sent = _push(
            tmp_path, staged=1000, archived=5000, raw=True, verified=None
        )
        assert sent == []
        assert stats["refused_degraded"] == 1

    def test_smaller_replacement_pushed_when_proven(self, tmp_path):
        """R3 -> R2: fewer bytes, same epochs. The caller counted them."""
        stats, sent = _push(
            tmp_path, staged=1000, archived=5000, raw=True, verified={REL}
        )
        assert sent == [REL]
