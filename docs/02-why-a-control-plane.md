# Why a control plane: the failure catalogue

Every rule in this repository exists because the default behaviour of a capable agent on a capable runtime fails in a
specific, repeatable way. A *control plane* is the layer an operator puts between intent and that capability: contracts
the agent reads, registries that hold the current facts, gates that refuse, and evidence that outlives the process. It
earns its cost only if every rule in it can name the failure it prevents. This chapter names the failure classes first
and derives the rules from them, so the rest of the repository reads as consequences rather than preferences.

Each entry has three parts. **Failure** is what is observed from outside the system — the "before". **Default** is why a
competent agent acting reasonably produces it when nothing structural prevents it. **Rule** is the change that makes the
failure impossible or makes it loud — the "after". These are failure *classes*, not incidents, and none requires a
defect: each is a locally correct choice made by a component that cannot see the boundary it is crossing. A control
plane exists because local correctness does not compose.

## Duplicate finals

**Failure.** One logical result appears twice — reworded, minutes apart, or after a restart. No reader can tell which is
authoritative, and downstream automation counts the work twice.

**Default.** Announcing completion is cheap; verifying it was already announced is not. Parent and worker each believe
they own the result, an ambiguous send error invites a retry, and a restart replays intent.

**Rule.** Exactly one terminal message per workstream, protected by an *idempotency claim*: a small file created with
exclusive-create semantics, so that when two senders race for the same result only the one that wins the creation may
speak. The claim is keyed over provider, account, hashed visible target, hashed origin, delivery class, and chunk
coordinates. It is reserved first in process, then persistently on disk, and marked complete only against a
provider-confirmed message id — so a claim carrying no receipt is a crash to recover from, not a send to suppress.

Two key-design choices carry most of the value. Message text is excluded, so a final and its correction land on the
same slot instead of becoming two visible results; and the target is canonicalized to the destination a reader
actually sees, so a thread send and a direct send that surface in the same place cannot both win. Chunk coordinates
stay in the key, because an intentionally multi-part final is one result rather than several. A *durable lane* — a
separately spawned run with its own identity, on-disk status record, and isolated working copy, described in
[execution and durable lanes](06-execution-and-durable-lanes.md) — owns the final it delivers, and the parent may send
only after a fresh read proves no equivalent final is visible. If the claim store cannot be read at all, the code fails
open, because a dropped real final is worse than a rare duplicate. See
[delivery and the control surface](13-delivery-and-control-surface.md).

## Silent execution-mode substitution

**Failure.** Work classified as a short inline reply becomes a long mutation, or the reverse, with no visible
transition. Isolation, approval scope, and evidence obligations were decided for a mode no longer running.

**Default.** Mode is usually implicit, and adapting as work reveals its true size is the right instinct at the task
level. It is the wrong instinct at the boundary level, because the boundary was drawn before the discovery.

**Rule.** Exactly one mode is chosen before any side effect — inline, durable isolated lane, or operator-side manual
containment — and the choice is persisted against the origin message. The record holds the selected mode, the mechanism
carrying it, the *selection provenance* (the recorded reason the mode was chosen: a provisional classifier decision, an
operator instruction, an active lane claiming the continuation, and so on), the approval envelope, an optional approval
receipt, and any promotion claim with its outcome. Substitution is not forbidden; *unrecorded* substitution is.

An inline turn whose provenance is exactly "provisional classifier decision" may be promoted once, atomically, into a
same-origin durable lane without a second approval, and only when objective, target, account or workspace route,
mutation and risk class, and stated exclusions are all unchanged. The promotion claim is recorded so it cannot be spent
twice. Every other provenance pins the mode. Unknown provenance, an operator instruction pinning the turn inline, a
bounded memory directive, or an active lane owning the continuation each *fail closed* — the run stops at the nearest
safe boundary and reports one narrow mismatch rather than guessing which mode the operator would have wanted.
Related diagram: [`diagrams/execution-mode-decision.mmd`](../diagrams/execution-mode-decision.mmd).

## A chat turn held open through long work

**Failure.** The inbound reply worker is kept alive across a build, install, restart, or deploy. The turn either expires
leaving no durable record of what was started, or returns a snapshot of an unfinished process that reads as a result.

**Default.** The inbound turn is the only context the agent can see, finishing inside it is the shortest path, and the
wall-clock budget is invisible from the inside.

