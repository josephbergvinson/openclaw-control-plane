# Capability provenance: policy-only, helper-backed, runtime-backed, live-proven

This chapter solves one problem: telling the difference between a behaviour the system
*guarantees* and a behaviour the system *asks for*. Both read identically in a contract
file, and the gap between them only becomes visible when something fails. An adopter needs
that difference to know what they would actually have to build; an auditor needs it to know
which claims are backed by code, which by a checked-in helper, and which by nothing but the
agent's willingness to comply.

Every control-plane capability described in this repository carries a provenance label.
The label states where the behaviour is actually implemented, and therefore what happens
when the agent does not cooperate, a helper is not invoked, or a component crashes. A
taxonomy that is defined and never applied is decoration, so the labels appear at the
point of each claim rather than in a summary appendix — and the matrix below is the
authoritative index of them.

## The four levels

**Policy-only.** The behaviour exists as text in a contract file the agent reads. Nothing
outside the agent's compliance enforces it. Verification is a text read.
*Failure mode:* silent and by omission — a session that never loads the rule, a rule that
loses to a longer prompt, or a rule satisfied in wording but not in effect.
*Correct use:* genuinely judgement-shaped rules that cannot be mechanised, and the honest
first level for any new rule.

**Helper-backed.** A checked-in script, validator, gate, or generator implements the
mechanical part of the rule outside the model — a closeout gate, a drift check, a
regeneration step, a probe.
*Failure mode:* the helper is not invoked, or is invoked on the wrong target.
*Correct use:* anything with a decidable predicate. The step from policy-only to
helper-backed is mostly the step from unfalsifiable to testable, and it is the highest-value
move available to an adopter.

**Runtime-backed.** The behaviour is implemented inside the agent runtime itself. It
applies whether or not the agent cooperates and whether or not a helper runs; ordering,
locking, and idempotency become properties of the substrate.
*Failure mode:* it simply does not exist on a build that lacks it, and no amount of policy
text substitutes.
*Correct use:* invariants that must hold across crashes, concurrency, and non-compliance —
leases, delivery barriers, idempotency claims, and admission control, the gate that turns an
approved request into a bound operation with its inputs pinned before any mutation runs.

**Live-proven.** A behaviour at any of the levels above that has additionally been
exercised end-to-end against the live boundary it protects, with retained dated evidence
and a declared freshness window. This is not an implementation level and not a property of
an architecture; it is a claim about proof against one installation, and it belongs to
whoever holds that evidence.
*Failure mode:* decay. When the evidence ages past its freshness window the capability
drops back to asserted until it is re-proven — nothing about the code changed, but the
claim did.

Four rules keep the labels honest:

1. Never claim a level above the artifact that supports it. "Has tests" is helper-backed,
   not live-proven.
2. Implementation and proof are different axes. A runtime-backed capability with no
   retained live evidence is still only asserted.
3. Record an evidence reference and a last-verified time next to every live-proven claim;
   a claim with no evidence pointer is policy-only about itself.
4. Downgrade on staleness, not only on failure. Freshness expiry is the normal path.

See [evidence, audit, and verification](15-evidence-audit-and-verification.md) for the
evidence records these labels depend on, and
[guards, health, and restoration](14-guards-health-and-restoration.md) for how a degraded
capability stays auditable over time.

## Live-proven is reached by an operator, not conferred by a document

The first three levels answer an architectural question — where a behaviour has to be
implemented for its rule to hold at all — and that answer travels with the design, which is
why they work as labels in a document. Live-proven answers a different question: has this
particular installation been observed doing it. Nothing in a document can answer that on a
reader's behalf, and a table that appended live-proven to its own rows would be asserting
something about a machine the reader has never touched. The matrix below therefore carries
implementation levels only.

Live-proven is instead the level an operator reaches, capability by capability, against
their own running installation. Reaching it takes four things:

1. **An exercise against the real boundary.** The capability was run end to end against the
   thing it protects — an actual delivery path, an actual contended lease, an actual loss of
   the resource a guard defends — rather than against a fixture standing in for it.
   Installing a mechanism is not exercising it, and a passing unit test is evidence about
   code, not about a boundary.
2. **A retained artifact, dated.** A probe run, a receipt, a journal segment, or an incident
   record that outlives the session which produced it, carrying the time the exercise
   actually happened rather than the time somebody wrote the note.
