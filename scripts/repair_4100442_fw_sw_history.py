#!/usr/bin/env python3
"""One-off TOS history repair for device 20159 (PolaRx5 serial 4100442).

Context (2026-05-30): this device was firmware-upgraded 5.5.0 → 5.7.0 on the
bench. Two problems were left in TOS:

  firmware_version  id=141404  value=5.7.0  from=2024-02-24  open
      → an earlier in-place `update-device` write overwrote the value to 5.7.0
        while keeping the original date_from, so TOS wrongly implies the device
        ran 5.7.0 since 2024. The pre-upgrade 5.5.0 period was lost.
  software_version  id=141405  value=5.50   from=2024-02-24  open
      → never updated; still the pre-upgrade value. software tracks firmware
        with a format change (firmware X.Y.Z ↔ software X.YZ), so 5.7.0 → 5.70.

Target state after repair (upgrade date 2026-05-30):

  firmware_version  5.5.0  [2024-02-24 → 2026-05-30]   (closed)
  firmware_version  5.7.0  [2026-05-30 → open]
  software_version  5.50   [2024-02-24 → 2026-05-30]   (closed)
  software_version  5.70   [2026-05-30 → open]

firmware repair is manual (PATCH the open row's value back to 5.5.0 + close it,
then open a new 5.7.0 row) because the open value is currently wrong. software
repair is a clean transition (its open value 5.50 is the correct old value).

Run dry first (default), then with --commit. Needs the VPN up (vi-api.vedur.is);
does NOT need the bench receiver — all values are known, no probe.

    scripts/repair_4100442_fw_sw_history.py            # dry-run
    scripts/repair_4100442_fw_sw_history.py --commit   # live
"""

from __future__ import annotations

import sys

ID_ENTITY = 20159
FW_OPEN_ID = 141404  # firmware_version open row (currently wrong value 5.7.0)
UPGRADE_DATE = "2026-05-30T00:00:00"
OLD_FW = "5.5.0"
NEW_FW = "5.7.0"
NEW_SW = "5.70"  # software format of NEW_FW (X.Y.Z → X.YZ)


def main() -> int:
    commit = "--commit" in sys.argv[1:]
    dry_run = not commit

    from tostools.api.tos_writer import TOSWriter

    writer = TOSWriter(dry_run=dry_run)

    # ---- Verify current state before touching anything ------------------
    hist = writer.get_entity_history(ID_ENTITY)
    attrs = (hist or {}).get("attributes") or []

    def open_row(code: str):
        rows = [a for a in attrs if a.get("code") == code and not a.get("date_to")]
        return rows[-1] if rows else None

    fw = open_row("firmware_version")
    sw = open_row("software_version")
    print(f"device {ID_ENTITY} current open periods:")
    print(f"  firmware_version: {fw.get('value')!r} from {fw.get('date_from')} "
          f"(id={fw.get('id_attribute_value')})" if fw else "  firmware_version: <none>")
    print(f"  software_version: {sw.get('value')!r} from {sw.get('date_from')} "
          f"(id={sw.get('id_attribute_value')})" if sw else "  software_version: <none>")
    print()

    # Safety guards — refuse if the state isn't what we expect.
    if not fw or fw.get("value") != NEW_FW or fw.get("id_attribute_value") != FW_OPEN_ID:
        print(
            f"❌ firmware_version open row is not the expected id={FW_OPEN_ID} "
            f"value={NEW_FW!r}; aborting (state changed since investigation).",
            file=sys.stderr,
        )
        return 1
    if not sw or sw.get("value") != "5.50":
        print(
            "❌ software_version open row is not the expected 5.50; "
            "aborting (state changed since investigation).",
            file=sys.stderr,
        )
        return 1

    mode = "DRY-RUN (no writes)" if dry_run else "LIVE COMMIT"
    print(f"== {mode} ==")
    print(f"  firmware_version: close {OLD_FW} at {UPGRADE_DATE}, open {NEW_FW}")
    print(f"  software_version: close 5.50 at {UPGRADE_DATE}, open {NEW_SW}")
    print()

    # ---- firmware_version: manual split (open value is wrong) -----------
    # 1) Correct the open row back to the real pre-upgrade value AND close it.
    writer.patch_attribute_value(
        FW_OPEN_ID, value=OLD_FW, date_to=UPGRADE_DATE
    )
    print(f"  ✓ firmware_version[{FW_OPEN_ID}] → value={OLD_FW}, date_to={UPGRADE_DATE}")
    # 2) Open the new post-upgrade period.
    writer.add_attribute_value(
        ID_ENTITY, "firmware_version", NEW_FW, UPGRADE_DATE
    )
    print(f"  ✓ firmware_version new period {NEW_FW} from {UPGRADE_DATE}")

    # ---- software_version: clean transition (open value is correct) -----
    writer.transition_attribute_value(
        ID_ENTITY, "software_version", NEW_SW, UPGRADE_DATE
    )
    print(f"  ✓ software_version transition 5.50 → {NEW_SW} at {UPGRADE_DATE}")

    print()
    if dry_run:
        print("dry-run complete — re-run with --commit to apply")
    else:
        print("committed. verify with the read query.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
