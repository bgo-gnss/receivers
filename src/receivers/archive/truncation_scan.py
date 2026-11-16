"""Scan for truncated raw files and build a reviewable repair plan.

The candidate set is cheap and deterministic, and needs no time heuristic.
A file goes into the long-term archive catalog only after its full content has
been hashed, which means decompressing it. So a raw file that never got an
``imo_archive`` catalog row, or whose size no longer matches its row, is the only
kind that can hide a broken stream. Only those candidates are decompressed;
``--deep`` probes every file in the window instead.

The output is a :class:`Plan`: every fact the decision rested on, plus the
decision itself. ``apply`` re-checks those facts before it writes anything.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from ..utils.canonical_key import canonical_key
from .path_parse import ParsedArchivePath, parse_archive_path
from .truncation import (
    BROKEN,
    Decision,
    GzipProbe,
    GzipStatus,
    ReceiverState,
    decide,
    probe_gzip,
    salvage,
)

logger = logging.getLogger("receivers.archive.truncation")

PLAN_VERSION = 1
RAW_SESSIONS = ("15s_24hr", "1Hz_1hr", "status_1hr")
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


@dataclass
class Item:
    rel: str
    station: str
    session_type: str
    file_date: str  # ISO
    file_hour: Optional[int]
    copies: dict  # location -> GzipProbe dict | None
    catalog: Optional[dict]  # {"file_size", "content_sha256"} or None
    receiver: dict  # {"state", "source", "oldest", "observed_at"}
    salvage: Optional[dict]  # Salvage.summary() + {"source"}
    decision: dict

    @property
    def action(self) -> str:
        return str(self.decision["action"])


@dataclass
class Plan:
    version: int
    created_at: str
    local_root: str
    archive_root: str
    start: str
    end: str
    stations: Optional[list]
    sessions: list
    deep: bool
    stats: dict = field(default_factory=dict)
    items: list = field(default_factory=list)  # list[Item]

    def to_json(self) -> str:
        d = dict(self.__dict__)
        d["items"] = [i.__dict__ for i in self.items]
        return json.dumps(d, indent=1, sort_keys=True)

    @classmethod
    def load(cls, path: str) -> Plan:
        with open(path) as fh:
            d = json.load(fh)
        if d.get("version") != PLAN_VERSION:
            raise ValueError(f"plan version {d.get('version')} != {PLAN_VERSION}")
        items = [Item(**i) for i in d.pop("items")]
        return cls(**d, items=items)


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


def _month_dirs(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y}/{_MONTHS[m - 1]}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def iter_raw_files(
    root: str,
    start: date,
    end: date,
    *,
    stations: Optional[set] = None,
    sessions: Iterable[str] = RAW_SESSIONS,
) -> Iterable[tuple[str, ParsedArchivePath]]:
    """Yield ``(relpath, ParsedArchivePath)`` for raw files dated in [start, end]."""
    sessions = tuple(sessions)
    for ym in _month_dirs(start, end):
        base = os.path.join(root, ym)
        try:
            stas = sorted(os.listdir(base))
        except OSError:
            continue
        for sta in stas:
            if sta.startswith(".") or (stations and sta.upper() not in stations):
                continue
            for sess in sessions:
                d = os.path.join(base, sta, sess, "raw")
                try:
                    names = sorted(os.listdir(d))
                except OSError:
                    continue
                for n in names:
                    p = os.path.join(d, n)
                    parsed = parse_archive_path(p, root)
                    if parsed is None or parsed.file_date is None:
                        continue
                    if start <= parsed.file_date <= end:
                        yield parsed.relative_path, parsed


# ---------------------------------------------------------------------------
# Database facts
# ---------------------------------------------------------------------------


def load_catalog(conn, start: date, end: date, stations: Optional[set]) -> dict:
    """``(station, session, canonical_key) -> {file_size, content_sha256}``
    for ``imo_archive`` raw rows in the window."""
    sql = """SELECT station, session_type, canonical_key, file_size, content_sha256
               FROM archive_catalog
              WHERE storage_location = 'imo_archive' AND file_category = 'raw'
                AND file_date BETWEEN %s AND %s"""
    params: list = [start, end]
    if stations:
        sql += " AND station = ANY(%s)"
        params.append(sorted(stations))
    out = {}
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for sta, sess, key, size, sha in cur.fetchall():
            out[(sta, sess, key)] = {
                "file_size": size,
                "content_sha256": (sha or "").strip() or None,
            }
    return out


def load_horizons(conn) -> dict:  # noqa: D401
    """``(sid, session_type) -> (oldest_date, oldest_hour, observed_at)``."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sid, session_type, oldest_date, oldest_hour, observed_at "
            "FROM receiver_horizon"
        )
        return {(r[0], r[1]): (r[2], r[3], r[4]) for r in cur.fetchall()}


