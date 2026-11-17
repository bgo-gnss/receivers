"""Recovery options for existing archive commands; no scheduler/resume engine."""

from __future__ import annotations

import json
from pathlib import Path

from ..archive.catalog_recovery import (
    CatalogPlan,
    archive_values,
    deadline,
    explicit_hosts,
    live_check,
    open_required,
    production_gateway,
)
from ..archive.staged_manifest import (
    namespace_snapshot,
    preserved_replacements,
    read_staged_manifest,
    require_namespace,
)


def gateway(target):
    return f"{target.user}@{target.host}:{target.dest}" if target.host else target.dest


def archive_owner_uid(target: str, location: str) -> int:
    import os

    if location != "imo_archive":
        return os.getuid()
    production_gateway(location, target)
    from ..archive.access import gateway_request

    result = gateway_request(
        target.split(":", 1)[0],
        {"capabilities_only": True, "execute": False, "production": True},
    )
    uid = result.get("uid")
    if not isinstance(uid, int) or isinstance(uid, bool):
        raise ValueError("gateway owner identity was not established")
    return uid


def failure(exc):
    # Do not print raw driver/connection/SSH exception text or DSNs.
    if isinstance(exc, ValueError):
        return str(exc)
    code = getattr(exc, "pgcode", None)
    if (
        not isinstance(code, str)
        or len(code) != 5
        or not code.isascii()
        or not code.isalnum()
    ):
        code = None
    return f"{type(exc).__name__} SQLSTATE={code}"


def add_catalog_recovery_flags(parser) -> None:
    parser.add_argument(
        "--manifest", help="Exact sha256sum catalog repair subset; no directory walk"
    )
    parser.add_argument(
        "--scope-manifest", help="Full daily RINEX scope for cross-host/MD5/hour guards"
    )
    parser.add_argument("--station", help="Station admitted by recovery manifests")
    parser.add_argument("--session", default="15s_24hr")
    parser.add_argument(
        "--write-plan",
        help="Write a new read-only catalog action snapshot (never overwrite)",
    )
    parser.add_argument("--plan", help="Previously reviewed catalog action snapshot")
    parser.add_argument(
        "--plan-sha256", help="Reviewed snapshot SHA256; mandatory for execution"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute reviewed recovery catalog actions; default preview",
    )
    parser.add_argument(
        "--owner-until",
        help="Actual coordinated owner-window expiry, ISO8601 with timezone",
    )
    parser.add_argument(
        "--publish-manifest",
        help="Optional exact already-staged data replacement SHA256 manifest",
    )
    parser.add_argument(
        "--publish-root", help="Root of already-staged replacement products"
    )
    parser.add_argument(
        "--originals-manifest",
        help="Exact SHA256 manifest of existing protected original copies",
    )
    parser.add_argument(
        "--originals-root", help="Root of existing protected originals, never written"
    )


