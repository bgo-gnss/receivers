# Septentrio PolaRX5 — Documentation Index

Reference documentation for configuring and communicating with Septentrio PolaRX5 GNSS receivers.

## Documents

| File | What it covers |
|------|---------------|
| [`tcp-command-reference.md`](tcp-command-reference.md) | Complete TCP/IP command reference — login, user management, IP services, SBF output, configuration, health |
| [`fw570-migration.md`](fw570-migration.md) | fw 5.7.0 migration guide — what changed (EU RED), provisioning a new receiver, HTTPS/FTP decisions |

## Firmware Manuals

| Version | Reference Guide | Release Notes / Card |
|---------|----------------|----------------------|
| **5.7.0** | [`firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Reference-Guide.pdf`](firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Reference-Guide.pdf) | [`firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Release-Notes.pdf`](firmware/5.7.0/PolaRx5-Firmware-v5.7.0-Release-Notes.pdf) |
| **5.5.0** | [`firmware/5.5.0/PolaRx5-Firmware-v5.5.0-Reference-Guide.pdf`](firmware/5.5.0/PolaRx5-Firmware-v5.5.0-Reference-Guide.pdf) | [`firmware/5.5.0/PolaRx5-Firmware-v5.5.0-Reference-Card.pdf`](firmware/5.5.0/PolaRx5-Firmware-v5.5.0-Reference-Card.pdf) |

## Quick Reference — fw 5.7.0 Breaking Changes

Receivers shipped with fw 5.7.0 have three behaviours that differ from all earlier firmware:

1. **TCP connections require `login` before any command.** The previous unauthenticated prompt is gone.
2. **No default user exists.** The first login must use factory credentials (`RxAdmin` / `S3pt3ntr10`).
3. **FTP is disabled by default.** Must be explicitly enabled with `setIPServices`.

See [`fw570-migration.md`](fw570-migration.md) for the full provisioning procedure.
