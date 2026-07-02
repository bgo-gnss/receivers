"""``receivers m3g`` — M3G (gnss-metadata.eu) site-log submission.

Exposes the M3G submission step (EPOS §3.2) as a standalone verb:

- ``receivers m3g submit --station RHOF``  → validate + upload as a **draft**
- ``receivers m3g validate --station RHOF`` → validate only (no upload, no token)
- ``receivers m3g diff --station RHOF``     → diff the local site log vs the live M3G draft

**Publishing is manual by design**: M3G exposes no API endpoint to publish a
draft (the "Submit saved draft for publication" button is web-UI-only). This
verb only ever creates or updates a draft; final publication stays an operator
click in the M3G portal. After a successful upload the CLI prints the web-UI
draft URL — the yellow post-upload "Alert(s)" banners (e.g. "Please check the
'Identification' section") are only visible there, not via the API.

See docs/architecture/epos-dissemination-plan.md (C6/T7).
"""

from __future__ import annotations

import argparse
import difflib
import logging
import sys
from pathlib import Path

logger = logging.getLogger("receivers.cli.m3g")


def _nine_char(station: str, country_code: str = "ISL", monument: str = "00") -> str:
    return f"{station.upper()}{monument}{country_code.upper()}"


def _print_validation(vr) -> None:
    """Render a ValidationResult to stdout for the operator."""
    if vr.ok:
        print(f"✅ validation OK (network={vr.network}, HTTP {vr.status_code})")
        return
    print(f"❌ validation FAILED (network={vr.network}, HTTP {vr.status_code})")
    errs = getattr(vr, "errors", []) or []
    warns = getattr(vr, "warnings", []) or []
    if errs:
        print(f"   {len(errs)} error(s):")
        for m in errs:
            f = m.get("field", "")
            msg = m.get("message", "")
            print(f"      • {f}: {msg}" if f else f"      • {msg}")
    if warns:
        print(f"   {len(warns)} warning(s):")
        for m in warns:
            f = m.get("field", "")
            msg = m.get("message", "")
            print(f"      • {f}: {msg}" if f else f"      • {msg}")
    if not errs and not warns:
        # 422 with no parseable messages — show the raw body for debugging.
        raw = getattr(vr, "raw", None)
        if raw is not None:
            print(f"   raw response: {str(raw)[:500]}")


def cmd_m3g_validate(args: argparse.Namespace) -> int:
    """Validate a locally generated site log against M3G network rules (no token)."""
    from ..dissemination.m3g_client import M3GClient, M3GError
    from ..dissemination.sitelogs import generate_site_log, resolve_sitelogs_repo

    sid = args.station.upper()
    content: str
    src: str
    if args.file:
        content = Path(args.file).read_text(encoding="utf-8")
        src = args.file
    else:
        out_dir = (
            Path(args.sitelog_dir) if args.sitelog_dir else resolve_sitelogs_repo()
        )
        path = generate_site_log(sid, out_dir)
        if path is None:
            print(f"Site log generation failed for {sid} (see log).")
            return 1
        content = path.read_text(encoding="utf-8")
        src = str(path)

    print(
        f"validating {sid} ({len(content)} bytes, src={src}) against M3G/{args.network}…"
    )
    client = M3GClient(endpoint=args.m3g_endpoint)
    try:
        vr = client.validate_sitelog(content, network=args.network)
    except M3GError as exc:
        print(f"❌ validate: {exc}")
        return 1
    _print_validation(vr)
    return 0 if vr.ok else 1


def cmd_m3g_submit(args: argparse.Namespace) -> int:
    """Validate + upload a site log to M3G as a draft (publishing is manual)."""
    from ..dissemination.m3g_client import M3GError
    from ..dissemination.sitelogs import submit_to_m3g

    sid = args.station.upper()
    dry_run = not args.submit
    site_log_path = Path(args.file) if args.file else None

    action = (
        "DRY RUN (validate only)" if dry_run else "SUBMIT (validate + upload draft)"
    )
    print(f"M3G {action} for {sid} (endpoint resolved from --m3g-endpoint/config)…")

    try:
        result = submit_to_m3g(
            sid,
            site_log_path=site_log_path,
            out_dir=Path(args.sitelog_dir) if args.sitelog_dir else None,
            network=args.network,
            country_code=args.country_code,
            monument_number=args.monument_number,
            dry_run=dry_run,
            endpoint=args.m3g_endpoint,
            skip_validation=args.skip_validation,
        )
    except M3GError as exc:
        print(f"❌ {exc}")
        return 1

    # 1. Validation phase
    if result.validation is not None:
        _print_validation(result.validation)
        if not result.validated:
            print("\n⚠️  upload skipped — fix the validation errors above first.")
            return 1
    elif args.skip_validation:
        print("   (validation skipped via --skip-validation)")

    # 2. Upload phase
    ur = result.upload
    if ur is None:
        # validate-only path or a skip (e.g. generation failed)
        if result.skipped:
            print(f"   ⚠ skipped: {result.skipped}")
        return 0 if result.validated else 1

    if ur.dry_run:
        print(f"\n✅ DRY RUN complete — draft NOT uploaded for {sid}.")
        print("   Pass --submit to validate + upload as a draft.")
        return 0

    if not ur.ok:
        print(
            f"\n❌ upload FAILED for {sid}: {ur.error or 'HTTP ' + str(ur.status_code)}"
        )
        return 1

    print(f"\n✅ draft uploaded for {sid} (HTTP {ur.status_code}).")
    if ur.md5_sitelog:
        print(f"   md5:      {ur.md5_sitelog}")
    if ur.sitelog_name:
        print(f"   filename: {ur.sitelog_name}")
    if ur.date_update:
        print(f"   updated:  {ur.date_update}")

    # The critical handoff: post-upload alerts (yellow banners) are web-UI-only.
    print("\n   🔔 Review the draft + post-upload alerts (not available via API):")
    print(f"      {ur.draft_url}")
    print(
        "   Then click 'Submit saved draft for publication' in the M3G portal to publish."
    )
    return 0


