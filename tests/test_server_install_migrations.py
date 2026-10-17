"""Exercise the installer's migration loop in bash without sudo or a database."""

import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "deployment/server/install.sh"


@pytest.mark.parametrize("extension_installed", [False, True])
def test_migration_loop_skips_helpers_and_handles_optional_extension(
    tmp_path, extension_installed
):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for name, text in {
        "001_applied": "-- already applied\n",
        "002_plain": "SELECT 1;\n",
        "002_plain_rollback": "-- never apply\n",
        "002_plain_verify": "-- diagnostic only\n",
        "003_optional": "-- requires-extension: timescaledb\nSELECT 1;\n",
    }.items():
        (migrations / f"{name}.sql").write_text(text)

    source = INSTALLER.read_text()
    loop = source.split("# Apply pending migrations\n", 1)[1].split(
        "# Grant gpsops full access", 1
    )[0]
    # Only the real loop runs: fake sudo responds to prerequisite probes and logs
    # migration execution. Strict mode reproduces the production pipefail abort.
    harness = r'''
set -euo pipefail
sudo() {
    case "$*" in
        *"SELECT migration_name"*) echo 001_applied ;;
        *pg_extension*) [ "$EXTENSION_INSTALLED" = 1 ] && echo 1; return 0 ;;
        *" -f "*) printf '%s\n' "$*" >> "$CALL_LOG" ;;
    esac
}
ok() { :; }
warn() { :; }
err() { :; }
ADMIN_USER=test
DB_NAME=test
APPLIED=001_applied
MIGRATIONS_DIR="$1"
CALL_LOG="$2"
EXTENSION_INSTALLED="$3"
'''
    call_log = tmp_path / "calls.txt"
    proc = subprocess.run(
        [
            "bash", "-c", harness + loop, "migration-test",
            str(migrations), str(call_log), str(int(extension_installed)),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = call_log.read_text().splitlines()
    assert len(calls) == (2 if extension_installed else 1)
    assert "002_plain.sql" in calls[0]
    assert all("_verify" not in call and "_rollback" not in call for call in calls)
    if extension_installed:
        assert "003_optional.sql" in calls[1]