def load_not_on_receiver(conn) -> set:
    """Relpaths a refetch already found missing on the receiver (ledger)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT rel_path FROM truncation_repair "
                "WHERE outcome = 'not_on_receiver'"
            )
            return {r[0] for r in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 — table may predate migration 076
        logger.warning("truncation_repair ledger unavailable: %s", exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return set()


def receiver_state(
    *,
    rel: str,
    station: str,
    session: str,
    file_date: date,
    file_hour: Optional[int],
    horizons: dict,
    not_on_receiver: set,
    now: datetime,
    max_age: timedelta,
) -> dict:
    """Whether the receiver still holds the file — from recorded facts only.

    GONE comes ONLY from a fresh horizon that has passed the period. A refetch
    that was answered "not found" can only DOWNGRADE a HOLDS to UNKNOWN (the
    item is deferred and not fetched again): receivers answer 550 for files
    they hold (SVIN, 2026-11-16), so one such answer must never lead to an
    irreversible salvage. Once the horizon passes the period it becomes GONE
    by itself. A horizon older than ``max_age`` is not trusted either.
    """
    h = horizons.get((station, session))
    if h is None:
        return {"state": ReceiverState.UNKNOWN.value, "source": "no horizon"}
    oldest_date, oldest_hour, observed_at = h
    if observed_at is None or now - observed_at > max_age:
        return {
            "state": ReceiverState.UNKNOWN.value,
            "source": "stale horizon",
            "observed_at": observed_at.isoformat() if observed_at else None,
        }
    if file_hour is None:
        holds = file_date >= oldest_date
    else:
        holds = (file_date, file_hour) >= (oldest_date, oldest_hour or 0)
    if holds and rel in not_on_receiver:
        return {
            "state": ReceiverState.UNKNOWN.value,
            "source": "receiver answered not-found but its horizon still covers it",
            "oldest": f"{oldest_date}" + (f"T{oldest_hour:02d}" if oldest_hour else ""),
            "observed_at": observed_at.isoformat(),
        }
    return {
        "state": (ReceiverState.HOLDS if holds else ReceiverState.GONE).value,
        "source": "horizon",
        "oldest": f"{oldest_date}" + (f"T{oldest_hour:02d}" if oldest_hour else ""),
        "observed_at": observed_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Building the plan
# ---------------------------------------------------------------------------


def _needs_probe(path: str, cat: Optional[dict], deep: bool) -> bool:
    if deep or cat is None:
        return True
    try:
        return bool(os.path.getsize(path) != cat["file_size"])
    except OSError:
        return False


def build_plan(
    conn,
    *,
    local_root: str,
    archive_root: str,
    start: date,
    end: date,
    stations: Optional[set] = None,
    sessions: Iterable[str] = RAW_SESSIONS,
    deep: bool = False,
    settle_hours: float = 6.0,
    horizon_max_age_hours: float = 48.0,
    now: Optional[datetime] = None,
) -> Plan:
    """Scan both roots for the window and decide what to do with each broken file."""
    now = now or datetime.now(UTC)
    sessions = tuple(sessions)
    settle_before = time.time() - settle_hours * 3600
    plan = Plan(
        version=PLAN_VERSION,
        created_at=now.isoformat(),
        local_root=local_root,
        archive_root=archive_root,
        start=start.isoformat(),
        end=end.isoformat(),
        stations=sorted(stations) if stations else None,
        sessions=list(sessions),
        deep=deep,
    )
    catalog = load_catalog(conn, start, end, stations)
    horizons = load_horizons(conn)
    gone = load_not_on_receiver(conn)

    found: dict[str, ParsedArchivePath] = {}
    for root in (archive_root, local_root):
        for rel, parsed in iter_raw_files(
            root, start, end, stations=stations, sessions=sessions
        ):
            if parsed.file_date is not None:
                found.setdefault(rel, parsed)

    stats: dict = {
        "files": len(found),
        "probed": 0,
        "skipped_not_gz": 0,
        "skipped_settling": 0,
        "uncataloged_complete": 0,
    }
    for rel in sorted(found):
        parsed = found[rel]
        assert parsed.file_date is not None  # filtered above
        name = rel.rsplit("/", 1)[-1]
        key = (parsed.station, parsed.session_type, canonical_key(name))
        cat = catalog.get(key)
        paths = {
            "archive": os.path.join(archive_root, rel),
            "local": os.path.join(local_root, rel),
        }
        if not any(
            _needs_probe(p, cat, deep) for p in paths.values() if os.path.isfile(p)
        ):
            continue
        if not name.endswith(".gz"):
            stats["skipped_not_gz"] += 1
            continue
        mtimes = [os.path.getmtime(p) for p in paths.values() if os.path.isfile(p)]
        if mtimes and max(mtimes) > settle_before:
            stats["skipped_settling"] += 1  # may still be in flight
            continue

        stats["probed"] += 1
        copies: dict[str, Optional[GzipProbe]] = {
            k: (probe_gzip(p) if os.path.isfile(p) else None) for k, p in paths.items()
        }
        if not any(c and c.status in BROKEN for c in copies.values()):
            if cat is None:
                stats["uncataloged_complete"] += 1
            continue

        recv = receiver_state(
            rel=rel,
            station=parsed.station,
            session=parsed.session_type,
            file_date=parsed.file_date,
            file_hour=parsed.file_hour,
            horizons=horizons,
            not_on_receiver=gone,
            now=now,
            max_age=timedelta(hours=horizon_max_age_hours),
        )
        # The broken copy with the most readable bytes; archive wins a tie.
        probed = {k: v for k, v in copies.items() if v is not None}
        broken = [
            k
            for k in ("archive", "local")
            if k in probed and probed[k].status in BROKEN
        ]
        salv_src = (
            max(broken, key=lambda k: probed[k].readable_bytes) if broken else None
        )
        salv = salvage(paths[salv_src]) if salv_src else None
        decision: Decision = decide(
            name=name,
            copies=copies,
            catalog_content_sha256=cat["content_sha256"] if cat else None,
            receiver=ReceiverState(recv["state"]),
            salvage_source=salv_src if salv else None,
            salvage_blocks=salv.blocks if salv else 0,
        )
        plan.items.append(
            Item(
                rel=rel,
                station=parsed.station,
                session_type=parsed.session_type,
                file_date=parsed.file_date.isoformat(),
                file_hour=parsed.file_hour,
                copies={k: (v.to_dict() if v else None) for k, v in copies.items()},
                catalog=cat,
                receiver=recv,
                salvage=({**salv.summary(), "source": salv_src} if salv else None),
                decision=decision.to_dict(),
            )
        )

    counts: dict[str, int] = {}
    for it in plan.items:
        counts[it.action] = counts.get(it.action, 0) + 1
    stats["actions"] = counts
    plan.stats = stats
    return plan


def format_plan(plan: Plan) -> str:
    """Human-readable plan summary, one line per item."""
    lines = []
    for it in plan.items:
        c = {
            k: (GzipStatus(v["status"]).value if v else "-")
            for k, v in it.copies.items()
        }
        extra = ""
        if it.salvage and it.action == "salvage":
            s = it.salvage
            pct = 100 * s["kept_bytes"] / max(1, s["source_readable_bytes"])
            extra = f" keep={s['kept_bytes']:,}B/{s['blocks']} blocks ({pct:.1f}% of readable)"
        lines.append(
            f"{it.action.upper():<13} {it.rel}  archive={c['archive']} local={c['local']} "
            f"receiver={it.receiver['state']}({it.receiver['source']}){extra}\n"
            f"{'':14}{it.decision['reason']}"
        )
    return "\n".join(lines)
