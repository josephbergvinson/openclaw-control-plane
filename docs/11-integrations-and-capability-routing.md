# Integrations and capability routing

An agent that holds credentials will reach for whichever tool is closest to hand: the browser window that is already open, the account that happens to be logged in, the surface that is easiest to see. It will then report success on the strength of a link it got back. This chapter describes the alternative — every external system the agent may touch is declared ahead of time as machine-readable data, the choice of path is made before the first call rather than discovered by trying things, and a write counts as finished only once the target has been read back through the same path.

Two words carry most of the weight here. A **route** is one named path to one concrete external system — a specific workspace, account, or object store, not a vendor in general. A **probe** is a cheap, strictly read-only command that proves a route works right now and, critically, proves *which identity answered*. Everything below is built from those two records and the rules that join them.

## Capability is a three-part predicate

Before acting, every tool must be classified against three independent predicates. They are answered by different layers and fail in different ways, and collapsing any two of them is how an agent talks itself into acting: a working route gets read as permission, or a granted approval gets read as proof the path still works.

| Predicate | Question | Answered by | Failure looks like |
|---|---|---|---|
| Available | Does a declared route exist for this system, and does its probe pass right now? | Route registry plus a fresh probe | No declared route, or a probe failure |
| Authorized | Is this session entitled to act on *this* account, workspace, or object? | Identity and workspace guards | A named blocker code, fail-closed |
| Approval-satisfied | Does this operation's approval class hold a live operator token? | The approval model in [policy and authority](05-policy-and-authority.md) | Waiting-approval, with the narrowest missing approval named |

The governing clause is short: **a registry row is evidence, not permission**. Proving a route is usable never authorizes using it, and resolving or probing a route never implies permission to write or to send. Route selection, mutation approval, and outbound-send approval are three separate gates.

## Route, readiness, and verification are three different things

Most integration outages an agent causes are not "the tool was broken". They are the agent treating a record of past success as a statement about the present. Three layers therefore answer three different questions, and only the last one is about now:

- The **route registry** says a path exists.
- The **status snapshot** says when that path was last proven.
- Only a **fresh probe** says it works right now.

Each capability row carries a freshness class deciding which of the three is sufficient for a given action class — stable, volatile, reprobe-before-use, reprobe-before-write, reprobe-before-access-expansion. Write-class actions predominantly carry a reprobe-before-write class, so a day-old green row does not license a mutation.

The status snapshot is a flat list of capability rows, and its shape is worth stating because it is the join target for everything else:

| Field | Holds |
|---|---|
| `capability_id` | Stable name of the capability, not of the vendor |
| `state` | One value from the degradation taxonomy below — ready, degraded, disabled, accepted-risk, documented-fallback, source-ready-live-inactive |
| `support_class` | How well the capability is supported, at finer grain than state: fully supported, built-in, legacy, experimental, local-development-only, costed read-only, read-only pilot, and similar |
| `freshness_class` | Which of route, status, or fresh probe suffices for this row |
| `last_verified_utc` | When the row was last proven |
| `evidence` | Pointer to the artifact or probe run that proved it |
| `constraint_or_fallback` | The narrow instruction that applies when the row is not simply green |

The reference shape is [`examples/capability-record.example.json`](../examples/capability-record.example.json). Routes say what is possible; status rows say what was last proven; the resolver joins the two and never confuses them.

## The mutation ladder

A **lane**, in this chapter, means one class of mechanism for reaching an external system — not the durable lane of [execution and durable lanes](06-execution-and-durable-lanes.md), which is a unit of isolated work. The order of preference between lanes is fixed, and it is chosen *before* the first mutating call rather than discovered by trying things.

| Tier | Lane | Why it ranks here |
|---|---|---|
| 1 | Verified API or integration lane | Stable contracts, verifiable identity, machine-checkable results |
| 2 | Managed browser automation | Brittle, identity-ambiguous, weakly verifiable |
| 3 | Host UI automation | Least verifiable of all; state must be asserted, not assumed |

