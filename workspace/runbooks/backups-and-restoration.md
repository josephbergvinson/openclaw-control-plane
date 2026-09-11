# Backup and restoration operations

Backblaze, local recovery archives and runtime activation snapshots have different
owners. A current request, copied directory or zero remaining count does not alone
establish a complete recovery path. Preserve originals until the required restore
has been independently checked.

## Backblaze

Configure the vendor executable/data directory, selected mount UUID, scratch root,
internal control directory and the separate installed watchdog as described in
`host-maintenance.md`. Enroll the vendor client through its supported native flow.
The helper derives an installation fingerprint from the vendor's installation
record; do not copy another installation's digest or pending request.

`backblaze_health.py --start` requests the weekly operation only after identity,
volume, schedule and resource admission. `--check` follows the same pending request
and can resume an interrupted operation after its configured delay. Both modes can
change vendor state; they are not read-only status commands. Their JSON reports and
private artifacts distinguish admission, requested work, scanning, uploading,
interruption and verified completion.

Before a vendor installer transition, use the supported
`--prepare-installation-hold SHA256` command with the independently read current
installation fingerprint. Confirm that a pause request became an observed idle
state before proceeding. The watchdog shares the persistent hold and lock.
`--release-installation-hold SHA256` is bound to the installation identity, manual
schedule and drained-worker checks. An identity replacement must be reconciled
explicitly; an old completion cannot be inherited by a new account/client.
`--adopt-legacy-identity SHA256` exists for a supported historical-state migration;
its presence is not permission to relabel an unrelated installation.

The optional `--transition-personal-trial` command preserves the original hold and
request and creates only an identity-bound trial exception. If the new installation
has no catalog, ordinary health admission stays strict. An explicitly requested
`--bootstrap-personal-catalog` attempt additionally requires independently bound
native first-scan publication, fresh admission evidence and the complete installed
watchdog package. It may upload selected files while initializing. Follow the
[trial and first-catalog runbook](backblaze-bootstrap.md); no trial or publication
fingerprint from the originating installation is included in this reference.

Current kernel pressure, available RAM versus catalog size, free disk and active
builds determine resource admission. Exact compiler names include `tsgo` and
`tsgolint`; a similarly named unrelated process does not qualify. Swap occupancy
and growth are retained diagnostics, not independent stop or resume predicates.

Verify fresh scans of every selected volume, upload completion and actual covered
files. Restore representative required files through the provider and compare
contents/metadata with their original authority. A provider enrollment, trial or
paused catalog is not upload or restore acceptance. An independent full-inventory
receipt is required when local retention depends on complete recovery.

## Local native recovery

`openclaw_weekly_archive_backup.py` uses the native runtime backup owner for state,
including SQLite, and adds the bounded Workspace/policy and Git-history supplements.
Its current archive-v4 creation path independently verifies the produced bytes and
restores into a fresh private verification directory without activating that state.
It checks source identity, confined traversal, disk headroom, artifact shape,
checksums and native restore results before publishing a generation.

The archive command uses configured roots; running it creates a real recovery set.
The exported tests use disposable trees and native CLI fixtures. The retained v3
fixture belongs only to the tests; do not use it as a replacement producer. The
legacy daily-backup wrapper remains for supported older recovery layouts and is
not a second enabled daily producer in this reference.

After an interruption, keep the staging tree and original process/phase receipts.
`openclaw_weekly_backup_reconcile.py` accepts explicit weekly/staging/quarantine
roots, exact task/job/run identities, producer/CLI paths and those receipts. Start
without `--apply`. It rejects live owners, mismatched or incomplete evidence,
unknown additional residue and conflicting publication. Its apply path quarantines
through checked same-filesystem moves and append-only receipts; it does not invent
a completed backup or replay the original producer.

`openclaw_independent_backup_receipt.py` compares the source weekly set, an independent
backup root and an isolated restore root. Its receipt binds inventories, bytes and
separate volume roles. The retention wrapper will not treat a self-copy on the same
filesystem as independent recovery. Declare existing frozen clone-v2 generation
names and manifest digests in `backup.frozen_clone_v2_pins`; use an explicit empty
list only when the adopter has no such generations. Never copy example hashes as
actual recovery evidence. Review the complete affected generation set
before `openclaw_backup_retention_cleanup.py --apply` or the retained v3 engine's
apply path. Unknown artifacts, held locks and unverified generations remain
preserved. A local archive is still local even when every internal verifier passes.

## Containers and developer applications

Use the configured Docker context explicitly. OrbStack's application-managed disk,
images and named-volume data are separate preservation objects. Use supported
export/import paths, verify exported bytes and perform an isolated import/readback
before removing an original. A moved raw disk or a stopped empty Docker context
proves neither image nor application-data recovery.

Xcode location preferences govern DerivedData, compilation caches and distribution
archives. Cleanup must preserve signed products and active builds. Include those
application-owned preservation choices in the host inventory and qualify their
restore paths independently of OpenClaw's native state archive.
