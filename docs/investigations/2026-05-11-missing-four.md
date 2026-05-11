# 2026-05-11 — investigation: 4 stations missed yesterday's 24h file

Post-deep-fix baseline night dropped chronic missing-raw from 32 → 2 expected
+ 2 surprise. Investigation finds three distinct operational causes plus one
silent-hang code bug.

## Baseline (pgdev)

154 targeted (non-passive, non-broken). 146/154 (94.8%) had yesterday's file
by 01:00 UTC. 3 more recovered via `morning_recovery` at 01:30. Final: 4 stuck.

## Per-station diagnosis

### AUST (PolaRX5) — chronic network. Working as designed.

- `ping -c5 10.6.1.156` → 20% loss, 169–339ms RTT (high latency + drops)
- Primary 00:01, second-chance 00:36, morning_recovery 01:30 all fired (per
  `~/.cache/gps_receivers/logs/stations/AUST.log`).
- All attempts: `stall_timeout` (no data in 10s) or `connection refused`.
- User downloading manually from laptop.

### BAUG (NetR9) — receiver HTTP daemon dead. Operational fix.

- TCP host reachable, but `:8060` returns `[Errno 111] Connection refused` on
  every health check (every 5 min) and the 00:01 download attempt.
- Pre-flight port check correctly short-circuited: `Failed: BAUG (15s_24hr)
  [other] - HTTP port 8060 not responding (3.3s)`.
- `morning_recovery` at 01:30 did NOT retry BAUG (no `Starting download` line
  in BAUG.log for that window — suspect `should_skip_station()`
  consecutive-failure backoff suppression, 7 failures in 48h).
- Fix: reboot/touch receiver HTTP service. Code is doing the right thing.

### FJOC + SKDA (NetRS) — silent HTTP hang. Code bug + slow receiver.

**The surprise.** Both stations show identical pattern:

| Time | FJOC | SKDA |
|---|---|---|
| 00:01:00 | `Starting download` ✅ | `Starting download` ✅ |
| 00:01:05 | `Downloading FJOC202605100000a.T00` (line 209) | `Downloading SKDA…` |
| 00:04:03 | next event: health check (3-min silent gap) | (same gap) |

No completion log. No error. No exception. The download thread is still
**holding the TCP socket 9+ hours later**:

```
$ ssh gpsops@rek-d01 ss -tnp | grep -E "10\.6\.1\.100|10\.4\.2\.178"
ESTAB  10.170.80.15:47998  10.6.1.100:8060  receivers(pids 3169660,3588908-11)  # FJOC
ESTAB  10.170.80.15:43432  10.4.2.178:8060  receivers(pid 3169660)              # SKDA
CLOSE-WAIT  10.170.80.15:42408  10.4.2.178:8060  receivers(pids 3588908-11)     # SKDA (old, remote closed)
```

**Root cause** — `src/receivers/trimble/netrs_http_download_client.py:222`:

```python
response = self.http_client.session.get(
    full_url,
    stream=True,
    timeout=(self.connect_timeout, None),   # ← read timeout = None
    auth=self.http_client.auth,
)
```

`requests`' read timeout is `None` → `for chunk in response.iter_content(...)`
blocks forever if data arrives slower than `chunk_size=65536`. The 180s
`self.stall_timeout` check at line 261 only fires when iter_content RETURNS an
empty chunk; a fully-blocked socket read never reaches that branch.

**Operational trigger**: user reports the receiver is currently serving the
file but very slowly (manual laptop download in progress at the same time).
Yesterday it served fast (00:01:46 completion). Today the receiver/router is
in a slow state and the missing read timeout exposes the hang.

**Morning_recovery at 01:30** fired for both FJOC and SKDA (per per-station
log: `Starting download: FJOC (15s_24hr)` and `Found 1 files in tmp directory
that need archiving`) — then went silent again, hung on the same code path.

**Non-drastic fixes available**:
1. Restart scheduler — frees the 4 hung threads. Next 00:01 may re-hang if
   receiver still slow.
2. One-line code fix: `timeout=(self.connect_timeout, self.stall_timeout)` —
   gives `requests` a real read timeout, raises `ReadTimeout` on
   slow-trickle that the existing exception handler logs as `failed`.

The receiver-side slowness is operational; the silent infinite hang is the
code bug.

## What worked

- `morning_recovery` fired correctly at 01:30 for AUST + FJOC + SKDA.
- `should_skip_station()` backoff for BAUG (if confirmed) is doing its job.
- Deep-fix night dropped chronic cohort 32 → 2 expected + 2 + 4 NetRS-hang
  cases that don't show up at this severity until the receiver gets slow.

## Open follow-ups

- Add read timeout to NetRS HTTP download client (PR).
- Decide whether to add similar timeout to NetR9 `http_download_client.py`.
- Confirm `should_skip_station()` is the reason BAUG wasn't in
  morning_recovery — grep for "skip" in receivers.log near 01:30.
