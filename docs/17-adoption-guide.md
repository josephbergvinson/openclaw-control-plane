# Adoption guide

This chapter is the staged path from "an agent runtime is installed" to "an agent system whose claims
can be checked". The order is deliberate: the trust assumption first, because several later steps are
unsafe if it does not hold; then a minimum viable control plane small enough to finish in an
afternoon and useful on its own; then four levels, each earning its complexity by fixing a failure
the level below cannot. Most of the leverage is available at the smallest level, and every level
above it answers a named class of failure — catalogued in
[why a control plane](02-why-a-control-plane.md).

## Read this first: the trust assumption

This is a **single-operator, high-trust design**, and the assumption is load-bearing rather than
incidental. The model treats one human as the source of authority. Approval tokens are not
authenticated; they are words in a conversation, trusted because only one person can reach the
surface. Nothing here separates two operators' privileges, attributes an action to a person, or
contains one person's blast radius from another's. A chat allowlist decides who may *speak* to the
agent; it is not an isolation boundary, an authorization system, or an audit identity. Four
consequences to accept before building anything:

1. **A second operator is a redesign, not a configuration change.** Adding a person, a shared channel,
   or reach past loopback changes the threat model. See
   [security and trust model](16-security-and-trust-model.md).
2. **Unattended write authority multiplies every weakness**, because scheduled and delegated work
   runs with nobody reading the output. Do not enable it before Level 3.
3. **Everything the agent reads is data, not instruction.** Memory notes, fetched pages, and tool
   output can contain text addressed to the model; the injected contracts are the only instruction
   channel, and material from elsewhere carries an untrusted-content header.
4. **Some boundaries never move.** Consent given on someone's behalf, credential entry, legal
   commitments, and moving funds stay human-only at every level — outside the system, not a high
   approval class.

## Level 0: the minimum viable control plane

Level 0 is the smallest subset honestly worth having, written for someone who will never build the
rest. It is three artifacts, all **policy-only** — text the agent reads, enforced by nothing but its
compliance and the operator's attention.

**One policy file.** A single Markdown contract in the workspace, injected every turn, with roughly
six short sections: what the agent may do unasked; what always stops and asks; how a broad request
becomes a bounded plan; where source of truth lives on this host; what "done" means; and what to do
when no rule covers the situation. Its value is not completeness — it is that a rule now has a home,
so "where does the fix go?" is never answered "nowhere".

**One explicit mode decision before side effects.** Before the first mutation of any kind, the agent
states which of three modes it is in: inline (finish in this turn), durable (hand the work to an
isolated lane with its own state record), or operator-side (hand back an exact procedure for the
human to run). What makes this worth anything is the prohibition on silent substitution: a task
announced as inline that turns out to be long stops and re-declares. See
[execution and durable lanes](06-execution-and-durable-lanes.md).

**One acceptance predicate per task.** Before work starts, write the decidable sentence that will be
checked at the end. "The build works" is not a predicate; "a bundle exists at
`<artifact-root>/example-project/`, it contains every module the task named, and a clean consumer of
that bundle passes the fixture test" is. Two properties make it worth stating in advance. It cannot
be rewritten afterwards to match whatever happened. And an absent artifact evaluates it false rather
than leaving it unexamined, which is the general form of the invariant: absence of an expected
result is an alert, not a pass.

| Level 0 gives you | Level 0 does not give you |
|---|---|
| A visible, reviewable decision before every side effect | Any enforcement when the agent does not comply |
| A named place for every rule you later add | Survival across crashes, restarts, or concurrency |
| Claims tied to a predicate stated in advance | Proof that a message was delivered or a write landed |
| Refusal boundaries that exist before you need them | Detection when a rule is simply skipped |

