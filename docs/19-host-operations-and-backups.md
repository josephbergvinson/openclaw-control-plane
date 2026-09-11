# Host operations and backups

The [exported installation runbook](../workspace/runbooks/host-maintenance.md) and [backup runbook](../workspace/runbooks/backups-and-restoration.md) give the concrete setup and operating steps.

The operating setup extends beyond the OpenClaw Workspace. macOS launches the gateway and node, an external volume holds source and runtime data, developer tools create disposable output, container software owns separate VM state, and backup software provides a potential independent recovery layer.

The reference treats these as explicit dependencies with separate owners. It does not claim that a configured backup destination or a healthy agent process makes the whole machine recoverable.

## Maintenance classes

Routine host maintenance uses native OpenClaw command jobs, with checked helpers and a stored reporting route. A representative daily order leaves room between storage cleanup and later checks; choose actual times and timezone for the adopter's workload.

| Job class | Cadence | Mechanism |
| --- | --- | --- |
| Storage and runtime cleanup | Daily | Run host-cache cleanup, release retention, promotion-record retention and retired-execution retention; check every child result |
| Workspace integrity | Daily | Reconcile classified residue while preserving source edits and unresolved work |
| Storage headroom | Daily | Measure internal/external capacity, run permitted cleanup when needed, measure again |
| Launch definition integrity | Daily | Validate managed definitions and repair only supported templates |
| Gateway health/auto-heal | Daily | Read readiness and supported status; use the configured bounded recovery path when applicable |
| Health audit | Daily, deeper variant weekly | Combine runtime, delivery and recent maintenance evidence; perform supported task-ledger maintenance |
| Cloud backup start | Weekly | Request fresh scanning/upload through the backup client's supported interface after admission |
| Cloud backup follow-up | Daily | Observe progress, preserve interruption state and resume only when its admission contract allows |

The daily and weekly health variants are separate schedule entries. This gives nine maintenance definitions. The intended reporting contract can include concise success and failure reports; no-change domain monitors may instead remain silent. Follow the stored route and message class.

A launchd backup supervisor runs independently of the daily gateway job. It observes resource state and can request a pause; it does not start backups or delete the provider's catalog. See [scheduling](12-scheduling-and-background-work.md) for definition/environment binding and natural-run acceptance.

## Guarded storage cleanup

Cleanup is a classification process. Known generated output may become eligible by age, ownership, format and inactivity. Source repositories, active databases, credentials, selected releases, signed products, recovery archives and unknown layouts retain their own policies.

The host cleaner checks both process references and open-file state, captures eligible entries without overwriting a replacement, re-checks their identity, and records its outcome. Runtime retention separately protects the selected release, rollback dependencies and active references. A child failure prevents the wrapper from reporting overall success even if another child reclaimed files.

For Node compile caches, namespace/version and on-disk format matter. A directory named “cache” is insufficient evidence. Recognized running Node versions protect their namespaces, and a changed or unrecognized layout remains preserved until the classifier supports it. Keep these rules close to the implementation rather than reproducing a stale binary-format checklist in prose.

Resource pressure is broader than disk free space. Builds, large Node processes, swap growth and backup catalog loading can compete. The headroom report measures before and after cleanup; it cannot promise that other applications will not consume the recovered capacity moments later. Logical sparse-file size is not allocated size, and neither establishes exclusive reclaimed bytes during concurrent work.

The Backblaze guards retain swap occupancy and growth as diagnostics. Their current admission and pause decisions use kernel pressure, free/reclaimable RAM, catalog size, disk headroom and exact active compiler names, including `tsgolint`. High swap occupancy alone does not establish current pressure.

## Local recovery and Backblaze have different jobs

| Layer | Purpose | Required evidence |
| --- | --- | --- |
| Stopped-state snapshot | Roll back one runtime activation with matching code/configuration/state | Immutable snapshot bound to its predecessor and successful checked restore |
| Local recovery archive | Recover supported OpenClaw state plus selected Workspace/history supplements | Native archive verification and an isolated restore/readback under the archive contract |
| Backblaze offsite layer | Recover covered files after loss of the local storage | Correct installation/account and volume selection, fresh upload completion, coverage and independent restore |
| Container/application export | Recover data owned by a VM or application | Supported export, verified contents and an import/readback appropriate to that application |

The local recovery engine uses the runtime's native backup owner for application state and SQLite capture, with selective Workspace/policy and Git-history supplements. A native restore is tested in a fresh private directory without activating restored state. Capture semantics, included roots and exclusions belong to that versioned engine; a file copy is not a substitute.

The current maintenance design assigns weekly cloud initiation and daily follow-up to Backblaze. Retained local archive engines and historical generations remain useful recovery resources; their presence does not mean a second periodic local producer is enabled. Schedule ownership must be declared explicitly to avoid overlapping producers or cleanup of a still-required recovery set.

Backblaze Personal Backup is filtered file backup, not a byte-for-byte VM or disk image. Supported-file coverage and a current scan must be established for the selected external volume. Do not infer coverage of live databases or raw virtual disks from the parent directory appearing in the client.

