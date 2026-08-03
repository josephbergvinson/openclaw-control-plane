# A worked example, start to finish

This chapter follows one ordinary request from the moment it arrives to the moment its record is closed, and shows
every file the system writes along the way. The request itself is deliberately dull; the point is to see exactly what
exists on disk at each moment, and therefore what a supervisor, a restarted process, or an auditor would find if
everything stopped right there. A newcomer can read it straight through as a narrative. An adopter can read it as the
minimum set of records worth writing.

Most of the work happens in a **durable lane**: a separately spawned child run with its own run identity, its own
working copy of the repository, and its own status file on disk, while the chat thread carries only an
acknowledgement, a checkpoint or two, and one final result.
[Execution modes and durable lanes](06-execution-and-durable-lanes.md) states those mechanisms as rules; this chapter
shows what they leave behind. Each stage gives the artifact, a sentence on what the artifact is for, and a short
"wrong here" list naming the failure that artifact prevents.

Every identifier is a placeholder, `<t0>` through `<t9>` are ordered timestamps, and `a1b2c3d` stands in for a commit.
The path itself is drawn separately — related diagram: [`diagrams/request-path.mmd`](../diagrams/request-path.mmd).

**The scenario.** The operator asks for a validation step in the build script of `example-project`, then for the
change to be opened for review. That is one repository, one *write set* — the concrete list of paths a single worker
is allowed to modify — one local test run, and one push to a remote.

## Stage 0 — the inbound message

Nothing has been written yet. What exists is one chat message, shown here in the four fields every later record binds
itself to: who sent it, which route it arrived on, its identity on that route, and its text. The trailing `GO` is an
operator approval token covering bounded, reversible change; the token vocabulary and the risk classes behind it are
in [policy and authority](05-policy-and-authority.md).

```text
from:    operator
channel: <channel-id>
message: <origin-message-id>
body:    add a validation step to the build script in example-project so a missing
         manifest field fails the build, then open it for review. GO
```

Wrong here:

- Editing files during this turn. Nothing is classified, no lane exists, and nothing written now survives a restart.
- Replying "on it" with no artifact behind it — that opens a delivery obligation with nothing to discharge it.
- Reading the trailing token as approval for the whole sentence. `GO` covers bounded reversible mutation; "open it for
  review" crosses into another system and is a separate boundary.

## Stage 1 — classification and mode choice

The reply worker that receives an inbound message runs under a short wall-clock budget, so before anything is
executed the turn commits to one of three execution modes: inline, a durable lane, or operator-side manual
containment. This request names two operations from the declared long-phase list — the operations known to outrun
that budget, such as build, test, install, push, deploy, and live verification — so inline is not available.

The decision is written to an execution-mode state record before any side effect occurs. The record is keyed by the
pair `(channel scope, origin message)`, which is how a later message in the same conversation finds the decision that
governs it.

```json
{
  "channel_scope_id": "<channel-id>", "origin_message_id": "<origin-message-id>",
  "selected_mode": "durable-isolated-lane", "mechanism": "native-subagent",
  "selection_provenance": "classifier-provisional",
  "approval_envelope": "GO: bounded reversible change to the build script of example-project; publication excluded",
  "approval_receipt_id": null,
  "adaptive_promotion_claim": { "state": "unspent", "origin": "<origin-message-id>" },
  "active_lane_check": { "consulted": true, "live_claim_found": false },
  "recorded_at": "<t0>"
}
```

Field by field:

| Field | Meaning |
|---|---|
| `channel_scope_id`, `origin_message_id` | The pair the record is keyed by. Every later artifact in this trace carries the origin message id so the whole chain can be reassembled from any one of its parts. |
| `selected_mode` | The mode committed to for this turn: inline, durable isolated lane, or operator-side manual containment. |
| `mechanism` | How that mode is carried out — an in-process reply, a native subagent spawn, an agent-protocol client, a direct execution, or a handoff to the operator's own terminal. |
| `selection_provenance` | Where the choice came from. One of six values: a provisional classifier decision, an operator pin, a pin by an already-live lane, a runtime selection, a provider-approved switch, or a prior adaptive promotion. |
| `approval_envelope` | Plain text stating what the operator's token actually covered, written before execution so the envelope cannot be widened later to fit what was done. |
| `approval_receipt_id` | The recorded approval a token was matched against, or `null` while no token has been consumed. |
| `adaptive_promotion_claim` | The one-shot right to upgrade this same turn from inline to a durable lane without a second token, plus whether it has been spent. |
| `active_lane_check` | Evidence that the index of live lanes was consulted before classifying, and what it returned. |
| `recorded_at` | When the decision was written. It must precede the first side effect, which is the whole reason the record exists. |