3. **A declared freshness window.** How long that artifact is allowed to stand for the claim
   before the claim has to be re-established. A claim with no window never expires, which is
   indistinguishable from never being rechecked.
4. **A pointer from the claim to the artifact.** The `evidence` field of the status row
   below, or an equivalent reference. A live-proven claim with no evidence pointer is
   policy-only about itself.

What counts as sufficient depends on the capability, and deciding it in advance is part of
the work rather than a formality afterwards. For an acknowledgement barrier, the evidence
has to show ordinary traffic producing barrier records across enough distinct runs that the
mechanism can be seen carrying routine work rather than passing one demonstration. For a
resource-availability guard, it has to show the check denying dependent writes during a real
loss, an incident record written, and dependent work staying blocked until a fresh check
passed. Both are observations of a running system, and neither is something an architecture
can supply.

The level also lapses on its own. Once the evidence ages past its window the capability
returns to asserted at whatever implementation level it has — nothing about the code
changed, only the claim — and re-proving it is ordinary maintenance rather than an incident.
That decay is the whole reason this level is kept separate from the other three.

## Assigning a label, and writing it down

Deciding the label is a short procedure, applied to one capability at a time and answered
in order. Stop at the first *yes*.

1. Does code inside the agent runtime enforce this whether or not the agent cooperates and
   whether or not any helper runs? Then it is **runtime-backed**.
2. Otherwise, is there a checked-in helper that decides the predicate mechanically and
   fails when it is not met? Then it is **helper-backed** — and the label is only as good
   as the answer to "what invokes it, and on which target".
3. Otherwise it is **policy-only**, including when the rule is excellent, widely followed,
   and never yet violated.
4. Separately, and regardless of the answer above, an operator may add **live-proven** for
   their own installation once the four conditions in the previous section are met. That
   step is about an installation rather than about a design, so it is recorded on a status
   row and never in a chapter table.

A label that lives only in prose decays silently, so the intent is to carry it on a
machine-readable status row: one row per capability, in the status layer of structured
authority described in [system architecture](04-system-architecture.md). The record splits in
two, and the split is the point of the table below. The first seven fields are the **compact
record**: the whole of what a row must carry, deliberately small so that writing one is
cheap enough to actually happen. The rest are **optional extensions** an adopter may add and
a reader must not assume — an adopter who adds them takes on validating them under the same
contract tests that cover the compact record, since a field that carries authority and is
checked by nothing becomes decoration readers trust:

| Field | Part of | Type | Meaning |
|---|---|---|---|
| `capability_id` | compact record | string | Stable identifier for the capability, referenced by generated views and by the owning chapter |
| `state` | compact record | enum | Observed readiness: `ready`, `degraded`, `disabled`, `accepted-risk`, `documented-fallback`, or `source-ready-live-inactive` for something fully built and probed but deliberately not wired to a live client |
| `support_class` | compact record | string | Finer-grained qualifier, for example supported, built-in, legacy, experimental, or supported-read-only |
| `freshness_class` | compact record | enum | The reprobe policy carried on the row itself — from `stable` through `reprobe-before-use`, `reprobe-before-write`, and `reprobe-before-access-expansion` — deciding whether the row is sufficient evidence for a given action class or a fresh probe is required |
| `last_verified_utc` | compact record | timestamp | When the state was last actually established, not when the row was last edited |
| `evidence` | compact record | string | Pointer to the artifact or probe run substantiating the state. A row with no evidence pointer is an assertion, not a status |
| `constraint_or_fallback` | compact record | string | The constraint in force, or the sanctioned fallback lane, rendered into the generated operator view |
| `provenance` | optional extension | enum | One of `policy-only`, `helper-backed`, `runtime-backed`, `live-proven`. The taxonomy is a property of the capability whether or not the row carries the field |
| `route_ref` | optional extension | string or null | The integration route this row was proven against, if any |
| `probe_ref` | optional extension | string or null | The probe whose run produced the evidence, if any |
| `note` | optional extension | string | A free-text remark for a human reader. It qualifies nothing mechanically and is never read as permission |