Level 0 makes invisible decisions visible. It is a correct stopping point for an operator running one
task at a time and reading every result; it fails silently, by omission, and hardest the moment two
things run at once. **Prove it** with two runs on throwaway targets. Ask for a small mutation: the
transcript must name the mode and the acceptance predicate *before* the first write, and the closing
message must evaluate that same predicate. Then ask for something plainly outside the file's stated
boundaries: the run must stop, name the exact boundary, and offer a bounded alternative. Ceiling:
**policy-only**, with no exceptions — nothing at this level can enforce anything.

## Levels 1 to 4

Entry criteria are not bureaucracy. A level built before the one below it is stable produces
machinery that hides the failures it was meant to expose. Each level below states when to enter it,
what to build, the first acceptance test that proves the build works, and its **ceiling** — the
strongest claim that level can honestly make, in the four-term vocabulary this repository uses
throughout. *Policy-only* means the rule is text the model may or may not follow. *Helper-backed*
means a local script or schema refuses when the rule is broken. *Runtime-backed* means the runtime
itself enforces it, so compliance is not required for the guarantee to hold. *Live-proven* means the
behaviour has been checked against the running installation and the evidence kept. The distinction is
the whole point of labelling: see [capability provenance](03-capability-provenance.md).

### Level 1: policy discipline — enter when Level 0 rules contradict each other

Two sentences in the single file disagree, or a rule about style is read as a rule about approvals.
**Build** a precedence-ordered stack with exactly one owner per rule class: a normative behaviour
contract, a normative execution mechanics contract, and below them style, durable preferences, and a
memory cue layer carrying no authority over the two contracts. Write the approval classes and the
operator control vocabulary, keeping outbound send a separate approval from mutation. Add the
bootstrap policy file and its machine-readable manifest classifying each contract by role, load
default, and sensitivity — stating in the manifest's own text that it is documentation and a lint
target, not the loader. See [policy and authority](05-policy-and-authority.md). **Prove it** by
constructing a deliberate conflict: a preference that wants what a policy rule forbids. The run must
resolve it by rank unprompted, name the winning file, and not treat the lower-ranked file as having
gained authority by being loaded. Ceiling: policy-only plus the first helpers.

### Level 2: structured facts — enter when prose about the machine has gone stale

A statement about the machine was believed because a Markdown file said it, and it was wrong.
**Build** three machine-readable layers: `registry/` for durable topology and routes, `status/` for
observed live state, `control/` for enforcement parameters. Add a read-only probe per route that
proves which identity answered. Generate the human-readable rollups from those layers, stamp each
non-authoritative, and delete the hand-maintained originals. Add the first helpers: a drift check
asserting single ownership per rule class, and a size check comparing each contract against the live
budget. See [integrations and capability routing](11-integrations-and-capability-routing.md).
**Prove it** by hand-editing a generated view so it disagrees with its source, then asking a question
the view answers. The answer must come from the structured source, the divergence must be reported as
a finding, and regeneration must produce no diff. Second check: a request naming a non-default
account stops with a route blocker rather than being satisfied by the ready default lane. Ceiling:
helper-backed.

### Level 3: durable execution — enter when work outlives a chat turn

A task runs past its turn, two pieces of work touch the same files, or a completion is announced and
nothing is delivered. **Build** the lane: a durable status artifact with a schema and a validator
invoked at every transition, lane identity captured from the spawn result as the first post-spawn
action, an advisory exclusive lock — a write lease — held over the write set, meaning the exact list of
paths this lane may modify, and a lane registry consulted before classifying anything. Then the
delivery layer: an obligation record written at acknowledgement time,
a closeout gate that keeps failing until a receipt exists, and exactly one final per workstream. See
[execution and durable lanes](06-execution-and-durable-lanes.md) and
[delivery and the control surface](13-delivery-and-control-surface.md). **Prove it** by starting a
long lane, interrupting the runtime mid-work, and restarting. The lane must resume in place rather
than starting a second one; a stale executing artifact must count as recovery evidence rather than
proof of a live lane; the final must be delivered once, with a recorded provider message identifier.
Then close a task without delivering, and watch closeout fail on the missing receipt. Ceiling:
runtime-backed where the build allows, helper-backed otherwise.

