"""A caster password must not reach the terminal or the log.

`redact_secrets` existed and was applied in the NTRIP handler's own logging —
and the password still appeared in full, because the generic `_push_configs`
dry-run print bypassed it. Found by running the real CLI against a live
receiver, not by a unit test: the builder was correct, the call site was not.

So this pins the CALL SITE. Mutating `_push_configs` back to printing raw
commands must turn this red.
"""

import logging
from types import SimpleNamespace

from receivers.cli.main import _push_configs

SECRET = "S3CR3T-CASTER-PW"


def _config_file(tmp_path):
    p = tmp_path / "ntrip_provision.txt"
    p.write_text(
        "setNtripSettings, NTR2, Server\n"
        'setNtripSettings, NTR2, , "ntrcaster.vedur.is"\n'
        'setNtripSettings, NTR2, , , , "gpsops"\n'
        f'setNtripSettings, NTR2, , , , , "{SECRET}"\n'
        'setNtripSettings, NTR2, , , , , , "VOGC1"\n'
        "eccf, Current, Boot\n"
    )
    return p


def _args(cfg):
    return SimpleNamespace(
        push=str(cfg),
        dry_run=True,
        no_save=False,
        save=False,
        output_dir=None,
        timeout=30,
        verbose=False,
    )


def test_dry_run_never_prints_the_caster_password(tmp_path, capsys):
    cfg = _config_file(tmp_path)
    _push_configs(_args(cfg), [("VOGC", "10.0.0.1", 28784)], logging.getLogger("t"))
    out = capsys.readouterr()
    combined = out.out + out.err
    assert SECRET not in combined, "caster password leaked to the terminal"
    assert "********" in combined, "the masked field should still be shown"


def test_the_rest_of_the_command_stays_legible(tmp_path, capsys):
    """Masking must not make a dry-run useless for review."""
    cfg = _config_file(tmp_path)
    _push_configs(_args(cfg), [("VOGC", "10.0.0.1", 28784)], logging.getLogger("t"))
    out = capsys.readouterr().out
    for keep in ("NTR2", "Server", "ntrcaster.vedur.is", "gpsops", "VOGC1", "eccf"):
        assert keep in out, keep


def test_the_file_on_disk_still_carries_the_real_secret(tmp_path):
    """Redaction is display-only — the receiver must still get the real value."""
    cfg = _config_file(tmp_path)
    assert SECRET in cfg.read_text()
