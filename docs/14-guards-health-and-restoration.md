# Guards, health, and restoration

Most of this architecture is about doing work correctly. This chapter is about the other half: knowing whether the system is currently fit to do any, containing damage when it is not, and restoring capability in a way that leaves a record. Its central claim is that a guard is an architectural component with its own lifecycle — not a script someone wrote once and hopes is still running.

## Guards are a first-class pattern

A guard is a standing precondition: a small program that asserts one invariant about the host — that a volume is mounted, that free space is above a floor, that an agent is writing into its own workspace — and that is *fail-closed*, meaning that when it cannot positively prove the invariant it stops the work depending on that invariant instead of emitting a warning nobody reads. Guards are designed as a family rather than as isolated scripts: one uniform shape, one place they are declared, and one supervisor responsible for them — the same host-level service supervisor that carries the host-layer scheduled jobs in [scheduling and background work](12-scheduling-and-background-work.md). Putting them there is deliberate. A guard that depends on the runtime being healthy cannot report that the runtime is unhealthy, and a guard that only exists as a command someone remembers to type is not a standing precondition at all.

Every guard carries three records:

| Record | Written when | Answers |
|---|---|---|
| Install plan | Before installation | What will be installed, where, under which supervisor, on what schedule, and what it is allowed to touch |
| Install receipt | At installation | What was actually installed, with the evidence path |
| Post-install verification | After an interval has elapsed | Did it actually run, and did it produce the output its contract promises |

The third record is the one that is usually missing elsewhere, and it is the one that matters most. A guard believed to be installed but not actually loaded is worse than no guard: it manufactures confidence. Installing a guard is itself a mutation of the host, so it earns the same evidence treatment as any other mutation — plan, receipt, verification — described in [evidence, audit, and verification](15-evidence-audit-and-verification.md).

Two design rules apply to the whole family:

- **A guard must not depend on what it protects.** A guard stored on the volume whose availability it asserts vanishes exactly when it is needed. A guard running inside the process it restarts cannot restart it.
- **Guards refuse; they do not repair by default.** Detection and remediation are separated deliberately, so a misfiring guard cannot cascade into an outage of its own making.

### Guard categories

These are categories, not an inventory. Each describes a class of invariant worth guarding on any host that runs an agent runtime continuously.

**External-volume availability guard.** Where the workspace, artifacts, or release store live on an external volume, a guard pinned to a written contract asserts three things in order: that the expected mount point is present and writable, that the volume answering at that mount is the expected one rather than a differently-formatted disk that happens to appear at the same place, and that every critical logical route — each alias or link the runtime resolves through — still lands on that volume. A missing mount, a read-only mount, a differing volume identity, a broken or off-volume route, or an already-active fail-safe denies every candidate-build, runtime, and artifact write. The guard never remounts and never activates a fallback path, because improvising storage is how a system ends up with two divergent copies of its own state and no way to tell which one is authoritative. Acceptance is not sticky: a dismount *after* a passing check is written as an incident record and the contract is re-evaluated from the top, so dependent work stays blocked until a fresh check passes rather than riding on a stale pass. The guard helper itself is installed off the storage it protects, so it cannot become unreachable at the same moment as the volume it watches.

**Storage-headroom guard.** Builds, promotions, large copies, and backup operations all fail badly and late when free space runs out — and the resulting log noise outlives the incident by months. A headroom guard asserts free capacity above a declared floor before space-consuming operations proceed, and refuses rather than warning.

**Workspace-isolation guard.** Per-agent state belongs to that agent's declared workspace. This guard asserts the binding, so session summaries, memory writes, and reset snapshots cannot land in a shared or default workspace. It has an **on-configuration-change variant** for a specific reason: a configuration edit can silently re-point a workspace binding without any code changing, so the invariant must be re-checked when configuration changes, not only on a timer.

**Execution-slice guard.** A *slice* is one bounded unit of work a turn commits to — one fix, one migration, one closeout — and it is the granularity that approvals, write boundaries, and closeout records are written against. Before a slice begins side effects, this class of guard asserts the preconditions the execution contract requires: exactly one execution mode selected out of inline work, a durable isolated lane, and operator-side manual containment; a visible acknowledgement delivered; the required approval token present; and the write boundaries declared. An unclassified slice is refused rather than allowed to start and be classified afterwards, because after-the-fact classification is classification by outcome — work that happened to go well gets remembered as low risk. See [execution and durable lanes](06-execution-and-durable-lanes.md).