## Backup admission and supervision

The start/check helper binds a pending request to the installed backup identity. It verifies selected volumes, external filesystem identity, configured temporary-data location and available resources before asking the supported client to work. It saves request/schedule state so interruption does not erase what was requested.

The independent internal supervisor uses lightweight local status and process observations. Under resource pressure or an installation hold, it requests the supported pause and records that request separately from observed worker drain. Both controls share a lock and persistent pause/hold state. An installer transition must participate in that ownership boundary, and a new identity cannot inherit an old installation's accepted request by accident.

Install the supervisor and its shared resource module together. Compare the deployed source identities with the canonical versions before relying on their receipts. Keep essential supervision state on storage that remains accessible when the external data volume or vendor installation is unavailable.

Cloud completion requires more than a zero remaining count. Bind the request, fresh per-volume scan, upload progress/completion and eligible namespace coverage. Verify a later restore of representative required material, and use a full inventory comparison when a retention contract depends on complete recovery. Account enrollment, a license, a trial, a paused client and a requested upload are configuration/lifecycle states; none independently proves recovery.

Automated full cloud restore qualification and full container image/VM recovery are separate acceptance gates. They are not conferred by this reference, a source test, or successful storage relocation. An adopter should keep each unqualified recovery path labelled pending and retain the original until its required proof exists.

## OrbStack, Docker and developer tools

OrbStack can supply the Docker engine for local development while keeping its application-managed data on external storage. Docker Desktop may also be installed; each context selects a different engine. Use an explicit configured context in operational commands instead of assuming the shell default identifies the intended service.

For an adopter who has created the `orbstack` context, these are read-only checks:

```sh
docker context ls
docker --context orbstack info
docker --context orbstack ps -a
docker --context orbstack volume ls
```

These inspect the selected daemon. They do not start containers, export volumes or prove their recoverability. Named-volume data, images, build cache and the application's raw disk are separate preservation classes. Use the application's supported location/export/import mechanisms and verify each required recovery path.

Developer applications also distinguish data classes. An external Xcode application can have a compatibility link, while DerivedData, compilation caches and distribution archives use Xcode's location preferences. Archives may be valuable signed products even when nearby build caches are disposable. Use supported application settings rather than relocating arbitrary active application databases.

The exported host package also includes the complete Claude stale-worker guard, a Keychain-backed desktop runtime environment helper, and the optional macOS keepawake definition. These are disabled adoption inputs. The worker guard binds signed replacement code, parent/process identity and durable attempt receipts; the environment bridge resolves configured state/cache paths and credential references without storing credential bytes.

Remote-access tools, shell toolchains and coding-agent applications are adjacent dependencies. Inventory their executable/configuration locations and ownership without publishing credentials, private network coordinates or account-specific state.

## Example: build, cleanup and backup

An operator schedules a weekly cloud backup after normal development work. Before starting, the helper finds a large active build and defers with a precise resource result. No upload is claimed. The next eligible check observes that the same pending request remains incomplete, rechecks the installation identity and available resources, and requests the backup through the supported client.

During scanning, the supervisor detects renewed pressure and requests a pause. Its receipt first says “pause requested”; a later process observation establishes whether the worker stopped. After resources recover, the daily owner decides whether its existing resume policy permits continuation. A completed upload then leads to coverage and restore verification, not immediate deletion of every local recovery copy.

Daily cleanup runs under its own classification rules throughout this workflow. It may reclaim an inactive known cache, but it preserves the active build, current runtime, database state and recovery archives. The final operator report states the actual changed files or backup state and the next unresolved gate.

## Reproducing the helpers

Copying a wrapper alone is insufficient. Preserve its local dependency graph, native subprocess requirements, registry/control references, launch template, declared environment and result tests. The helper families include the activator, release-retention metadata, cron/process receipts, root-drift classification, volume checks, resource checks and shared effect predicates.

Parameterize operator paths, source/state roots, timezone, selected container context, reporting route, retention classes and backup installation binding. Derive authentication-order checks and protected-source identities from the adopter's own configuration. Never transplant an existing operator's identity digest as a universal default.

The independent Backblaze package pins four files: watchdog, shared resources, the native volume guard and the operator contract. All must be installed internally and appear with matching hashes in a later natural receipt. The optional first-catalog path preserves one-attempt and cancellation semantics, but its native publication fingerprints and stat tuples come from independently reviewed adopter evidence. Missing evidence fails closed. See the [bootstrap runbook](../workspace/runbooks/backblaze-bootstrap.md) for exact commands and the distinction between scan publication, catalog creation, uploaded coverage and restore proof.

Validate the exported helpers in an isolated fixture environment first. Then verify configured bindings, a permitted manual run, its observable effect and delivery, and a naturally scheduled run. [Capability provenance](03-capability-provenance.md) and the [adoption guide](17-adoption-guide.md) distinguish shipped mechanisms from installation-specific proof.
