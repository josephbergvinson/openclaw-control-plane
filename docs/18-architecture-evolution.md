# Design decisions and direction

This reference is built around a practical objective: let one operator delegate useful work across their projects and personal administration without constantly specifying tools, repeating context or diagnosing the agent's internal state.

The current build is a particular implementation of that objective. The direction described here is not a claim that every desired property has already been achieved.

## Keep judgment flexible and effects verifiable

Ordinary requests are incomplete descriptions of a workflow. A capable assistant needs freedom to choose sources, divide work and resolve low-risk details. Rigid prompt templates and mandatory approval vocabulary make the operator do more of that reasoning.

At the same time, effects such as changing an event, publishing a file or switching a runtime generation need explicit targets and verification. The design puts flexibility in investigation and judgment, while applying precise identity, idempotency and readback at the points where the world changes.

This is why source-first instructions and effect checks coexist. The policy asks the agent to retrieve the correct invitation; the destination readback proves that the event contains the intended details.

## Prefer one owner for each kind of state

Older arrangements accumulated parallel status files, wrapper processes and overlapping policy layers. Each extra owner introduced a reconciliation problem: two records could disagree about whether work was active, who should send the answer or which rule applied.

The current direction favors the native runtime for sessions, goals, scheduler state, worker continuation and delivery coordination. The workspace retains the instructions, integrations and operational procedures that belong outside the model loop. A derived report stays a report; it does not become a second scheduler.

Retiring a wrapper is useful only after its real responsibility has a supported owner. Deleting a confusing file while leaving its job unowned would reduce visible complexity at the expense of correctness.

## Treat instructions as maintained interfaces

`AGENTS.md` provides the small, shared operating contract. Detailed provider mechanics and repeatable procedures are read when relevant. Style, preferences, remembered context and current facts have different owners.

This reduces contradictory instructions and avoids paying for every operational detail in every model interaction. It also makes changes reviewable: a voice preference should not alter execution authority, and an old memory entry should not override a newly verified source.

The public templates preserve these behavioral decisions. They substitute neutral identities and examples while retaining useful instructions. Their purpose is to help another operator build similar behavior, rather than merely admire the architecture.

## Preserve context without pretending it is infallible

A large context window is useful, but it is temporary and finite. Durable objectives, source pointers and progress checkpoints allow work to continue after context reduction. Structured personal or company data can supply facts that conversation summaries omit.

Compaction introduces uncertainty when it loses a constraint or the interface leaves its status unclear. The correct response is to preserve critical instructions and expose supported lifecycle information. A warning threshold is not evidence that compaction finished, and a successful compaction does not prove that every relevant fact survived.

## Make operational cost visible

Persistent runtimes accumulate release trees, dependencies, task artifacts, scheduler history and backups. A local-first installation also depends on host permissions, storage mounts, network routes and application sessions. These costs remain even when the model is excellent.

Use independent maintenance for deterministic housekeeping, preserve rollback and restore evidence, and distinguish configured backup coverage from verified recoverability. Keep the active installation small enough to inspect. Avoid placing redundant copies and speculative helper layers into the live path.

## Test the operator's experience

A unit test can establish that a worker receives its timeout. It does not show that a user receives a clear result after a complex request. A good report can arrive after an unacceptable silent wait. A task that succeeds after repair demonstrates more than an unverified claim, but less than a consistently successful first attempt.

Evaluation should therefore include natural requests, independent source checks, actual destination effects, visible progress and intervention history. The reference's verification instructions retain these distinctions. A new adopter must earn their own live acceptance on their accounts, devices and workloads.

## The next standard of success

The desired end state is repeated useful completion with little operator intervention: correct sources and destinations, clear progress, preserved authorization and context, predictable interruption recovery, and backups that have been restored successfully.

Progress toward that standard should be measured through completed work and reduced recovery burden. Adding a provider, a plugin or another persistence layer is justified when it removes a demonstrated limitation. Feature count alone does not show that the assistant has become more dependable.

The [capability matrix](03-capability-provenance.md) records implemented responsibilities, the [release guide](10-runtime-releases-and-promotion.md) records how a build is adopted, and the [verification chapter](15-evidence-audit-and-verification.md) explains what evidence a completion claim requires.
