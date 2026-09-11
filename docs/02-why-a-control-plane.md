# Why these controls exist

The goal is an assistant that accepts ordinary requests, chooses appropriate tools and keeps working until the requested outcome is verified. Achieving that requires both capable judgment and dependable execution machinery.

The controls in this reference address failures observed in real workflows. Each control should earn its maintenance cost by preventing a failure or making recovery clearer. Adding another status file or approval phrase is not automatically an improvement.

## An answer can be polished and incomplete

A calendar entry may be created without the joining link. A technical report may sound authoritative while relying on an obsolete design. A personal-memory answer may confuse unavailable records with facts that never existed.

The remedy is to choose the authoritative source for each claim, inspect it and verify the actual result. Current invitations determine meeting details. Repository code determines implemented behavior. A destination application's readback determines whether a write landed correctly. The agent must distinguish evidence, inference and missing information without making the operator supervise every retrieval step.

## Work can finish internally and disappear externally

A child can return a result while its parent fails to resume. A conversation can contain two final-looking records while the channel displays only one message. A delivery attempt can time out after the provider has accepted the message.

Execution state and delivery state therefore need separate evidence. The runtime owns continuation and delivery coordination; the assistant reports the practical outcome. Retry only after checking the destination and retained receipts. Do not promise exactly-once external effects across an ambiguous provider boundary.

## A task can look active after progress stops

An old status document can continue to say working after its process has ended. A context warning can remain visible after compaction completes. A long writing task can eventually succeed while giving the operator no useful indication of progress.

Use the runtime's current lifecycle evidence and the actual process or job handle. Send concise updates at meaningful milestones during substantial work. A warning describes a condition; it does not prove that a subsequent action completed. Where a lifecycle path has no completion notice, document that limit rather than manufacturing certainty from a lower token count.

## Instructions cannot repair execution plumbing

Telling an agent to finish reliably does not pass a configured deadline into a worker process. Asking it to remember its goal does not make accepted usage survive a context transition. Requiring prompt delegation does not guarantee that the child's session data is available when the parent returns.

These are implementation problems. The reference includes runtime changes and relevant tests for the execution paths involved. Policy explains how the operator expects the assistant to behave; code provides the mechanisms that make the expectation practical.

## More approval steps can make a system less useful

A request such as fix this bug already authorizes the ordinary investigation, edit and validation needed to fix it. Requiring a new magic token for every reversible step creates stalls without resolving a real decision.

Authorization follows the authenticated request and its scope. A child inherits that scope. Ask for input when a missing choice would change the target, account, consequence or requested outcome, or when a human-only credential boundary is reached. This retains meaningful boundaries while allowing competent initiative.

## An operational success can belong to the wrong configuration

A scheduled job may keep the same identifier after its command changes. Its last successful result can then describe an older producer. A launcher can point to the correct script but select an unsupported Node executable or the wrong runtime state directory.

Bind execution evidence to the current job definition, interpreter, state root and source revision. Read a job through the environment it actually receives. Keep installation, scheduled admission, producer completion, delivery and domain acceptance separate. A manual probe cannot stand in for an untouched scheduled run.

## A backup can exist without proving recoverability

A cloud account can be enrolled while no new files have uploaded. A local archive can pass byte comparisons but lose required filesystem metadata. A container engine can run while its data remains absent from the intended backup.

State the backup scope, destination, exclusions and verification predicate. Restore actual content and compare the properties that matter for its use. Keep original data until the required proof has passed. Independent host maintenance should operate without asking the model to remember whether a backup happened.

## The direction of travel

Prefer the native runtime mechanism when it already owns the relevant state. Remove superseded wrappers and duplicate policy. Keep detailed mechanics available on demand and leave the always-loaded instructions focused on judgment, authority and completion.

The desired result is reduced operator effort: useful work completes with fewer corrections, less unexplained waiting and predictable recovery. [Architecture evolution](18-architecture-evolution.md) separates that direction from capabilities already implemented and verified.
