# Exact staged archive recovery

The AFST recovery path uses existing `receivers rinex` publication and
`archive-reindex`, followed by the metadata-only `archive-access` command. No
scratch publisher, automatic retries, completed-stage replay or scheduler changes.
**Implementation/tests are not deployment or production-write approval.**

## Inputs and authority

- File sets remain standard **sha256sum manifests**: on-disk SHA256, two spaces
  (or space-star), then `YYYY/mon/SID/session/rinex/FILE`.
- Full candidate scope; exact catalog-repair subset; exact publication subset;
  and existing-original-copy subset are separate manifests. Publication and its
  original-copy manifest must name identical relative paths. Recovery currently
  admits dateable short-name daily observations only; unsupported long names
  refuse, never become NULL-date catalog rows.
- These are **accepted staged products**, not a request to reconvert, repair
  headers or repeat scientific QC. Checksums prove identity, not scientific
  acceptance. AFST's exact ten accepted exceptions and permanent audit scope
  incident remain attached; never replay manifests containing raw entries.
- Original copies must already exist. The tool hashes/checks them; it never
  recreates, overwrites or cleans them. Preserve R2 originals at 0664 and BNC/
  modern original copies at 0644.
- Coordinate source, archive data **and parent namespaces**, including any
  previously issued/in-flight work. Exclusion is external: checks are not a
  distributed lock/CAS, and stopping a caller does not cancel kernel/NFS work.
- Live operations require the reviewed action-file SHA and a **fresh actual**
  `--owner-until` ISO8601 expiry with timezone. It is not an automatic window
  grant or renewal. AFST's two old confirmations remain consumed/expired.

The last captured AFST state requires **145 replacements, 195 catalog keys per
host, and 1716 mode corrections across 1861 final names**. Those are captured
counts, not a fresh live census; a changed preview needs review.

## 1. Read-only action preview

Run with approved code on the chosen executor. `$READ` must be the authoritative
archive read mount, **not** staging. All catalog endpoints are explicit; production
localhost is admitted only on verified rek-d01, never silently on a laptop.

```bash
receivers archive-reindex --dir "$READ" --catalog-prod --station "$SID" \
  --scope-manifest scope.sha256 --manifest catalog-subset.sha256 \
  --publish-manifest replacements.sha256 --publish-root "$STAGE" \
  --originals-manifest originals-subset.sha256 --originals-root "$ORIGINALS" \
  --write-plan catalog-plan.json
```

This creates a new local action file (refuses overwrite), not catalog DML. It
checks both catalogs' complete scoped identity/path/date/NULL-MD5/hour shape and
initial parity, then pins the reviewed roots, destination, hosts, file sets,
source/original manifest hashes and archive file/parent identities. Production
owner UID is obtained by a capability-only gateway query (no archive opens).

Inspect the exact output and copy its SHA into `$CAT_SHA` only after review.
The action file is a precondition snapshot, **not write authority or a ledger**.
No unguarded legacy reindex walk is used for these manifests.

## 2. Already-staged data only — preview, then separate approval

```bash
receivers rinex "$SID" --push-only --manifest replacements.sha256 \
  --work-dir "$STAGE" --source-dir "$READ" --catalog-prod \
  --recovery-plan catalog-plan.json --plan-sha256 "$CAT_SHA" \
  --originals-manifest originals-subset.sha256 --originals-root "$ORIGINALS"
```

No `--push` means preview. `--dry-run` always overrides `--push`. The recovery
plan path verifies original copies against the current baseline and candidate
hashes; it performs **no raw discovery, conversion or size/scientific-QC probes**.
Already-installed candidates are omitted. Unknown bytes, owner/mode changes,
replaced inodes or parent namespaces refuse. Bare `--push-only` without this
recovery profile retains the ordinary raw-availability/size guards: **do not use
that bare path for AFST**.

