#!/usr/bin/env python3
"""
Grafana dashboard sync — push local dashboards to remote Grafana instances.

Reads dashboard JSON from docs/grafana/ (source of truth), remaps datasource
UIDs and inter-dashboard link UIDs, then pushes via the Grafana HTTP API.

Usage:
    python scripts/grafana_sync.py export                     # Export from local Grafana API
    python scripts/grafana_sync.py push [--target vedur]      # Push to remote
    python scripts/grafana_sync.py diff [--target vedur]      # Show diff
    python scripts/grafana_sync.py status [--target vedur]    # Show versions

Environment:
    GRAFANA_VEDUR_TOKEN  — Service account token for grafana.vedur.is (preferred)
    GRAFANA_LOCAL_TOKEN  — Token for local Grafana (optional, basic auth default)

Auth priority:
    1. Environment variable (GRAFANA_<TARGET>_TOKEN)
    2. Token file (~/.config/gpsconfig/grafana_tokens.yaml)
    3. Cookie file (~/.config/gpsconfig/grafana_cookies.yaml)
    4. Interactive prompt

Cookie auth (temporary workaround until service account tokens are available):
    Grafana has session token rotation enabled — the cookie is invalidated after
    each request. You must grab a FRESH cookie from the browser immediately before
    running the push. Both grafana_session and grafana_session_expiry are required.

    1. Log into grafana.vedur.is
    2. DevTools → Application → Cookies → copy both values
    3. Update ~/.config/gpsconfig/grafana_cookies.yaml:
         vedur: "grafana_session=<value>; grafana_session_expiry=<value>"
    4. Run the push IMMEDIATELY (cookie rotates on next browser request)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

# ── Paths ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
TARGETS_FILE = SCRIPT_DIR / "grafana_targets.yaml"
DASHBOARDS_DIR = PROJECT_ROOT / "docs" / "grafana"
EXPORT_DIR = PROJECT_ROOT / "dumps" / "grafana-export"

# ── Colors ───────────────────────────────────────────────────────────────────

if sys.stdout.isatty():
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    YELLOW = "\033[0;33m"
    BLUE = "\033[0;34m"
    DIM = "\033[0;90m"
    NC = "\033[0m"
else:
    RED = GREEN = YELLOW = BLUE = DIM = NC = ""


def info(msg: str) -> None:
    print(f"{GREEN}[INFO]{NC}  {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}[WARN]{NC}  {msg}")


def error(msg: str) -> None:
    sys.stdout.flush()  # keep error output in order with info lines
    print(f"{RED}[ERROR]{NC} {msg}", file=sys.stderr)
    sys.stderr.flush()


def header(msg: str) -> None:
    print(f"\n{BLUE}── {msg} ──{NC}")


# ── Config loading ───────────────────────────────────────────────────────────


def load_targets() -> dict[str, Any]:
    """Load target definitions from grafana_targets.yaml."""
    if not TARGETS_FILE.exists():
        error(f"Targets file not found: {TARGETS_FILE}")
        sys.exit(1)
    with open(TARGETS_FILE) as f:
        return yaml.safe_load(f)


def get_target(config: dict[str, Any], name: str) -> dict[str, Any]:
    """Get a specific target by name."""
    targets = config.get("targets", {})
    if name not in targets:
        error(f"Unknown target '{name}'. Available: {', '.join(targets)}")
        sys.exit(1)
    return targets[name]


# ── Auth ─────────────────────────────────────────────────────────────────────


def get_auth_headers(target: dict[str, Any], target_name: str) -> dict[str, str]:
    """Build HTTP auth headers for a target."""
    auth_type = target.get("auth_type", "token")

    if auth_type == "basic":
        import base64

        user = target.get("username", "admin")
        pwd = target.get("password", "admin")
        cred = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        return {"Authorization": f"Basic {cred}"}

    if auth_type == "token":
        env_var = f"GRAFANA_{target_name.upper()}_TOKEN"
        token = os.environ.get(env_var, "")
        if token:
            return {"Authorization": f"Bearer {token}"}

        # Fallback: check config file
        token_file = Path(
            os.environ.get("GPS_CONFIG_PATH", "~/.config/gpsconfig")
        ).expanduser() / "grafana_tokens.yaml"
        if token_file.exists():
            with open(token_file) as f:
                tokens = yaml.safe_load(f) or {}
            token = tokens.get(target_name, "")
            if token:
                return {"Authorization": f"Bearer {token}"}

        # Fallback: check cookie file
        config_dir = Path(
            os.environ.get("GPS_CONFIG_PATH", "~/.config/gpsconfig")
        ).expanduser()
        cookie_file = config_dir / "grafana_cookies.yaml"
        if cookie_file.exists():
            with open(cookie_file) as f:
                cookies = yaml.safe_load(f) or {}
            cookie = cookies.get(target_name, "")
            if cookie:
                info(f"Using session cookie from {cookie_file}")
                return {"Cookie": cookie}

        # Fallback: prompt for session cookie
        warn(f"No token found in ${env_var} or {token_file}")
        cookie = input("  Enter Grafana session cookie (grafana_session=...): ").strip()
        if cookie:
            return {"Cookie": cookie}
        error("No authentication provided")
        sys.exit(1)

    error(f"Unknown auth_type: {auth_type}")
    sys.exit(1)


# ── Cookie persistence ───────────────────────────────────────────────────

# Track which target we're pushing to (set by cmd_push)
_active_target: str = ""


def _save_rotated_cookie(new_cookie: str) -> None:
    """Persist a rotated Grafana session cookie to disk.

    Called after each successful API request that returns Set-Cookie headers,
    so the next script invocation uses the rotated token.
    """
    if not _active_target:
        return
    config_dir = Path(
        os.environ.get("GPS_CONFIG_PATH", "~/.config/gpsconfig")
    ).expanduser()
    cookie_file = config_dir / "grafana_cookies.yaml"
    if not cookie_file.exists():
        return
    try:
        with open(cookie_file) as f:
            cookies = yaml.safe_load(f) or {}
        cookies[_active_target] = new_cookie
        with open(cookie_file, "w") as f:
            f.write(
                "# Grafana session cookies — temporary auth until"
                " service account tokens are available\n"
                "# Auto-updated by grafana_sync.py on cookie rotation\n"
            )
            yaml.dump(cookies, f, default_flow_style=False)
    except Exception:
        pass  # best-effort, don't break the push


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def api_get(
    url: str, headers: dict[str, str]
) -> dict[str, Any]:
    """GET a Grafana API endpoint, return parsed JSON."""
    req = Request(url, headers={**headers, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        error(f"HTTP {e.code} from {url}: {body[:300]}")
        raise
    except URLError as e:
        error(f"Connection error to {url}: {e.reason}")
        raise


def _handle_cookie_rotation(resp: Any, headers: dict[str, str]) -> None:
    """Update headers in-place if Grafana rotated the session cookie."""
    if "Cookie" not in headers:
        return
    new_parts = {}
    for raw in resp.headers.get_all("Set-Cookie") or []:
        first = raw.split(";", 1)[0].strip()
        if "=" in first:
            k, v = first.split("=", 1)
            if k.strip().startswith("grafana_session"):
                new_parts[k.strip()] = v.strip()
    if new_parts:
        new_cookie = "; ".join(f"{k}={v}" for k, v in new_parts.items())
        headers["Cookie"] = new_cookie
        _save_rotated_cookie(new_cookie)


def api_post(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any]:
    """POST JSON to a Grafana API endpoint, return parsed JSON.

    Handles Grafana session token rotation: if the response includes a
    Set-Cookie header, the cookie in ``headers`` is updated in-place so
    subsequent requests use the rotated session token.
    """
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            _handle_cookie_rotation(resp, headers)
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        error(f"HTTP {e.code} from POST {url}: {body[:500]}")
        raise
    except URLError as e:
        error(f"Connection error to {url}: {e.reason}")
        raise


def api_patch(
    url: str, headers: dict[str, str], payload: dict[str, Any]
) -> dict[str, Any]:
    """PATCH JSON to a Grafana API endpoint, return parsed JSON."""
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            _handle_cookie_rotation(resp, headers)
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        error(f"HTTP {e.code} from PATCH {url}: {body[:500]}")
        raise
    except URLError as e:
        error(f"Connection error to {url}: {e.reason}")
        raise


# ── UID remapping ────────────────────────────────────────────────────────────


def remap_datasource_uid(dashboard: dict[str, Any], target_ds_uid: str) -> int:
    """Replace all datasource UIDs (type=grafana-postgresql-datasource) in-place.

    Returns count of replacements made.
    """
    count = 0
    # Find all current postgresql datasource UIDs and replace them
    # Pattern: {"type": "grafana-postgresql-datasource", "uid": "..."}
    # We do a structural walk instead of regex for correctness

    def _walk(obj: Any) -> Any:
        nonlocal count
        if isinstance(obj, dict):
            if (
                obj.get("type") == "grafana-postgresql-datasource"
                and "uid" in obj
                and obj["uid"] != target_ds_uid
            ):
                obj["uid"] = target_ds_uid
                count += 1
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(dashboard)
    return count


def remap_dashboard_links(
    dashboard: dict[str, Any],
    link_mappings: dict[str, dict[str, str]],
    target_name: str,
) -> int:
    """Rewrite /d/<uid>/... URLs in dashboard JSON for the target environment.

    Returns count of URL replacements made.
    """
    count = 0

    # Build replacement map: local_uid -> target_uid
    uid_map: dict[str, str] = {}
    for local_uid, targets in link_mappings.items():
        target_uid = targets.get(target_name)
        if target_uid and target_uid != local_uid:
            uid_map[local_uid] = target_uid

    if not uid_map:
        return 0

    def _rewrite_urls(obj: Any) -> Any:
        nonlocal count
        if isinstance(obj, str):
            new_val = obj
            for local_uid, target_uid in uid_map.items():
                if f"/d/{local_uid}" in new_val:
                    new_val = new_val.replace(f"/d/{local_uid}", f"/d/{target_uid}")
                    count += 1
            return new_val
        elif isinstance(obj, dict):
            return {k: _rewrite_urls(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_rewrite_urls(item) for item in obj]
        return obj

    # Must modify in-place by updating keys
    remapped = _rewrite_urls(dashboard)
    dashboard.clear()
    dashboard.update(remapped)
    return count


def set_dashboard_uid(dashboard: dict[str, Any], uid: str) -> None:
    """Set the dashboard UID and clear the id (let Grafana assign it)."""
    dashboard["uid"] = uid
    dashboard.pop("id", None)


# ── Commands ─────────────────────────────────────────────────────────────────


def cmd_export(_args: argparse.Namespace) -> None:
    """Export dashboards from local Grafana API to dumps/grafana-export/."""
    config = load_targets()
    target = get_target(config, "local")
    headers = get_auth_headers(target, "local")
    base_url = target["url"].rstrip("/")

    header("Exporting dashboards from local Grafana")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    dashboards = target.get("dashboards", {})
    for name, uid in dashboards.items():
        info(f"Fetching {name} (uid={uid})")
        try:
            data = api_get(f"{base_url}/api/dashboards/uid/{uid}", headers)
        except Exception:
            error(f"  Failed to fetch {uid}")
            continue

        out_file = EXPORT_DIR / f"{name}.json"
        with open(out_file, "w") as f:
            json.dump(data["dashboard"], f, indent=2)
        info(f"  Saved to {out_file.relative_to(PROJECT_ROOT)}")

    info("Export complete")


def cmd_push(args: argparse.Namespace) -> None:
    """Push local dashboards to a remote Grafana target."""
    global _active_target
    target_name = args.target
    _active_target = target_name
    config = load_targets()
    target = get_target(config, target_name)
    headers = get_auth_headers(target, target_name)
    base_url = target["url"].rstrip("/")
    link_mappings = config.get("link_mappings", {})

    header(f"Pushing dashboards to {target_name} ({target['url']})")

    dashboards = target.get("dashboards", {})
    folder_uid = target.get("folder_uid")
    target_ds_uid = target["datasource_uid"]

    for name, target_uid in dashboards.items():
        src_file = DASHBOARDS_DIR / f"{name}.json"
        if not src_file.exists():
            warn(f"Source not found: {src_file.relative_to(PROJECT_ROOT)}")
            continue

        info(f"Processing {name}")

        with open(src_file) as f:
            dashboard = json.load(f)

        # Remap datasource UIDs
        ds_count = remap_datasource_uid(dashboard, target_ds_uid)
        info(f"  Datasource UIDs remapped: {ds_count}")

        # Remap inter-dashboard link UIDs
        link_count = remap_dashboard_links(dashboard, link_mappings, target_name)
        info(f"  Dashboard link UIDs remapped: {link_count}")

        # Set target dashboard UID
        set_dashboard_uid(dashboard, target_uid)
        info(f"  Target UID: {target_uid}")

        # Build API payload
        payload: dict[str, Any] = {
            "dashboard": dashboard,
            "overwrite": True,
            "message": f"Synced from local via grafana_sync.py",
        }
        if folder_uid:
            payload["folderUid"] = folder_uid

        if args.dry_run:
            info(f"  [DRY RUN] Would push to {base_url}/api/dashboards/db")
            # Save payload for inspection
            dry_file = EXPORT_DIR / f"{name}_dry_run.json"
            EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            with open(dry_file, "w") as f:
                json.dump(payload, f, indent=2)
            info(f"  Payload saved to {dry_file.relative_to(PROJECT_ROOT)}")
            continue

        try:
            result = api_post(f"{base_url}/api/dashboards/db", headers, payload)
            status = result.get("status", "unknown")
            version = result.get("version", "?")
            url = result.get("url", "")
            info(
                f"  {GREEN}OK{NC} — status={status}, version={version}, url={url}"
            )
        except Exception:
            error(f"  Failed to push {name}")

    info("Push complete")


def cmd_diff(args: argparse.Namespace) -> None:
    """Show diff between local dashboards and remote target."""
    target_name = args.target
    config = load_targets()
    target = get_target(config, target_name)
    headers = get_auth_headers(target, target_name)
    base_url = target["url"].rstrip("/")
    link_mappings = config.get("link_mappings", {})

    header(f"Comparing local dashboards with {target_name}")

    dashboards = target.get("dashboards", {})
    target_ds_uid = target["datasource_uid"]

    for name, target_uid in dashboards.items():
        src_file = DASHBOARDS_DIR / f"{name}.json"
        if not src_file.exists():
            warn(f"Source not found: {src_file.relative_to(PROJECT_ROOT)}")
            continue

        info(f"Comparing {name} (uid={target_uid})")

        # Load and remap local
        with open(src_file) as f:
            local_dash = json.load(f)
        remap_datasource_uid(local_dash, target_ds_uid)
        remap_dashboard_links(local_dash, link_mappings, target_name)
        set_dashboard_uid(local_dash, target_uid)
        local_dash.pop("version", None)

        # Fetch remote
        try:
            remote_data = api_get(
                f"{base_url}/api/dashboards/uid/{target_uid}", headers
            )
            remote_dash = remote_data["dashboard"]
            remote_dash.pop("id", None)
            remote_dash.pop("version", None)
        except Exception:
            warn(f"  Could not fetch remote — dashboard may not exist yet")
            continue

        # Compare as normalized JSON strings
        local_str = json.dumps(local_dash, sort_keys=True, indent=2)
        remote_str = json.dumps(remote_dash, sort_keys=True, indent=2)

        if local_str == remote_str:
            info(f"  {GREEN}In sync{NC}")
        else:
            # Count differences at line level
            local_lines = local_str.splitlines()
            remote_lines = remote_str.splitlines()
            added = sum(1 for l in local_lines if l not in remote_lines)
            removed = sum(1 for l in remote_lines if l not in local_lines)
            warn(
                f"  {YELLOW}Differs{NC} — ~{added} lines added, ~{removed} lines removed"
            )
            if args.verbose:
                import difflib

                diff = difflib.unified_diff(
                    remote_lines,
                    local_lines,
                    fromfile=f"remote:{target_uid}",
                    tofile=f"local:{name}",
                    lineterm="",
                )
                for line in list(diff)[:100]:
                    if line.startswith("+") and not line.startswith("+++"):
                        print(f"  {GREEN}{line}{NC}")
                    elif line.startswith("-") and not line.startswith("---"):
                        print(f"  {RED}{line}{NC}")
                    else:
                        print(f"  {DIM}{line}{NC}")


def cmd_status(args: argparse.Namespace) -> None:
    """Show dashboard versions and metadata on a target."""
    target_name = args.target
    config = load_targets()
    target = get_target(config, target_name)
    headers = get_auth_headers(target, target_name)
    base_url = target["url"].rstrip("/")

    header(f"Dashboard status on {target_name} ({target['url']})")

    dashboards = target.get("dashboards", {})
    for name, uid in dashboards.items():
        try:
            data = api_get(f"{base_url}/api/dashboards/uid/{uid}", headers)
            dash = data["dashboard"]
            meta = data.get("meta", {})
            version = dash.get("version", "?")
            title = dash.get("title", "?")
            updated = meta.get("updated", "?")
            updated_by = meta.get("updatedBy", "?")
            folder = meta.get("folderTitle", "General")
            url = meta.get("url", "")
            print(
                f"  {GREEN}{name}{NC}\n"
                f"    Title:   {title}\n"
                f"    UID:     {uid}\n"
                f"    Version: {version}\n"
                f"    Updated: {updated} by {updated_by}\n"
                f"    Folder:  {folder}\n"
                f"    URL:     {base_url}{url}"
            )
        except Exception:
            print(f"  {RED}{name}{NC}\n    UID: {uid}\n    Status: NOT FOUND")


def cmd_seed_library(args: argparse.Namespace) -> None:
    """Create or update library panels on a Grafana target.

    Reads all library_panel_*.json files from docs/grafana/ and pushes them
    via the Library Elements API.  Grafana does not support file-provisioned
    library panels, so this is the only way to seed them on a fresh instance.
    """
    global _active_target
    target_name = args.target
    _active_target = target_name
    config = load_targets()
    target = get_target(config, target_name)
    headers = get_auth_headers(target, target_name)
    base_url = target["url"].rstrip("/")

    panel_files = sorted(DASHBOARDS_DIR.glob("library_panel_*.json"))
    if not panel_files:
        warn("No library panel files found (expected library_panel_*.json in docs/grafana/)")
        return

    header(f"Seeding library panels to {target_name} ({target['url']})")

    for panel_file in panel_files:
        with open(panel_file) as f:
            panel_data = json.load(f)

        uid = panel_data.get("uid")
        name = panel_data.get("name", "?")
        kind = panel_data.get("kind", 1)
        model = panel_data.get("model", {})
        version = panel_data.get("version", 1)

        if not uid:
            warn(f"No uid in {panel_file.name} — skipping")
            continue

        info(f"Processing library panel '{name}' (uid={uid})")

        # Check existence
        exists = False
        existing_version = 1
        try:
            existing = api_get(f"{base_url}/api/library-elements/{uid}", headers)
            exists = True
            existing_version = existing.get("result", {}).get("version", version)
            info(f"  Exists at version {existing_version}")
        except HTTPError as e:
            if e.code == 404:
                info("  Does not exist — will create")
            else:
                error(f"  Failed to check existence: HTTP {e.code}")
                continue
        except Exception:
            error("  Failed to check existence")
            continue

        if args.dry_run:
            action = "PATCH" if exists else "POST"
            info(f"  [DRY RUN] Would {action} to {base_url}/api/library-elements")
            continue

        try:
            if exists:
                payload: dict[str, Any] = {
                    "name": name,
                    "kind": kind,
                    "model": model,
                    "version": existing_version,
                }
                result = api_patch(
                    f"{base_url}/api/library-elements/{uid}", headers, payload
                )
                new_ver = result.get("result", {}).get("version", "?")
                info(f"  {GREEN}UPDATED{NC} — version={new_ver}")
            else:
                payload = {"name": name, "uid": uid, "kind": kind, "model": model}
                result = api_post(
                    f"{base_url}/api/library-elements", headers, payload
                )
                new_ver = result.get("result", {}).get("version", "?")
                info(f"  {GREEN}CREATED{NC} — version={new_ver}")
        except Exception:
            error(f"  Failed to seed '{name}'")

    info("Library panel seeding complete")


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grafana dashboard sync tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s export                         Export local dashboards\n"
            "  %(prog)s push --target vedur             Push dashboards to grafana.vedur.is\n"
            "  %(prog)s push --target vedur --dry-run   Preview without pushing\n"
            "  %(prog)s diff --target vedur             Compare local vs remote\n"
            "  %(prog)s diff --target vedur -v          Show detailed diff\n"
            "  %(prog)s status --target vedur           Show remote dashboard info\n"
            "  %(prog)s seed-library --target vedur     Create/update library panels\n"
            "  %(prog)s seed-library --target local     Seed library panels on local\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # export
    sub.add_parser("export", help="Export dashboards from local Grafana")

    # push
    p_push = sub.add_parser("push", help="Push dashboards to a target")
    p_push.add_argument(
        "--target", "-t", default="vedur", help="Target name (default: vedur)"
    )
    p_push.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without pushing"
    )

    # diff
    p_diff = sub.add_parser("diff", help="Diff local vs remote dashboards")
    p_diff.add_argument(
        "--target", "-t", default="vedur", help="Target name (default: vedur)"
    )
    p_diff.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed diff output"
    )

    # status
    p_status = sub.add_parser("status", help="Show dashboard versions on target")
    p_status.add_argument(
        "--target", "-t", default="vedur", help="Target name (default: vedur)"
    )

    # seed-library
    p_seed = sub.add_parser(
        "seed-library", help="Create/update library panels on a target"
    )
    p_seed.add_argument(
        "--target", "-t", default="vedur", help="Target name (default: vedur)"
    )
    p_seed.add_argument(
        "--dry-run", "-n", action="store_true", help="Preview without pushing"
    )

    args = parser.parse_args()

    commands = {
        "export": cmd_export,
        "push": cmd_push,
        "diff": cmd_diff,
        "status": cmd_status,
        "seed-library": cmd_seed_library,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