Convenience runs this ladder backwards — the already-open window is the cheapest thing to click. That is precisely why the order is enforced mechanically rather than left to judgement.

Four rules keep the ladder honest:

1. **Named-predicate fallback gate.** A tier may not be dropped until the specific unsupported-or-failed predicate of the preferred lane is recorded. "It was already open", "this would be faster", and "the other window was visible" are not predicates.
2. **User-requested-UI-state exception.** The only non-failure route to the browser tier is an explicit human request to use existing logged-in session state.
3. **Equivalent-mechanism normalization.** Driving the same underlying surface, profile, or session through a different tool — a raw protocol script, a different automation library, synthetic input, coordinate clicking — is the *same* lane. It does not reset a failure budget and does not launder a refused result into a fresh attempt.
4. **Lane failure is not capability failure.** A failed preferred lane does not make a task blocked while another authorized lane can still complete it. Conversely, a claim that a whole capability is unavailable must be proven per lane. Failure reports use a fixed template naming each checked lane separately with pass, failed, or blocked status, so "blocked" can never be claimed after trying one path.

Within the browser tier there is a further exhaustion order before a browser task may be called blocked: an explicitly handed-off session, then the intended canonical lane, then an existing automation lane, then fresh automated navigation, then in-site recovery.

## The route record

The implemented route record is deliberately small: **exactly six fields, and this six-field shape is the normative one for this architecture.** Six fields is a statement about what a route may carry authority for, not a validator that rejects everything else — a consumer that meets a field it does not know skips it, which is the compatibility convention described in [architecture evolution](18-architecture-evolution.md) and what lets the record gain a schema marker later without a coordinated upgrade. A route names a path and nothing else. Every readiness question is delegated to a separate probe registry with its own schema, so a route entry can never drift into being a stale cache of how well the path was working last week. The reference instance is [`examples/integration-route.example.json`](../examples/integration-route.example.json).

| Field | Holds |
|---|---|
| `route_id` | Stable slug for this exact path |
| `system` | Coarse system family |
| `credential_ref_name` | A *name or path*, never a value |
| `default_contexts` | Neutral context tags that make this route the default, such as primary, secondary, sandbox, read-only |
| `access_probe_id` | Pointer into the probe registry |
| `notes` | Free-text constraints — read-only, reprobe before writes, or a statement that a ready default route is not evidence for this named route |

The probe record it points at is a separate object:

| Field | Holds |
|---|---|
| `probe_id` | Stable identifier |
| `command` | A concrete, strictly non-mutating command |
| `expected_signal` | Prose describing a pass, normally including the requirement that no secret value is printed |
| `safe_lane` | What the probe itself costs and touches — local read, local read plus network read, or metered API read |
| `default_ttl_ms` | How long the result stays fresh, in milliseconds; local credential checks get a long time-to-live on the order of a day, network- or state-volatile checks something closer to an hour |
| `scope` | The route or subsystem the probe proves |
| `notes` | Constraints on the probe itself |

Registries accumulate history, so the loader is deliberately tolerant in one narrow way: where an older entry names its pass condition under a superseded field name, the loader normalizes that spelling onto `expected_signal` before the record is validated, so the schema keeps one canonical name while the reader tolerates both. Tolerating a superseded field name is cheap; silently treating a probe with no declared pass condition as passing is not, and that case remains an error.

Two properties of this split matter more than the field list. Proving readiness is itself a budgeted action: the safe lane declares its price, so the router can reason about what it costs to ask. And control-plane self-consistency is registered as just another probe — the validator that checks the registry has a probe id like any integration, which means "is the control plane internally consistent" is answered the same way as "is the calendar reachable".

### Optional extensions

