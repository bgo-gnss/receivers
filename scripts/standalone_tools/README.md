# PolaRx5 standalone tools

Two small command-line tools to download data from one Septentrio PolaRx5 with
firmware 5.7.0 and check its health, without the `receivers` package or a database.
Use them from inside the IMO network or VPN, for one station at a time. Production
collection stays with the `receivers` scheduler.

| | `prx5.py` | `prx5.sh` |
|---|---|---|
| Needs | Python 3.9+, gtimes | bash 4+, GNU coreutils, curl, gtimes (`timecalc`) |
| Optional | RxTools (`bin2asc`, `sbf2rin`) | none |
| `download`, `ports`, `stations` | yes | yes |
| `health`: ports, identity, CPU, temperature, uptime, errors, voltage, fix | yes | yes |
| `health`: satellites per constellation, disk | yes | no |
| `--rinex` quick-look conversion | yes | no |
| `--json` | all commands | `health` only |

## Read this first: the login lockout

On fw 5.7.0, repeated failed logins lock **every account on the receiver for about
nine hours**. That includes the production scheduler, so downloads and health checks
for that station stop too. The countdown cannot be cancelled, but further attempts do
not extend it.

Both tools log in once and stop at the first refusal, and they refuse a password with
a comma or whitespace before sending it. If you get `login refused and NOT retried`,
fix the credentials before running again. Don't loop these tools in a shell script
with credentials you haven't confirmed work.

## Install

### gtimes

gtimes builds the receiver and local paths. `health`, `ports` and `stations` work
without it.

```bash
pip install gtimes        # from PyPI, into a virtualenv or `pip install --user`
timecalc -v               # check: prx5.sh needs this command on PATH
```

Inside IMO's `gpslibrary` mamba environment it is already installed. To work from a
checkout instead: `pip install -e ~/work/projects/gpslibrary/gtimes`.

### RxTools (optional, `prx5.py` only)

Without RxTools `prx5.py` still works. The disk report then shows only mounted state
and size, and `--rinex` is refused. RxTools is proprietary Septentrio software, so
keep it out of public repositories.

**Option 1: IMO's copy.** It is in the `gps-tools` repository (internal):

```bash
git clone git@git.vedur.is:aut/gnss/gps-tools.git ~/git/gps-tools
sudo ln -sf ~/git/gps-tools/rxtools/bin/bin2asc /usr/local/bin/bin2asc
sudo ln -sf ~/git/gps-tools/rxtools/bin/sbf2rin /usr/local/bin/sbf2rin
# The binaries load shared libraries from their own directory:
echo "$HOME/git/gps-tools/rxtools/bin" | sudo tee /etc/ld.so.conf.d/rxtools.conf
sudo ldconfig
```

**Option 2: Septentrio's installer.** Download RxTools for Linux from Septentrio's
support site (an account may be needed) and run the installer. If it installs to
`/usr/local/rxtools/`, `prx5.py` finds `bin2asc` and `sbf2rin` there without PATH
changes; `sbf2rin` and `bin2asc` on PATH take priority.

Check it worked:

```bash
bin2asc 2>&1 | head -1
ldd "$(command -v bin2asc)" | grep 'not found'    # should print nothing
```

### The scripts

They are single files, so copy them wherever you like:

```bash
cp prx5.py prx5.sh ~/.local/bin/
mkdir -p -m 700 ~/.config/polarx5-tools
cp stations.cfg.example ~/.config/polarx5-tools/stations.cfg   # optional
```

## Credentials

Fw 5.7.0 requires a login for FTP and for control commands. Use the receiver's normal
user: the `tcp_username` and `tcp_password` that IMO keeps in `receivers.cfg`
`[polarx5]`. They are looked up in this order:

1. `-u USER` and `-p PASSWORD` on the command line. With `-u` alone, the tool asks for
   the password without echoing it.
2. Environment variables `PRX5_USER` and `PRX5_PASS`, for example
   `export PRX5_PASS="$(pass show some/path)"`.
3. The file given with `--cred-file`.
4. `~/.config/polarx5-tools/credentials`.

The file is read as data and must be mode 600, or the tool refuses it:

```bash
install -m 600 /dev/null ~/.config/polarx5-tools/credentials
$EDITOR ~/.config/polarx5-tools/credentials
```

```ini
username = <user>
password = <password>
```

`-p` is convenient but leaves the password in your shell history and visible in `ps`
while the tool runs, so prefer `-u` alone, the environment or the file on shared
machines. Without credentials, `health` sends no login at all and
reports `Not authorized` from a 5.7.0 receiver, which is safe. `download` refuses to
start unless you pass `--anonymous` for older firmware.

## Usage

```text
prx5 download STA [HOST[:FTP_PORT]]     [options]
prx5 health   STA [HOST[:CONTROL_PORT]] [options]
prx5 ports    STA [HOST]                [options]
prx5 stations [--cfg FILE]
```

Here `prx5` means `prx5.py` or `prx5.sh`. The port after `HOST:` belongs to the
command: FTP for `download`, control for `health`. Each value is taken from the
command line first, then the stations file, then the default: FTP 2160, HTTP 8060,
control 28784.

### Health

```bash
prx5.py health ENTC 192.0.2.10
prx5.py health ENTC 192.0.2.10:28784 --json
prx5.py ports  ENTC 192.0.2.10       # TCP connect only: no login, nothing sent
```

| Exit code | Meaning |
|---|---|
| 0 | ok |
| 1 | warning (for `download`: some files not fetched) |
| 2 | critical, such as `ANTENNA` (overcurrent, so no observations), or temperature, CPU or disk beyond the critical limit |
| 3 | unreachable, login refused, or unknown |
| 64 | bad usage |

