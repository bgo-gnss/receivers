"""EPOS RINEX dissemination — push RINEX3 long-name files to the EPOS files server.

Runs the EPOS dissemination pipeline entirely from ``receivers``, replacing the
legacy ``epos-gnss`` swarm container. Unlike the ``receivers.archive`` package
(which feeds the immutable long-term archive *as-is*), this package *derives* a
disseminated product: it converts archived RINEX to RINEX 3.04 with **long IGS
names** (a capability the legacy library lacks), sets headers from TOS, QC-gates
against TOS/site logs, and indexes the result.

Phase 1 keeps the existing EPOS ``gnss-europe`` DB (md5 index); a later phase
migrates the index to ``content_sha256`` + ``gps_health``.

This module is **T1 — the tracer bullet**: the convert+rename+push chain for one
(station, date), gated off. See ``docs/architecture/epos-dissemination-plan.md``
and design ``1781867391-data-dissemination-archive-sync-design``.
"""

from .config import DisseminationTarget, load_dissemination_config
from .engine import DisseminateResult, EposDisseminate
from .epos_etl import EtlResult, run_etl
from .qc_gate import QCVerdict, qc_check
from .rinex_index import index_rinex_file, rinex_md5s
from .tos_access import epos_markers, epos_stations, make_session_provider, TOSSesionCache

__all__ = [
    "DisseminationTarget",
    "load_dissemination_config",
    "EposDisseminate",
    "DisseminateResult",
    "QCVerdict",
    "qc_check",
    "epos_stations",
    "epos_markers",
    "make_session_provider",
    "TOSSesionCache",
    "run_etl",
    "EtlResult",
    "index_rinex_file",
    "rinex_md5s",
]