Only a provisional classifier decision may later be upgraded without asking again, and only once. Every other
provenance value forbids promotion, because an operator who pinned a mode, or a lane that already owns the
conversation, has said something the classifier is not entitled to overrule.

Wrong here:

- Recording the mode after the first tool call. This record is the proof a mode was chosen rather than drifted into.
- Writing `operator-pinned` because the operator sounded decisive. Provenance describes where the selection came from,
  not how the message read, and only exactly `classifier-provisional` may later be promoted.
- Skipping the active-lane check because the topic looks new; the index is consulted before classification.

## Stage 2 — the acknowledgement

The status artifact is created, the child run is spawned, its identity is read back from the spawn result, and only
then does one acknowledgement go out. The acknowledgement is the operator's single guarantee that a lane now exists
and that a result is owed; it is also what converts the thread into a control surface, where nothing but control
traffic appears until the final. Its heading comes from a fixed three-phrase vocabulary — `Durable lane established`,
`Durable lane checkpoint`, `Durable lane final` — so the visible text can never disagree with the structured state
behind it.

Two things start at this moment. A delivery obligation is opened, naming what is owed and to which route, and it
clears only when a final is visibly delivered or the run is explicitly cancelled. And an acknowledgement barrier
comes into force: until this message is confirmed delivered, no checkpoint and no final may be sent, because a
progress update arriving with no established lane behind it is indistinguishable from noise.

```text
Durable lane established
Lane: <lane-id>
Run: <run-id>
Surface: example-project
Worktree: <worktree-root>
Scope: add a build-script validation step; publication held at its own boundary
Owed: one durable lane final
First checkpoint due within about five minutes. This thread is control only from here — please wait.
```

Wrong here:

- Sending this before the status artifact exists — it is what a supervisor reads if the process dies.
- Putting a planning slug in `Lane` or `Run`; identity comes from the spawn result, and a placeholder that later
  disagrees produces an identifier-propagation blocker.
- Sending a second, friendlier acknowledgement, or promising a completion time instead of a checkpoint deadline.

## Stage 3 — the status artifact at initialization

The status artifact is the lane's authoritative state. It is the file a supervisor reads to decide whether the lane is
alive, a restarted process reads to decide what to resume, and an auditor reads to decide what actually happened. It
is written before the acknowledgement precisely so that a crash one second later still leaves a complete account.

The record is flat — one level of keys, with structure only where a field is genuinely a set — because the readers
that matter most are a supervisor deciding whether to escalate and a restarted process deciding what to resume, and
both want one key rather than a tree. Five of those keys are immutable after the spawn. A worker may append to the
artifact or edit specific fields, but overwriting `lane_id`, `runtime_run_id`, `child_session_key`,
`origin_message_id`, or `delivery_target` is a blocking condition rather than an update, because those are what every
other record joins on.

One record is deliberately absent from it. The write lease — the compare-and-set record deciding which run may write
which paths — is not part of this artifact and is not referenced from it at all. It is a separate record in its own
file alongside, and it names this artifact by path, device, and inode. The binding runs one way on purpose: no record
can bind the file it lives in, so a lease carried inside the artifact would travel with a replacement copy and vouch
for it. The two also fail independently — this artifact outlives the lease at every park and at closeout, while a
lease whose artifact has vanished is a diagnosable inconsistency rather than a lost lease. The lease's own shape is in
[`examples/write-lease.example.json`](../examples/write-lease.example.json) against
[`schemas/write-lease.schema.json`](../schemas/write-lease.schema.json). This artifact's contract is
[`schemas/durable-status.schema.json`](../schemas/durable-status.schema.json), with a completed instance in
[`examples/durable-status.example.json`](../examples/durable-status.example.json); the record below is the same shape
at its first write.