**Rule.** Inbound work carries a hard wall-clock budget — tens of seconds, not minutes — together with a
safe-remaining threshold at which the turn must start winding down. Handing off cannot wait for the work to reveal its
own size, so a declared list of long phase kinds does it in advance: build, test, install, gateway restart, long
execution, process polling, coding session, model download, deploy, push, external mutation, live verification. Naming
any of them forces a durable lane to be established *before* execution begins rather than after the budget is already
spent.

The endgame is explicit, because this is where a turn under time pressure improvises. Below the threshold with a lane
already established, release the inbound worker with one visible checkpoint. Below the threshold with no completed
handoff, do not attempt a fresh spawn inside an expiring worker — emit one visible blocker and stop, since a spawn
begun in the last seconds of a turn is the case most likely to leave a half-established lane nobody owns. A visible
checkpoint is not a durable handoff: it tells the operator work continues, and proves nothing about whether anything
is still running. See [execution and durable lanes](06-execution-and-durable-lanes.md).

## A green unit test over a broken wrapper

**Failure.** The suite passes, the change is reported complete, and the user-facing path is still broken. The defect
sits in the wrapper, runner, callback, an injected dependency, or the artifact writer.

**Default.** A capable agent optimises for the strongest signal within reach, and a green suite is the strongest one
available locally. Integration boundaries are slow, credentialed, and easy to declare out of scope for "this fix".

**Rule.** Architecture acceptance discipline. Before any patch, two things are established: the user-facing acceptance
path — the route a real request takes to the boundary that matters — and its proof fields, meaning the specific values
a reader would have to see to believe the fix worked. Then the whole chain is traced end to end: entrypoint, wrapper,
runner, injected dependency, artifact writer, live boundary. Closeout on local evidence alone is prohibited for
integration-boundary fixes, because local evidence is precisely the evidence that was already green. Two failures of
the same class is a hard stop rather than a third patch, since a repeat failure says the model of the defect is wrong
and another patch only tests the same wrong model; the delivery-specific variant additionally requires a matrix of the
integration boundaries in play and a failing end-to-end reproduction before work resumes. See [evidence, audit, and
verification](15-evidence-audit-and-verification.md).

## A hand-edited generated view masking reality

**Failure.** A human-readable rollup — dashboard, index, status page — is edited directly so it says what the operator
expects. Every later reader trusts it; the structured source it was rendered from says otherwise.

**Default.** The view is what everyone reads, editing it is a one-line change, and regenerating requires knowing which
of several sources owns the field. The most visible surface is always the cheapest to correct.

**Rule.** Generated artifacts are stamped generated, declared non-authoritative, and lose to their machine-readable
source on conflict. A machine-readable policy manifest names the authoritative layers and enumerates exactly which
views are generated, so "is this file authoritative?" is answered by a lookup rather than by how official the file
looks; anything else that still reads as authoritative is a labelled compatibility shim, an older surface kept working
but explicitly demoted. Generated views are never hand-edited, only regenerated from canonical JSON — and a stale view
is repaired at its source, because patching the rendering leaves the next regeneration to undo the fix. Live evidence
outranks every index, canonical ones included: actual repository state, actual processes, and actual scheduler state
beat any record of them. See [policy and authority](05-policy-and-authority.md).

## Editing a mirror instead of canonical source

**Failure.** A change lands in a copy — a mirror clone, an extracted release directory, an archive, an artifact tree, a
stale worktree — and appears to work. Canonical history never receives it, and the copy is later refreshed away or
diverges in a way no single checkout explains.

**Default.** Any checkout containing the file looks like the project. Finding a plausible source is far faster than
resolving the authoritative one, and sessions inherit whatever directory they started in.

**Rule.** Canonical source is resolved through a *topology registry* — a declared list of roots naming, for each
surface, the one directory that is authoritative and the role every other directory plays — and never inferred from the
existence of a copy. Namespaces are declared and role-typed: release roots, artifact roots, mirrors, and archives are
marked never-source dead ends, so a plausible checkout cannot become the edit target merely by being present. Retiring
a root is a recorded operation rather than a deletion, and no alias may be declared beneath a retired parent, because
an unregistered retirement is exactly how a copy quietly becomes a second source of truth. Long-lived services run only
from canonical roots or sealed releases. Unmatched paths are a blocker: the drift classifier fails closed on a location
it cannot classify, rather than defaulting it to something harmless-looking. Namespaces and worktree layout are covered
in [source layout and artifacts](09-source-layout-and-artifacts.md). Related diagram:
[`diagrams/source-namespaces.mmd`](../diagrams/source-namespaces.mmd).

