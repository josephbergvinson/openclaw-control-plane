# Guards, health, and restoration

A guard checks a specific precondition and controls the operation that depends on it. Its value comes from the checked boundary: a log warning does not prevent a write, and a successful installation does not prove a later scheduled execution.

The reference uses small host helpers around native OpenClaw facilities. They should expose what they checked, what they allowed or refused, and where the result was recorded.

## Guard families

| Family | Checked boundary | Allowed consequence |
| --- | --- | --- |
| External-volume availability | Expected mounted filesystem identity and protected path bindings | Admit dependent work or refuse it; no automatic fallback state tree |
| Storage headroom | Free space and relevant resource pressure before/after bounded cleanup | Preserve a failure when adequate capacity was not recovered |
| Workspace integrity | Git/source identity and classified root residue | Remove known disposable residue while preserving unknown or conflicting source work |
| LaunchAgent integrity | Parseable definitions, valid entrypoints and required launch contracts | Repair only explicitly supported templates; preserve retired and deliberately untriggered definitions |
| Runtime health | Readiness, supported status and required operational surfaces | Report the narrow failure or use an already configured recovery path |
| Release retention | Selected, rollback, active and explicitly retained release dependencies | Remove only eligible unreferenced releases |
| Backup admission and supervision | Installation identity, resource state and pending request ownership | Start/check through the vendor interface or issue a protective pause |
| Project result guard | Expected artifact or record for a declared period | Report a missing/failed effect, with remediation only when separately configured |

These mechanisms are helper-backed. Their successful operation on an adopter's host requires installation and fresh verification. Policy can describe the invariant but cannot provide a filesystem check, process fence or completed restore by itself.

## Availability without a shadow state tree

An external-volume check must identify the filesystem, not merely test whether a mountpoint directory exists. Check protected logical paths after resolving their parents and symlinks. A historical directory with the right name cannot silently become the replacement source or state root.

Install the observer and its essential contract/receipt path somewhere that remains available if the protected volume disappears. Keep the synchronous preflight and any event observer distinct: a definition that exists on disk but has no configured trigger is not a continuously running monitor.

A successful check is a point-in-time observation. Re-evaluate it before the write it protects, and record a later disconnect as a separate incident. Do not claim that installing a guard proves all disconnect and recovery paths have been exercised.

## Narrow repair and explicit ownership

Some helpers intentionally repair; others only observe. The launch definition guard can restore a small allowlist of known templates. The headroom guard can call the same guarded storage cleaner used by daily maintenance. Neither is authorized to repair arbitrary services or delete unfamiliar directories.

The scheduled gateway health/auto-heal command depends on native dispatch. It can check and perform permitted recovery while that control path remains usable; it is not an independent rescue mechanism for a completely absent scheduler. launchd owns process supervision, and exceptional service recovery requires an available operator/control path with the correct lifecycle authority.

Runtime activation is a different operation. The [activation owner](10-runtime-releases-and-promotion.md) selects a sealed candidate under a stopped-state boundary, preserves its matching predecessor snapshot and verifies the resulting process identity. Routine health checks must not become a second promotion or rollback system.

## Health and task state

A task may complete while one optional integration is unavailable. A process may be running while its required channel cannot deliver. Report the narrowest claim supported by fresh evidence.

| Description | What the operator needs to know |
| --- | --- |
| Healthy for the checked scope | Required checks passed for the named surface and observation window |
| Usable with contained degradation | A specific capability is limited, and the containment is verified |
| Uncontained failure | A required capability still fails or its containment did not hold |
| Conflicting authority | Configured paths, selected release, loaded definitions or observed process identity disagree |
| Unknown or stale | Evidence is missing, ambiguous or belongs to an earlier state |

These descriptions need not become another runtime state machine. Native task/session state remains authoritative for its own lifecycle. The health report adds the practical consequence and remaining verification gate.

Use fresh process identity, start time and current probes for a current-outage claim. Keep historical logs and old receipts as context. Evidence from a previous release or a previous command under the same job ID cannot automatically certify the current one.

## Containment and restoration

Containment limits the effect of a known problem while retaining a route for the operator to recover. Record the reduced feature, reason, actual containment, lost functionality and re-enable condition. Verify that the problematic path stopped and that essential operator access remains usable.

Restore the bounded feature only after its re-enable condition holds. Record the regained capability and the checks that establish it. A requested pause is not a stopped worker; a loaded timer is not a successful next run; an accepted restart is not verified readiness.

A retired feature has a different disposition. Keep enough history to prevent accidental rediscovery and reactivation, but do not require an obsolete process merely because a retained template mentions it.

The [known-issue example](../examples/known-issue.example.json) is a compact way to record a containment, evidence reference and re-verification condition. It is an example record, not proof that every installation has a live issue-tracking service.

## Backup-conditioned deletion

When deletion depends on preserved recovery, verify the exact recovery contract before deletion. Bind source, destination, exclusions and required readback. A copied directory or green provider status is insufficient if the contract requires an independently restored file set.

Re-check the source identity and protected dependencies at the deletion boundary. Fail closed on ambiguous backup state, changed source, live references or missing proof. If an operation stops after partial cleanup, record what changed and retain the remainder with a recovery manifest; never describe it as a full rollback of already deleted cache entries.

Regenerable cache retention has a different contract from historical archive retirement. Cache cleanup must prove that the exact entries are known, inactive and disposable. Archive retirement must prove the required recovery copy and restore. One successful cleanup does not authorize the other.

## A bounded incident workflow

1. Name the failing surface and inspect its current owner, process and evidence window.
2. Read the supported status or readiness interface before choosing a recovery action.
3. Trace the failed boundary through launcher, interpreter, state selection, helper, effect and delivery.
4. Use the narrowest existing authorized recovery or containment path. Reconcile ambiguous side effects before a retry.
5. Verify the intended repair in the target surface, then record the remaining limitation or restored capability.

Source tests establish behavior under their fixtures. A native command run establishes that execution path. End-to-end acceptance also checks the effect and the user-facing route. [Evidence and verification](15-evidence-audit-and-verification.md) explains how to retain those claims separately; [host operations and backups](19-host-operations-and-backups.md) applies them to storage and recovery.
