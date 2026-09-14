#!/usr/bin/env python3
"""Ask the receivers whether terminally-absent files are REALLY absent.

This is the "validate" half of #174/S2. Flipping `use_terminal_absence` makes a
terminal `file_absence` row a PERMANENT skip, in the ordinary backfill as well
as in LTB. Migration 059 refused to honour terminal rows precisely because a
wrong one is silent data loss: a bad `receiver_base_path` 404s every file, the
station goes terminal after 3 days, and the data is never fetched again even
once the config is fixed. Migrations 061 (served-gate) and 067 (fleet-health
gate) were written to earn that trust back.

Database-only checks can show the gate is *plausible* — on rek-d01 2026-09-14
they showed zero terminal rows contradicted by our own archive, and zero
stations 100 % terminal. They cannot show a terminal row is *true*. Only the
receiver can. This script asks it.

SAFETY — read this before running:

* **Read-only.** One FTP `NLST` of one day-directory per sample. No writes, no
  downloads, no deletes.
* **Aborts on the first login failure.** A PolaRX5 that rejects logins will
  lock out GLOBALLY for ~9 h if hammered, and that lockout counts down 1:1
  (see the 2026 firmware-upgrade incident). One failure means stop, not retry.
* **Sequential, one connection per station**, with a small sample by default.
  The scheduler already logs into these same receivers every hour with the same
  credentials, so a handful of extra read-only sessions is not new load — but
  do not turn this into a fleet sweep.
* Septentrio only. Trimble NetRS has no HTTP `directory` verb, and the Leica
  path handling differs; both are out of scope here.

Interpreting the result:

    ABSENT  — the receiver does not have it. The terminal row is TRUE.
    PRESENT — the receiver DOES have it. The terminal row is a FALSE positive
              and `use_terminal_absence` MUST NOT be flipped until explained.
    unknown — the day-directory itself is missing/unlistable. Not evidence
              either way; the directory may have aged out entirely.

A single PRESENT is disqualifying. That is the whole point of running this.
"""

from __future__ import annotations

import argparse
import sys
from ftplib import FTP


def _sample(limit, session, within_horizon_only):
    """Terminal absences to test, newest first (most likely to still exist)."""
    from receivers.health.database_factory import DatabaseConnectionFactory

    horizon_clause = (
        "AND fa.file_date >= (SELECT oldest_date FROM receiver_horizon rh "
        "                     WHERE rh.sid=fa.sid AND rh.session_type=fa.session_type "
        "                     ORDER BY observed_at DESC LIMIT 1)"
        if within_horizon_only
        else ""
    )
    sql = f"""
        SELECT fa.sid, fa.session_type, fa.file_date, fa.file_hour, fa.confirmations
        FROM file_absence fa
        WHERE fa.terminal AND fa.session_type = %s
          {horizon_clause}
        ORDER BY fa.file_date DESC
        LIMIT %s
    """
    with (
        DatabaseConnectionFactory.connection(single_host=True) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, (session, limit))
        return cur.fetchall()