After reviewed scope and actual owner coordination, add `--push --owner-until
"$OWNER_UNTIL"`. It reuses files-from rsync (`--checksum`, D0755/F0644,
`--no-implied-dirs`), preserving existing parent-directory attributes. It sends a
singleton first, verifies actual archive hash/mode read-back, then batches of at
most 50. It never moves/deletes originals or uses old incoming objects. Catalog
writes are deliberately deferred to phase 3. A partial result stops; reconcile
actual state and review before any further invocation. Reinvocation does not
rewrite already-correct data, including after access restoration.

## 3. Exact catalog-only reconciliation

```bash
receivers archive-reindex --dir "$READ" --catalog-prod \
  --plan catalog-plan.json --plan-sha256 "$CAT_SHA"
```

Default is preview. Every selected archive file must already match its approved
compressed SHA **before any catalog DML**. Hashes come from no-follow archive FD
read-back (Linux `/proc`), not assumed delivery from staging. Both compressed and
compression-decoded SHA256 values and size are checked.

After approval add `--execute --owner-until "$OWNER_UNTIL"`. Both catalog hosts
are mandatory. Full-scope guards repeat at bounded write intervals; each row is
checked/locked on its writer connection, the actual upsert has a prior-state CAS
predicate (including insert races), and its final row is checked before commit.
**Any non-NULL `md5checksum`, `md5uncompressed` (including empty strings), or daily
`file_hour` (including zero) refuses — never cleared/recomputed.** Known
baseline/verified poststates allow reconciliation of a reviewed partial outcome;
unexplained changes refuse. Cross-host writes are not atomic. On error stop,
report divergence/unknown outcomes, and reconcile rather than blindly retrying.

## 4. Access restoration — only after all data and catalogs verify

Prepare a full `access.sha256` containing all published RINEX plus required
`rinex_org` names. `aliases.json` must explicitly map **every** final name to its
known private hardlink aliases (`[]` for no aliases). Unknown/extra links refuse.
Private alias paths must end in the public relative path; all private parents
must remain owned/0700. No alias discovery or directory scan occurs.

`protected.json` is a list of objects with `root`, `manifest`,
`manifest_sha256`, and integer `mode` (**436 = 0664**, **420 = 0644**). Its
explicit RINEX-only manifests must cover all original observations exactly once;
BNC originals use `rinex_org` paths. Live candidate objects cannot masquerade as
separate original copies. Do not feed raw-inclusive incident manifests here.

```bash
receivers archive-access --catalog-plan catalog-plan.json --catalog-prod \
  --read-root "$READ" --gateway-root /mnt/gps/gpsdata \
  --manifest access.sha256 --aliases aliases.json \
  --protected-originals protected.json --write-plan access-plan.json
```

This verifies the complete archive/catalog scope, all protected originals, both
gateway/read-mount modes and exact file/namespace/alias pins. It performs no
chmod and cannot produce a ready plan while publication/catalogs are incomplete.
The gateway worker runs with existing Python 2.7 or 3 stdlib/openat; no install.

After a **separately reviewed** access-plan SHA (`$ACCESS_SHA`) and coordinated
window, execute with `--plan access-plan.json --plan-sha256 "$ACCESS_SHA" --yes
--owner-until "$OWNER_UNTIL"` (same catalog/read/gateway arguments). A stale plan
refuses. The worker uses owned no-follow FDs and fchmod only, singleton read-back
before batches <=50, checking deadlines immediately before each effect. Bytes,
UID/GID, links, size and mtime must remain unchanged; all final modes are 0644.
Fresh final gateway, archive, catalog and protected-original checks establish the
result. A timeout is **not** cancellation proof. Preserve partial evidence and
re-preview/review; do not blindly repeat the old plan.

## Close-out is still a separate verified decision

Successful access repair is not permission to write a completion visit. Only
then finish/check canonical GAMIT (`gps-config-data/gamit/station.info.sopac.apr05`,
**okada only**, no p01 cutover), create/verify the site log with existing tools,
and review truthful completion text. Non-EPOS AFST omits M3G/EPOS dissemination
and rollout allowlist edits. The next five stations remain read-only until their
concrete proposed batch has been approved.
