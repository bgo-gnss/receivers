"""Compare a live receiver identity dict against ``stations.cfg``.

Used by every code path that performs a health probe — CLI
(``receivers health``), the bulk scheduler's per-station health job, and
``StatusTask`` in the scheduler task interface — so that *all* probes
populate the same ``cfg_discrepancy`` audit log.

The function logs one warning line per detected drift and records the
state in :mod:`receivers.cfg.discrepancy_log`. Fields where cfg agrees
with the receiver auto-close any matching open row.

This module owns the comparison rules previously buried in
``cli/main.py:_flag_identity_vs_cfg``; that function now delegates here.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from . import discrepancy_log as _dlog  # type: ignore[attr-defined]
from .field_manifest import fields_by_key as _fields_by_key


def flag_identity_vs_cfg(
    station_id: str,
    identity: Dict[str, Any],
    station_config: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Detect, log, and record cfg/receiver drift for one station.

    Behaviour:
      * receiver_type uses the fingerprint matcher so ``PolaRX5`` and
        ``SEPT POLARX5`` are treated as equal.
      * receiver_serial / receiver_firmware_version use case-insensitive
        string equality.
      * Fields where cfg agrees with the receiver auto-resolve any open
        cfg_discrepancy row.
      * Fields that drift get a ``record_detection`` row (idempotent —
        same drift across many probes does not multiply rows) and a
        single warning line aggregating all flagged fields.

    Failures in the discrepancy_log writes are swallowed — the log is
    best-effort and must not break the health probe.
    """
    flagged: List[str] = []
    # (cfg_key, cfg_value, receiver_value, verdict) — collected for DB record
    records: List[Tuple[str, Optional[str], Optional[str], str]] = []

    reported_model = identity.get("receiver_model")
    cfg_type = station_config.get("receiver_type")
    if reported_model:
        try:
            from ..health.receiver_fingerprint import check_identity_mismatch

            mismatch = check_identity_mismatch(
                str(cfg_type) if cfg_type else "", identity
            )
        except Exception:  # noqa: BLE001
            mismatch = None

        if mismatch and not cfg_type:
            flagged.append(f"receiver_type=[missing] reported={reported_model!r}")
            records.append(("receiver_type", None, str(reported_model), "missing"))
        elif mismatch:
            flagged.append(f"receiver_type={cfg_type!r} reported={reported_model!r}")
            records.append(
                ("receiver_type", str(cfg_type), str(reported_model), "conflict")
            )
        else:
            _dlog.auto_resolve_if_open(station_id, "receiver_type")

    _field_specs = _fields_by_key()
    for cfg_key, reported in [
        ("receiver_serial", identity.get("serial_number")),
        ("receiver_firmware_version", identity.get("firmware_version")),
    ]:
        if not reported:
            continue
        cfg_val = station_config.get(cfg_key)
        spec = _field_specs.get(cfg_key)
        # Use normalized comparison when a FieldSpec is available (handles
        # firmware notation variants like "4.6.2" == "4.62"). Fall back to
        # case-insensitive string equality for fields without a spec.
        if cfg_val:
            equal = (
                spec.values_equal(str(cfg_val), str(reported))
                if spec is not None
                else str(cfg_val).strip().lower() == str(reported).strip().lower()
            )
            if equal:
                _dlog.auto_resolve_if_open(station_id, cfg_key)
                continue
        if not cfg_val:
            flagged.append(f"{cfg_key}=[missing] reported={reported!r}")
            records.append((cfg_key, None, str(reported), "missing"))
        else:
            flagged.append(f"{cfg_key}={cfg_val!r} reported={reported!r}")
            records.append((cfg_key, str(cfg_val), str(reported), "conflict"))

    for cfg_key, cfg_val, rx_val, verdict in records:
        _dlog.record_detection(
            station_id,
            cfg_key,
            cfg_value=cfg_val,
            receiver_value=rx_val,
            tos_value=None,
            verdict=verdict,
            detected_by=_dlog.DETECTED_BY_HEALTH,
        )

    if flagged:
        logger.warning(
            f"[{station_id}] receiver identity differs from stations.cfg: "
            f"{'; '.join(flagged)} — review with "
            f"'receivers cfg reconcile {station_id}'"
        )


def flag_from_health_data(
    station_id: str,
    health_data: Dict[str, Any],
    station_config: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Convenience wrapper for callers that have a ``health_data`` dict.

    Extracts ``receiver_identity`` from the gathered health payload and
    routes through :func:`flag_identity_vs_cfg`. No-ops when identity is
    missing or empty (e.g. unreachable receiver).
    """
    identity = health_data.get("receiver_identity")
    if not isinstance(identity, dict) or not identity:
        return
    if station_config.get("_adhoc"):
        return
    flag_identity_vs_cfg(station_id, identity, station_config, logger)
