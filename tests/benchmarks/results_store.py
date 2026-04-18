"""SQLite storage for download experiment results.

Stores trial metadata, per-station results, and system metric time-series
in ``~/.cache/gps_receivers/experiment_results.db``.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

DEFAULT_DB_PATH = Path.home() / ".cache" / "gps_receivers" / "experiment_results.db"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS trials (
    trial_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    concurrency     INTEGER NOT NULL,
    batches         INTEGER NOT NULL,
    distribution_window REAL NOT NULL,
    wall_clock_seconds  REAL,
    files_downloaded    INTEGER DEFAULT 0,
    stations_successful INTEGER DEFAULT 0,
    stations_unreachable INTEGER DEFAULT 0,
    stations_failed     INTEGER DEFAULT 0,
    retried         INTEGER DEFAULT 0,
    retry_recovered INTEGER DEFAULT 0,
    session         TEXT,
    days_back       INTEGER,
    total_stations  INTEGER DEFAULT 0,
    started_at      TEXT,
    finished_at     TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS system_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id        INTEGER NOT NULL REFERENCES trials(trial_id),
    elapsed_seconds REAL,
    cpu_load_1m     REAL,
    cpu_load_5m     REAL,
    network_mbps    REAL,
    open_connections INTEGER,
    memory_rss_mb   REAL
);

CREATE TABLE IF NOT EXISTS station_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id        INTEGER NOT NULL REFERENCES trials(trial_id),
    station_id      TEXT NOT NULL,
    status          TEXT NOT NULL,
    files_downloaded INTEGER DEFAULT 0,
    duration_seconds REAL DEFAULT 0.0,
    attempt         INTEGER DEFAULT 1,
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_samples_trial ON system_samples(trial_id);
CREATE INDEX IF NOT EXISTS idx_station_results_trial ON station_results(trial_id);
"""


class ResultsStore:
    """Read/write interface to the experiment results SQLite database."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_trial(
        self,
        *,
        concurrency: int,
        batches: int,
        distribution_window: float,
        session: str,
        days_back: int,
        total_stations: int,
        notes: str = "",
    ) -> int:
        """Insert a new trial row, return its trial_id."""
        with self._conn() as conn:
            cur = conn.execute(
                """\
                INSERT INTO trials
                    (concurrency, batches, distribution_window, session,
                     days_back, total_stations, started_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    concurrency,
                    batches,
                    distribution_window,
                    session,
                    days_back,
                    total_stations,
                    datetime.now().isoformat(),
                    notes,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def finish_trial(
        self,
        trial_id: int,
        *,
        wall_clock_seconds: float,
        files_downloaded: int,
        stations_successful: int,
        stations_unreachable: int,
        stations_failed: int,
        retried: int,
        retry_recovered: int,
    ) -> None:
        """Update trial with final results."""
        with self._conn() as conn:
            conn.execute(
                """\
                UPDATE trials SET
                    wall_clock_seconds = ?,
                    files_downloaded = ?,
                    stations_successful = ?,
                    stations_unreachable = ?,
                    stations_failed = ?,
                    retried = ?,
                    retry_recovered = ?,
                    finished_at = ?
                WHERE trial_id = ?
                """,
                (
                    wall_clock_seconds,
                    files_downloaded,
                    stations_successful,
                    stations_unreachable,
                    stations_failed,
                    retried,
                    retry_recovered,
                    datetime.now().isoformat(),
                    trial_id,
                ),
            )

    def insert_system_samples(
        self, trial_id: int, samples: list[dict[str, Any]]
    ) -> None:
        """Bulk-insert system metric samples for a trial."""
        if not samples:
            return
        with self._conn() as conn:
            conn.executemany(
                """\
                INSERT INTO system_samples
                    (trial_id, elapsed_seconds, cpu_load_1m, cpu_load_5m,
                     network_mbps, open_connections, memory_rss_mb)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        trial_id,
                        s["elapsed_seconds"],
                        s.get("cpu_load_1m", 0),
                        s.get("cpu_load_5m", 0),
                        s.get("network_mbps", 0),
                        s.get("open_connections", 0),
                        s.get("memory_rss_mb", 0),
                    )
                    for s in samples
                ],
            )

    def insert_station_results(
        self, trial_id: int, stations: dict[str, dict[str, Any]]
    ) -> None:
        """Bulk-insert per-station results for a trial."""
        if not stations:
            return
        with self._conn() as conn:
            conn.executemany(
                """\
                INSERT INTO station_results
                    (trial_id, station_id, status, files_downloaded,
                     duration_seconds, attempt, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        trial_id,
                        sid,
                        r.get("status", "unknown"),
                        r.get("files_downloaded", 0),
                        r.get("duration", 0.0),
                        r.get("attempt", 1),
                        r.get("error_message"),
                    )
                    for sid, r in stations.items()
                ],
            )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_trials(self) -> list[dict[str, Any]]:
        """Return all trials ordered by started_at."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trials ORDER BY started_at").fetchall()
            return [dict(r) for r in rows]

    def get_trial(self, trial_id: int) -> dict[str, Any] | None:
        """Return a single trial by ID."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM trials WHERE trial_id = ?", (trial_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_system_samples(self, trial_id: int) -> list[dict[str, Any]]:
        """Return system samples for a trial, ordered by time."""
        with self._conn() as conn:
            rows = conn.execute(
                """\
                SELECT * FROM system_samples
                WHERE trial_id = ? ORDER BY elapsed_seconds
                """,
                (trial_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_station_results(self, trial_id: int) -> list[dict[str, Any]]:
        """Return per-station results for a trial."""
        with self._conn() as conn:
            rows = conn.execute(
                """\
                SELECT * FROM station_results
                WHERE trial_id = ? ORDER BY station_id
                """,
                (trial_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_latest_trials(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the N most recent trials."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trials ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