A richer route record is a reasonable design, and earlier sketches of this architecture described one: required scopes, an inline fallback order, declared readback fields, a freshness class, an explicit mutation gate, and a constraint list carried on the route itself. **None of that is the implemented shape.** The compact six-field record above is normative; everything in this subsection is optional. In the reference implementation those extra concerns live elsewhere — fallback order is returned by the resolver, freshness lives on the status row, readback requirements live in the continuity contract, and constraints live in `notes`.

Treat the richer set as optional extensions with **policy-only** provenance, meaning they hold only as far as instructions and operator discipline hold. An adopter who adds them inherits an obligation: any field carrying authority must be validated by the same contract tests that cover the compact record, or it becomes decoration that readers trust and nothing enforces. The specific failure is a route that declares `mutation_gate: approval-required` which no code path ever reads, giving an auditor a false positive on every row.

## Resolution is read-only

A single read-only resolver takes a request shaped as system, intent, and context and returns a decision without executing anything. Its procedure is short enough to state completely:

1. Normalize the requested system through an alias map, because many colloquial names collapse onto one system slug, and normalize the verb through an intent map — create, update, send and mutate become `write`; search becomes `read_search`.
2. Load the route registry, the probe registry, and the status snapshot.
3. Score every candidate route for this system.
4. Join each surviving route to its best-matching status row.
5. Emit a decision payload and exit with a code that says which kind of decision it is.

Scoring is explicit rather than intuitive, and the weights only matter relative to each other. A request context that matches one of the route's declared contexts is the largest single bonus. A context miss on a route that declares contexts is a small penalty rather than a disqualification, because a route with no better competitor should still be reachable. A write intent against a route marked read-only takes a penalty larger than any bonus can offset, so it is effectively a refusal. A read intent mildly favours read-only routes. The point of writing the weights down is that route selection becomes reviewable: an operator can ask why a route won and get an arithmetic answer.

The payload carries the preferred lane, the full list of lanes it checked with each one's probe command and readiness, the fixed fallback order, the constraint list, a failure-report template, and a browser-fallback gate. The fallback order is a constant, not a suggestion:

1. the declared API or integration route;
2. the declared probe or readiness check for that route;
3. the browser route, only if the API lane is unavailable or the operator explicitly asked for existing UI state;
4. host UI automation, last resort.

Exit codes make the outcome branchable without parsing prose: a normal exit means a decision was produced, one distinct non-zero code means no declared route matched the requested system, and a second distinct code means a route guard blocked the proposed lane. The two failures need different handling — the first means the registry is incomplete and the honest report is "no declared route", while the second means the registry is fine and the request was refused.

The failure-report template is part of the payload for one reason: a report written from memory tends to describe the lane that failed and omit the ones never tried. The template forces the shape, one line per lane:

```text
Checked <system> route <route_id>: pass | failed | blocked — <named predicate>
Checked browser route <profile>: pass | failed | blocked — <named predicate>
Checked host UI lane: pass | failed | blocked — <named predicate>
```

The resolver runs no probes and mutates nothing. It produces a decision, not an action. That separation is what lets the decision be logged, replayed, and unit-tested: the reference implementation locks its behaviour with named test cases covering, among others, a request naming a non-default route refusing to resolve to the ready default route, a browser fallback refused because the API lane had not been exhausted, and a browser fallback allowed only against the exact requested account.

## Credential-reference indirection

Registry entries carry the *name* of a credential source and never its value, so the registry can be read, diffed, and reviewed by anyone who is not trusted with the credentials themselves. Four sanctioned reference styles cover the cases that arise:

- a provider-native credential store, referenced as the active credential store for a named identity;
- a bare environment-variable name;
- a path to a permission-restricted environment file, one integration family per file;
- a named reference into an OS credential store.

One further style exists and is labelled as what it is: a compatibility alias, present only where a legacy tool insists on an older variable name, recorded as a category that should shrink rather than as a fifth sanctioned option. Materialization runs in a fixed order — read non-secret configuration, materialize the referenced values into the process environment, apply any required aliases, then run narrow post-load probes.