def cmd_guarded_reindex(args, target, root, hosts) -> int:
    from ..archive.reindex import reindex_files_multi

    try:
        explicit_hosts(hosts, target.name)
        if not getattr(target, "active", True) or target.name != args.storage_location:
            raise ValueError("explicit active archive target required")
        if args.only_existing or bool(args.catalog_prod) == bool(args.catalog_host):
            raise ValueError(
                "recovery requires exactly one catalog target and cannot skip absent rows"
            )
        prefix = args.dest_prefix or target.dest
        production_gateway(target.name, gateway(target))
        if args.write_plan:
            if (
                args.execute
                or args.plan
                or not args.manifest
                or not args.scope_manifest
                or not args.station
            ):
                raise ValueError(
                    "snapshot requires station, scope/selection manifests and preview mode"
                )
            scope = read_staged_manifest(
                args.scope_manifest,
                work_dir=root,
                stations=[args.station.upper()],
                session=args.session,
                validate_files=False,
            )
            selected = read_staged_manifest(
                args.manifest,
                work_dir=root,
                stations=[args.station.upper()],
                session=args.session,
                validate_files=False,
            )
            publication = None
            pub_args = (
                args.publish_manifest,
                args.publish_root,
                args.originals_manifest,
                args.originals_root,
            )
            if any(pub_args):
                if not all(pub_args):
                    raise ValueError(
                        "publication snapshot requires both manifests and separate source/original roots"
                    )
                products = read_staged_manifest(
                    args.publish_manifest,
                    work_dir=args.publish_root,
                    stations=[args.station.upper()],
                    session=args.session,
                )
                originals = read_staged_manifest(
                    args.originals_manifest,
                    work_dir=args.originals_root,
                    stations=[args.station.upper()],
                    session=args.session,
                )
                if not set(products.checksums) <= set(selected.checksums) or any(
                    selected.checksums[r] != h for r, h in products.products
                ):
                    raise ValueError(
                        "publication must be an exact-hash subset of catalog repair selection"
                    )
                pending = preserved_replacements(
                    products,
                    originals,
                    original_root=Path(args.originals_root).expanduser().absolute(),
                    archive_root=Path(root).expanduser().absolute(),
                )
                publication = {
                    "manifest_sha256": products.sha256,
                    "originals_sha256": originals.sha256,
                    "work_root": str(Path(args.publish_root).expanduser().absolute()),
                    "originals_root": str(
                        Path(args.originals_root).expanduser().absolute()
                    ),
                    "archive_namespace": namespace_snapshot(
                        Path(root).expanduser().absolute(),
                        products.checksums,
                        owner_uid=archive_owner_uid(gateway(target), target.name),
                    ),
                }
                print(
                    f"Publication: {len(pending)} baseline replacements, {len(products.products) - len(pending)} already candidates; existing original copies verified; NO raw discovery/QC"
                )
            with open_required(hosts) as conns:
                plan = CatalogPlan.snapshot(
                    conns,
                    scope,
                    selected,
                    root=root,
                    location=target.name,
                    prefix=prefix,
                    gateway=gateway(target),
                    publication=publication,
                )
            sha = plan.write(args.write_plan)
            print(
                json.dumps(
                    {
                        "status": "PREVIEW_ONLY",
                        "plan": args.write_plan,
                        "sha256": sha,
                        "archive_root": root,
                        "destination": gateway(target),
                        "hosts": hosts,
                        "scope": len(scope.products),
                        "catalog_selection": len(selected.products),
                        "publication": publication,
                        "files": selected.checksums,
                    },
                    indent=2,
                )
            )
            return 0
        if not args.plan:
            raise ValueError(
                "recovery requires --write-plan or --plan; no bare-manifest write"
            )
        execute = bool(args.execute and not args.dry_run)
        if execute and not args.plan_sha256:
            raise ValueError("execution requires reviewed --plan-sha256")
        plan = CatalogPlan.load(args.plan, approved_sha=args.plan_sha256)
        plan.bind(
            root=root,
            location=target.name,
            prefix=prefix,
            hosts=hosts,
            gateway=gateway(target),
        )
        until = deadline(args.owner_until)
        if execute:
            live_check(until)
        with open_required(hosts) as conns:
            plan.validate(conns)
        print(
            json.dumps(
                {
                    "phase": "catalog",
                    "execute": execute,
                    "plan_sha256": plan.sha256,
                    "root": root,
                    "destination": gateway(target),
                    "hosts": hosts,
                    "files": plan.data["selected"],
                },
                indent=2,
            )
        )
        # Prove EVERY selected archive product before the first catalog DML.
        # A skipped/partial data phase must never catalog only its easy prefix.
        values = {
            r: archive_values(root, r, h) for r, h in plan.data["selected"].items()
        }
        with open_required(hosts) as conns:
            plan.validate(conns, verified_values=values)
        if execute:
            live_check(until)
        result = reindex_files_multi(
            hosts,
            [str(Path(root) / r) for r in plan.data["selected"]],
            root=root,
            storage_location=target.name,
            dest_prefix=prefix,
            dry_run=not execute,
            expected_checksums=plan.data["selected"],
            recovery_plan=plan,
            owner_until=until,
            recovery_values=values,
        )
        print(
            json.dumps(
                {
                    host: stats.to_dict() if stats else None
                    for host, stats in result.items()
                },
                indent=2,
            )
        )
        if set(result) != set(hosts) or any(
            s is None
            or s.errors
            or s.skipped
            or s.skipped_new
            or s.updated + s.inserted + s.unchanged != len(plan.data["selected"])
            for s in result.values()
        ):
            return 1
        return 0
    except Exception as exc:
        print("Recovery refused: " + failure(exc))
        return 2


