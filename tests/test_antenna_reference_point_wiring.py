"""`--antenna-reference-point` defaults to DHARP and reaches the TOS builder.

The failure: `tosGPS syncMeta` wrote `BPA` into station.info's HtCod and the
GAMIT run failed (VFLS/VFLN 2026-10-03). `BPA` is the IGS site-log ARP code for
the antenna model; GAMIT needs a height code. tostools `c84d5fc` makes the
builder always emit the attribute and validate it; this pins the receivers half
— the flag exists, defaults to DHARP so it can be omitted, and is forwarded.

Asserting the builder alone would pass with the CLI unwired, which is how an
abstraction ends up better tested than the code that calls it.
"""

from __future__ import annotations

import argparse
import ast
import inspect
from pathlib import Path

import pytest

OPS = Path("src/receivers/cfg/operations.py")

VALID = ["DHARP", "DHPAB", "DHBCR", "DHTCR"]

_BUILDERS = {
    "add-antenna": "_add_add_antenna_parser",
    "replace-antenna": "_add_replace_antenna_parser",
}


def _parser_for(verb: str) -> argparse.ArgumentParser:
    """Build just this verb's real subparser, via its own builder function."""
    from receivers.cli import cfg as cli_cfg

    root = argparse.ArgumentParser(prog="receivers cfg")
    subs = root.add_subparsers(dest="cfg_command")
    getattr(cli_cfg, _BUILDERS[verb])(subs)
    return subs.choices[verb]


def _base_args(verb: str) -> list[str]:
    return ["--station", "VFLS", "--model", "SEPVC6150L"]


# --- the flag --------------------------------------------------------------


@pytest.mark.parametrize("verb", sorted(_BUILDERS))
def test_the_flag_defaults_to_dharp_so_it_can_be_omitted(verb):
    """bgo's requirement: skip the flag unless it differs from DHARP."""
    ns = _parser_for(verb).parse_args(_base_args(verb))
    assert ns.antenna_reference_point == "DHARP"


@pytest.mark.parametrize("verb", sorted(_BUILDERS))
def test_the_other_real_codes_are_selectable(verb):
    """DHPAB/DHBCR/DHTCR are real fleet values — 471/267/4 station.info rows."""
    for code in VALID:
        ns = _parser_for(verb).parse_args(
            _base_args(verb) + ["--antenna-reference-point", code]
        )
        assert ns.antenna_reference_point == code


@pytest.mark.parametrize("verb", sorted(_BUILDERS))
def test_an_igs_arp_code_is_rejected_at_the_cli(verb):
    """BPA must not even parse — it is the value that broke the GAMIT run."""
    with pytest.raises(SystemExit):
        _parser_for(verb).parse_args(
            _base_args(verb) + ["--antenna-reference-point", "BPA"]
        )


# --- the wiring, not the helper -------------------------------------------


@pytest.mark.parametrize("op", ["add_antenna", "replace_antenna"])
def test_the_op_forwards_it_to_the_builder(op):
    from receivers.cfg import operations

    body = inspect.getsource(getattr(operations, op))
    assert (
        "antenna_reference_point=antenna_reference_point" in body
    ), f"{op} accepts the parameter but drops it before build_antenna_attributes"


@pytest.mark.parametrize("handler", ["cmd_cfg_add_antenna", "cmd_cfg_replace_antenna"])
def test_the_cli_handler_passes_the_parsed_value(handler):
    from receivers.cli import cfg as cli_cfg

    body = inspect.getsource(getattr(cli_cfg, handler))
    assert (
        "antenna_reference_point=args.antenna_reference_point" in body
    ), f"{handler} defines the flag but never hands it to the operation"


def test_the_default_is_a_literal_not_a_tostools_constant():
    """A signature default is evaluated at IMPORT time.

    Referencing `tostools.device.DEFAULT_ANTENNA_REFERENCE_POINT` would force a
    MODULE-level tostools import, and rek-d01's pinned tostools predates that
    constant — every importer of operations.py, including paths the scheduler
    touches, would raise ImportError and systemd would crash-loop
    (Restart=always). Same reason `station_kind` is imported lazily in this file.
    """
    from receivers.cfg import operations

    tree = ast.parse(OPS.read_text())
    module_level = [
        n.module
        for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
        and "tostools.device" in (getattr(n, "module", "") or "")
    ]
    assert (
        not module_level
    ), "tostools.device must not be imported at module level in operations.py"

    for fn in ("add_antenna", "replace_antenna"):
        default = (
            inspect.signature(getattr(operations, fn))
            .parameters["antenna_reference_point"]
            .default
        )
        assert default == "DHARP", f"{fn}'s default must be 'DHARP', got {default!r}"

    assert OPS.read_text().count('antenna_reference_point: str = "DHARP"') == 2