## Deleting a worktree that backed a live service

**Failure.** Cleanup removes a task worktree and a running service, scheduled job, or installed entrypoint loses its
files. The removal was correct as housekeeping and catastrophic as an operation, and the two facts were never connected.

**Default.** Worktrees are advertised as disposable, and a finished lane's worktree looks like garbage. Nothing inside
the directory announces that something long-lived resolves into it.

**Rule.** Installed runtimes live in stable release directories outside any disposable worktree, and every active
scheduled job must target a canonical runtime path or a declared host shim — never a lane worktree. Deleting anything
runtime-adjacent requires a realpath-closure check first: resolve every link, pointer, and service definition that
could terminate inside the candidate path, and treat any that does as a blocker. Lanes that must not be pruned are
declared as protected, a prune runs a preflight that lists what it would remove and refuses on a protected lane, and
closeout ordering — the fixed sequence of end-of-work steps that finalizes a lane's record and evidence — puts removal
after verification rather than before it. See [source layout and artifacts](09-source-layout-and-artifacts.md).

## Deletion on an unverified backup predicate

**Failure.** A delete-after-backup operation proceeds because the backup step returned success. The backup was partial,
written to the wrong route, or verified only that some file exists. The loss is discovered later, by someone looking for
the data.

**Default.** Two steps in one instruction read as one operation, and the second step's precondition is treated as
satisfied by the first step's exit code. Success codes are abundant; verified predicates are not.

**Rule.** Backup verification and deletion are separate gates. Deletion requires a positively verified predicate stated
in advance and checked against the destination, never inferred from a return code. An unverified, partial, or ambiguous
predicate moves the run to blocked or waiting-approval and names the exact failed predicate rather than degrading to a
weaker check. A fallback that changes backup semantics — different destination, scope, or retention — requires fresh
narrow approval, because it is a different operation wearing the same name. See [guards, health, and
restoration](14-guards-health-and-restoration.md).

## A restart loop that destroys attribution

**Failure.** A service is restarted repeatedly to clear a fault. Runs in flight vanish, or are closed out carrying
results that belong to a different attempt. Afterwards nobody can say which run produced which artifact, and the
original fault is unattributable.

**Default.** Restart is the highest-leverage remedy available and is nearly always locally effective, so each retry is
individually justified and only the aggregate is pathological. In-flight state lives in process memory and dies without
a sound.

**Rule.** Three constraints, because one is not enough.

First, a lifecycle action — a restart, a reload, or anything else that disturbs a running service — must claim a
*permit* before it runs: one unit drawn from a named counter with a budget declared in advance for that phase.
Exhausting the budget raises rather than retrying, which bounds how far a retry storm can reach at a number chosen
beforehand instead of by how long someone keeps trying.

Second, dispatch is separated from adjudication. The entrypoint that decides whether an action succeeded consumes a
bound external receipt and is structurally incapable of invoking a lifecycle action itself, so recovery after an
interruption resolves from receipts rather than by replaying the side effect to see what happens.

Third, an in-flight marker and its provenance record are written and cleared together. A marker that survives a restart
is therefore missing its record by construction, which makes it identifiable as interrupted and closable with an honest
terminal state instead of being reconciled into a fabricated result. One process-global set of active runs is the only
thing separating a run this process still owns from an abandoned reservation, so any reservation path that forgets to
add itself to that set can finalize a run that is still alive. See
[scheduling and background work](12-scheduling-and-background-work.md).

## An internally-finished worker that never delivers

**Failure.** The work is complete on disk — commits, artifacts, passing tests, a written terminal status — and the
operator never sees a result. Internally the run succeeded; externally nothing happened.

**Default.** Completion is defined by the agent's own state. Delivery is the last step, runs after all the interesting
work, and fails in ways that leave no trace in the work itself: a provider outage, an expired route, a crash between
terminal state and send.

