# PolaRx5 standalone tools

Two small command-line tools to download data from one Septentrio PolaRx5 receiver
with firmware 5.7.0 and check its health. They work on one station at a time, talk
only to the receiver, and use no database.

| | `prx5.py` | `prx5.sh` |
|---|---|---|
| Needs | Python 3.9+, gtimes | bash 4+, GNU coreutils, curl, gtimes (`timecalc`) |
| Optional | RxTools (`bin2asc`, `sbf2rin`) | none |
| `download`, `ports`, `stations` | yes | yes |
| `health`: ports, identity, CPU, temperature, uptime, errors, voltage, fix | yes | yes |
| `health`: satellites per constellation, disk | yes | no |
| `--rinex` quick-look conversion | yes | no |
| `--json` | all commands | `health` only |

`prx5.py --help` and `prx5.sh --help` print the tutorial below.

## Read this first: the login lockout

On fw 5.7.0, repeated failed logins lock **every account on the receiver for about
nine hours**, so anything else that uses that receiver stops too. The countdown cannot
be cancelled, but further attempts do not extend it.

Both tools log in once and stop at the first refusal, and they refuse a password with
a comma or whitespace before sending it. If you get `login refused and NOT retried`,
fix the credentials before running again. Don't loop these tools in a shell script
with credentials you haven't confirmed work.

## Install

### gtimes

gtimes builds the receiver and local paths. `health`, `ports` and `stations` work
without it.

```bash
pip install gtimes        # into a virtualenv, or with pip install --user
timecalc -v               # check: prx5.sh needs this command on PATH
```

### RxTools (optional, `prx5.py` only)

Without RxTools, `prx5.py` still works. The disk report then shows only mounted state
and size, and `--rinex` is refused. RxTools is proprietary Septentrio software.

Download RxTools for Linux from Septentrio's support site (an account may be needed)
and run the installer. If it installs to `/usr/local/rxtools/`, `prx5.py` finds
`bin2asc` and `sbf2rin` there without any PATH change. Copies on your PATH take
priority.

Check that it works:

```bash
/usr/local/rxtools/bin/bin2asc 2>&1 | head -1
ldd /usr/local/rxtools/bin/bin2asc | grep 'not found'    # should print nothing
```

### The scripts

They are single files, so copy them wherever you like:

```bash
cp prx5.py prx5.sh ~/.local/bin/
```

## Tutorial

Replace `TEST` with your 4-character station ID and `192.0.2.10` with its address.
All times are UTC. The examples use `prx5.py`. `prx5.sh` takes the same commands,
apart from the differences in the table at the top.

### 1. Check that the receiver can be reached

This sends no login, so it is always safe:

```console
$ prx5.py ports TEST 192.0.2.10
TEST ftp      192.0.2.10:2160 open
TEST http     192.0.2.10:8060 open
TEST control  192.0.2.10:28784 open
```