**Retired-path guard.** Retirement is a recorded operation, and this guard is what keeps it recorded. It asserts that no live definition, alias, service, scheduled job, or checkout resolves beneath a registered retired parent. Without it, a retired root reappears the first time someone follows a stale note, and the system acquires a second source of truth. See [source layout and artifacts](09-source-layout-and-artifacts.md).

**Gateway autoheal guard.** An out-of-band watcher that can restore the supervised runtime service when it is down, through the application's own supported restart path rather than raw service-manager commands. It lives in the host service layer for the obvious reason: a healer inside the patient is not a healer. Its bounds are declared as a budget — a fixed number of restore attempts within a window — and exhausting the budget raises a blocker and stops rather than looping, because an unbounded healer turns a short outage into a restart loop that also destroys the evidence of what failed first. That failure has its own entry in the [failure catalogue](02-why-a-control-plane.md).

Provenance, in the four-level vocabulary of [capability provenance](03-capability-provenance.md): a guard family built this way sits at **helper-backed** — its mechanics are checked-in scripts run by a host-level service supervisor, with contract and incident records written to disk, rather than judgment exercised by a model. A volume-availability guard reaches **live-proven** on a given installation only once a real loss of availability has exercised it there, end to end, and its contract states exactly what that evidence has to show: the check denied dependent writes, an incident record was written, the contract was re-evaluated from the top, and dependent work stayed blocked until a fresh check passed. Short of that, the guard stays helper-backed however carefully it was installed — installation is not exercise. On a stock runtime, all of this is **policy-only** until an adopter builds it.

## Health is a separate dimension from task state

Task state answers *what happened to this unit of work*. Health answers *can this system be trusted with the next one*. They are carried as two distinct fields on the durable status artifact — the on-disk file that holds the authoritative state of one durable lane, a long-running unit of isolated work spawned out of a conversation with its own child session, worktree, and write boundaries, specified in [execution and durable lanes](06-execution-and-durable-lanes.md). Collapsing the two fields produces the two worst reporting failures at once: a green result reported over a degraded substrate, and a whole system described as broken because a single lane is blocked.

Seven classes make up the health vocabulary. They are listed narrowest first, and the ordering is descriptive rather than a severity ladder.

| Class | Means |
|---|---|
| `healthy` | Required operator-facing surfaces and the current lane work, with no material known defects |
| `healthy-noisy` | Usable, with non-blocking defects or observability noise |
| `degraded-contained` | A reversible containment is in place; the core runtime remains usable |
| `degraded-uncontained` | One or more features are still failing and affecting current operation |
| `unstable` | Restart-prone or crash-prone; not trustworthy for ordinary execution |
| `split-brain` | Live authority surfaces disagree in a way that can mislead execution or diagnosis |
| `historical-residue-heavy` | Old incident residue is heavy enough that freshness discipline is mandatory |

Five rules keep the vocabulary from collapsing back into "fine", which is where health reporting ends up whenever the classes are treated as adjectives rather than as claims.

- Health is **additive** to lifecycle state. Report both whenever runtime condition materially affects execution.
- Prefer the **narrowest truthful class**. The classes are a description, not a severity ladder to climb.
- A **contained feature loss is not healthy.** It stays `degraded-contained` until restoration is verified, not until containment is applied.
- A noisy but usable runtime stays `healthy-noisy` unless the noise materially impairs the current task. Optional, non-enabled components producing errors are a live non-blocking defect with a stated blast radius — how far the defect's effects can reach if it is left alone — not an outage, and their chatter must not crowd out fresher, higher-severity failures.
- If health is degraded enough that in-band verification cannot be trusted, **stop and request operator-side verification** rather than continuing speculative runtime changes.

The classes are a vocabulary rather than an algorithm, but the rules above resolve to one workable ordering, applied to fresh evidence each time rather than inherited from the previous report:

1. Is there fresh evidence at all? If not, the honest output is not a class but a request for verification from outside the runtime.
2. Do two live authority surfaces disagree — two roots each claiming to be canonical, or a pointer, a service definition, and a running process naming different releases? That is `split-brain`, even though every component is up.
3. Is the runtime restart-prone or crash-prone right now? `unstable`.
4. Is a containment in force? `degraded-contained` while the containment holds and the core runtime stays usable; `degraded-uncontained` while a feature is still failing in current operation.
5. Otherwise, does the remaining noise materially impair the current task? If it does not, `healthy-noisy`; `healthy` only when there is also no material known defect.
6. If nothing is failing but old incident residue is heavy enough to mislead diagnosis, `historical-residue-heavy` is the truthful class, and it obliges freshness discipline on every claim made from that evidence.

`split-brain` and `historical-residue-heavy` exist because both states are commonly misread. A split brain is not an outage — every component may be running — it is a disagreement between authorities, and the danger is that each looks correct in isolation. Heavy residue is not an outage either; it is a diagnosis hazard.

## Evidence freshness windows

**A noisy historical log is not a fresh outage.** This single confusion generates more wasted incident response than any real defect, so freshness is classified explicitly rather than assumed.

Diagnostic evidence carries one of three window labels:

| Window | Meaning |
|---|---|
| `fresh-window` | Current process identity, start time, and probes taken now |
| `mixed-window` | A blend of current and retained evidence |
| `historical-residue` | Retained logs and stale state from prior episodes |

A current-outage claim requires fresh-window evidence. Retained residue is reported separately from live failures, never merged into one severity.

Capability readiness carries its own freshness discipline in the status layer — the structured JSON files recording what has been *observed* about the machine, kept separate from the registries that declare what exists and from the contracts that declare what is permitted. Every capability row there has a freshness class — stable, volatile, reprobe-before-use, reprobe-before-write, reprobe-before-access-expansion — and every readiness probe declares a time-to-live, tiered so that local credential and readiness checks stay valid for about a day while network- or state-volatile checks expire within the hour. See [integrations and capability routing](11-integrations-and-capability-routing.md).

The consequence is the honest one: **absence of complaint is not evidence of health.** A health claim past its freshness window is stale, not true, and a capability decays to unproven by expiry as normally as it does by failure.

## The containment protocol

Containment is what happens between discovering a defect and having a fix. It has a written shape so that a contained system stays legible to whoever inherits it.

1. **Prefer the narrowest reversible containment that preserves operator access.** Disabling the surface the operator uses to reach the system is not containment; it is an outage with a rationale.
2. **Record five fields**: the feature reduced or disabled, the exact reason, the expected lost functionality, the verification target, and the re-enable gate.
3. **Verify both directions after containment**: that the unstable path is genuinely no longer being exercised, and that required operator-facing surfaces remain usable.
4. **Do not present a contained runtime as healthy.** The class is `degraded-contained` until restoration is verified.
5. **Restore one feature at a time**, unless the operator explicitly accepts a wider re-enable blast radius. Each restoration records the regained capability, the risk returning with it, the post-restore verification target, and the rollback trigger.

Retirement is the terminal case of containment: when a path, facade, or capability is not coming back, it is retired as a recorded operation with quarantined recovery evidence and validator rules that keep it from reappearing — not deleted.

## Backup-conditioned deletion

Deletion conditioned on a backup is the highest-consequence routine operation in this architecture, and it gets the strictest rule:

> **Never delete on an unverified backup predicate.** Backup verification and deletion are two separate gates.

The full boundary has seven parts, and each one closes a way that a deletion has actually been justified on a backup that did not exist.

- **Verify directly, not by inference.** A folder name, a visible path, or a returned success is not verification. Acceptable predicates are count, size, or hash manifests, archive integrity tests, restore-list checks, or explicitly documented exclusions.
- **Unverified, partial, failed, or ambiguous means stop.** The run moves to blocked or waiting-approval and names the exact failed predicate — not a generic cleanup status.
- **Checkpoint immediately on a failed verification**, especially when the attempt increased disk pressure or materialized a synchronized copy locally. Do not wait for the next operator prompt.
- **Changing backup semantics requires fresh narrow approval.** Loose-file copy to archive, exact mirror to partial backup, full backup to dependency-excluded backup — each is a different contract, and switching between them silently is how a "verified" backup ends up missing the only directory that mattered.
- **Re-check immediately before deleting.** Both source and backup are re-read at the deletion gate, and the operation stops if the predicate no longer holds.
- **Never delete a path that backs a live service.** Before anything runtime-adjacent is removed, every link, alias, and pointer leading to it is resolved to its end, because a disposable-looking worktree — a separate working directory attached to a repository — can be exactly what a running service is executing from.
- **Completion states the whole shape**: source retained or deleted, backup location, verification predicate, exclusions, failed paths, and the next safe action.