```json
{
  "schema": "durable-lane-status/v1",
  "lane_id": "<lane-id>", "runtime_run_id": "<runtime-run-id>",
  "child_session_key": "<session-key>", "origin_message_id": "<origin-message-id>",
  "delivery_target": "<channel-id>",
  "state": "executing", "health": "healthy", "acceptance_level": "not-yet-verified",
  "scope": "add a validation step to the build script of example-project; open for review after approval",
  "single_worker_reason": "one write set in one repository; no independent read-only partition to split",
  "write_boundaries": {
    "allowed": ["<worktree-root>/scripts/", "<worktree-root>/tests/", "<worktree-root>/docs/build.md"],
    "forbidden": ["<project-root>/", "<workspace-root>/status/", "any release or deploy path"] },
  "phases": [{"name": "accepted", "state": "complete"}, {"name": "implementation-started", "state": "pending"},
    {"name": "tests-started", "state": "pending"}, {"name": "tests-passed", "state": "pending"},
    {"name": "commit-and-normalize", "state": "pending"}, {"name": "closeout", "state": "pending"},
    {"name": "blocker", "state": "not-entered"}],
  "checkpoint_due_within_minutes": 5, "checkpoint_delivery_strategy": "phase-boundary",
  "owed_final_type": "durable-lane-final",
  "blockers": [], "diagram_artifact": null,
  "delivery_state": "ack-delivered", "delivery_message_id": null,
  "updated_at": "<t1>"
}
```

Four fields carry most of the meaning. `phases` is the named progress ladder — accepted, implementation-started,
tests-started, tests-passed, commit-and-normalize, closeout, and a blocker phase entered only on failure — and a
checkpoint is emitted when one of these transitions, not when a timer expires. `write_boundaries` states the paths
this lane may and may not touch, which is what turns an out-of-scope edit into a detectable event rather than a
surprise found later. `acceptance_level` is the honesty ladder: `not-yet-verified` until something is checked,
`verified-local` once local evidence exists, `verified-live` only once the real external effect has been observed.
And `checkpoint_due_within_minutes` is where silence starts to count as a fault: the first operator-visible update is
due within about five minutes. The two later thresholds — a silence warning near fifteen minutes, and a hard ceiling
near eighteen past which the lane is treated as unhealthy rather than merely slow — belong to supervision rather than
to this file, because a lane that has stopped writing cannot be relied on to record its own overdue deadline.

Wrong here:

- Leaving `write_boundaries.allowed` empty "for now". The boundary is what makes an out-of-scope edit
  detectable.
- Pointing the allowed paths at `<project-root>` instead of the verified worktree, whose Git common directory was
  checked against the canonical source root.
- Copying the lease into this file, or adding a field that points at it. A lease that lives inside the artifact it
  protects cannot detect the artifact being replaced underneath it, and a pointer here would tie the two records
  together in both directions, which is exactly the independence the separation buys.
- Omitting `single_worker_reason`, or setting `acceptance_level` to a verified value before anything is
  verified.

## Stage 4 — the first checkpoint

The change lands and the local suite runs. The checkpoint is emitted at a phase boundary, not on a timer.

```text
Durable lane checkpoint
Lane: <lane-id>
Phase: tests passed
The build now fails when the manifest lacks a required field and passes when it is present.
One new test covers both directions; the local suite is green.
Next: the publication step sits outside the current approval. Approval request follows.
```

The matching change to the status artifact is shown as a delta because that is how it is applied: the worker edits
the fields that moved and leaves everything else, including the five immutable identity fields, untouched. Three
phases close and the acceptance level rises from `not-yet-verified` to `verified-local`, with the evidence written
under the lane's artifact root so the claim can be opened rather than believed.

```json
{ "phases": [{"name": "implementation-started", "state": "complete"},
             {"name": "tests-started", "state": "complete"},
             {"name": "tests-passed", "state": "complete"}],
  "acceptance_level": "verified-local",
  "updated_at": "<t2>" }
```

The delivered checkpoint itself is not written back into this file. The acknowledgement barrier records
worker-visible deliveries, checkpoints included, which is what keeps the emission countable afterwards without the
artifact having to hold a second copy of a fact the delivery path already owns.

Wrong here:

- Emitting "still working on it" with no phase transition behind it. A contentless ping is not a checkpoint.
- Raising `acceptance_level` to `verified-local` with no evidence a reader could open under the lane's artifact root.