Probes are written to prove readiness from non-secret evidence — key ownership, file mode, public metadata, a "who am I" call — and their declared pass condition requires that no token value is printed. Two registry-contract tests keep that structural rather than aspirational: every route must point at a probe that exists and whose declared safe lane is a read-only class, and the mutating subcommands of the tool being probed must be absent from that probe's command string. A probe that cannot find its credential exits with a structured blocked result naming which credential source was missing, rather than raising an exception or printing what it did find; the caller then gets a routing fact ("this route is not ready, because its credential source is absent") instead of a stack trace.

Two honest boundaries:

- **Secret-safe readiness** is a support class, not a universal property. Some capabilities can be proven ready entirely from public evidence; others cannot, and are marked so.
- **Migration to reference-by-name is a process, not a state.** A credential estate moves onto named references one integration at a time, so for some stretch a portion of it is still held inline in runtime configuration, protected by filesystem permissions rather than by indirection. What makes that survivable is refusing to treat it as a steady state: any credential still held inline is a known gap, and it gets a row in the known-issues ledger with an owner and a containment note, so the remainder is tracked rather than forgotten. Verify each move with a narrow per-integration probe against the affected route. Reading runtime configuration back to see what is in it is not a readiness check — it re-exposes exactly what the indirection exists to stop exposing, and it answers a much broader question than the one being asked. The ledger and its fields are covered in [guards, health and restoration](14-guards-health-and-restoration.md).

## Account and workspace routing is a hard predicate

When a request names a workspace, organization, account, shared drive, folder, owner, or domain, that name is a routing predicate — not a preference.

> A ready default route must never satisfy a request that named a specific one. Writing to the wrong workspace is a **blocked route**, not a degraded success.

This is arguably the highest-value rule in the design, because its failure mode is silent and looks like success. Three mechanisms make it hold:

- **Session state is not identity.** Whichever account is currently open in a browser, whichever workspace was used last, and any per-session account index embedded in a URL are all declared non-evidence for route selection.
- **Anti-cross-wiring in readiness matching.** When routes are joined to status rows, tokens anchoring a *different* identity score negatively, so one account's ready row cannot be harvested as evidence that another account's route is ready.
- **Fail-closed identity guard.** For identity-bearing systems the resolver derives the exact account the route requires and compares it against what a fresh probe actually returned, emitting distinct machine-readable blocker codes — one for an unverified identity route, another for a browser fallback proposed before the API lane was exhausted — plus a non-zero exit status rather than a soft warning.

For such systems the browser fallback is triple-gated: the API lane must have failed or be unavailable, **and** the browser session must be independently verified as the exact requested account, **and** the ordinary approval class still applies.

## External work products and authoritative readback

For substantial writes to external document, knowledge-base, or storage systems, routing is only the first half. The second half is proving the write landed.

Before mutating, the agent records an **external document lifecycle** decision from a fixed enum — new work product, revision to an existing work product, appendix or append-only, changelog or audit trail, status checkpoint. Edits to an existing reader-facing document default to *revision*, not append. A revision additionally requires a before-and-after **structure plan** naming target sections, superseded blocks, stale placeholders and duplicate headings, each classified replace, update, archive, or append. A draft quality gate then runs over the local draft, checking the structural faults that survive a rushed edit — duplicate headings, placeholders left from an outline, and tables so dense they will be unreadable on the target surface. A composed no-write preflight chains plan validation and draft quality into one call that must return write-allowed before the mutation wrapper proceeds. Composing them matters: two separate gates invite a caller to run the one that passes and forget the one that does not.

After mutating, completion has one definition: **the declared target object has been read back through the same authoritative lane**. For document surfaces the readback is recursive over the reader-facing sections. A returned URL, a success status code, an append confirmation, a marker string, a local render, an export, and a screenshot are all classified as *preparation*, not proof.