def _probe_station(sid, session, rows, timeout):
    """List the relevant day-dirs on one receiver. Returns list of verdicts.

    Mirrors ``receiver_horizon_probe._probe_septentrio``'s connection setup so
    the path/credential resolution is identical to the code that already talks
    to these receivers daily.
    """
    from receivers.cli.main import create_receiver, get_station_config

    cfg = get_station_config(sid)
    if not cfg:
        return [(sid, d, h, "unknown", "no station config") for _, _, d, h, _ in rows]
    receiver = create_receiver(sid, cfg)
    base = getattr(receiver, "base_path", None)
    session_map = getattr(receiver, "session_map", {}) or {}
    ip = getattr(receiver, "ip_number", None)
    port = getattr(receiver, "ip_port", None)
    mapping = session_map.get(session)
    if not (base and ip and port and mapping):
        return [
            (sid, d, h, "unknown", "unresolved ftp config") for _, _, d, h, _ in rows
        ]

    index = f"{base}{mapping[1]}/"
    out = []
    ftp = FTP()
    try:
        ftp.connect(ip, port, timeout=timeout)
        if getattr(receiver, "ftp_anonymous", True):
            ftp.login("anonymous")
        else:
            ftp.login(
                getattr(receiver, "ftp_username", None) or "anonymous",
                getattr(receiver, "ftp_password", None) or "",
            )
        ftp.set_pasv(getattr(receiver, "pasv", True))
    except Exception as e:  # noqa: BLE001
        # Do NOT retry — see the lockout note in the module docstring.
        raise SystemExit(
            f"\nABORTED: login/connect failed for {sid} ({e}).\n"
            "Not retrying: repeated failed logins lock a PolaRX5 out globally "
            "for ~9 h. Investigate this station before re-running."
        )

    try:
        for _sid, _sess, d, h, conf in rows:
            daydir = f"{index}{d.strftime('%y%j')}/"
            try:
                names = [e.rsplit("/", 1)[-1] for e in ftp.nlst(daydir)]
                names = [n for n in names if n not in (".", "..")]
            except Exception:  # noqa: BLE001
                out.append((sid, d, h, "unknown", "day-dir unlistable/absent"))
                continue
            if not names:
                out.append((sid, d, h, "unknown", "day-dir empty"))
                continue
            # A daily session has one file per day-dir; an hourly one has the
            # hour in the filename. Either way, presence of ANY file for this
            # slot contradicts "terminally absent".
            if h is None:
                hit = names
            else:
                hit = [n for n in names if f"{d.strftime('%Y%m%d')}{h:02d}" in n]
            verdict = "PRESENT" if hit else "ABSENT"
            note = (hit[0] if hit else f"{len(names)} other file(s)") + f" conf={conf}"
            out.append((sid, d, h, verdict, note))
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--session", default="15s_24hr")
    ap.add_argument("--limit", type=int, default=12, help="rows to sample (small!)")
    ap.add_argument("--max-stations", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument(
        "--all-terminal",
        action="store_true",
        help="sample ALL terminal rows, not just those inside the receiver "
        "horizon. The within-horizon ones are the interesting population: "
        "the receiver claims to still hold data from that date.",
    )
    args = ap.parse_args()

    rows = _sample(args.limit, args.session, not args.all_terminal)
    if not rows:
        print("no terminal rows match that filter — nothing to verify")
        return 0

    by_station = {}
    for r in rows:
        by_station.setdefault(r[0], []).append(r)
    stations = list(by_station)[: args.max_stations]

    print(
        f"sampling {sum(len(by_station[s]) for s in stations)} terminal "
        f"{args.session} rows across {len(stations)} station(s): {', '.join(stations)}"
    )
    print("read-only NLST; aborts on the first login failure\n")

    verdicts = []
    for sid in stations:
        verdicts += _probe_station(sid, args.session, by_station[sid], args.timeout)

    for sid, d, h, verdict, note in verdicts:
        hh = "" if h is None else f" h{h:02d}"
        print(f"  {verdict:8s} {sid} {d}{hh}   {note}")

    n_present = sum(1 for v in verdicts if v[3] == "PRESENT")
    n_absent = sum(1 for v in verdicts if v[3] == "ABSENT")
    n_unknown = sum(1 for v in verdicts if v[3] == "unknown")
    print(f"\nPRESENT={n_present}  ABSENT={n_absent}  unknown={n_unknown}")
    if n_present:
        print(
            "\nRESULT: FAIL — a terminally-absent file is still on the receiver.\n"
            "Do NOT set use_terminal_absence=true. A terminal row is supposed to\n"
            "mean 'gone for good'; honouring it here would permanently skip data\n"
            "we can still fetch."
        )
        return 1
    if n_absent == 0:
        print("\nRESULT: INCONCLUSIVE — nothing confirmed either way.")
        return 2
    print(
        f"\nRESULT: PASS — {n_absent} terminal row(s) confirmed genuinely absent "
        "on the receiver, 0 contradicted."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