**Rule.** Delivery is proven by receipt, not by intent. A visible acknowledgement creates a *delivery obligation* — a
machine-checkable record that something is still owed to a named target — and it is created *at acknowledgement time*,
before any side effect, carrying the target, the last visible update, the next owed update, and the completion path. It
clears only on a visibly delivered final or an explicit cancellation, and phrasing a message to avoid the word "done"
does not waive it; closeout gates keep failing until a provider-confirmed message id or a readback receipt — proof
obtained by reading the destination back — exists.

Ordering carries the rest. The *status artifact*, the lane's authoritative on-disk record of its own state, must reach
a terminal state before the visible final is sent, so a crash between the two leaves a recoverable record rather than
an announcement with nothing behind it. Send failures are not dropped: they enter a store-and-forward queue, which
parks the delivery on disk and retries it on a bounded backoff ladder. An active-delivery claim marks an entry as
owned, so a second drainer cannot pick up the same entry and send it twice — the queue that fixes drops would
otherwise create duplicates. A permanent-error classifier stops retries that can never succeed, and
exhausted entries move to a retained failure directory rather than being deleted, because a delivery nobody can inspect
is indistinguishable from one that never existed. Related diagram:
[`diagrams/delivery-path.mmd`](../diagrams/delivery-path.mmd).

## A memory cue treated as approval authority

**Failure.** A line in a low-precedence memory or preference file is read as standing permission, and a mutation
proceeds without a current approval. The line was written as a hint; it is now doing the work of policy.

**Default.** Everything in context arrives as text of apparently equal weight. A file describing how the operator likes
things done is indistinguishable at read time from one describing what is allowed — and the cue is usually right about
intent, which is what makes it dangerous about authority.

**Rule.** Precedence is declared and total: normative policy, then execution mechanics, then style, then preferences,
then memory cues — and a lower-precedence file never gains authority by implication. Memory is a cache, not an
authority. Approval tokens come from the operator's current message, bound to the origin message, requester, and exact
route; outbound send is gated separately from mutation and is never implied by it. Exceptions to the default posture are
enumerated as named standing directives inside normative policy, never inferred from a cue. Symmetrically, text the
agent itself wrote — an acknowledgement, a pre-mutation summary, a self-declared exclusion — is descriptive and can
neither create nor remove an approval boundary. Related diagram:
[`diagrams/authority-resolution.mmd`](../diagrams/authority-resolution.mmd).

## A convenient account substituted for a named route

**Failure.** A request naming a specific account, workspace, shared drive, folder, or owner is fulfilled on whichever
route was already authenticated. The object is created, a returned link looks correct, and the result sits in the wrong
place with the wrong ownership and sharing.

**Default.** The ready lane is the one that works, and substituting it is the difference between an answer and a
blocker. The substitution is invisible at creation time because the interface returns success and a link.

**Rule.** Named routes are hard predicates, not preferences. A ready default lane may not satisfy a non-default request;
creation stops with a route blocker until the requested route is verified. Post-mutation verification proves placement
by reading the object back and checking its owner, parent, and location fields, rather than trusting the link the
interface returned — a link is generated from the request, so it reflects what was asked for, not where the object
landed. Mutation ordering is API-first: a verified integration, then browser automation, then host user interface, in
that order, because each step down the ladder returns weaker evidence of what actually happened. Transmission is gated
separately from drafting, so populating a draft on the wrong route can never become a send. See
[integrations and capability routing](11-integrations-and-capability-routing.md).

## What the classes have in common

Five invariants generate most of the rules above: **write before you announce**, so a crash between the two is
recoverable rather than deniable; **prove, do not infer**, because receipts, readbacks, and live evidence outrank return
codes and indexes; **fail closed on unknown identity**, stopping with one narrow blocker when provenance, ownership,
route, or approval cannot be positively established; **one writer per object**, with ownership as a claim rather than a
convention; and **evidence outlives the process**, so anything existing only in a running process is assumed lost.

Whether a rule is enforced by the runtime, by a helper, or by text alone is a separate question: a rule is only as
strong as its weakest enforcement level. That is the subject of [capability provenance](03-capability-provenance.md),
which should be read next; [system architecture](04-system-architecture.md) then names the layer that owns each of
these decisions. One discipline governs the catalogue itself: a rule added without naming the failure class it prevents
is how a policy stack becomes unfalsifiable, because nobody can say what would break if it were removed. Every rule
here should trace to a class above, or introduce a new named one.
