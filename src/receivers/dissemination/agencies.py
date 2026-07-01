"""Agency reference data for EPOS dissemination (deployed config).

The RINEX header OBSERVER/AGENCY and the IGS site-log §11/§12/§13 need each station's
agency in **English**, with an abbreviation / URL / generic GNSS-team email — none of
which a TOS *contact entity* can carry (single Icelandic ``name``, no english-name /
abbreviation / url field). So a curated ``agencies.yaml`` supplies the presentation,
while TOS contact **roles** say which agency plays each part (see
``docs/architecture/epos-observer-agency-and-sitelog.md``).

``agencies.yaml`` is a **deployed config file** — it lives in ``gps-config-data`` under
version control and is synced to ``~/.config/gpsconfig/`` (or ``GPS_CONFIG_PATH``), the
same mechanism as ``stations.cfg`` / ``sync.yaml``. This module loads it and resolves a
TOS owner organization → :class:`AgencyInfo`, with the IMO default for the operator /
data-center roles a station may not carry.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("receivers.dissemination.agencies")


def default_agencies_path() -> Path:
    """``agencies.yaml`` in ``GPS_CONFIG_PATH`` (else ``~/.config/gpsconfig``).

    Mirrors :func:`receivers.archive.config._default_config_path` for ``sync.yaml`` so
    the whole config set resolves the same way.
    """
    base = os.getenv("GPS_CONFIG_PATH")
    root = Path(base) if base else Path.home() / ".config" / "gpsconfig"
    return root / "agencies.yaml"


@dataclass(frozen=True)
class AgencyInfo:
    """One agency's render data (from ``agencies.yaml``).

    English fields (``english_name`` / ``abbrev``) are the international IGS/EPOS site-log
    forms; ``observer`` / ``agency_label`` are the RINEX header strings. ``address`` is a
    tuple of mailing-address lines (site-log Mailing Address is multi-line).
    """

    org: str
    """The Icelandic TOS org key this entry resolves (contact.owner.organization)."""
    english_name: str
    icelandic_name: str = ""
    department_en: str = ""
    department_is: str = ""
    abbrev: str = ""
    """English preferred abbreviation (site-log Preferred Abbreviation)."""
    abbrev_is: str = ""
    observer: str = ""
    """RINEX OBSERVER (e.g. ``GNSSatIMO``)."""
    agency_label: str = ""
    """RINEX AGENCY (≤40 chars, e.g. ``Vedurstofa Islands``)."""
    address: tuple[str, ...] = ()
    contact_name: str = ""
    phone: str = ""
    email: str = ""
    url: str = ""


class AgencyResolver:
    """Resolve a TOS owner organization → :class:`AgencyInfo` via ``agencies.yaml``.

    Exact-match by org string; :meth:`resolve` returns None for an unknown org (the
    caller then uses the config/hardcoded default). The operator / data-center
    **defaults** (IMO) back the site-log §11/§13 when a station carries no operator /
    data-owner role — see the role-guided model in the design doc.
    """

    def __init__(
        self, agencies: dict[str, AgencyInfo], defaults: dict[str, str]
    ) -> None:
        self._by_org = agencies
        self._defaults = defaults

    # -- construction ------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> AgencyResolver:
        """Load from ``agencies.yaml``; a missing/empty file yields an empty resolver.

        Non-fatal by design: the config is optional infrastructure, so a missing file
        must not crash dissemination — callers fall back to their config defaults.
        """
        p = path or default_agencies_path()
        if not p.is_file():
            logger.warning("agencies.yaml not found at %s — using empty resolver", p)
            return cls({}, {})
        try:
            import yaml

            raw = yaml.safe_load(p.read_text()) or {}
        except Exception as exc:  # noqa: BLE001 - bad config ⇒ empty, never fatal
            logger.warning("could not read %s (%s) — using empty resolver", p, exc)
            return cls({}, {})
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgencyResolver:
        """Build from a parsed ``agencies.yaml`` mapping (testable offline)."""
        agencies: dict[str, AgencyInfo] = {}
        for org, d in (raw.get("agencies") or {}).items():
            d = d or {}
            addr = d.get("address") or ()
            if isinstance(addr, str):
                addr = (addr,)
            agencies[org] = AgencyInfo(
                org=org,
                english_name=str(d.get("english_name", "")),
                icelandic_name=str(d.get("icelandic_name", "")),
                department_en=str(d.get("department_en", "")),
                department_is=str(d.get("department_is", "")),
                abbrev=str(d.get("abbrev", "")),
                abbrev_is=str(d.get("abbrev_is", "")),
                observer=str(d.get("observer", "")),
                agency_label=str(d.get("agency_label", "")),
                address=tuple(str(x) for x in addr),
                contact_name=str(d.get("contact_name", "")),
                phone=str(d.get("phone", "")),
                email=str(d.get("email", "")),
                url=str(d.get("url", "")),
            )
        defaults = {k: str(v) for k, v in (raw.get("defaults") or {}).items()}
        return cls(agencies, defaults)

    # -- resolution --------------------------------------------------------
    def resolve(self, org: Optional[str]) -> Optional[AgencyInfo]:
        """The :class:`AgencyInfo` for ``org``, or None if unknown/blank."""
        if not org:
            return None
        return self._by_org.get(org.strip())

    def operator_default(self) -> Optional[AgencyInfo]:
        """§11 On-Site POC default (``defaults.operator_agency`` → its AgencyInfo)."""
        return self.resolve(self._defaults.get("operator_agency"))

    def data_center_default(self) -> Optional[AgencyInfo]:
        """§13 Primary Data Center default (``defaults.data_center_agency``)."""
        return self.resolve(self._defaults.get("data_center_agency"))

    def url_default(self) -> str:
        """§13 URL for More Information (``defaults.url``)."""
        return self._defaults.get("url", "")