def cmd_preserved_push(args, logger, products, work, read_root, hosts, target) -> int:
    """Existing pusher, exact preserved-original recovery; catalog is a later phase."""
    import os
    import stat
    from copy import copy

    from ..archive.format_guard import split_bad_z
    from ..archive.staged_manifest import (
        StagedManifest,
        file_checksum,
        open_regular_nofollow,
    )
    from .main import _push_reconverted

    try:
        dry = bool(getattr(args, "dry_run", False) or not getattr(args, "push", False))
        if not all(
            getattr(args, key, None)
            for key in ("recovery_plan", "originals_manifest", "originals_root")
        ):
            raise ValueError(
                "preserved recovery requires plan and both original-copy inputs"
            )
        if not dry and not args.plan_sha256:
            raise ValueError("execution requires reviewed --plan-sha256")
        plan = CatalogPlan.load(args.recovery_plan, approved_sha=args.plan_sha256)
        plan.bind(
            root=str(read_root),
            location=target[1],
            prefix=target[2],
            hosts=hosts,
            gateway=target[0],
        )
        original_root = Path(args.originals_root).expanduser().absolute()
        originals = read_staged_manifest(
            args.originals_manifest,
            work_dir=str(original_root),
            stations=[s.upper() for s in args.stations],
            session=args.session,
        )
        binding = {
            "manifest_sha256": products.sha256,
            "originals_sha256": originals.sha256,
            "work_root": str(work),
            "originals_root": str(original_root),
        }
        reviewed = plan.data["publication"]
        if (
            not isinstance(reviewed, dict)
            or {key: reviewed.get(key) for key in binding} != binding
            or not set(products.checksums) <= set(plan.data["selected"])
            or any(plan.data["selected"][r] != h for r, h in products.products)
        ):
            raise ValueError(
                "publication/original bindings changed since reviewed snapshot"
            )
        until = deadline(getattr(args, "owner_until", None))
        if not dry:
            live_check(until)
        namespace = reviewed.get("archive_namespace")
        if (
            not isinstance(namespace, dict)
            or namespace.get("owner_uid") != archive_owner_uid(target[0], target[1])
            or set(namespace.get("files", {})) != set(products.checksums)
        ):
            raise ValueError("reviewed archive namespace/owner scope is invalid")
        with open_required(hosts) as conns:
            plan.validate(conns)
        pending = preserved_replacements(
            products, originals, original_root=original_root, archive_root=read_root
        )
        require_namespace(read_root, pending, namespace)
        _, bad = split_bad_z(pending, logger, root=str(work))
        if bad:
            raise ValueError("publication format guard refused selection")
        print(
            json.dumps(
                {
                    "phase": "publication",
                    "dry_run": dry,
                    "plan_sha256": plan.sha256,
                    "source": str(work),
                    "originals": str(original_root),
                    "archive_readback": str(read_root),
                    "destination": target[0],
                    "catalog_hosts": hosts,
                    "pending": pending,
                    "already_candidates": len(products.products) - len(pending),
                    "catalog_phase": len(plan.data["selected"]),
                    "raw_discovery": False,
                    "scientific_qc_rerun": False,
                    "cleanup": False,
                },
                indent=2,
            )
        )
        if dry or not pending:
            return 0
        planned = copy(args)
        planned.dry_run = False
        batches = [pending[:1]] + [
            pending[i : i + 50] for i in range(1, len(pending), 50)
        ]
        for batch in batches:
            subset = StagedManifest(
                products.sha256, tuple((r, products.checksums[r]) for r in batch)
            )
            baseline = StagedManifest(
                originals.sha256, tuple((r, originals.checksums[r]) for r in batch)
            )

            def before_transfer():
                # All blocking checks precede the final fresh deadline check.
                with open_required(hosts) as conns:
                    plan.validate(conns)
                for r, h in subset.products:
                    if file_checksum(work, r) != h:
                        raise ValueError("candidate changed before transfer")
                if (
                    preserved_replacements(
                        subset,
                        baseline,
                        original_root=original_root,
                        archive_root=read_root,
                    )
                    != batch
                ):
                    raise ValueError("archive changed since publication preview")
                require_namespace(read_root, batch, namespace)
                live_check(until)

            result = _push_reconverted(
                work,
                planned,
                logger,
                only_rel=batch,
                preserved_replacements=batch,
                before_transfer=before_transfer,
                defer_catalog=True,
                archive_target=target,
                owner_until=until,
            )
            if result.get("rc") != 0 or result["staged"] != len(batch):
                print(
                    "PARTIAL/STOP: reconcile actual archive before any further execution; a timeout is not proof that remote/kernel writes were cancelled"
                )
                return 1
            for r, h in subset.products:
                if file_checksum(read_root, r) != h:
                    raise ValueError("published archive read-back mismatch")
                with open_regular_nofollow(read_root, r) as stream:
                    if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != 0o644:
                        raise ValueError("published archive mode is not 0644")
            # First singleton is authoritatively read back before widening.
            print(f"Verified archive read-back: {len(batch)} exact file(s)")
        print(
            "Data phase verified. Catalog reconciliation, global verification and access repair remain separate; no completion visit is implied."
        )
        return 0
    except Exception as exc:
        print("Publication refused/partial: " + failure(exc))
        return 2