## Stage 5 — the approval boundary

Publication is a different mutation class against a different system, so the lane stops before it, names the boundary,
and parks in a resumable state. Parking rather than stopping matters: the lane keeps its identity, its worktree, and
its write lease — held across the park in its own record beside the artifact, not released and reacquired — so the
approved step can be carried out later by the same run instead of being rebuilt from scratch.

In the artifact itself the park is a small change: the lifecycle state moves to `waiting-approval` and nothing else
about the lane is torn down.

```json
{ "state": "waiting-approval", "health": "healthy", "updated_at": "<t3>" }
```

The detail behind that one word is a derived view rather than stored fields, computed by the parser when a supervisor
or a resume path reads the artifact. It has two halves. The wait half is the resumable state — who is being waited on,
until when, and the exact action that will be taken on resume, registered as a real resume path rather than an
intention. The boundary half is the classified next step, and it carries exactly the six things a higher-risk plan
must state before it can be approved at all: target and action, blast radius — how far the effects reach and how
reversible they are — the rollback or stop condition, the verification predicate, the exclusions, and where the
evidence will be written. `STRONG GO` is the operator token for exactly this class — an exact-scope higher-risk action
described in advance — and it is never implied by the earlier `GO`.

| Derived field | Value in this run |
|---|---|
| `waitSignal` | approval required |
| `waitOwner` | the operator |
| `waitDeadline` | `<t5>` |
| `waitResumeAction` | push branch `task/example-project-build-validation` and open one review; nothing else |
| `waitResumeRegistered` | true — a registered resume path, not an intention |
| Next boundary class | executable under a bounded approval |
| Required token | `STRONG GO` |
| Target and action | publish the task branch of `example-project` to the review remote and open one review |
| Blast radius | one branch and one review on one remote; no default-branch change, no deploy |
| Rollback or stop | close the review and delete the pushed branch; local commit `a1b2c3d` is retained |
| Verification predicate | the review exists, points at `a1b2c3d`, and no protected branch has moved |
| Exclusions | no merge, no release, no notification to anyone but the requester |
| Evidence path | `<artifact-root>/<lane-id>/` |

The visible message offers the three required options: proceed with the approved subset, approve the bounded
higher-risk plan, or stop.

Wrong here:

- Publishing anyway because "open it for review" appeared in the original request. The stated objective is not the
  approval envelope; the envelope is what the token covered.
- Parking with no registered resume path. An intention to resume is not a registered resume path, and a
  non-terminal worker may park only on a receipt-backed wait or a fully registered background wait.
- Ending the lane and asking the operator to start a new one, or opening a second lane to ask the question. The
  boundary is a state inside the lane, and follow-ups bind to that lane.

## Stage 6 — the operator token and the same-lane resume

The operator answers in the same thread. Because that conversation already owns a live lane, the reply binds to that
lane rather than starting anything new — the rule is that while a lane is live, every plausibly related message from
the operator, including corrections and added acceptance criteria, routes into it. A second concurrent lane requires
the operator to ask for one explicitly.

```text
from:    operator
channel: <channel-id>
message: <resume-message-id>
body:    STRONG GO
```

The resume record below proves three things at once: the token was matched against a registered wait rather than read
out of passing text, the run resumed under its original identity, and no new ownership was claimed. A *write lease*
is the compare-and-set record naming which run may write to which paths; its *generation* counts how many times
ownership has changed hands. Both stay as they were, which is what makes the resume idempotent. The lease is its own
file, so the resume touches it in place and the status artifact is not rewritten by the reacquire at all — there is
no pointer in it that could go stale.

```json
{
  "event": "approval_token_matched", "lane_id": "<lane-id>", "token": "STRONG GO",
  "matched_wait_signal": "approval_required",
  "resume": { "mode": "same-lane", "child_session_key": "<session-key>",
              "runtime_run_id": "<runtime-run-id>", "new_lane_claim": false },
  "lease": { "record": "<artifact-root>/<lane-id>/lease.json", "generation": 1, "epoch": 1,
             "reacquired": "idempotent-same-run", "successor_record": null,
             "protects_status_artifact": "unchanged" },
  "replayed_side_effects": [], "next_atomic_step": "push the task branch and open one review",
  "recorded_at": "<t6>"
}
```