If FTP is closed, downloads cannot work. See [Troubleshooting](#troubleshooting).

### 2. Check its health

`-u` asks for the password without showing it:

```console
$ prx5.py health TEST 192.0.2.10 -u myuser
Password for myuser:
TEST @ 192.0.2.10  2026-11-05T11:49:19Z
  ftp port         2160 open
  http port        8060 open
  control port     28784 open
  receiver         PolaRx5 fw 5.7.0 serial 1234567 marker TEST
  cpu              54 %  [ok]
  temperature      37 C  [ok]
  uptime           65d 11h 21m
  antenna power    on
  errors           none  [ok]
  voltage          14.93 V
  position         mode 3, 64.000000 -22.000000 70.9 m  [ok]
  satellites used  28 GLONASS:9 GPS:9 Galileo:10  [ok]
  disk 1           mounted, 88.0 % of 14894 MB
  disk status      [ok]
STATUS: OK
```

Read the tags and the `STATUS` line. `errors ANTENNA` with `antenna power OFF` means
an antenna short, and no data is being recorded. Voltage is reported as a value only
and never changes the status, because its limits depend on your power supply.

| Exit code | Meaning |
|---|---|
| 0 | ok |
| 1 | warning (for `download`: some files not fetched) |
| 2 | critical |
| 3 | unreachable, login refused, or unknown |
| 64 | bad usage |

| Check | Warning | Critical |
|---|---|---|
| Temperature | below -10 or above 50 °C | below -20 or above 60 °C |
| CPU | 75 % | 90 % |
| Disk | 90 % | 97 %, full, or none mounted |
| Receiver errors | any other error bit | `ANTENNA` |

### 3. Save the address

```bash
prx5.py health TEST 192.0.2.10 -u myuser --save
prx5.py health TEST -u myuser      # the address now comes from the file
prx5.py stations                   # lists what is stored
```

### 4. Stop typing the login

```bash
mkdir -p -m 700 ~/.config/polarx5-tools
install -m 600 /dev/null ~/.config/polarx5-tools/credentials
printf 'username = myuser\npassword = mypass\n' > ~/.config/polarx5-tools/credentials
```

`prx5.py health TEST` now works alone, and the rest of this tutorial assumes this. See
[Credentials](#credentials) for other ways.

### 5. See what the receiver holds

This logs in and reports, but downloads nothing. `-D 3` means the last 3 complete
sessions, here the last 3 days:

```console
$ prx5.py download TEST -D 3 --dry-run
available                 6976413 gpsdata/2026/nov/TEST/15s_24hr/raw/TEST202611020000a.sbf.gz
available                 6978205 gpsdata/2026/nov/TEST/15s_24hr/raw/TEST202611030000a.sbf.gz
available                 7042744 gpsdata/2026/nov/TEST/15s_24hr/raw/TEST202611040000a.sbf.gz
3/3 files available on the receiver
```

### 6. Download

```bash
prx5.py download TEST                   # yesterday's 15s_24hr file
prx5.py download TEST -D 7              # the last 7 days
prx5.py download TEST -s 1Hz_1hr -D 6   # the last 6 hours of 1 Hz data
prx5.py download TEST --start 2026-11-01 --end 2026-11-03   # 3 days: --end is inclusive
prx5.py download TEST -s 1Hz_1hr --start 2026-11-03T14 --end 2026-11-03T18   # 5 hours
```

A 7 MB daily file can take about 2 minutes over a mobile link. Progress is shown while
it runs:

```text
[1/3] fetching TEST3060.26_.gz (6.98 MB)
  3.82 MB of 6.98 MB   55 %  ETA 0m52s  61 kB/s
```

Files go to `./gpsdata/YYYY/mon/STA/SESSION/raw/`. You can run the same command again
at any time: finished files are skipped and interrupted ones resume.

### 7. Use it in a script

```bash
prx5.py health TEST --json > status.json || echo "exit code $?"
prx5.py download TEST -D 1 -o /data/gnss || echo "some files are missing"
```

Progress goes to stderr, and the live bar appears only in a terminal, so logs stay
clean.

## Reference

### Addresses and ports

```text
prx5 download STA [HOST[:FTP_PORT]]     [options]
prx5 health   STA [HOST[:CONTROL_PORT]] [options]
prx5 ports    STA [HOST]                [options]
prx5 stations [--cfg FILE]
```

The port after `HOST:` belongs to the command: FTP for `download`, control for
`health`. Each value is taken from the command line first, then the stations file,
then the default: FTP 2160, HTTP 8060, control 28784.

### Download options

| Option | Effect |
|---|---|
| `-s`, `--session` | `15s_24hr` (default), `1Hz_1hr` or `status_1hr` |
| `-D`, `--sessions-back N` | The last N complete sessions: days for `15s_24hr`, hours for `1Hz_1hr` and `status_1hr`. Not with `--start` or `--end`. With `--unfinalised`, also the running session. |
| `--start`, `--end` | UTC, `YYYY-MM-DD` or `YYYY-MM-DDTHH`. **`--end` is inclusive.** With only a date, an hourly session means the whole day. |
| `-o`, `--outdir` | Root of the tree, default `./gpsdata` |
| `--flat` | Put all files directly in `--outdir` |
| `--dry-run` | Log in and report what the receiver has, writing nothing |
| `--print-paths` | Print the paths: no network and no credentials |
| `--unfinalised` | Also fetch a file still being written, and gzip it locally |
| `--force` | Fetch even when the local size already matches |
| `--retries N` | After network errors, default 2. A refused login is never retried. |
| `--active` | Start in active FTP instead of passive |
| `-u`, `-p` | Login user and password, see [Credentials](#credentials) |
| `--anonymous` | No login, for firmware before 5.7 |
| `--base-path` | On the receiver, default `/DSK1/SSN/` |
| `--rinex [VERSION]` | `prx5.py` only: also convert with `sbf2rin`. Headers come straight from the receiver, so treat the output as a quick look. |
| `--timeout` | Network timeout in seconds, default 30 |

### Expected receiver layout

The tools expect these logging sessions on the receiver:

| Session | Directory | Frequency | Local name ends in |
|---|---|---|---|
| `15s_24hr` | `LOG1_15s_24hr` | daily | `a.sbf.gz` |
| `1Hz_1hr` | `LOG2_1Hz_1hr` | hourly | `b.sbf.gz` |
| `status_1hr` | `LOG5_status_1hr` | hourly | `c.sbf.gz` |

gtimes builds both paths:

```text
receiver  /DSK1/SSN/LOG1_15s_24hr/26307/TEST3070.26_.gz
local     gpsdata/2026/nov/TEST/15s_24hr/raw/TEST202611030000a.sbf.gz
```

`PolaRx5_TEST_config.txt` sets up this layout. It comes from a working fw 5.7.0
receiver with its identity, network and account settings removed, and the marker set
to `TEST`. Before using it:

1. Change the values under "Station identity" at the top of the file.
2. Make sure the receiver has a user account, because the file doesn't create one.
3. Log in on the control port, send the file's commands, then save them with
   `eccf, Current, Boot`. Without that, a reboot loses them.

This file has been checked for content, but it hasn't been applied to a receiver in
this form, so check a spare receiver first if you can.

If your receiver logs under other names, change `SESSIONS` in `prx5.py` or the
`case "$SESSION"` block in `prx5.sh`.

### Download outcomes

A file downloads to `<name>.part`, resumes after an interruption, and gets its final
name only once its size matches and `gzip -t` passes.

| Outcome | Meaning |
|---|---|
| `fetched`, `present`, `available` | The file is in place, or can be (`available` is from `--dry-run`) |
| `absent` | The receiver doesn't have it |
| `unfinalised` | Only the bare file exists, still being written. Use `--unfinalised`. |
| `failed: ...` | Retries used up. The partial file stays for the next run. |

### FTP modes

FTP uses a second connection for the data. The tools start in passive mode and switch
to active once if the transfer can't start, and `--retries` then applies. If neither
works, the run stops with exit code 3. `ftp_mode = active` in the stations file starts
a station in active mode.

### Stations file

`--save` writes the address to the stations file. The file is `--cfg FILE`, then
`$PRX5_STATIONS_CFG`, then `~/.config/polarx5-tools/stations.cfg`. See
`stations.cfg.example`.

Any INI file with these keys can be read. `--save` refuses a file that has other keys,
so that it can't change a config maintained some other way.

### Credentials

Fw 5.7.0 requires a login for FTP and for control commands. The tools look in this
order:

1. `-u USER` and `-p PASSWORD` on the command line. With `-u` alone, the tool asks for
   the password without echoing it.
2. Environment variables `PRX5_USER` and `PRX5_PASS`.
3. The file given with `--cred-file`.
4. `~/.config/polarx5-tools/credentials`, which must be mode 600 and is read as data:

```ini
username = <user>
password = <password>
```

`-p` is convenient but leaves the password in your shell history and visible in `ps`
while the tool runs. Without credentials, `health` sends no login at all and reports
`Not authorized` from a 5.7.0 receiver, which is safe. `download` refuses to start
unless you pass `--anonymous`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `login refused and NOT retried` | Wrong user or password. Fix it before running again, because repeated failures lock the receiver. |
| `receiver locked by failed logins` | Wait for the countdown. While locked, the receiver can also claim a user with a password is needed, which doesn't mean the accounts are gone. |
| `Not authorized` | No credentials were given, or `--no-login` was used. |
| `FTP port ... refused` right after a firmware upgrade | Upgrading to 5.7.0 turns FTP off. On the control port, after logging in, send `sis, all, FTP` then `eccf, Current, Boot`. The receiver keeps logging meanwhile. |
| Control port closed, TLS port 28783 open | The upgrade left the receiver in `sis, secure`. Set it back from a TLS connection. |
| `passive FTP data connection failed ...: trying active` | Normal. Many routers refuse passive data connections. `prx5.sh` also prints a `curl: (7)` line here. |
| `no data connection in either mode`, though login and `--dry-run` work | The router refuses passive, and the receiver can't connect back to you for active. Over a VPN, active can fail now and then, so try again, or run from a computer on the receiver's own network. |
| HTTP port won't serve the web UI | 5.7.0 can redirect to HTTPS, which a forward of port 8060 doesn't carry. These tools use FTP, not HTTP. |
| `absent` for a date you expect | The receiver's disk holds a limited history. |
| No satellites, `errors ANTENNA`, `antenna power OFF` | Antenna overcurrent. A reset won't clear it while the fault remains, so check the antenna and cable. |

## Development

```bash
pytest tests/test_standalone_prx5.py
```

The tests run both scripts against a fake receiver and FTP server on loopback, and
never touch a real receiver. Keep the login count assertions: they are what caught
the bash version logging in once per file with a wrong password.