Deletion of this class is operator-side containment work by default, not autonomous lane work. See [policy and authority](05-policy-and-authority.md).

## Runbook: gateway incident

The gateway is the single long-lived process that is the runtime, so "the gateway looks unhealthy" is the most common operator observation there is. The steps below are ordered, and the order is the point: each one exists to stop a specific way the diagnosis goes wrong when the steps are taken in a different sequence.

1. **Classify evidence freshness first.** Do not diagnose a current outage from retained log lines.
2. **Probe readiness before spending retries.** Wait boundedly for the service to report ready with no failing components. A readiness timeout consumes no retry budget and triggers no lifecycle action, so a probe cannot race a service that has opened its listener but is still settling.
3. **Run the health probes serially**, each in its own process group. Reclaim and diagnose a timeout before at most one isolated retry; a residual process group blocks both the retry and any closeout.
4. **Separate required surfaces from optional components.** Failures confined to optional, non-enabled plugins or channels are a non-blocking defect with a stated blast radius.
5. **Never restart the runtime from the lane that depends on it.** Use the application's own supervised restart path, not raw service-manager lifecycle commands. A service-level reload of the gateway itself requires an out-of-band operator lane, with the outage risk surfaced first.
6. **If a restart does not converge, stop at persisted-not-applied.** Preserve the evidence; do not improvise a fallback restart. See [runtime releases and promotion](10-runtime-releases-and-promotion.md).
7. **If in-band verification cannot be trusted, request operator-side verification** instead of continuing speculative changes.
8. **Record the health class and any containment**, and open a known-issues entry if a feature stays reduced.

## Runbook: tool or lane failure

The second everyday failure is narrower than an unhealthy gateway: one tool will not work, or one lane cannot proceed. This runbook exists to stop the two reflexes that make such failures expensive — retrying blind, and narrating as though progress were happening. One warning about vocabulary: *lane* carries two senses in these steps. A durable lane is a unit of isolated work; a capability lane is one tier of the routing ladder — verified integration first, then browser automation, then host automation — described in [integrations and capability routing](11-integrations-and-capability-routing.md).

1. **Classify the tool on three axes before acting**: available (it exists and can be called), authorized (policy and posture permit it), approval-satisfied (any required token has been granted).
2. **If any axis is false, stop.** Do not blind-retry, and do not narrate as if work is progressing.
3. **Report the exact missing condition**, state `waiting-approval` when approval is the blocker, and name which approval is missing.
4. **Give the narrowest next step**, then stop retrying until something materially changes.
5. **Probe before declaring a capability unavailable.** Read the structured status layer and run the declared probe. A registry row is evidence, not permission; if the probe fails, report the precise blocker and request only the minimum operator-only step; if no reliable probe exists, mark readiness unknown rather than guessing.
6. **After a first capability lane fails, acknowledge it once and continue on the next viable lane** rather than stalling on repeated explanations while a practical fallback exists. A failed preferred lane is not a blocked task while another authorized lane can still finish the work.
7. **After setup, breakage, or recovery, update the status layer and regenerate the generated views.** Repairing a generated view by hand is prohibited: the view is a rendering of canonical JSON, so a hand-patch both hides the state it was supposed to display and is silently reverted the next time the generator runs.
8. **Two failures of the same class stop the patching.** Trace the whole boundary chain first — entrypoint, wrapper, runner, injected dependency, artifact writer, live boundary — because a second failure of the same shape usually means the fault is at a different layer from the one being edited.

## The known-issues ledger

Health classes describe *now*. The known-issues ledger is what makes `degraded-contained` auditable **over time**, and it is a durable artifact in the status layer rather than a note in a thread.

Six fields are required, and the set is small deliberately — a ledger nobody can fill in during an incident does not get filled in.

