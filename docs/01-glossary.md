# Glossary

This chapter defines the vocabulary the rest of the repository uses. Terms are defined as
they are used here, which is sometimes narrower than common usage. Where a term names a
mechanism that exists only in the reference implementation rather than in a stock runtime,
the entry says so; [capability provenance](03-capability-provenance.md) is the systematic
version of that distinction.

Start with [chapter 00](00-start-here.md) if the runtime itself is unfamiliar — the
entries below assume the gateway, session, and tool model described there.

Entries are alphabetical, and most run to two sentences rather than one. The second sentence
is usually the useful half: where a term names a rule or a mechanism, it states the failure
that rule prevents, because a definition that omits the failure gives a reader no way to judge
whether the mechanism is worth having.

## Notation

Angle-bracket names are the notation used throughout this repository. Fill them in for your
own environment as you go.

| Notation | Means |
|---|---|
| `<workspace-root>` | The control-plane workspace directory |
| `<project-root>` | A canonical source repository root |
| `<runtime-dir>` | The runtime state and configuration directory |
| `<releases-root>` | The immutable release store |
| `<release-id>` | One sealed release identifier |
| `<worktree-root>` | A task worktree location |
| `<artifact-root>` | Evidence and artifact output root |
| `<run-id>` | A durable run identifier |
| `<session-key>` | A session or child-session identifier |
| `<channel-id>` | A chat channel route |
| `<account-route>` | An account or workspace route |
| `<credential-ref>` | The name of a credential source, never a credential value |
| `example-project`, `example-org` | Generic project and organization names |
| `operator@example.com` | Generic address |

Short forms such as `a1b2c3d` stand in for commit hashes, and `<t0>` through `<t9>` denote
ordered timestamps.

## Terms