Example: [`examples/capability-record.example.json`](../examples/capability-record.example.json),
schema: [`schemas/capability-record.schema.json`](../schemas/capability-record.schema.json).
Two properties of the row matter more than its fields. It records what was last *proven*,
not what is *permitted* — the operation's approval class applies independently, so a `ready`
row never functions as authorization. And because `provenance`, where a row carries it, is a
separate field from `state`, a capability can be runtime-backed and degraded at the same
time, which is the common case during an incident and the one a single "status" field cannot
express. Readiness
states and freshness windows are covered in
[guards, health, and restoration](14-guards-health-and-restoration.md); routes and probes in
[integrations and capability routing](11-integrations-and-capability-routing.md).

## Several primitives require runtime support, not policy

This is the repository's central honesty commitment, and it belongs before the matrix.

A control plane of this shape cannot be assembled out of contract files alone. A family of
its primitives has to be implemented inside the agent runtime, because what they guarantee
is ordering, exclusivity, and idempotency across concurrency and restarts — properties no
quantity of instruction text supplies. The areas that most often need it are durable-lane
ownership, delivery-path correctness, run admission, lineage and cancellation semantics, and
build and plugin mirror correctness. The reference implementation therefore assumes a runtime
that carries those primitives, and the consequence for a reader is direct: **a version string
does not identify which control-plane guarantees a build supplies.** Installing a build whose
version number matches does not by itself deliver the behaviour described here. Where builds
must be told apart, identify a release by version plus source commit plus promotion-run
identifier rather than by version alone.

Several guarantees this repository describes are runtime invariants wherever the runtime
implements them, and on a build that does not, they are discipline at best.

Carrying runtime code is a real and recurring cost, since every upstream change then arrives
through a deliberate port or rebase. That cost is part of the honest accounting: choosing
runtime-backed enforcement means owning a runtime. None of which says an adopter should take
that on. It says the choice has to be made deliberately and row by row — which invariants are
worth code, which are worth a helper, and which can honestly stay text — which is what the
matrix below is for.

The primitives that need runtime support rather than policy fall into three groups. The
first is the durable-lane machinery, which is what makes a long-running unit of isolated
work survive concurrency and restarts:

| Module | What it does |
|---|---|
| Acknowledgement barrier | Refuses to deliver any checkpoint or final until the lane's single visible acknowledgement is confirmed delivered |
| Acknowledgement suppression | Enforces the one-acknowledgement rule against repeat announcements for the same lane identity, and suppresses late output from cancelled or superseded runs |
| Active-lane index | Holds one authenticated claim per conversation scope answering "does this conversation already own a live lane" |
| Approval boundary | Carries the approval envelope across phases and names the boundary when a later phase would cross out of it |
| Closeout reconciliation | Reconciles status, evidence, and delivery before a lane may report completion |
| Launch contract | Fixes the establishment sequence and the supervision constants — first checkpoint due, silence warning, silent-gap ceiling |
| Message formatting | Renders the fixed lifecycle headings so the visible text agrees with the structured delivery metadata |
| Resume | Restores a parked lane in the same lane on the same identity when its approval or input arrives |
| Status artifact | Owns the on-disk record that is a lane's authoritative state, including its immutable identity fields |
| Write lease | Binds the compare-and-set ownership record that decides whether this run may mutate right now |

Each of those carries its own tests. The second group is three guards that sit on the chat
surface itself: an execution-mode guard enforcing the mode decision of
[execution and durable lanes](06-execution-and-durable-lanes.md), an inline-destructive
guard standing between an inline turn and a destructive action, and a context-read guard
governing what a turn reads into its context — the mechanical counterpart to the discipline
described in [memory and context](08-memory-and-context.md). The third group is two
primitives that are easy to overlook and expensive to reimplement: bootstrap budgets, the
per-file and total character limits applied to injected contract text, and announce
idempotency, the claim that makes one visible final exactly one.

Everything in those three groups was, at an earlier stage of the same architecture,
Markdown discipline. It migrated into the runtime because discipline does not survive
concurrency, crashes, or restarts. That migration is the single largest reason this
behaviour is not reproducible by copying contract files, and it is the reason this chapter
exists.

What did **not** migrate is equally informative: precedence, domain separation, approval
classes, route predicates, retention classes, and result interpretation stay policy-only
or helper-backed, because they encode judgement rather than ordering.

## The capability matrix