| Field | Required | Purpose |
|---|---|---|
| Issue id | yes | Stable handle, so the same defect is discussed as one thing across months |
| Capability id | yes | The capability row this degrades, joining the ledger to the capability matrix in [capability provenance](03-capability-provenance.md) |
| Classification | yes | One value from a fixed set — degraded, disabled, accepted-risk, retired-lane, blocked-on-external-permission, documented-fallback — so entries can be counted and compared instead of read one at a time |
| Containment | yes | The compensating instruction currently in force, rendered into the operator-facing view |
| Evidence | yes | Where the proof of the classification lives |
| Freshness class | yes | How long the classification may be trusted before re-verification, drawn from the same freshness vocabulary as capability rows |
| Opened at, re-verify by | no | When the entry was raised and when its classification expires |
| Re-enable criteria | no | What must be true to switch the capability back on, so re-enable is a gate rather than a judgement call by whoever finds the entry next |
| Successor capability id | no | Which capability took over the purpose, when a lane was retired rather than repaired |
| Closure | no | Set only on restoration: when it closed, what verified it, and where that verification lives |

The reference shape is [`examples/known-issue.example.json`](../examples/known-issue.example.json), checked against [`schemas/known-issue.schema.json`](../schemas/known-issue.schema.json).

A healthy ledger is short, and its shape is worth stating because length is the easiest thing to misread. It is not a defect history: entries close on restoration rather than accumulating, so its length tracks how many capabilities are reduced right now, not how many have ever been. Every open row names a containment actually in force and carries an unexpired freshness class, and the classification values are drawn from the fixed set above so rows can be grouped rather than reread one at a time. A ledger that has grown long, or whose rows have outlived their re-verify dates, is itself the signal — either containment has quietly become the normal operating mode, or nobody is re-verifying.

Four things follow from keeping it, and together they are why the ledger is worth more than its size suggests.

- **Containment stops being tribal knowledge.** The reason a lane is off is written down beside the lane.
- **Re-enable becomes a gate, not a judgment call.** The entry names what must be true.
- **Staleness is visible.** An entry whose freshness class has expired is a prompt to re-verify, not a fact.
- **The operator view cannot drift.** Containment text is rendered into the generated capability dashboard from this ledger, so the human-readable page and the machine-readable state cannot disagree.

An issue is closed by restoration and verification, and the closure records what verified it — which is the same discipline as every other terminal state in this architecture.

## What is proven versus asserted

| Capability | Reference implementation | Stock-runtime adopter |
|---|---|---|
| Guard family with plans, receipts, and post-install verification | helper-backed | policy-only |
| External-volume availability guard | helper-backed | policy-only |
| Storage-headroom, workspace-isolation, execution-slice, retired-path guards | helper-backed | policy-only |
| Gateway autoheal | helper-backed | policy-only |
| Health class model on the status artifact | runtime-backed field plus policy vocabulary | policy-only |
| Evidence freshness windows and capability freshness classes | helper-backed status layer plus policy | policy-only |
| Containment and restoration protocol | policy-only | policy-only |
| Backup-conditioned deletion boundary | policy-only, with gate scripts for adjacent checks | policy-only |
| Known-issues ledger and its rendering | helper-backed | policy-only |

The taxonomy itself is defined in [capability provenance](03-capability-provenance.md).

## Adopting this

1. Pick the one invariant whose violation would hurt most, and write a guard that refuses when it fails. One real guard beats six advisory ones.
2. Give every guard an install receipt and a post-interval verification. Assume nothing you installed is still running.
3. Add a health field beside your task state, and forbid `healthy` while a containment is in force.
4. Label diagnostic evidence with a freshness window before you argue about severity.
5. Keep a known-issues file in version control with an id, a containment, and a re-verification date. It is the cheapest artifact in this chapter and it pays back the fastest.

Where this leads: everything in this chapter produces records — incident records, containment entries, install receipts, ledger rows — and the next chapter, [evidence, audit, and verification](15-evidence-audit-and-verification.md), is about what makes such a record count as proof rather than as a note.

Related chapters: [capability provenance](03-capability-provenance.md), [system architecture](04-system-architecture.md), [scheduling and background work](12-scheduling-and-background-work.md), and [delivery and the control surface](13-delivery-and-control-surface.md).