| Term | Definition |
|---|---|
| **acceptance predicate** | The explicit, checkable condition that decides whether a run, gate, or artifact succeeded, stated before the work rather than judged after it. Where evidence is monotonic, exact equality is deliberately not used as the predicate. |
| **acknowledgement** | The single visible message opening a durable lane, stating that the work was accepted, which execution mode was chosen, and what the next update will be. Exactly one is permitted per lane, and it is the event that creates the delivery obligation. |
| **acknowledgement barrier** | The rule that a durable lane may emit no checkpoint and no final until its single visible acknowledgement is confirmed delivered. A missing or rejected acknowledgement blocks delivery rather than allowing an unstructured fallback message. |
| **active context** | The model's context window for one session. It is a cache: nothing in it survives a reset or a compaction because it was said, only because a file was written. |
| **active-lane index** | The authenticated runtime record of which durable lane currently owns a conversation scope. It is consulted before classification and before spawn, and it outranks any status file found by scanning the artifact store. |
| **admission** | The gate that turns an approved request into a bound, executable operation: inputs pinned by content hash, preconditions such as quiescence checked, and a receipt written before any mutation is permitted. |
| **agent turn** | One accepted request travelling through the loop — intake, context assembly, model inference, tool execution, streaming reply, persistence. Named by a run identifier returned at acceptance, not at completion. |
| **approval envelope** | The bounded scope an approval actually covers: objective, target, account or workspace route, mutation and risk class, and stated exclusions. Escalation is defined as a change to one of those boundaries, which is what stops an approval from stretching to cover work it never saw. |
| **approval token** | The operator word granting a class of action, valid only in the operator's own current message and bound to that message, the requester, and the exact route. Outbound send is a separate token from mutation and is never implied by it. |
| **artifact root** | The output root under which run evidence is written. It stays a stable addressing surface even when the underlying storage is reorganized, so references written months apart still resolve. |
| **blast radius** | How far an action's effects reach, from local and contained to external and irreversible. With reversibility it determines the execution class and the execution mode. |
| **bootstrap budget** | The per-file and total character limits applied to contract files injected into a session. Over-budget files are head and tail truncated with a marker naming the file and the kept versus original size, never silently dropped. |
| **bootstrap manifest** | The declaration classifying every contract and deep memory file by role, load default, sensitivity, and owner file. It is documentation and a lint target rather than the loader, so what it declares and what the runtime injects can diverge and must be reconciled by hand. |
| **canonical root** | The single declared source-of-truth directory for a surface, resolved through the topology registry. A copy that merely exists on disk is never treated as canonical. |
| **capability provenance** | The four-level honesty taxonomy this repository labels features with: policy-only, helper-backed, runtime-backed, and live-proven. See [chapter 03](03-capability-provenance.md). |
| **channel** | A messaging route the agent appears on. The set of channel identifiers is derived at runtime from bundled plugin manifests rather than fixed in core. |
| **channel adapter** | The extension package implementing one platform's inbound event handling and outbound sending, including per-platform limits, chunking, and gating. |
| **chat surface** | A messaging route where an operator directs work and reads results, as distinct from where work runs. It holds no authoritative state, so losing one costs reachability and never history. |
| **checkpoint** | A non-terminal, operator-visible progress update from a durable lane, emitted at named phases and on a deadline rather than by polling. A checkpoint is never a handoff and never a final. |
| **closeout** | The end-of-work step that finalizes a lane's record and evidence — verification results, artifact references, and the terminal status — before or alongside the final delivery. |
| **closeout gate** | The helper deciding whether a slice may be reported complete, reconciling source state, standing-branch normalization, evidence, and delivery first. It keeps failing while a delivery obligation has no receipt, which is what stops an internally finished run from closing invisibly. |
| **compaction** | The reduction of a session's accumulated transcript or context once it crosses a size threshold. Because compaction discards detail, anything that must outlive the session is written to a file before it happens rather than after. |
| **containment** | The recorded, narrowest reversible reduction of a capability applied between discovering a defect and fixing it, naming the reduced feature, the reason, the lost functionality, the verification target, and the re-enable gate. A contained system is not a healthy one. |
| **continuity contract** | The versioned plan governing a substantial external work product: exactly one authoritative object, an object-creation budget with a disposition for every transient object, the proven route, an ordered fallback list, and a required authoritative readback. |
| **contract file** | A Markdown file in the workspace root that the runtime injects into the system prompt from a fixed filename list. See [chapter 05](05-policy-and-authority.md). |
| **control surface** | The conversation or client through which an operator directs and observes work, as distinct from where the work actually runs. For durable work the chat thread degrades to a control surface. |
| **credential reference** | The name or path of a credential source recorded in place of the credential itself — an environment-variable name, a permission-restricted file path, a named entry in an operating-system credential store, or a provider-native store. A configured credential never by itself satisfies a mutation approval. |
| **cue layer** | The always-injected pointer file, hard-capped at roughly a dozen entries, each of which is a pointer to the file owning a concept rather than an explanation of it. Its budget is spent on routing, because a cue layer that grows explanations becomes a second unversioned copy of what it describes. |
| **delivery obligation** | The machine-checkable record created at acknowledgement time, before any side effect, naming the target, the last visible update, the next owed update, and the completion path. It clears only on a visibly delivered final or an explicit cancellation, so careful wording cannot discharge it. |
| **delivery receipt** | The record proving an outbound message landed, carrying the provider's own message identifier. Transport acceptance without a receipt is not treated as delivery. |
| **draft-only** | The default posture: absent an approval token or a standing directive, work stops at a plan or a draft, nothing is mutated, and nothing leaves the machine. |
| **drift lint** | The check asserting that exact headings and phrases are present in the normative contracts, that retired text is genuinely gone, and that structural anchors and manifest state strings are intact. It exists because authority usually erodes by paraphrase rather than by deletion. |
| **durable lane** | A long-running unit of isolated work spawned out of a conversation, with its own child session, spawn-derived run identity, status artifact, worktree, and write lease. See [chapter 06](06-execution-and-durable-lanes.md). |
| **durable memory** | Markdown files in the workspace recording what was believed at the time of writing. Descriptive and never normative, and never a substitute for re-probing a fact that has a live counterpart. |
| **epoch** | A monotonic counter carried on the write lease beside generation. The pair is what a compare-and-set acquire checks, so an acquire holding an out-of-date view of ownership is rejected instead of silently winning. |
| **event log** | The append-only workspace log whose lines carry four fields — time, kind, scope, and one human-readable sentence — and point at evidence held elsewhere. Lines are never edited or removed; a correction is a new superseding line. |
| **evidence freshness** | How recently a piece of evidence was verified, tracked as a class on status and audit records so that stale proof is not read as a current fact. |
| **exactly-once final** | The guarantee that a lane's terminal result reaches its surface once. Enforced by a canonical idempotency key plus a reservation the sender must win before composing the message. |
| **execution class** | The approval tier a request falls into, derived from blast radius and reversibility, ranging from read-only discovery that needs no approval to exact-scope high-risk work that needs a reviewed plan. |
| **execution mode** | The one way work will run, committed to before any side effect: inline, durable lane, or operator-side manual containment — also written terminal-side, meaning at the operator's own terminal and unrelated to a run's terminal state. Silently substituting a different mode is prohibited. |
| **fail-closed** | The posture that when identity, ownership, provenance, or approval cannot be positively proven, the system stops at the nearest safe boundary and reports one narrow blocker instead of guessing. |
| **fan-in** | The named owner responsible for merging parallel workers' output back together. Fan-out with no named fan-in owner is forbidden, because an unowned merge is where two correct halves become one wrong whole. |
| **freshness class** | The rule attached to a capability row deciding how recently it must have been proven for a given class of action — stable, volatile, reprobe-before-use, reprobe-before-write, reprobe-before-access-expansion. Write-class actions mostly require a fresh probe, so a day-old green row licenses nothing. |
| **freshness window** | The bounded interval within which an artifact or receipt still counts as current evidence. Outside the window the same file is recovery evidence, not proof of a live state. |
| **gateway** | The single long-lived daemon that is the runtime: it owns every channel connection, runs the agent loop in-process, serves the control protocol, and is the sole authority for state. See [chapter 04](04-system-architecture.md). |
| **generation** | A monotonic counter on an ownership record. Taking a lease from a dead owner requires generation N plus one, together with prior-owner-dead proof, a diff inventory, and a fan-in plan. |
| **guard** | A supervised job that asserts one precondition — storage present, headroom sufficient, workspace isolated — and fails closed with an incident record rather than warning. See [chapter 14](14-guards-health-and-restoration.md). |
| **handoff** | The transfer of a unit of work from one owner to another, complete only once the receiving lane exists and demonstrably owns it. A visible message announcing a handoff is not itself a handoff, and treating it as one discharges the operator's attention against work nothing is doing. |
| **hash-chained journal** | An append-only record whose events each carry the previous event's digest as well as their own. Every read re-verifies naming, sequence contiguity, chain linkage, and digests before an append is permitted, so a gap or an edit is fatal rather than cosmetic. |
| **health class** | The enumerated answer to whether the system can be trusted with the next unit of work, carried beside task state and never inferred from it. Reporting only one of the two produces either a green result over a degraded substrate or a healthy system described as broken. |
| **helper-backed** | Provenance level: the mechanical part of a rule is implemented by a checked-in script, gate, validator, or generator outside the model. It holds whenever the helper is actually invoked, on the right target. |
| **hook** | A registered callback the runtime invokes at a named point in the agent, tool, or gateway pipeline. Hook decisions are deliberately asymmetric: a blocking result is terminal, while a permissive result is a no-op that cannot clear another handler's block. |
| **idempotency key** | The canonical string identifying one logical delivery — provider, account, hashed visible target, hashed origin, delivery class, and chunk coordinates — under which a terminal message is reserved exactly once. Message text is excluded on purpose, so a corrected final cannot become a second visible one. |
| **identity convergence** | The state in which every surface naming the running release agrees: the pointers, the on-disk service definitions, the loaded service arguments, and the live processes of each supervised service. Anything less is not a completed promotion. |
| **inbound deadline guard** | The hard wall-clock budget on the inbound reply worker, with a safe-remaining threshold and a declared list of long phases that force durable-lane establishment before execution rather than after the first slow step. |
| **inventory diff** | A comparison of a tree's inventory taken before and after an action. A zero diff asserts that the action changed nothing outside what it was permitted to change. |
| **known-issues ledger** | The durable record that makes a contained degradation auditable over time, one entry per defect carrying an issue id, the capability it degrades, its classification, the containment in force, evidence, and a freshness class. It is what stops containment from becoming tribal knowledge. |
| **lane** | Used in two senses, always qualified in context: a durable lane, which is a unit of isolated work; and a promotion lane, which is the per-run directory holding one promotion operation's evidence. In the runtime a session lane is also the queue that admits one run at a time for a given session key. |
| **lease** | A time- and identity-bound claim of exclusive ownership. Ownership questions are answered by the authenticated lease, not by the presence of a file that looks live. See also write lease. |
| **live-proven** | Provenance level: a behaviour at any implementation level that has additionally been exercised end to end against the live boundary it protects, with retained dated evidence and a declared freshness window. It is a claim about proof, and it lapses when the evidence ages out. |
| **load tier** | When a file enters context — always, conditional, or never. It is independent of the file's sensitivity rating, and conflating the two is how a high-sensitivity file ends up in the default profile. |
| **MCP server** | An external tool source mounted over stdio or HTTP, contributing tools to the catalog without being installed into the runtime. |
| **memory tier** | The layering of memory by when it enters context: always-injected contracts and cues, a runtime-selected window of recent notes assembled per new session, and everything else reachable only by search followed by a line-scoped read. |
| **mutation ladder** | The fixed order of preference for touching an external system: verified API or integration lane first, then managed browser automation, then host user-interface automation. Each step down weakens the readback, so the order is chosen before the first mutating call rather than discovered by trying things. |
| **node service** | The second supervised process: a peripheral device agent that connects to the same control protocol as any other client but declares the node role, offering commands such as camera, screen, notifications, and shell execution. |
| **operation lock** | The exclusively created record that owns one high-consequence operation for its lifetime. Receipts are bound to its path and hash, so a receipt cannot be replayed under a different operation. |
| **origin message** | The operator message a unit of work is bound to. Mode records, approval envelopes, lane claims, and delivery keys all anchor to it, which is what lets a replayed inbound message be recognized as the same request rather than served twice. |
| **permit** | A durable one-shot authorization for a lifecycle action, journaled before dispatch and consumed by any ambiguous outcome — non-zero exit, timeout, interruption, or exception. Ambiguity counts as "it happened", so "just run it again" has no representation in the model. |
| **persisted-not-applied** | A promotion whose new state is written but not yet reflected by the running processes. It is reported as exactly that, never as live, and it is the correct stopping point when a restart does not converge. |
| **phase receipt** | A phase's declared receipt contract — receipt kind, required output fields, and their expected values — checked against what the command actually emitted. A command that exits zero but did the wrong thing still fails the phase. |
| **plugin** | An extension package that registers typed capabilities against the runtime's plugin API, and may also contribute hooks, tools, commands, services, and HTTP routes. |
| **policy manifest** | The machine-readable record that versions the policy kernel, names which layers are authoritative, and carries cutover state and a fresh-session flag. Distinct from the bootstrap manifest, which declares which files load rather than which layers rule. |
| **policy-only** | Provenance level: the behaviour exists as text in a contract file, and nothing outside the agent's compliance enforces it. Its failure mode is silent omission, which is the hardest kind to notice. |
| **precedence ladder** | The five declared ranks that resolve conflicting instructions: normative behaviour, normative execution mechanics, style, durable preferences, memory pointers. Lower never overrides higher, and no file becomes normative merely by being loaded. |
| **preserve-disabled** | A scheduled job deliberately not running and kept in the inventory on purpose, carrying why it was switched off, what would have to be true to re-enable it, and which successor took over its purpose. Deleting the row instead destroys exactly what the next operator needs. |
| **probe** | A concrete, strictly non-mutating command that proves a route works right now, declaring what it costs to run and how long its result stays fresh. A registry row says a path exists; only a probe says it works. |
| **promotion** | Making a sealed release the live one: repointing the pointers, rewriting any service definition that pins an absolute path, one supervised restart, then postcheck. Distinct from building. See [chapter 10](10-runtime-releases-and-promotion.md). |
| **prompt injection** | Instruction-shaped text arriving inside data the agent was asked to look at — a web page, an issue body, a commit message, tool output, an image. Observed content may inform and never direct; authorization exists only in the operator's own message. |
| **protected worktree** | A named lane backing a live process, port listener, scheduler entry, or active service baseline, and therefore not disposable. Removing anything runtime-adjacent requires resolving every link, pointer, and argument that could terminate inside it first. |
| **quiescence** | The precondition that no queued or running work and no colliding build processes exist before a stage claims exclusivity. Checked at admission and again immediately before the claim. |
| **quiet-success token** | The marker that lets a routine internal step finish without emitting an operator-facing message, so a successful startup or load does not become the visible answer to a question the operator actually asked. |
| **readback** | Re-reading a mutated object through the same authoritative lane that wrote it, which is the only definition of a completed external write. A returned link, a success status, an append confirmation, a local render, or a screenshot are preparation, not proof. |
| **release seal** | The step that makes a release immutable: after the copy is proven equal to its source, directories and files are set read-only. A sealed release is never rebuilt in place. |
| **result contract** | A scheduled job's own declaration of what its output means, read instead of the transport status. A job that ran, reached the network, was politely refused, and exited zero is a failure and is reported as one. |
| **retention class** | The named policy deciding how long an artifact is kept. Four classes are defined, and every artifact run and audit report carries one. |
| **retirement** | The terminal case of containment: a path, facade, or capability recorded as not coming back, with quarantined recovery evidence and validator rules that keep it from reappearing. It is a recorded operation rather than a deletion, so a stale note cannot resurrect a second source of truth. |
| **rollover checkpoint** | A validated safe-handoff artifact written when a session is about to roll over, carrying objective, honest state, verified evidence, resume safety including what must not be rerun, blockers and approval gates, and the next atomic step. |
| **root-drift policy** | The machine-readable classifier deciding what may live at a workspace root and what must move into a task worktree, sorting paths into removable residue, root-local shims, content to normalize onto the standing branch, and retained state. An unmatched path classifies as a blocker rather than being guessed at. |
| **route registry** | The declared inventory of paths to external systems, one row per route, carrying a stable route id, a system family, a credential reference name, default contexts, and a pointer into the probe registry. A registry row is evidence that a route exists, never permission to use it. |
| **runtime-backed** | Provenance level: the behaviour is implemented inside the agent runtime, so it applies whether or not the agent cooperates and whether or not a helper runs. On a build that lacks it, no amount of policy text substitutes. |
| **sandbox profile** | An operating-system-enforced, deny-by-default profile bound by content hash to the action it wraps and required to appear as the literal argument prefix of the command. Offline flags and package-manager offline modes are requests to a tool; a profile is a constraint on a process. |
| **scheduler registry** | The single declared inventory of every scheduled job across both scheduling layers, including the deliberately disabled ones, with per-job lifecycle state, a timezone-qualified schedule, entrypoints, a result contract, a failure signal, and an evidence reference. |
| **selection provenance** | Where an execution-mode choice came from — classifier-provisional, operator-pinned, active-lane-pinned, runtime-selected, provider-approved-switch, or adaptive-promotion. Only an exactly provisional classification may be promoted without a fresh operator round trip. |
| **self-sufficiency invariant** | The rule that the minimum authoritative statement of a gate lives in one of the normative contracts, never only in a runbook, script, test, or side file. It is what lets a fresh session holding just the injected contracts behave correctly on its first turn. |
| **sensitivity rating** | The per-file judgement of what a file contains — low, medium, or high — recorded independently of its load tier. Sensitivity gates injection, not indexing: a file kept out of the always-loaded set is still one query away. |
| **session** | The durable conversation unit: a session key, a transcript, a trajectory sidecar, and a row in the per-agent session index. Resets on a daily boundary, an idle timer, or an explicit command. |
| **session key** | The identifier deciding which session an inbound message joins, produced by routing policy from channel scope, peer scope, and thread binding. |
| **skill** | A named bundle of procedure and instruction, snapshotted into a run at start. A skill changes how the model approaches a task; a tool changes what it can do. |
| **split-brain** | A state in which two sources disagree about what is authoritative — two roots each claiming to be canonical, or a pointer, a service definition, and a running process naming different releases. |
| **standing branch** | The single long-lived branch on which a surface is worked, declared per surface in the registry alongside its canonical root and validation entrypoint. |
| **standing directive** | A named, durable, narrow exception to the draft-only default, written like a contract clause: it names the lane, the destination, and the payload shape, states its failure rule, and lists what it does not authorize. A directive that cannot be summarized in one row is a policy change wearing a convenience costume. |
| **status artifact** | The on-disk record that is a durable lane's authoritative state: immutable identity fields, phases, health, write boundaries, blockers, and terminal state. Workers append or edit but never rewrite its identity fields. |
| **store-and-forward queue** | The on-disk queue holding outbound messages that could not reach their provider, drained by a single claim-holding drainer over a bounded backoff ladder. Permanently failed entries move to a retained failure area rather than being deleted, because a queue that deletes its failures cannot be audited. |
| **structured authority** | The machine-readable layers that carry current fact instead of prose: a registry of durable topology and routes, a status layer of observed readiness and known issues, a control layer of enforcement parameters, and an audit layer of incidents and events. Human-readable rollups are generated from them and lose to them on conflict. |
| **subagent session** | A child session spawned by a turn, with its own session key, run identifier, transcript, and context, and a reduced set of injected contract files. |
| **surface slug** | The short registry name for one addressable repository or system, resolved through the topology registry rather than typed by hand at spawn time. |
| **terminal state** | A lane state from which no further work follows — complete, blocked, failed, cancelled, superseded, or preserve. The status artifact must reach one before the visible final is sent, and only complete counts as success-terminal. |
| **tool** | One callable capability exposed to the model with a typed signature, from the core catalog, a plugin, or an MCP server. Every call passes the tool-policy pipeline. |
| **topology registry** | The declaration resolving each surface to its canonical root, standing branch, authoritative history target, permitted worktree roots, live binding, and validation entrypoint, and registering roots that have been retired. Canonical source is resolved through it, never inferred from a copy that happens to exist. |
| **trajectory sidecar** | The per-session record of a run's internal steps, written alongside the transcript rather than inside it, so the conversation and the reasoning trail can be read and retained separately. |
| **visible delivery** | The predicate that a message reached the operator's visible target, proven by a provider-confirmed message identifier or an authoritative readback. Internal completion, a clean worker exit, and a plausible success body with no identifier are none of them delivery. |
| **worker** | A subagent session doing defined delegated work under a named entrypoint, with a declared scope, allowed and forbidden paths, expected output, and a named fan-in owner. |
| **worker ledger** | The record of a multi-worker run, one row per worker carrying worker id, scope, source surface, worktree or read scope, allowed and forbidden paths, shared resources, status artifact, expected output, and fan-in owner. It is what makes an envelope travel with a lane instead of living in one coordinator's head. |
| **workspace root** | The control-plane directory holding contract files, registries, control parameters, scripts, tests, and audit records. Distinct from the runtime state directory. |
| **worktree** | A separate working directory attached to one repository, giving a lane an isolated write surface. Validity is proven by matching its Git common directory to the declared canonical root, never by the path looking right. |
| **write lease** | The compare-and-set ownership record for durable mutation, binding run identity, generation and epoch, the status artifact's device and inode, the runtime owner, and operating-system exclusive locks on each write set. |
| **write set** | The concrete set of paths one worker may modify. Write sets are locked exclusively and are required not to overlap between concurrent workers. |

## Where this leads

Vocabulary alone does not explain why any of it exists. Every term above that names a rule
exists because a specific class of failure kept happening;
[why a control plane](02-why-a-control-plane.md) names those classes and derives the rules
from them. For how strongly each mechanism is actually enforced — text, script, runtime, or
proven in operation — read [capability provenance](03-capability-provenance.md) next.
