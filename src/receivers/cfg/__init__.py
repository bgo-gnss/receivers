"""Configuration reconciliation between stations.cfg, live receivers, and TOS.

The reconciler compares values across three potential sources:

* ``stations.cfg`` — the operational configuration file (gps-config-data repo)
* live receiver — what the device reports about itself via the health pipeline
* TOS — the authoritative metadata registry

The intended workflow is **TOS → cfg**: changes to station equipment are
recorded in TOS first and then propagated to ``stations.cfg``. The live
receiver is a validation source — it confirms what is actually deployed and
flags discrepancies so they can be addressed in TOS.

The CLI front-end is :mod:`receivers.cli.cfg`.
"""

from .reconciler import (
    FieldDiff,
    SourceUnavailableError,
    Verdict,
    apply_diff,
    compare_station,
)

__all__ = [
    "FieldDiff",
    "SourceUnavailableError",
    "Verdict",
    "apply_diff",
    "compare_station",
]