Limits are the defaults from `receivers/health/metrics.py`:

| Check | Warning | Critical |
|---|---|---|
| Temperature | below -10 or above 50 °C | below -20 or above 60 °C |
| CPU | 75 % | 90 % |
| Disk | 90 % | 97 %, full, or none mounted |

Voltage is reported as a value only and never changes the status, because its limits
depend on the site's power type.

### Download

```bash
prx5.sh download ENTC 192.0.2.10:2160                     # yesterday's 15s_24hr
prx5.sh download ENTC 192.0.2.10 -s 1Hz_1hr               # the last complete hour
prx5.sh download ENTC --start 2026-11-01 --end 2026-11-03
prx5.sh download ENTC -D 7                               # the last 7 complete days
prx5.sh download ENTC -s 1Hz_1hr -D 6                    # the last 6 complete hours
prx5.sh download ENTC 192.0.2.10 -u <user>               # asks for the password
prx5.sh download ENTC -s 1Hz_1hr --start 2026-11-03T14 --end 2026-11-03T18
prx5.py download ENTC --start 2026-11-03 --rinex         # + quick-look RINEX
```

| Option | Effect |
|---|---|
| `-s`, `--session` | `15s_24hr` (default), `1Hz_1hr` or `status_1hr` |
| `--start`, `--end` | UTC, `YYYY-MM-DD` or `YYYY-MM-DDTHH`. **`--end` is inclusive.** With only a date, an hourly session means the whole day. |
| `-D`, `--sessions-back N` | The last N complete sessions: days for `15s_24hr`, hours for `1Hz_1hr` and `status_1hr`. Not with `--start` or `--end`. With `--unfinalised`, also the running session. |
| `-o`, `--outdir` | Root of the tree, default `./gpsdata` |
| `--flat` | Put all files directly in `--outdir` |
| `--dry-run` | Log in and report what the receiver has, writing nothing |
| `--print-paths` | Print the paths: no network and no credentials |
| `--unfinalised` | Also fetch a file still being written, and gzip it locally |
| `--force` | Fetch even when the local size already matches |
| `--retries N` | After network errors, default 2. A refused login is never retried. |
| `--active` | Active FTP instead of passive (rarely works through a router) |
| `-u`, `-p` | Login user and password, see Credentials |
| `--anonymous` | No login, for firmware before 5.7 |
| `--base-path` | On the receiver, default `/DSK1/SSN/` |
| `--timeout` | Network timeout in seconds, default 30 |

Files use the same names as IMO's archive tree. gtimes builds both sides:

```text
receiver  /DSK1/SSN/LOG1_15s_24hr/26307/ENTC3070.26_.gz
local     gpsdata/2026/nov/ENTC/15s_24hr/raw/ENTC202611030000a.sbf.gz
```

A file downloads to `<name>.part`, resumes after an interruption, and gets its final
name only once its size matches and `gzip -t` passes. A file whose local size matches
is skipped.

| Outcome | Meaning |
|---|---|
| `fetched`, `present`, `available` | The file is in place, or can be (`available` is from `--dry-run`) |
| `absent` | The receiver doesn't have it |
| `unfinalised` | Only the bare file exists, still being written. Use `--unfinalised`. |
| `failed: ...` | Retries used up. The partial file stays for the next run. |

`--rinex` output is for a quick look only. Its headers come from the receiver, not
from TOS, so it isn't equivalent to the archive's RINEX. Do not publish it.

### Storing stations

`--save` writes the address to the stations file, after which the address can be left
out:

```bash
prx5.py health ENTC 192.0.2.10 --save
prx5.py health ENTC
prx5.py stations
```

The file is `--cfg FILE`, then `$PRX5_STATIONS_CFG`, then
`~/.config/polarx5-tools/stations.cfg`. See `stations.cfg.example`.

The keys are the same as in IMO's `stations.cfg`, so the full file works for reading:

```bash
prx5.py health ENTC --cfg ~/.config/gpsconfig/stations.cfg
```

`--save` refuses any path under a `gpsconfig` directory, because IMO's configuration
is changed in `gps-config-data`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `FTP port ... refused` right after a firmware upgrade | `rxupgrade` to 5.7.0 turns FTP off. `receivers rec-provision <SID>` turns it back on. The receiver keeps logging meanwhile. |
| Control port closed, TLS port 28783 open | The upgrade left the receiver in `sis, secure`. Fix with `rec-provision`. |
| `receiver locked by failed logins` | Wait for the countdown. Do not treat it as lost accounts: while locked, the receiver can also say a user with a password is needed. |
| `Not authorized` | No credentials were given, or `--no-login` was used. |
| HTTP port won't serve the web UI | 5.7.0 redirects to HTTPS, which the router's 8060 forward doesn't carry. These tools use FTP, not HTTP. |
| `absent` for a date you expect | The receiver's disk holds a limited history. Older data is in the archive. |
| No satellites, and `errors ANTENNA`, `antenna power OFF` | Antenna overcurrent. A reset won't clear it if the fault remains, so this usually needs a field visit. |

## Development

```bash
pytest tests/test_standalone_prx5.py
```

Both scripts were also run live against NYLA (fw 5.7.0) on 2026-11-05: `health`,
`download --dry-run` and a real `status_1hr` download, which gave identical files.

The tests run both scripts against a fake receiver and FTP server on loopback, and
never touch a real receiver. Keep the login count assertions: they are what caught
the bash version logging in once per file with a wrong password.