Readbacks are **bound receipts**: evidence that carries the identity of the context it belongs to, so it cannot be borrowed. A readback record must carry the run identifier, the generation (the monotonic counter on the run's ownership record, described in [execution and durable lanes](06-execution-and-durable-lanes.md)), the route, and the account, workspace and object identifiers, each matching the approved plan. A readback performed by a different run, on a different account or workspace, or against a different object therefore cannot be credited to this one. Without that binding, the easiest way to pass a readback gate is to read back the wrong object successfully.

Progress advances through a fixed stage taxonomy, and the stages are ordered:

`local_preparation` → `source_ready` → `target_mutating` → `target_readback` → `complete`

Local rendering, exports, and quality checks all belong to `local_preparation`. Keeping them there is the point of the taxonomy: a checkpoint can then never imply external progress that has not happened, because the stage name itself says the external system has not been touched yet.

## Continuity contracts and the same-class stop rule

Substantial work products are governed by a versioned continuity contract validated before and after the mutation. The plan must bind:

- exactly **one authoritative object** selector or id;
- an explicit **object-creation budget**, with a declared disposition for every transient object — delete or archive after verified handoff, or retain with a stated reason;
- the proven route, including account and workspace;
- an ordered fallback list that may not switch the authoritative object;
- required native-structure properties and provenance boundaries for assets;
- a shared failure ledger;
- a flag asserting that authoritative target readback is required.

Creating a throwaway "final" copy merely to test whether a route works is prohibited. Once an object carries human edits, comments, sharing, or a designated working-copy role it must be mutated in place, and completion requires readback proving those survived.

Failures are recorded per normalized failure class with sequence numbers, and the ledger is **carried across successor lanes, workers, and objects**. The stop rule follows: after **two failures of the same class**, blind retrying stops. The next step is to trace the boundary chain — entrypoint, wrapper, runner, injected dependency, artifact writer, live boundary — and continuing requires an explicitly recorded architecture-review decision. Attempts logged after the hard stop are validation errors, not further tries.

## The default write procedure

Everything above is enforced as one ordered checklist, which is the default for any system that has no more specific instruction bundle of its own:

1. Identify the target — the exact system, the exact account or workspace, and the exact object.
2. Confirm which approval class the operation needs and whether it is currently held.
3. Check readiness and the preferred lane through the resolver.
4. Select the lane *before* opening any tool.
5. Resolve the route, including account and workspace.
6. Probe access on that route, including the specific target object where the probe accepts one.
7. If leaving the preferred lane, declare the fallback and the named predicate that justifies it.
8. Mutate.
9. Verify by authoritative readback through the same lane.
10. Report per lane, using the failure-report template if anything did not pass.

Systems that write reader-facing documents carry a longer bundle that inserts the lifecycle decision, the structure plan, the draft quality gate, and the composed preflight between steps 7 and 8. The ordering is the part that generalizes: lane selection precedes tool use, and verification is a separate step from mutation rather than a property of it.

## Browser profile routing

Inside the browser tier, *which profile* is a routing decision with the same standing as which account.

- The **managed automation profile** is the default and the only first-class browser lane.
- Attaching to a human's already-open session is a workflow-specific exception that must be explicitly requested, not a convenience default.
- Superseded browser lanes are recorded as disabled or legacy with containment text, so a retired lane cannot be silently rediscovered as available.

Browser failures must be classified narrowly into distinct classes — user-session attach failure, profile-specific failure, automation-lane failure, site or session-state failure, capability failure. One profile failing is not a browser outage, and reporting it as one suppresses the lanes that would still have worked.

Node-delegated browsing — handing a browser task to a separate node rather than driving a browser on the gateway host — is a supported topology, and whether it is used is a deployment choice rather than a property of the design. Both settlements need a stated rule. Where browser work stays on the gateway host, the profile alone identifies the lane. Where it is delegated to a node, the node becomes part of the route: two nodes running the same profile name are two different lanes, and a probe must prove which node answered, exactly as it must prove which account answered. See [system architecture](04-system-architecture.md).

## Mediating a third-party tool server

Third-party tool servers are treated as a supply-chain surface, not as a trusted extension. The pattern is a small checked-in local adapter in front of the remote server, driven by a policy manifest that:

- pins the remote endpoint, transport, protocol version, server identity, and a digest over a canonical projection of the remote tool schema, so a schema change forces explicit re-approval;
- declares the expected remote tool inventory *and* a blocked inventory covering every write-style tool;
- maps a fixed, minimal set of local read-only tool names onto the permitted remote reads, with no generic remote-tool dispatch;
- forbids egress of credentials, environment, local paths, and surrounding conversation context;
- caps query length, output size, timeouts and retries;
- degrades to a cached public snapshot with a maximum acceptable age rather than failing open.

A mediated route of this kind is the archetypal case for the **source-ready, live-inactive** state, and that label is worth reading precisely: it asserts that the adapter and its manifest are built, registered, probed and tested, and it asserts equally that the route is deliberately not wired to a live client. Wiring it to one is a separate approval, and the label changes only when that approval is granted and exercised. The state is first-class precisely because it prevents "it is built" from being read as "it is running" — a distinction most capability registers lack, which is why they drift into optimism.

## Capability views are generated artifacts

The JSON layer is canonical. Human-readable dashboards are generated views over it, and they say so in their own first lines.

- Every generated file opens with a do-not-edit banner naming its canonical sources.
- The generator is **fail-closed**: a required status source that is missing or contains invalid JSON aborts the entire run with a structured diagnostic telling the operator to restore the JSON layer, never to hand-patch the view.
- Hand-maintained readable twins of the registries exist for diffing, and their equality with the JSON authority is asserted by unit tests rather than produced by a generator, so the readable form stays hand-editable without becoming a second source of truth.
- Every non-ready capability carries a state from a degradation taxonomy — ready, degraded, disabled, accepted-risk, documented-fallback, source-ready-live-inactive — plus a containment instruction, and that containment text is rendered into the operator view.

Health classes, the known-issues ledger, and restoration paths are covered in [guards, health and restoration](14-guards-health-and-restoration.md).

## What is proven versus asserted

| Capability | Reference implementation | Stock-runtime adopter |
|---|---|---|
| Declared route and probe registries | helper-backed, contract-tested | policy-only until built |
| Lane-order resolution and fallback gate | helper-backed, behaviour locked by unit tests | policy-only |
| Identity and workspace route guard | helper-backed, fail-closed with blocker codes | policy-only |
| Managed browser profile lane | runtime-backed | runtime-backed |
| Host UI automation lane | policy-only reliability contract over host tooling | policy-only |
| Continuity contract and bound readback receipts | helper-backed | policy-only |
| Generated capability views | helper-backed, fail-closed generator | policy-only |
| Mediated third-party tool server | helper-backed, source-ready live-inactive, not live-proven | policy-only |

The taxonomy itself is defined in [capability provenance](03-capability-provenance.md).

## Adopting this

1. Write the compact route record for every external system already in use, including the second account that is easy to forget. Six fields is enough to start.
2. Write one non-mutating probe per route that proves *which identity answered*, and nothing else.
3. Make the lane order a returned value, not a remembered rule.
4. Make "which account" a hard predicate before making anything else strict.
5. Refuse to claim a write until it has been read back through the same lane.

Where this leads: routes are exercised most often not by an operator asking for something but by a job on a timer, so the next chapter, [scheduling and background work](12-scheduling-and-background-work.md), covers how scheduled jobs run these lanes unattended and how a silently broken one is caught.

Related chapters: [policy and authority](05-policy-and-authority.md) for approval classes, [execution and durable lanes](06-execution-and-durable-lanes.md) for the run identity a bound receipt refers to, [delivery and the control surface](13-delivery-and-control-surface.md) for send semantics, and [security and trust model](16-security-and-trust-model.md) for the trust boundaries these lanes cross.