### Level 4: unattended and release-grade operations — enter when nobody is watching

Work runs on a schedule, or the runtime itself must be upgraded without an outage nobody notices.
**Build** three things in order. A scheduler registry: every job from both layers in one file, each
with exactly one of three lifecycle states, an explicit timezone, its own result contract, and a flag
asserting it does not point at a disposable worktree — plus one monitor sharing no components with
what it monitors. Guards as receipted installations, each with an install plan, an install receipt,
and post-install verification. Runtime promotion as separated phases: build in a candidate path, seal
an immutable release, switch a pointer atomically, prove live acceptance — never rebuild in place.
See [scheduling and background work](12-scheduling-and-background-work.md),
[guards, health, and restoration](14-guards-health-and-restoration.md), and
[runtime, releases, and promotion](10-runtime-releases-and-promotion.md). **Prove it** by breaking a
scheduled job upstream of its terminal marker: the independent data-level check must alert on the
absent result rather than reporting success, and recovery must emit the same transition message that
recovery from an explicit failure would. For promotion, promote a build and run the convergence check
— pointer, installed link, service definition, loaded arguments, and live process must all name the
same release, with the prior release retained as a rollback target. Ceiling: helper-backed, and
live-proven once the evidence is retained.

## The corrected minimum file set

A five-file stack is a teaching example, not the shape a working control plane converges on. Three
additions matter more than their size suggests: the **policy manifest**, the **generated-view
distinction**, and the **memory tier**. Only always-tier rows cost context on every turn.

| Path | Role | Load tier | First needed |
|---|---|---|---|
| Behaviour contract | Rank 1 normative | always | Level 0 |
| Execution mechanics contract | Rank 2 normative | always | Level 1 |
| Style contract, preference contract | Rank 3 voice only; rank 4, no policy authority | always | Level 1 |
| Memory cue file, identity stub | Rank 5 pointers only; identity and defaults outside the ladder | always | Level 1 |
| Bootstrap policy plus its JSON manifest | Declared load intent; documentation and lint target | conditional / never | Level 1 |
| Policy manifest (JSON) | Versions the kernel, names the authoritative layers, records cutover state and a fresh-session flag | never | Level 1 |
| Policy changelog | Historical audit only, explicitly non-normative | never | Level 1 |
| Generated capability view, landing index, liveness view | Rendered from the structured layers; a banner names the canonical source | conditional | Level 2 |
| `registry/`, `status/`, `control/` | Durable facts, observed state, enforcement parameters | on demand | Level 2 |
| `memory/` | Dated notes and topic sources; sensitivity is a per-file attribute | on demand | Level 2 |
| `runbook/`, `scripts/`, `tests/` | Relocated mechanics; gates, generators, and the invariant suite | on demand or not injected | Level 2 |
| `artifacts/`, `audit/`, `hooks/` | Evidence trees, append-only event log, write-audit hook | not injected | Level 3 |

The **policy manifest is not the bootstrap manifest**: the bootstrap manifest declares which contract
files should load, the policy manifest declares which *layers* are authoritative and versions the
kernel as a whole. Without it, "which file wins" is answered by prose that drifts. A generated view
without its banner becomes a second source of truth the moment someone edits it. And the **memory tier belongs in
the file set**: its absence pushes durable facts back into the always-loaded contracts, which is what
makes them overflow. See [memory and context](08-memory-and-context.md).

## Installation sequence

Steps 1 to 5 are Level 0 and 1; the rest follow the levels. Adaptable skeletons for the files named
in the table above — both normative contracts, the style and preference files, the memory pointer
layer, the identity stub, the load-policy file, the changelog, and the policy manifest — live in
[`templates/`](../templates/), and `scripts/install_templates.py` copies them into a workspace
without overwriting anything already there. Every angle-bracket placeholder in them is a decision
still to be made: a placeholder left in place is an unwritten rule, not a default.