Read the columns this way. *Where it must be implemented* is the level a capability has to
reach before its rule can hold at all: policy-only where the rule is genuinely
judgement-shaped, helper-backed where a decidable predicate can be checked outside the model,
runtime-backed where the guarantee is ordering or exclusivity across concurrency and
restarts. That is a property of the design rather than of any installation, which is what
makes it the transferable part — and it is why live-proven is not a column here. *Stock
OpenClaw* describes what an unmodified build supplies as a control-plane guarantee — not whether some
adjacent feature exists, since a spawn API that returns identifiers is not lane ownership
and a send API is not exactly-once delivery. *Adopter must supply* is the minimum honest
substitute: what has to exist before the row's rule can be claimed at all, not the ideal
version of it.

The third column is the one that ages. Upstream moves, and a behaviour absent from one
build may be present in the next, so treat every entry in it as a hypothesis to verify
against the build in hand — then record the result using this same taxonomy rather than
carrying this table forward as fact. Where that column reads **unknown — verify**, builds
differ enough that no single answer holds, and the row's rule cannot be claimed at any level
until the build in hand has been checked. That marker means the same thing everywhere it
appears and is never a substitute for an answer that is actually known.

This matrix is the authoritative index of provenance labels. Where a chapter table names the
same capability, its label matches the row here; if the two ever disagree, this table is the
one to correct against.

| Capability | Where it must be implemented | What stock OpenClaw gives you | What an adopter must supply |
|---|---|---|---|
| Execution-mode selection and pinning | Runtime-backed | A turn is a turn; no mode concept | Policy text, a persisted per-origin mode record, and review of substitutions after the fact |
| One-shot inline-to-durable promotion | Runtime-backed | Nothing equivalent | Explicit re-classification with a fresh operator token; no silent promotion path |
| Inbound deadline guard | Helper-backed, with its parameters in the contract | Run and idle timeouts that abort a turn, with no concept of handing off | A wall-clock budget read inside the turn, a declared list of long phases that forces a lane before execution, and a rule that below the threshold the turn emits one blocker instead of spawning |
| Inline destructive-action guard | Runtime-backed | Exec sandbox settings only | Pre-mutation checklist in policy; accept that a permissive sandbox does not waive approval tokens |
| Durable lane identity from spawn result | Runtime-backed (launch contract) | Spawn returns identifiers | Capture identifiers into the status file as the first post-spawn action; treat pre-spawn placeholders as defects |
| Durable status artifact | Runtime-backed | Plain files, no schema | A status schema, a writer helper, and a validator invoked at every transition |
| Write lease over the mutation set | Runtime-backed | Nothing equivalent | Advisory OS-exclusive lock files plus a one-lane convention; accept that two lanes can still both write |
| Active-lane index | Runtime-backed | A session list | A lane registry file with locking; scanning status artifacts is weaker evidence, since a stale executing artifact is not proof of life |
| Acknowledgement barrier | Runtime-backed | Messages send in any order | A helper that refuses to emit a checkpoint or final without a recorded acknowledgement id |
| Acknowledgement suppression | Runtime-backed | Nothing equivalent | Idempotency check before every acknowledgement send |
| Approval boundary preservation | Runtime-backed | Nothing equivalent | Restate the envelope in every checkpoint and require a fresh token at each boundary crossing |
| Same-lane resume | Runtime-backed | Manual re-spawn | A worker entrypoint registry with declared resume entrypoints and idempotent starts |
| Closeout reconciliation | Runtime-backed | Nothing equivalent | A closeout gate script that reconciles status, evidence, and delivery before allowing completion |
| Lifecycle message formatting | Runtime-backed | Free-form text | A formatter helper and a lint that rejects non-conforming headings and missing run metadata |
| Exactly-once final delivery | Runtime-backed (terminal-delivery ledger and announce idempotency) | Provider send API | Exclusive-create claim files keyed on target, origin, class, and chunk; an explicit fail-open or fail-closed decision |
| Delivery obligation tracking | Helper-backed (an obligation record plus a closeout gate) | Nothing equivalent | An obligation record written at acknowledgement time and a closeout gate that fails until a receipt exists |
| Outbound store-and-forward queue | Runtime-backed | Unknown — verify | An outbox with a bounded backoff ladder, a single-drainer claim, a permanent-error classifier, and a failed directory |
| Context-read guard | Runtime-backed | Nothing equivalent | State the read discipline in the contract file that owns context, and say plainly that nothing enforces it; a stock build cannot refuse a read |
| Bootstrap budgets | Runtime-backed (configuration) | Context and bootstrap settings where the build exposes them | A size check in the test suite and relocation of secondary detail to conditional sources |
| Bootstrap manifest classification — role, owner, load default, sensitivity | Policy-only | Nothing equivalent | One manifest row per injected file, with the manifest's own text stating it is documentation rather than enforcement; the runtime enforces character budgets, not manifest rows |
| Precedence stack and domain separation | Policy-only | Nothing equivalent | The contract files themselves plus a drift check that asserts single ownership per rule class |
| Registry authority and generated views | Helper-backed | Nothing equivalent | Generator scripts, non-authoritative stamps, and a test that regeneration produces no diff |
| Promotion gate contract | Helper-backed | Install and upgrade commands | Build in a candidate path, verify, switch the pointer atomically, and run a manual gate checklist |
| Identity convergence checks | Helper-backed | Nothing equivalent | A script comparing pointer, package link, installed bin link, service definition, loaded arguments, and live process for each service |
| Retention classes | Policy-only plus helper-backed | Nothing equivalent | Retention metadata on every artifact and a scheduled cleanup job that honours it |
| Contract and memory write auditing | Helper-backed handler over a runtime-backed hook host | A hook interface | A hook or wrapper that emits an audit line to a fixed private route on contract and memory writes |
| Scheduler result interpretation | Helper-backed guard scripts plus policy | Cron dispatch and transport status | A wrapper that reads the job's own result contract, escalates an absent terminal marker, and an independent datastore-level check |
| Guard family | Helper-backed | Nothing equivalent | Host service definitions with install plans, install receipts, and post-install verification records |
| Route predicates and probes | Policy-only plus helper-backed | Integration clients, but no route-predicate or probe concept in front of them | A route registry, a probe registry with expected signals and freshness, and placement readback after every mutation |