Wrong here:

- Spawning a fresh lane to carry out the approved step. A new claim would strand the old one and duplicate the
  obligation; resume keeps run identity, child session, and write lease.
- Incrementing the lease generation. Generation N+1 is succession from a dead owner and demands prior-owner-dead
  proof, a diff inventory, and a fan-in plan; a same-run resume is idempotent.
- Re-running the earlier build and test steps after a mid-turn restart; recovery resumes from recorded state.
- Accepting a token that appears only inside quoted assistant text, or reading `STRONG GO` as authorizing an outbound
  message to anyone other than the requester.

## Stage 7 — terminal status

The branch is pushed, the review is opened, and the artifact reaches a terminal state **before** anything visible is
sent. A terminal state is one from which no further work follows — complete, blocked, failed, cancelled, superseded,
or preserve — and reaching one is the precondition for sending the final. The ordering is the guarantee: a durable
record that exists without a visible message is recoverable, while a visible message that exists without a durable
record is not.

The predicate stated at the approval boundary is checked against the real remote first, and the evidence for it is
written under the lane's artifact root, because `verified-live` is the one acceptance level that claims an external
effect was observed rather than merely produced. Only then does the state move.

```json
{ "state": "complete", "health": "healthy", "acceptance_level": "verified-live",
  "phases": [{"name": "commit-and-normalize", "state": "complete"},
             {"name": "closeout", "state": "complete"}],
  "blockers": [], "diagram_artifact": null,
  "delivery_state": "final-pending", "delivery_message_id": null,
  "updated_at": "<t7>" }
```

`state` carries the terminal outcome directly, so a supervisor reads one key rather than deciding whether a separate
terminal block agrees with a separate lifecycle field. Of the six outcomes only `complete` is success-terminal, and
`delivery_state` moving to `final-pending` while the state is already terminal is precisely the ordering this stage
exists to demonstrate.

Wrong here:

- Sending the final first and updating the artifact afterwards. A restart between the two leaves a visible result with
  no durable record behind it.
- Recording `complete` while a `blockers` row is open, or while the closeout gate reports a blocking dirty class.
- Reading `verified-live` off a zero exit code; the predicate is about the review and the protected branches.

## Stage 8 — the delivery receipt

One terminal message is reserved, sent, and confirmed. The receipt is the proof that it landed: transport acceptance
is not delivery, and only a provider-issued message identifier closes the obligation opened back at the
acknowledgement.

The reservation happens before the message is composed, in two steps. The sender first takes an in-process slot, then
creates a claim file with an exclusive-create operation that fails if the file already exists — so two processes
racing for the same final produce one send and one loser, not two visible messages. The `idempotency_key` is what the
claim is named after, and each of its parts is chosen deliberately: provider and account route scope the key to one
destination system, the hashed target is canonicalized to the visible destination so a thread send and a
parent-surface send collapse into one slot, the hashed origin ties the message to the request that caused it, the
delivery class separates a final from a checkpoint, and the chunk coordinates keep an intentionally multi-part final
multi-part. The full record and the arbitration rules that decide between a crash and a duplicate are in
[`examples/delivery-receipt.example.json`](../examples/delivery-receipt.example.json) and
[delivery and the control surface](13-delivery-and-control-surface.md).

```json
{
  "delivery_class": "final",
  "idempotency_key": "terminal-dispatch:<provider>:<account-route>:<hash-target>:<hash-thread-slot>:<hash-origin>:final:1of1",
  "reserved_in_process_at": "<t7>", "claim_create_mode": "exclusive-create",
  "claim_file": "<runtime-dir>/cache/terminal-dispatch-claims/<claim-id>.json",
  "provider_message_id": "<message-id-final>", "completed_at": "<t8>",
  "chunk": { "index": 1, "count": 1 }, "suppressed_duplicates": 0
}
```

The artifact then records `delivery_state: "final-delivered"` and puts that id in `delivery_message_id`, clearing the
obligation opened at the acknowledgement. The lease is released separately, in its own file, and nothing in the
artifact changes because of that release — the two records end the run independently, which is the point of keeping
them apart.

Wrong here:

- Marking the claim complete without a provider message id; completion is proven by the provider.
- Letting the parent also send a summary. When the lane owns the final, a parent send happens only after a fresh read
  proves no equivalent final is visible, and is plainly supplemental when it does.
- Splitting the result into a summary plus a separate diff message; one logical final is one message with at most
  one attachment, and if it cannot be prepared, nothing partial is sent.
- Including the message text in the idempotency key — text is excluded so a corrected final cannot become a second
  visible final.

## Stage 9 — the closeout record

The closeout record is the lane's own account of whether it finished properly: what state the source was left in,
what was verified and by what evidence, what the diff actually contained, and whether the obligation cleared. It is
the artifact an audit reads months later, when nobody remembers the run. The gate runs before the terminal state is
recorded, but the record is finalized after delivery, because the delivery outcome is one of the things it attests.
Where these records live and how long they are kept is covered in
[evidence, audit, and verification](15-evidence-audit-and-verification.md).

One field needs unpacking. A *dirty class* is a category of uncommitted change still present in the working tree at
the end of a run. Regenerable residue — build output, caches — is observed and recorded but does not block; an
unexplained source modification does block, because closing a run over an edit nobody accounted for is how an
unrelated change reaches a branch under cover of an approved one.

```json
{
  "schema": "slice-closeout/v1", "lane_id": "<lane-id>", "decision": "complete",
  "source_state": { "surface": "example-project", "branch": "task/example-project-build-validation",
                    "head_commit": "a1b2c3d", "worktree_clean": true },
  "dirty_classes_observed": ["generated-residue"], "blocking_dirty": [],
  "acceptance": { "level": "verified-live", "evidence": "<artifact-root>/<lane-id>/verification.json" },
  "self_review": { "diff_reviewed": true, "unrelated_changes": 0, "note": "single-purpose diff plus one test" },
  "docs_impact": { "required": true, "path": "<worktree-root>/docs/build.md", "state": "updated" },
  "actual_entrypoint": { "claimed": "the build script", "verified": true,
    "traced": ["build script", "validation helper", "test module", "review remote"] },
  "delivery": { "final_message_id": "<message-id-final>", "obligation_state": "cleared" },
  "closeout_exception": "this surface stops at local normalization; publication ends at the open review",
  "recorded_at": "<t9>"
}
```

Wrong here:

- Reporting completion in chat while the gate says blocked. The gate decides; the message reports.
- Treating unrelated operational drift as a blocker, or a blocking dirty state as residue; that classification
  belongs to the gate.
- Filling `actual_entrypoint` with the file that was edited rather than the chain traced — entrypoint, wrapper,
  runner, injected dependency, artifact writer, live boundary.
- Hand-editing a generated status view to make the run look closed; generated views are only regenerated.

## What the operator saw versus what exists on disk

| Operator saw | Durable record behind it |
|---|---|
| One acknowledgement | Status artifact, an active-lane claim in `pending-startup` — claimed but not yet running — and an opened delivery obligation |
| One checkpoint | Phase transitions, acceptance level raised, evidence written under the artifact root |
| One approval request | A waiting lifecycle state, a registered resume path, and the classified next boundary read back off it |
| One final | Terminal status, reserved idempotency claim, provider-confirmed message id, cleared obligation |

Four visible messages, one lane, one writer, one final. Everything else is recoverable from
[artifacts](09-source-layout-and-artifacts.md) without asking the operator what happened.

## Reading this trace as an adopter

Three properties carry most of the value and can be adopted before any of the rest: the mode decision is written down
before the first side effect, the status artifact exists before the acknowledgement, and the final is sent only after
the artifact is terminal. The rest of the machinery — leases, barriers, idempotency claims — exists to keep those
three true when a process dies at the worst possible moment. See [capability provenance](03-capability-provenance.md)
for which parts need a modified runtime.

One thing this trace deliberately leaves out is where the lane's knowledge came from. Nothing here was recalled; the
scope, the boundaries, and the verification predicate were all stated in the request or derived from registries. What
the agent may remember between runs, how it is stored, and why recall is treated as evidence rather than authority is
the subject of [memory and context](08-memory-and-context.md). For where the worktree, the branch, and the artifact
root sit relative to each other on disk, continue to
[source layout and artifacts](09-source-layout-and-artifacts.md).