1. Put the workspace under version control before writing any contract text — a local authoritative
   remote is enough. Rules change faster than anyone expects, and without history there is no way to
   answer "which version of this rule was in force when that run behaved that way?", which is the
   first question every policy investigation asks. Then write the single Level 0 contract and measure
   it against the per-file bootstrap character budget the runtime actually applies.
2. Write down the trust model: one operator, what the allowlist does and does not do, the boundaries
   that stay human-only, and the operator control vocabulary, recording that outbound send is a
   separate approval that mutation approval never implies.
3. Configure exactly one chat control surface, with the gateway bound to loopback and read-only tools
   only, so that during the early period when nothing is enforced there is one entry point to reason about
   and nothing reachable that a mistake could damage. Declare one canonical source root per active
   surface and treat nothing else as source: two plausible roots produce split-brain editing, where a
   convenient copy of a repository is treated as canonical because it happens to exist, and the fix
   costs far more than the prevention. See
   [source layout and artifacts](09-source-layout-and-artifacts.md).
4. Run the Level 0 acceptance tests. Do not enable a write tool until they pass.
5. Split the contract into the precedence stack; add the bootstrap manifest and the policy manifest;
   start the changelog with one row per policy change.
6. Stand up `registry/`, `status/`, and `control/`; add one probe per route; generate the views and
   stamp them.
7. Add the first helpers — drift check, size check, regeneration check — as executable tests rather
   than prose checklists. A predicate a script can decide belongs in a script; only judgement-shaped
   rules stay prose.
8. Build the durable lane: status schema, validator, lane registry, write lease. Then the delivery
   layer: obligation at acknowledgement, receipt at completion, closeout gate between them.
9. Add external integrations one route at a time, each with a read-only probe that proves which
   account or workspace actually answered, and a readback that proves the write landed where it was
   asked to land. A route nobody has probed is unknown rather than ready, and a link returned by an
   interface is a claim about a request, not evidence of placement.
10. Add scheduled work: registry first, then jobs, then one independent safety net. Then guards, with
    install plans, receipts, and post-install verification.
11. Add runtime promotion last, once ordinary source workflows are boring.
12. After any contract change, start a fresh session before the next execution slice. Edits do not
    reliably retro-apply to running sessions, so a run that began earlier still operates under the
    old text.

## Context sizing

Injected contract text is charged against the same context window as the task, on every turn, forever
— the cost adopters discover late. Two budgets govern it, both in the runtime's agent defaults: a
**per-file** bootstrap character budget and a **total** budget across all injected files.
Over-budget content is not dropped; it is kept as a head plus a tail — a typical split is roughly
three quarters head and one quarter tail — joined by an inline marker naming the file and the
kept and original character counts, so truncation announces itself rather than silently shrinking a
rule. That shape matters when deciding where a rule sits in a file: the material most at risk is the
middle. An announced truncation is still a rule the model may not have read.

| Symptom | Likely cause | Response |
|---|---|---|
| A rule is followed early in a session, not later | Total budget pressure plus conversation growth | Move detail to a runbook; checkpoint and roll over sooner |
| A rule is never followed at all | The file overflowed its per-file budget and the rule sits in the truncated middle | Relocate the section; verify the count against the live budget |
| A delegated or scheduled run ignores a rule a chat run follows | Reduced injection set for child and scheduled sessions | Move the minimum authoritative sentence into a contract those sessions receive |
| The stack fits but leaves no room to work | Always-loaded set too large for the model in use | Apply the relocation ladder below |

Three sizing rules are worth adopting verbatim: measure against the *live* configuration rather than
the declared manifest, since the two diverge; keep the always-loaded-plus-highly-sensitive
combination empty by construction; and treat "restore headroom" as recurring maintenance with an
owner rather than a one-off cleanup. When the stack outgrows the window, apply this ladder in order:

1. Relocate mechanics to runbooks read on demand, and move every statement of current fact into the
   structured layers. Detail is not lost; it stops being charged per turn.
2. Retire superseded rules rather than appending qualifications. Additive patching drives most growth.
3. Apply the self-sufficiency invariant in reverse: anything a delegated session must obey stays
   injected, everything else may leave.
4. Reduce the always-loaded set, accepting that some judgement moves into helper scripts — a downgrade
   in flexibility, not in safety.
5. Only then raise the budgets, and re-test with the exact model and channel path in use: a stack that
   fits one provider's window is not portable evidence. Where prompt prefixes are cached, a large
   stable set is cheaper than a small volatile one — churn at the top of the stack is what costs.

## Storage and operational economics

Durability produces artifacts, and artifacts accumulate. Plan retention alongside the mechanism that
generates the data, not after the disk fills.

- **Immutable releases accumulate.** Every promotion adds a complete self-contained tree plus a
  per-release dependency cache, so the release store grows monotonically at whatever rate builds
  are promoted. A retention layer is a day-one requirement, and its helpers should be timid:
  dry-run by default, scoped to known roots under admitted naming families, always keeping the
  newest few and anything younger than a minimum age, and refusing to remove a release still
  referenced as a rollback target.
- **Scheduler run histories accumulate.** Each job appends a record per execution, and guard and
  report jobs write artifact trees alongside. That history is what makes "at which run did this job
  stop producing a result?" answerable, so the answer is a retention class per artifact family —
  neither deletion nor unbounded growth.
- **Configuration backups accumulate.** Every edit to runtime configuration leaves a dated snapshot
  beside a known-good copy: small files, unbounded count. Evidence trees, memory notes, and task
  worktrees grow monotonically without a policy.

Decide four things before the problem arrives: the retention class of every artifact family; who
prunes and on what schedule; what is protected from pruning and how that protection is asserted
mechanically; and where the evidence of a deletion is written. A storage headroom guard that fails
closed is cheaper than a recovery.

## What you must supply yourself on a stock runtime

Much of what this repository describes belongs *inside* a modified runtime rather than in Markdown,
so a stock runtime does not supply it. [Capability provenance](03-capability-provenance.md)
carries the full matrix and the minimum honest substitute for each capability; read it before
assuming any behaviour here is free.

| Capability | Backing when the runtime carries it | Stock-runtime substitute |
|---|---|---|
| Execution-mode selection, guards, pinning | runtime-backed | Policy text plus a persisted per-origin mode record, reviewed after the fact |
| Lane identity, write lease, active-lane index | runtime-backed | Advisory OS-exclusive lock files and a lane registry; two lanes can still both write |
| Acknowledgement barrier, exactly-once final | runtime-backed | A helper refusing to emit without a recorded acknowledgement id, plus exclusive-create claim files keyed on target, origin, class, and chunk |
| Bootstrap budgets | runtime configuration | A size check in the test suite, and aggressive relocation |
| Sensitivity-aware loading | policy-only in every case | Nothing — do not describe it as enforcement |

Four consequences. Guarantees become compliance, so every runtime-backed row degrades to "the agent
usually does this". Failures become quiet, because policy-only rules fail by omission. Verification
has to compensate: where enforcement drops a level, add a check that detects the violation
afterwards — the apparatus in
[evidence, audit, and verification](15-evidence-audit-and-verification.md) exists for exactly this
substitution — since detection is worth less than prevention and far more than nothing. And concurrency
is the cliff edge — an adopter who holds strictly to one live lane per surface stays safe at
policy-only far longer than one who does not.

## Where this leads

Adopt in this order and the system stays legible at every stage. For which parts of this design have
stabilized, which are still moving, and why the levels sit in this order, continue to
[architecture evolution](18-architecture-evolution.md); for one request traced through every
mechanism at once, see the [worked example](07-worked-example.md).