Related diagram for the lane mechanics behind the first block of rows:
[`diagrams/durable-lane.mmd`](../diagrams/durable-lane.mmd). Related diagram for the promotion and
convergence rows: [`diagrams/runtime-promotion.mmd`](../diagrams/runtime-promotion.mmd).

## What this means for an adopter

A complete control plane on a stock runtime is achievable, and it is genuinely useful. It
is not equivalent, and the difference should be stated rather than discovered:

- **Guarantees become compliance.** Every runtime-backed row degrades to "the agent
  usually does this". That is acceptable for a single operator watching a single lane and
  unacceptable for concurrent lanes or unattended scheduled work.
- **Failures become quiet.** Runtime-backed rules fail closed with a named blocker;
  policy-only rules fail by omission, which is the harder failure to notice and the one
  the [failure catalogue](02-why-a-control-plane.md) is written about.
- **Verification has to compensate.** Where enforcement drops a level, add a
  helper-backed check that detects the violation afterwards. Detection is worth less than
  prevention and far more than nothing.
- **Concurrency is the cliff edge.** Nearly every runtime-backed row exists because two
  writers or a restart broke a policy-only version of it. An adopter who keeps strictly to
  one live lane per surface can stay policy-only far longer than one who does not.

The staged path — a minimum viable control plane first, helpers second, runtime work only
where an invariant genuinely requires it — is in the
[adoption guide](17-adoption-guide.md). The runtime-side consequences of owning runtime
code, including how a source commit reaches live identity convergence, are in
[runtime, releases, and promotion](10-runtime-releases-and-promotion.md).

## Keeping the matrix honest

The matrix says where behaviour has to live, not what any installation is doing. It still
decays, because implementations change and upstream moves, so four maintenance rules apply.

1. Every capability named here appears in exactly one owning chapter, and the label in
   that chapter is the same as the label here.
2. A capability that gains an implementation moves up a level; one that loses it moves back
   down. Both are ordinary maintenance, not incidents. An operator's own live-proven claims
   expire on their own freshness windows, independently of this table and recorded on their
   own status rows.
3. New capabilities enter at policy-only. Claiming a higher level without the artifact is
   the failure this taxonomy exists to prevent.
4. When an upstream build absorbs a behaviour that previously needed local runtime code,
   the stock column changes and the adopter column shrinks. That is the intended direction
   of travel, and it is recorded in
   [architecture evolution](18-architecture-evolution.md).

Where this leads: [system architecture](04-system-architecture.md) puts these labels on a
map, naming which layer owns which decision — and therefore where a guarantee a stock build
does not supply would have to be rebuilt.