def cmd_m3g_diff(args: argparse.Namespace) -> int:
    """Diff the locally generated site log against the live M3G draft."""
    from ..dissemination.m3g_client import M3GClient
    from ..dissemination.sitelogs import generate_site_log, resolve_sitelogs_repo

    sid = args.station.upper()
    if args.file:
        local = Path(args.file).read_text(encoding="utf-8")
    else:
        out_dir = (
            Path(args.sitelog_dir) if args.sitelog_dir else resolve_sitelogs_repo()
        )
        path = generate_site_log(sid, out_dir)
        if path is None:
            print(f"Site log generation failed for {sid} (see log).")
            return 1
        local = path.read_text(encoding="utf-8")

    client = M3GClient(endpoint=args.m3g_endpoint)
    remote = client.view_sitelog(sid)
    if remote is None:
        print(f"❌ no live M3G site log for {sid} (station may not exist yet).")
        return 1

    local_lines = local.splitlines(keepends=True)
    remote_lines = remote.splitlines(keepends=True)
    diff = difflib.unified_diff(
        remote_lines,
        local_lines,
        fromfile=f"m3g:{sid}",
        tofile=f"local:{sid}",
    )
    out = "".join(diff)
    if not out:
        print(f"✅ {sid}: local site log is identical to the live M3G draft.")
        return 0
    sys.stdout.write(out)
    return 0


def create_m3g_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "m3g",
        help="M3G site-log submission (validate / upload draft / diff).",
        description=(
            "M3G (gnss-metadata.eu) site-log submission. Uploads save a DRAFT "
            "only — publishing is a manual web-UI step (no M3G API endpoint "
            "exists for publication). The post-upload 'Alert(s)' banners are "
            "web-UI-only; the CLI prints the draft URL to review them."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  receivers m3g validate --station RHOF          # check against EPOS rules\n"
            "  receivers m3g submit --station RHOF           # dry run: validate only\n"
            "  receivers m3g submit --station RHOF --submit   # validate + upload draft\n"
            "  receivers m3g submit --station RHOF --submit --m3g-endpoint test\n"
            "  receivers m3g diff --station RHOF              # local vs live M3G draft\n"
        ),
    )
    m3g_sub = parser.add_subparsers(
        dest="m3g_command", title="m3g subcommands", description="Available m3g actions"
    )

    # Common args reused across subcommands
    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--station", required=True, help="4-char station id (e.g. RHOF)")
        p.add_argument(
            "--file",
            help="Use this existing site log file instead of rendering from TOS",
        )
        p.add_argument(
            "--sitelog-dir",
            help="Output dir when rendering (default: gps-sitelogs repo)",
        )
        p.add_argument(
            "--m3g-endpoint", help="M3G endpoint URL or alias: prod (default) / test"
        )
        p.add_argument(
            "--network", default="EPOS", help="M3G network short name (default: EPOS)"
        )

    # validate
    p_val = m3g_sub.add_parser(
        "validate",
        help="Validate a site log against M3G network rules (no token, no upload)",
    )
    add_common(p_val)
    p_val.set_defaults(func=cmd_m3g_validate)

    # submit
    p_sub = m3g_sub.add_parser(
        "submit", help="Validate + upload a site log as an M3G draft"
    )
    add_common(p_sub)
    p_sub.add_argument(
        "--submit",
        action="store_true",
        help="Actually upload the draft (default: dry run / validate only)",
    )
    p_sub.add_argument(
        "--country-code", default="ISL", help="Country code (default: ISL)"
    )
    p_sub.add_argument(
        "--monument-number", default="00", help="Monument number (default: 00)"
    )
    p_sub.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip the pre-upload validate step",
    )
    p_sub.set_defaults(func=cmd_m3g_submit)

    # diff
    p_diff = m3g_sub.add_parser(
        "diff", help="Diff the local site log vs the live M3G draft"
    )
    add_common(p_diff)
    p_diff.set_defaults(func=cmd_m3g_diff)

    return parser
