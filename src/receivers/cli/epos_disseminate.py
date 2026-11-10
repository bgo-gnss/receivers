"""``receivers epos-disseminate`` — EPOS RINEX3 long-name dissemination.

Drives the whole phase-1 chain for ``--station``/``--date``: convert the archived
RINEX (or raw) to a RINEX 3.04 long-name file, QC the header against TOS, push to
the dissemination target, and record the pushed file in gps_health
(``archive_catalog``, ``epos_portal``). Also: ``--list-stations`` (TOS EPOS filter).
Dry-run-safe; the target is gated ``active: false`` in sync.yaml.
See docs/architecture/epos-dissemination-plan.md.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("receivers.cli.epos_disseminate")


def _result_dict(r) -> dict:
    return {
        "station": r.station,
        "file_date": r.file_date.isoformat(),
        "ok": r.ok,
        "dry_run": r.dry_run,
        "source_path": r.source_path,
        "long_name": r.long_name,
        "cached": r.cached,
        "pushed": r.pushed,
        "artifact_path": r.artifact_path,
        "qc_passed": r.qc_passed,
        "qc_message": r.qc_message,
        "dest": r.dest,
        "message": r.message,
        "errors": r.errors,
    }


def _resolve_cli_date(value: str, role: str, args: argparse.Namespace, target):
    """Resolve a ``--start``/``--end``/``--date`` value to a ``date``.

    Accepts a literal (``YYYY-MM-DD`` / ``YYYYMMDD``) or a symbolic token
    (``first``, ``last``, ``device_start``, ``device_end``, ``today``,
    ``yesterday``) with an optional ``±N`` day offset. ``first``/``last`` read the
    archive extent for ``--station``; ``device_*`` read the TOS session for
    ``--device SN``. Raises :class:`~receivers.utils.date_vocab.DateVocabError`
    (a ``ValueError``) with an actionable message on anything unresolvable.
    """
    from ..dissemination.engine import archive_date_bounds
    from ..dissemination.tos_access import TOSSesionCache, device_session_bounds
    from ..utils.date_vocab import DateContext, resolve_date

    station = (args.station or "").upper() or None
    hist_cache: dict[str, list] = {}

    def _archive():
        return archive_date_bounds(target.source_root, target.sessions, station)

    def _device(serial: str):
        if "hist" not in hist_cache:
            meta = TOSSesionCache().get_metadata(station) or {}
            hist_cache["hist"] = meta.get("device_history", []) or []
        return device_session_bounds(hist_cache["hist"], serial)

    ctx = DateContext(
        role=role,
        today=datetime.now(UTC).date(),
        station=station,
        device_serial=getattr(args, "device", None),
        archive_bounds=_archive if station else None,
        device_bounds=_device if station else None,
    )
    return resolve_date(value, ctx)


def cmd_epos_disseminate(args: argparse.Namespace) -> int:
    from ..dissemination import EposDisseminate, load_dissemination_config

    config_path = Path(args.config) if args.config else None
    targets = load_dissemination_config(config_path)
    if args.target:
        targets = [t for t in targets if t.name == args.target]
    if not targets:
        print(
            "No dissemination targets configured (sync.yaml has no tier:dissemination)."
        )
        return 1
    target = targets[0]

    # --list-stations: print the EPOS-eligible set from TOS and exit.
    if args.list_stations:
        from ..dissemination import epos_markers

        markers = epos_markers()
        if args.json:
            print(json.dumps(markers, indent=2))
        else:
            print(f"{len(markers)} EPOS-eligible stations:")
            print(" ".join(markers))
        return 0

    # --reactive: TOS-fingerprint diff sweep — re-disseminate / stop the
    # stations whose TOS metadata or EPOS eligibility changed since the last run.
    if args.reactive:
        from ..dissemination.job import run_epos_reactive_job

        summary = run_epos_reactive_job(
            config_path=args.config,
            target_name=args.target,
            backfill_days=args.reactive_backfill_days,
            no_qc=args.no_qc,
            state_path=args.state_path,
            sitelogs_dir=args.sitelog_dir,
        )
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            print(
                "reactive sweep: "
                f"new={summary['new']} changed={summary['changed']} "
                f"activated={summary['activated']} deactivated={summary['deactivated']} "
                f"unchanged={summary['unchanged']} failed={summary['failed']}"
            )
        return 0 if not summary.get("failed") else 1

    # --sitelog / --publish-m3g: change-gated site log (+ optional M3G publish).
    # The gate renders current TOS and writes a new dated log ONLY when the
    # station content changed vs the latest committed one — so this verb is safe
    # to "run on change": unchanged ⇒ no-op (no write, no commit, no M3G).
    if args.sitelog or args.publish_m3g:
        if not args.station:
            print("--sitelog/--publish-m3g requires --station")
            return 1
        from ..dissemination.sitelogs import (
            commit_site_log,
            generate_site_log,
            generate_site_log_if_changed,
            resolve_sitelogs_repo,
            submit_to_m3g,
        )

        out_dir = resolve_sitelogs_repo(args.sitelog_dir)

        if args.sitelog_plain:
            # Plain (undated RHOF00ISL.log) is a manual one-off — always written,
            # not part of the dated series the gate compares.
            path = generate_site_log(
                args.station,
                out_dir,
                country_code=target.format.country_code,
                monument_number=target.format.monument_number,
                include_date=False,
            )
            if path is None:
                print(f"Site log generation failed for {args.station} (see log).")
                return 1
            print(f"✅ site log: {path}")
            return 0

        gate = generate_site_log_if_changed(
            args.station,
            out_dir,
            country_code=target.format.country_code,
            monument_number=target.format.monument_number,
        )
        if gate is None:
            print(f"Site log generation failed for {args.station} (see log).")
            return 1
        if gate.path is None:
            print(f"Site log write failed for {args.station} (see log).")
            return 1
        path = gate.path

        # --dry-run makes this a pure preview: the log is still rendered (needed
        # to show the diff and to validate against M3G), but nothing persistent
        # or outward happens — no git commit, no M3G PUT. Without it, --sitelog
        # commits a changed log and --publish-m3g publishes it.
        preview = bool(args.dry_run)

        # The gate governs REGENERATION (write only on change). A manual
        # --publish-m3g is explicit intent, so it publishes the CURRENT log to M3G
        # even when this run didn't change it (else a log committed on an earlier
        # run could never be published).
        if gate.changed:
            if preview:
                print(f"✅ site log WOULD update: {path}  (--dry-run: not committed)")
            else:
                print(f"✅ site log updated: {path}")
                committed = commit_site_log(
                    out_dir, path, f"sitelog: {args.station} update {path.name}"
                )
                print(
                    "   committed to gps-sitelogs"
                    if committed
                    else "   commit: nothing to commit"
                )
                # Push immediately. A commit that is never pushed leaves the
                # repo behind while the log itself goes live on M3G (which
                # reads the LOCAL file), and nothing downstream notices — 67
                # such commits had piled up by 2026-09-01. Best-effort: a push
                # failure is reported, never fatal, because the commit is
                # already safe locally and the fix needs an operator.
                if committed and not getattr(args, "no_sitelog_push", False):
                    from ..dissemination.sitelogs import push_site_logs

                    ok, detail = push_site_logs(out_dir)
                    print(
                        f"   pushed to origin ({detail})"
                        if ok
                        else f"   ⚠ push FAILED — commit is local only: {detail}"
                    )
        else:
            print(f"✅ site log unchanged for {args.station} ({path.name})")

        if not args.publish_m3g:
            return 0
        # --dry-run → validate-only (no PUT). Same verb, so you don't have to
        # switch to `receivers m3g submit` just to preview M3G validation.
        dry_run = preview
        print(
            f"→ {'validating' if dry_run else 'publishing'} {path.name} "
            f"{'against' if dry_run else 'to'} M3G"
        )

        # --publish-m3g: validate + publish to M3G (the API publishes directly).
        from ..dissemination.m3g_client import M3GError

        # Reuse the standalone m3g submit flow but feed the just-written path.
        try:
            result = submit_to_m3g(
                args.station,
                site_log_path=path,
                network=args.m3g_network,
                country_code=target.format.country_code,
                monument_number=target.format.monument_number,
                dry_run=dry_run,
                endpoint=args.m3g_endpoint,
            )
        except M3GError as exc:
            print(f"❌ M3G: {exc}")
            return 1

        from .m3g import _print_validation

        if result.validation is not None:
            _print_validation(result.validation)
            if not result.validated:
                print("\n⚠️  publish skipped — fix the validation errors above first.")
                return 1

        ur = result.upload
        if ur is None:
            # validate-only (--dry-run) or a skip — no PUT was sent.
            if result.skipped:
                print(f"   ⚠ skipped: {result.skipped}")
            elif dry_run:
                print(
                    f"\n✅ DRY RUN — {path.name} validated, NOT published for "
                    f"{args.station}. Drop --dry-run to publish."
                )
            return 0 if result.validated else 1
        if getattr(ur, "dry_run", False):
            print(
                f"\n✅ DRY RUN — {path.name} validated, NOT published for "
                f"{args.station}. Drop --dry-run to publish."
            )
            return 0
        if ur.ok:
            print(f"\n✅ M3G PUBLISHED for {args.station}.")
            if ur.sitelog_name:
                print(f"   filename: {ur.sitelog_name}")
            print(f"   🔔 review + alerts: {ur.draft_url}")
            return 0
        print(f"\n❌ M3G publish failed: {ur.error or 'HTTP ' + str(ur.status_code)}")
        return 1

    # ---- range mode (--start/--end or --dates): the sweep driver ---------
    if args.start or args.end or args.dates:
        if args.date:
            print("--date cannot be combined with --start/--end/--dates")
            return 1
        if not target.active and not args.force:
            print(
                f"Target {target.name!r} is inactive — pass --force for a "
                "pre-stage run (use --dest-override to a staging path)."
            )
            return 1
        try:
            if args.dates:
                from ..utils.time_utils import parse_dates_arg

                run_dates = sorted(parse_dates_arg(args.dates))
            else:
                if not (args.start and args.end):
                    print("range mode needs BOTH --start and --end (or --dates)")
                    return 1
                d0 = _resolve_cli_date(args.start, "start", args, target)
                d1 = _resolve_cli_date(args.end, "end", args, target)
                if d1 < d0:
                    print(f"--end ({d1}) before --start ({d0})")
                    return 1
                run_dates = [d0 + timedelta(days=n) for n in range((d1 - d0).days + 1)]
        except (ValueError, OSError) as exc:
            print(f"invalid dates: {exc}")
            return 1

        from ..dissemination.job import run_epos_disseminate_job

        # Cached TOS access shared across EVERY date/chunk of the run: the QC
        # session provider (TOSSesionCache → 1 metadata fetch/station) and the
        # set-header corrector (tos_metadata_cache → 1 fetch/station). Without these
        # a full-history range re-hit TOS twice PER FILE (thousands of round-trips,
        # and one transient VPN/TOS blip failed a file).
        session_provider = None
        if not args.no_qc:
            from ..dissemination.tos_access import TOSSesionCache

            session_provider = TOSSesionCache().get_session
        tos_meta_cache: dict = {}

        def engine_factory(tgt):
            return EposDisseminate(
                tgt,
                dry_run=args.dry_run,
                dest_override=args.dest_override,
                session_provider=session_provider,
                tos_metadata_cache=tos_meta_cache,
            )

        print(
            f"Range dissemination: {args.station or 'allowlist sweep'} — "
            f"{len(run_dates)} date(s) "
            f"{run_dates[0]}..{run_dates[-1]}" + (" [dry-run]" if args.dry_run else "")
        )
        summary = run_epos_disseminate_job(
            config_path=args.config,
            target_name=args.target,
            no_qc=args.no_qc,
            markers=[args.station.upper()] if args.station else None,
            dates=run_dates,
            parallel=args.parallel,
            force=args.force,
            engine_factory=engine_factory,
        )
        icon = "✅" if summary["failed"] == 0 else "⚠️ "
        print(
            f"{icon} range summary: pushed={summary['pushed']} "
            f"cached={summary['cached']} skipped={summary['skipped']} "
            f"failed={summary['failed']}"
        )
        if args.json:
            print(json.dumps(summary, indent=2))
        return 0 if summary["failed"] == 0 else 1

    if not args.station or not args.date:
        print("--station and --date are required (or use --list-stations)")
        return 1

    if not target.active and not args.force:
        print(
            f"Target {target.name!r} is inactive — pass --force for a pre-stage run "
            f"(use --dest-override to a staging path)."
        )
        return 1

    try:
        d = _resolve_cli_date(args.date, "date", args, target)
    except ValueError as exc:
        print(f"Invalid --date {args.date!r}: {exc}")
        return 1

    # Multi-product config (format.products with >1 entry): the sweep driver
    # owns the per-product loop + catalog write — reroute this
    # single date through it rather than duplicating that logic here.
    _prods = target.format.active_products()
    if len(_prods) > 1:
        from ..dissemination.job import run_epos_disseminate_job

        session_provider2 = None
        if not args.no_qc:
            from ..dissemination.tos_access import TOSSesionCache

            session_provider2 = TOSSesionCache().get_session
        tos_meta_cache2: dict = {}

        def _ef(tgt):
            return EposDisseminate(
                tgt,
                dry_run=args.dry_run,
                dest_override=args.dest_override,
                session_provider=session_provider2,
                tos_metadata_cache=tos_meta_cache2,
            )

        print(f"{len(_prods)} products configured — using the sweep driver")
        summary = run_epos_disseminate_job(
            config_path=args.config,
            target_name=args.target,
            no_qc=args.no_qc,
            markers=[args.station.upper()],
            dates=[d],
            force=args.force,
            engine_factory=_ef,
        )
        icon = "✅" if summary["failed"] == 0 else "⚠️ "
        print(
            f"{icon} pushed={summary['pushed']} cached={summary['cached']} "
            f"skipped={summary['skipped']} failed={summary['failed']}"
        )
        return 0 if summary["failed"] == 0 else 1

    # The header-QC gate runs by default (live TOS session provider); --no-qc
    # disables it (e.g. an offline pre-stage where TOS is unreachable).
    session_provider = None
    if not args.no_qc:
        from ..dissemination import make_session_provider

        session_provider = make_session_provider()

    engine = EposDisseminate(
        target,
        dry_run=args.dry_run,
        dest_override=args.dest_override,
        session_provider=session_provider,
    )
    result = engine.run_one(args.station, d)

    # Record the pushed file in the file index (archive_catalog, epos_portal).
    # Best-effort, and only for a real push: a --dest-override pre-stage is not on
    # the portal, and _record_pushed itself skips a dry run.
    if result.ok and not args.dry_run and not args.dest_override:
        from ..dissemination.job import _close_catalog_conn, _record_pushed

        _record_pushed(target, result)
        _close_catalog_conn()

    if args.json:
        print(json.dumps(_result_dict(result), indent=2))
    else:
        icon = "✅" if result.ok else "❌"
        tag = " [dry-run]" if result.dry_run else ""
        print(f"{icon} {result.station} {result.file_date}{tag}: {result.message}")
        if result.source_path:
            print(f"   source={result.source_path}")
        if result.qc_passed is not None:
            print(
                f"   qc={'pass' if result.qc_passed else 'FAIL'} ({result.qc_message})"
            )
        if result.dest:
            print(f"   dest={result.dest}")
        for err in result.errors:
            print(f"   ⚠ {err}")
    return 0 if result.ok else 1


def create_epos_disseminate_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "epos-disseminate",
        help="Convert archived RINEX to RINEX3 long names and push to EPOS (T1)",
        description=(
            "EPOS dissemination (phase 1, tracer bullet): convert one archived "
            "RINEX for --station/--date to RINEX 3.04 with a long IGS name and push "
            "it to the tier:dissemination target. Gated active:false in sync.yaml."
        ),
    )
    parser.add_argument("--station", help="4-char station id (e.g. FIM2)")
    parser.add_argument(
        "--date",
        metavar="DATE|TOKEN",
        help="Observation date: YYYY-MM-DD / YYYYMMDD, or a token "
        "(today, yesterday, first, last, device_start, device_end) "
        "with an optional ±N day offset (e.g. yesterday-1).",
    )
    parser.add_argument(
        "--start",
        metavar="DATE|TOKEN",
        help="Range mode: first observation date (inclusive). Accepts a literal "
        "date or a token — 'first' = the earliest ARCHIVED day for --station, "
        "'device_start' = the TOS session start for --device SN — with an "
        "optional ±N offset (first+7). The full-history portal refresh after a "
        "re-rinex campaign is simply --start first --end last. Uses the sweep "
        "driver: engine reuse, batched push, optional --parallel.",
    )
    parser.add_argument(
        "--end",
        metavar="DATE|TOKEN",
        help="Range mode: last date (inclusive). Literal or token — 'last' = the "
        "latest ARCHIVED day for --station, 'device_end' = the TOS session end "
        "for --device SN (open session ⇒ last archived day) — with an optional "
        "±N offset (last-1).",
    )
    parser.add_argument(
        "--device",
        metavar="SN",
        help="Receiver serial for the device_start / device_end date tokens "
        "(the TOS session bounds of that specific receiver).",
    )
    parser.add_argument(
        "--dates",
        metavar="YYYYMMDD[,..]|@FILE",
        help="Range mode: disseminate exactly these dates (comma-separated or "
        "@file, one per line) — e.g. an archive-audit regen list.",
    )
    parser.add_argument(
        "--parallel",
        nargs="?",
        const="auto",
        default=None,
        metavar="N",
        help="Range mode: (station, year) chunks on a load-aware thread pool; "
        "each chunk gets its own engine.",
    )
    parser.add_argument(
        "--list-stations",
        action="store_true",
        help="List EPOS-eligible stations from TOS (in_network_epos + min attrs) and exit",
    )
    parser.add_argument(
        "--sitelog",
        action="store_true",
        help="Generate the IGS/M3G site log for --station from TOS (C6/T7)",
    )
    parser.add_argument(
        "--sitelog-dir",
        help="Output directory for --sitelog (default: the gps-sitelogs repo from "
        "receivers.cfg [paths] sitelogs_repo, else ~/git/gps-sitelogs)",
    )
    parser.add_argument(
        "--sitelog-plain",
        action="store_true",
        help="Write the plain <9CHAR>.log instead of the default dated M3G form "
        "(<9char>_<YYYYMMDD>.log with §0 Previous Site Log chaining)",
    )
    parser.add_argument(
        "--no-sitelog-push",
        action="store_true",
        help="Commit a regenerated site log but do NOT push it to origin. Default "
        "is to push: an unpushed commit leaves gps-sitelogs behind while the log "
        "is already live on M3G (which reads the local file), and nothing "
        "downstream notices.",
    )
    parser.add_argument(
        "--publish-m3g",
        action="store_true",
        help="After --sitelog, validate + PUBLISH the site log to M3G. The M3G API "
        "publishes directly (no draft state). Implies --sitelog. Validation runs "
        "first and blocks on errors. Endpoint/token from --m3g-endpoint / [m3g].",
    )
    parser.add_argument(
        "--m3g-endpoint",
        help="M3G endpoint URL or alias (prod/test). Used with --publish-m3g.",
    )
    parser.add_argument(
        "--m3g-network",
        default="EPOS",
        help="M3G network short name for validation (default: EPOS)",
    )
    parser.add_argument(
        "--reactive",
        action="store_true",
        help="Run the reactive TOS-fingerprint sweep: re-disseminate / "
        "stop the stations whose TOS metadata or EPOS eligibility changed (T6)",
    )
    parser.add_argument(
        "--reactive-backfill-days",
        type=int,
        default=365,
        help="Backfill window (days) re-disseminated for a changed/activated "
        "station in --reactive mode (default 365; cache makes re-runs cheap)",
    )
    parser.add_argument(
        "--state-path",
        help="Path to the reactive fingerprint store JSON "
        "(default: ~/.cache/gps_receivers/epos_reactive_state.json)",
    )
    parser.add_argument(
        "--no-qc",
        action="store_true",
        help="Skip the header-QC gate (e.g. offline pre-stage with TOS unreachable)",
    )
    parser.add_argument(
        "--target", help="Dissemination target name (default: first in sync.yaml)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview mode, no persistent/outward writes. Range mode: convert + "
        "rsync --dry-run (no file written to dest). --sitelog: render but do not "
        "commit. --publish-m3g: validate only, no PUT (same as `m3g submit` "
        "without --publish).",
    )
    parser.add_argument(
        "--dest-override",
        help="Override the target dest (e.g. /tmp/epos_stage for pre-stage)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if the target is inactive (pre-stage; use --dest-override)",
    )
    parser.add_argument(
        "--config", help="Path to sync.yaml (default: GPS_CONFIG_PATH/sync.yaml)"
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON result"
    )
    parser.set_defaults(func=cmd_epos_disseminate)
    return parser
