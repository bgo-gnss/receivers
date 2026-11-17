"""Archive access phase: inspect -> reviewed action file -> exact metadata repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from ..archive import load_sync_config, resolve_catalog_hosts
from ..archive.access import (
    access_rows,
    gateway_request,
    protected_originals,
    read_json,
    readback,
)
from ..archive.catalog_recovery import CatalogPlan, deadline, live_check, open_required
from ..archive.staged_manifest import read_staged_manifest
from .archive_recovery import failure, gateway

PRODUCTION_GATEWAYS = frozenset(("rawdata", "rawdata.vedur.is"))


def create_archive_access_parser(subparsers) -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = subparsers.add_parser(
        "archive-access",
        help="Verify/restore exact published RINEX modes after full data/catalog completion",
    )
    parser.add_argument("--catalog-plan", required=True)
    parser.add_argument("--read-root", required=True)
    parser.add_argument(
        "--gateway-root",
        required=True,
        help="Canonical absolute gateway root (not a symlink), e.g. /mnt/gps/gpsdata",
    )
    parser.add_argument("--catalog-host")
    parser.add_argument("--catalog-prod", action="store_true")
    parser.add_argument("--config")
    parser.add_argument(
        "--manifest", help="Full final-name SHA256 manifest: rinex + rinex_org"
    )
    parser.add_argument(
        "--aliases",
        help="Exact JSON mapping of each public name to its known private hardlink aliases",
    )
    parser.add_argument(
        "--protected-originals",
        help="JSON roots/manifests/hashes/expected modes for all protected original copies",
    )
    parser.add_argument(
        "--write-plan",
        help="Write a fresh read-only gateway inspection; refuses existing output",
    )
    parser.add_argument("--plan", help="Reviewed access action file")
    parser.add_argument(
        "--plan-sha256", help="Reviewed access action-file SHA256, mandatory with --yes"
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--owner-until")
    parser.set_defaults(func=cmd_archive_access)
    return parser


def cmd_archive_access(args) -> int:
    try:
        execute = bool(args.yes and not args.dry_run)
        if bool(args.catalog_prod) == bool(args.catalog_host):
            raise ValueError("exactly one explicit catalog target is required")
        catalog = CatalogPlan.load(args.catalog_plan)
        hosts = resolve_catalog_hosts(args.catalog_host, prod=args.catalog_prod)
        target = next(
            (
                t
                for t in load_sync_config(Path(args.config) if args.config else None)
                if t.name == catalog.data["location"] and getattr(t, "active", True)
            ),
            None,
        )
        if target is None or not target.host:
            raise ValueError("active remote archive gateway required")
        if target.name == "imo_archive" and (
            target.host not in PRODUCTION_GATEWAYS or target.user != "gpsops"
        ):
            raise ValueError("rawdata/gpsops is the sole production writer")
        catalog.bind(
            root=args.read_root,
            location=target.name,
            prefix=target.dest,
            hosts=hosts,
            gateway=gateway(target),
        )
        ssh_target = f"{target.user}@{target.host}"
        started = time.time()
        capabilities = gateway_request(
            ssh_target,
            {
                "capabilities_only": True,
                "execute": False,
                "production": target.name == "imo_archive",
            },
        )
        if not started - 2 <= capabilities["server_time"] <= time.time() + 2:
            raise ValueError(
                "gateway/client clock disagreement; re-coordinate owner window"
            )
        until = deadline(args.owner_until)
        if execute:
            if not args.plan or not args.plan_sha256 or args.write_plan:
                raise ValueError(
                    "execution requires reviewed access plan/SHA and no --write-plan"
                )
            live_check(until)
        prior = None
        if args.plan:
            prior, sha = read_json(args.plan)
            if execute and sha != args.plan_sha256:
                raise ValueError("reviewed access plan SHA mismatch")
            if (
                prior.get("schema") != "receivers.archive-access.v1"
                or prior["catalog_plan_sha256"] != catalog.sha256
                or prior["read_root"]
                != str(Path(args.read_root).expanduser().absolute())
                or prior["gateway_root"] != args.gateway_root
                or prior["gateway"] != ssh_target
            ):
                raise ValueError("access role/catalog bindings changed")
            inputs = prior["inputs"]
        else:
            if not all((args.manifest, args.aliases, args.protected_originals)):
                raise ValueError(
                    "access preview needs manifest, exact aliases and protected-original specifications"
                )
            inputs = {
                "manifest": args.manifest,
                "aliases": args.aliases,
                "protected": args.protected_originals,
            }
        stations = sorted({r.split("/")[2] for r in catalog.data["scope"]})
        session = next(iter(catalog.data["scope"])).split("/")[3]
        manifest = read_staged_manifest(
            inputs["manifest"],
            work_dir=args.read_root,
            stations=stations,
            session=session,
            validate_files=False,
            categories=("rinex", "rinex_org"),
        )
        aliases, aliases_sha = read_json(inputs["aliases"])
        protected, protected_sha = protected_originals(inputs["protected"], catalog)
        input_hashes = {
            "manifest": manifest.sha256,
            "aliases": aliases_sha,
            "protected": protected_sha,
        }
        if prior is not None and input_hashes != prior["input_hashes"]:
            raise ValueError("access inputs changed since review")
        rows = access_rows(manifest, aliases, catalog, protected)
        # This is an actual fresh whole-scope archive/canonical/blob/size check,
        # not inference from a detached job, selected stages or transfer exit.
        with open_required(hosts) as conns:
            observed = catalog.validate(conns, complete=True)
        verified_values = {}
        for relative, checksum in catalog.data["scope"].items():
            row = observed[hosts[0]][relative]
            if row is None:
                raise ValueError("complete catalog proof unexpectedly lacks a row")
            verified_values[relative] = (row[10], checksum, row[9])
        root = Path(args.read_root).expanduser().absolute()
        modes = readback(root, rows)
        packet = {
            "root": args.gateway_root,
            "rows": rows,
            "execute": False,
            "production": target.name == "imo_archive",
        }
        if not execute:
            response = gateway_request(ssh_target, packet)
            pins = {r["relative"]: r["pin"] for r in response["files"]}
            if any(pins[r][7] != modes[r] for r in modes):
                raise ValueError("gateway/read-mount permission disagreement")
            data = {
                "schema": "receivers.archive-access.v1",
                "catalog_plan_sha256": catalog.sha256,
                "read_root": str(root),
                "gateway_root": args.gateway_root,
                "gateway": ssh_target,
                "inputs": inputs,
                "input_hashes": input_hashes,
                "rows": rows,
                "pins": pins,
                "namespace": response["namespace"],
                "root_pin": response["root_pin"],
            }
            sha = None
            if args.write_plan:
                raw = json.dumps(data, sort_keys=True, indent=2).encode() + b"\n"
                with Path(args.write_plan).open("xb") as stream:
                    stream.write(raw)
                sha = hashlib.sha256(raw).hexdigest()
            print(
                json.dumps(
                    {
                        "phase": "access-preview",
                        "plan_sha256": sha,
                        "files": len(rows),
                        "would_restore": sum(m != 0o644 for m in modes.values()),
                        "protected_originals_verified": len(protected),
                        "catalogs_complete": hosts,
                        "files_sha256": manifest.checksums,
                    },
                    indent=2,
                )
            )
            return 0
        if prior is None:
            raise ValueError("reviewed access plan required before execution")
        if prior["rows"] != rows:
            raise ValueError("access selection changed since review")
        # Recheck even already-0644 names: changed inodes/aliases/parents must
        # not be hidden by filtering the pending chmod subset.
        fresh = gateway_request(ssh_target, packet)
        if (
            {r["relative"]: r["pin"] for r in fresh["files"]} != prior["pins"]
            or fresh["namespace"] != prior["namespace"]
            or fresh["root_pin"] != prior["root_pin"]
        ):
            raise ValueError("gateway state changed since reviewed access plan")
        pending = [row for row in rows if prior["pins"][row["relative"]][7] != 0o644]
        batches = (
            ([pending[:1]] + [pending[i : i + 50] for i in range(1, len(pending), 50)])
            if pending
            else []
        )
        for batch in batches:
            # Repeat global metadata/NULL guards; bytes of every touched file
            # are freshly verified on the gateway and authoritative read mount.
            with open_required(hosts) as conns:
                catalog.validate(conns, verified_values=verified_values)
            live_check(until)
            gateway_request(
                ssh_target,
                dict(
                    packet,
                    rows=batch,
                    execute=True,
                    until=until,
                    pins=prior["pins"],
                    namespace=prior["namespace"],
                    root_pin=prior["root_pin"],
                ),
            )
            readback(root, batch, final=True)  # singleton verified before widening
        with open_required(hosts) as conns:
            catalog.validate(conns, complete=True)
        protected_originals(inputs["protected"], catalog)
        final = gateway_request(ssh_target, packet) if pending else fresh
        if (
            final["namespace"] != prior["namespace"]
            or final["root_pin"] != prior["root_pin"]
        ):
            raise ValueError("final gateway namespace changed")
        for result in final["files"]:
            before = prior["pins"][result["relative"]]
            after = result["pin"]
            if (
                after[:7] != before[:7]
                or after[7] != 0o644
                or (before[7] == 0o644 and after[8] != before[8])
            ):
                raise ValueError(
                    "final gateway identity/owner/group/link/size/mtime/mode changed unexpectedly"
                )
        readback(root, rows, final=True)
        print(
            json.dumps(
                {
                    "phase": "access-verified",
                    "files": len(rows),
                    "protected_originals_unchanged": len(protected),
                    "catalogs": hosts,
                    "completion_visit_authorised": False,
                }
            )
        )
        return 0
    except Exception as exc:
        print(
            "Access refused/partial: " + failure(exc) + "; reconcile before any retry"
        )
        return 2
