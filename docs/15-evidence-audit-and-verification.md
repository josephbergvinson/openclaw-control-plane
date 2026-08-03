# Evidence, audit, and verification

Every other chapter here makes claims: this ran, that was approved, this landed on the standing branch —
the one long-lived branch each repository is worked on. This chapter is about checking those claims
afterwards by reading files, without asking the agent to recall anything, because an agent's account of
what it did is a summary of a conversation rather than a record of an event. Two ideas carry the chapter.
Evidence is written at or before the moment of the event it records, never reconstructed later from that
summary. And the contracts that govern behaviour are themselves checked by executable tests, so a rule
quietly vanishing from a policy file fails a build today instead of surfacing months later as an
inexplicable incident.

## The workspace event log is the spine

The control-plane workspace keeps one append-only log, `<workspace-root>/audit/events.jsonl`, with a
deliberately minimal line shape.

| Field | Meaning |
|---|---|
| `ts` | When the event was recorded |
| `kind` | What class of thing happened — a change, a closeout, a containment, a restoration |
| `scope` | Which area it touched, named the same way the changelog names scopes |
| `text` | One short human-readable sentence |

Two lines look like this:

```text
{"ts":"<timestamp-utc>","kind":"policy-change","scope":"contracts/mechanics","text":"Worktree validity now requires a Git common-directory match"}
{"ts":"<timestamp-utc>","kind":"containment","scope":"capability/host-ui-automation","text":"Host UI lane disabled; see known issue issue-0000"}
```

The shape is thin on purpose. A log with room for structured payloads invites writers to put the evidence
*in* the log; four fields force the evidence into an artifact directory and leave the line as an index
entry pointing at it. Three rules keep it usable: lines are appended and never edited or removed, so a
correction is a new superseding line; the line is a pointer rather than the record; and other contracts
anchor to it, naming an event-log line instead of restating a claim in prose.

The same directory holds a few dozen long-lived incident and fix records, most named after the defect they
close, several kept as a pair — a human-readable record and a machine-readable counterpart with the same
base name — so narrative and structured record travel together and can be checked against each other.
Cleanup closeouts land here too. A *closeout* is the record a piece of work leaves behind when it claims
to be finished, specified field by field later in this chapter, and the root-drift policy in
[source layout and artifacts](09-source-layout-and-artifacts.md) requires one before normalizing a source
root may be reported complete.

## Auditing the policy stack itself

An edit to a governing contract — one of the Markdown files injected into every system prompt, ranked and
described in [policy and authority](05-policy-and-authority.md) — is the highest-leverage change anyone
can make on this system: it changes what the agent does next time, in every session, including sessions
nobody is watching. It is also the easiest change to forget having made. Writes to those contracts and to
the [memory](08-memory-and-context.md) files are therefore audited twice, by two mechanisms that fail
independently: automatically, as an outbound line to a private audit route, and deliberately, as an
appended changelog row. A watcher can be uninstalled without anyone noticing, and a human can forget to
append a row; the two failures are unrelated, which is exactly why neither is allowed to cover for the
other.

### The audit hook is a package, not a script

The automatic half is a workspace-installed runtime hook. Treating it as a package rather than a one-file
watcher is what makes it survivable.

| Module | Responsibility |
|---|---|
| Manifest | Declares the runtime events the hook binds to — gateway startup — and its install kind, so the runtime knows when and where to load it |
| Handler | Owns the lifecycle below, and decides whether an observed change qualifies for an audit at all |
| Formatter | Turns a set of changed files into the fixed payload: the changed files, a short summary, and the outcome |
| Content-state tracker | Holds a content digest per qualifying file — the thing that makes metadata-only events invisible |
| Send path | Performs the delivery, one at a time, under deadlines, with the retry rule below |
| Compatibility shim | Keeps a previous entrypoint name resolving to the current handler, so an already-installed hook does not break when internals are renamed |

Startup runs in four ordered steps: baseline the qualifying files, install the filesystem watcher, take a
second snapshot and reconcile it against the baseline, then activate. The reconcile step exists because
the window between reading a baseline and having a watcher attached is real — without it, an edit made
during gateway startup is invisible forever.

Six mechanisms then keep the audit stream honest, each defending against a specific failure.

- **Digest suppression**, because watchers fire on metadata changes: without a content digest, a
  permission or timestamp touch produces a line asserting a change that did not happen.
- **Burst coalescing**, because editors write a file in several operations: a stable content burst of a
  little over a second yields at most one audit, so one save is one line.
- **Periodic content rescan**, because platform watchers drop events under load: comparing digests on a
  slow interval makes a dropped event cost latency rather than the record.
- **Serialized delivery**, so a burst of edits is not a burst of sends racing for the same route.
- **Bounded deliveries**, with parent and child hard deadlines and verified process-group cleanup, so a
  hung send cannot leak a process group that outlives the run.
- **Singleton release on terminal error.** The hook holds a single-instance marker so two watchers cannot
  run at once, and a watcher that dies still holding that marker can never be reinstalled; releasing it on
  a terminal error lets the next gateway start install the hook again.

The destination is one fixed private audit route, named once in the normative behaviour contract, and the
mechanics contract forbids reconstructing that route from a bare identifier — a destination reassembled
from a fragment is a destination nobody can audit, and it is how a second, slightly different route ends
up in a second place and then drifts. A named standing directive pre-approves this specific send so it
needs no per-event approval token, and the same directive states what it does not authorize: any other
payload on that route, and any other destination. Its failure rule is to retry once and then report in
thread rather than retrying blind, so a broken route surfaces as a visible problem instead of a silent
backlog. The pattern is what transfers: one fixed destination, named in exactly one place, resolved by
reference everywhere else.

## The policy changelog

The deliberate half of the audit is a dated, status-tagged, append-only changelog with an enforced
one-line row format. Enforcement matters more than it sounds: a free-form change history degenerates
within weeks into prose nobody can query, and it cannot be a lint target.

| Column | Content |
|---|---|
| Date | The date the change took effect |
| Status | `live`, `draft`, or `superseded`, describing the change when the row was appended and never edited afterwards |
| Scope | A slash-joined list of the contract, script, and test areas touched |
| Summary | One clause describing the change |
| Evidence | A pointer to the artifact, event-log line, or test that substantiates it |

One row, in the fixed order, looks like this:

```text
<date> | live | contracts/mechanics/tests | Worktree validity now requires a Git common-directory match | <artifact-root>/run-0000/closeout.json
```

Two rules make the file trustworthy. It is declared **historical audit only** — never a normative source,
and placed in the never-default-load tier, meaning it is not injected into any session automatically and
is opened deliberately when someone needs to know why a rule exists. See
[policy and authority](05-policy-and-authority.md) for the tier model. And **the file is append-only,
including the status column**: a row states what was true on its own date, so a change of state is
recorded by appending a later row that names the earlier one, never by editing the earlier row in place.
An entry recorded as `draft` that has since reached the live stack is answered by a later `live` row
naming it, or by a row stating the residual gap; a row appended only to withdraw an earlier rule carries
the status `superseded` and names the row it withdraws. A draft left as the newest word on its subject is
worse than a missing entry, because it makes the trail imply pending work that already shipped and no
reader can tell "planned" from "forgotten". The bar to hold is simple: the state of any rule is the newest
row that names it, and no landed change should have a `draft` row as its newest.

A copyable version of the format, with the row shape and the rules stated as a template, is
[`templates/POLICY_CHANGELOG.example.md`](../templates/POLICY_CHANGELOG.example.md).

## Drift lint: making authority erosion a build failure

Authority does not usually disappear in one deliberate act. It erodes by paraphrase: someone tightens a
precedence section, the sentence saying lower ranks never override higher ranks goes with it, and nothing
observable changes until a delegated session resolves a conflict wrongly weeks later. A drift lint turns
that edit into an immediate failure by asserting the *literal* text.

| Assertion class | What it checks |
|---|---|
| Existence | Every core contract, manifest, registry/status/control file, generated view, key runbook, and required test module is present |
| Executability | Named helper scripts exist and are executable, not merely readable |
| Literal presence | Exact headings and exact phrases inside the two normative contracts |
| Literal absence | Retired text is actually gone, so a superseded rule cannot linger and be cited |
| Structural anchors | The do-not-edit banner on each generated view, and the authority-shortcut block on the index page |
| Manifest state | The exact cutover-state and generated-views-state strings in the policy kernel manifest, so a silent authority flip cannot pass |
| Delegation | It shells out to the bootstrap-limits checker and adopts its exit code as its own |

Unit tests carry the complementary half: the precedence header and its per-rank role descriptions, the
generated view's own role-boundary sentence, the approval-token vocabulary, the owner-authority gate
sentence, and the mutual cross-reference between the two normative contracts.

**An honest note on what to do when a budget gate fails.** The bootstrap budget is the per-file and total
character limit the runtime applies to contract files before injecting them into a session's system
prompt, and the limits check reads those limits from the live runtime configuration rather than from any
static copy of them. The instructive case is what happens when the largest normative contract outgrows its
per-file budget: the check fails, and the drift lint fails with it. The correct response is not to raise
the limit. The check stays failing and visible, and the size pressure is treated as the real defect, to be
resolved by splitting or tightening the contract rather than by widening the gate around it. Runtime
behaviour under truncation should stay legible meanwhile: keep the head and tail of an over-budget file
and join them with a marker naming the file and the kept-versus-original size, so the model can see it is
reading a partial contract instead of silently reasoning from a truncated one. A gate that is adjusted
whenever it fails has stopped being a gate.

## What binds an artifact to a claim

[Source layout and artifacts](09-source-layout-and-artifacts.md) gives the full metadata record. Three of
its fields do the actual binding, and an artifact missing any of them cannot support a claim.

| Field | Answers | Consequence of omitting it |
|---|---|---|
| Producer | Which entrypoint, job, or lane wrote it | Nobody can re-run it, so the artifact can be neither refreshed nor falsified |
| Target | Which surface, canonical root, branch, commit, or external object it describes | Evidence is read against the wrong system and looks like agreement |
| Acceptance predicate | The exact condition the artifact is claimed to satisfy | The file is a log, not evidence; it records activity rather than proving an outcome |

Two rules attach. The predicate is stated *before* execution, not chosen afterwards to match what
happened; and when a later step expects an artifact that was never produced, the report names the missing
producer step rather than the downstream file-not-found. A path existing, a marker string existing, or a
command having been emitted are none of them evidence of completion.

For the highest-consequence operations — the ones that modify the runtime itself — the evidence is
stronger than metadata. The record is a **hash-chained journal**: a directory of sequentially numbered
event files in which each event stores a digest of the event before it as well as a digest of itself, so
deleting, reordering, or editing any event breaks the chain at that point and at every point after it.
Before an append is permitted, reading the journal re-verifies six things in order:

1. **Naming** — every file follows the required sequence-numbered name pattern, so a stray file cannot sit
   in the journal directory unnoticed.
2. **Sequence contiguity** — the numbers run without gaps, so a removed event is detected as a hole rather
   than as a shorter history.
3. **Chain linkage** — each event's recorded previous-digest equals the actual digest of the preceding
   event.
4. **Per-event digest** — each event's own digest matches its content, so an edited event fails on its own
   terms even if the links were rewritten.
5. **Operation id** — every event names the same operation, so two operations cannot share a journal.
6. **Authority hash** — the digest of the document that authorized the operation is unchanged, so the
   rules the run started under cannot be swapped underneath it.

Phase receipts are chained the same way and are additionally bound to the operation lock with a readback
assertion, which is what stops a receipt from being lifted out of one operation and presented as part of
another. A terminal receipt binds the journal's length and its head digest, fixing the entire history in a
single value that later reads can be checked against. Every input is pinned by content hash before
execution. Journal events, receipts, and locks are all created exclusively — the create fails if the name
already exists — and then made read-only, so a leftover file from an abandoned attempt blocks the next
attempt instead of being quietly overwritten. See
[runtime, releases, and promotion](10-runtime-releases-and-promotion.md).

## Executable contract tests

The checks that matter are the ones that run. Verification comes in two layers: a workspace test suite that
exercises the contract surfaces the control plane depends on, and a small set of repo-local validators that
an adopter can copy directly.

For the first layer, an adopter should cover at least these classes, because each is a place where a
regression leaves a system that still runs and quietly stops enforcing something: precedence and conflict
resolution across the policy stack; the structural invariants each contract asserts about its own shape;
the per-file and total bootstrap budgets, so a growing contract fails a test rather than silently
displacing context; the audit hook that records a decision, so a decision path cannot lose its record; and
the standing exceptions that legitimately bypass a rule, so an exception cannot widen unnoticed. The second
layer is the copyable one.

| Validator | Proves | Fails when |
|---|---|---|
| Workspace validate | The workspace still has the structure the contracts assume | A required contract, registry, or control file is missing or unreadable |
| Runtime validate | The pointer chain, service definitions, and build identity agree about which code is live | One service resolves to a different release than another |
| Cross-surface contract validate | Declarations repeated across registries and views still agree | A surface's declared branch or validation entrypoint diverges from the live binding |
| Policy drift check | The literal-language and structural assertions above | Precedence language, a banner, or a manifest state string changed |
| Bootstrap limits check | Injected contract sizes measured against the live runtime configuration | A contract outgrows the per-file or total budget |
| Capability validation runner | Each registered integration route still has a passing readiness probe | A route's probe fails, or the route has no probe at all |
| Closeout gates | A slice — one bounded unit of work — may be reported complete | Source state, standing-branch normalization, or a required record is missing |

Three properties make the set copyable rather than host-specific. Each check runs from one argument-free
command at the workspace root, so it can be a habit rather than a procedure. Each is read-only and
fail-closed, so running them is never itself a risk and ambiguity is a failure. And each names the exact
contract clause it enforces, so a failure says which rule is at stake, not only which assertion tripped.

## The closeout checklist

A closeout is what a run leaves behind when it claims to be finished, and it is the most useful artifact
here: the only place where what was done, what was checked, and what was *not* checked appear together.

| Field | What it records | Why it is here |
|---|---|---|
| Source root | The canonical root the work was performed against | Distinguishes the real surface from a mirror, handoff copy, or similarly named folder |
| Worktree | The separate working directory the edits were actually made in | Proves the edit did not land on a standing branch directly |
| Branch | The task branch and the standing branch it targets | Makes the landing path explicit |
| Before and after commit | The two commit identifiers bracketing the change | Gives a reviewer an exact diff without reconstructing it |
| Changed files | The write set — the concrete list of paths the work modified — as it ended up | Surfaces files touched outside the stated scope |
| Checks run | Which validators and test suites were executed, and their results | This is the acceptance evidence |
| Checks skipped | Which were *not* run, and why | The honesty field; a closeout with no skipped list is usually incomplete rather than perfect |
| Docs impact | Whether documentation needed changing, and whether it did | Stops documentation drift from becoming invisible debt |
| Live gates | Which live-system verifications passed, and at what boundary they were observed | Separates "tests pass" from "the running system does the thing" |
| Remote state | What was pushed, to which authoritative history target, and what was not | A change that only exists locally has not landed anywhere anyone else can see |
| Residual drift | Unrelated uncommitted changes left in the tree, each classified | A slice may land while drift remains, but the drift must be named rather than absorbed |
| User-facing run path | The path or command the operator actually uses | The control-plane path is not the user's path, and confusing them makes a working change look broken |

The gate producing this record has three subcommands worth copying as a shape: one emits the
machine-readable closeout, one validates a pre-change acceptance manifest naming the call chain the fix
must exercise, and one validates the final report text against the emitted closeout. The third is the
quiet essential — it stops the summary from claiming more than the gate concluded. The gate also refuses
`complete` while source state or standing-branch normalization fails, distinguishes non-blocking residue
from real blockers, and requires a structured self-review, a docs-impact record, and proof of the actual
entrypoint exercised.

A filled-in closeout, shown alongside the run that produced it, is stage 9 of
[the worked example](07-worked-example.md).

## What is proven versus asserted

| Capability | Reference implementation | Stock-runtime adopter |
|---|---|---|
| Append-only workspace event log | policy-only convention over a plain file | policy-only |
| Policy and memory audit hook | runtime-backed hook host, helper-backed handler package | policy-only, or an external file watcher with its own send path |
| Strict-format policy changelog with status hygiene | policy-only, enforced by lint | policy-only |
| Literal-string drift lint and precedence unit tests | helper-backed | helper-backed — this is the cheapest thing here to reproduce |
| Artifact metadata binding | policy-only convention | policy-only |
| Hash-chained journals and terminal receipts | helper-backed | policy-only unless reimplemented |
| Repo-local validators and closeout gates | helper-backed | helper-backed |

The taxonomy itself is defined in [capability provenance](03-capability-provenance.md). The columns carry
implementation levels only. Live-proven sits on a status row for one installation rather than in a table like
this one, and an operator claiming it here would need retained evidence of the mechanism carrying ordinary work:
handler output produced by everyday policy and memory edits rather than by a rehearsal, and journal segments
whose chain was re-verified against terminal receipts after real operations — each dated and inside a declared
freshness window.

## Adopting this

1. Start with the changelog: one file, five columns, one rule about drafts. It is the artifact you will
   miss first.
2. Add a drift lint asserting three or four literal sentences you cannot afford to lose, and grow it only
   when something almost disappeared.
3. Give every artifact a producer, a target, and an acceptance predicate before investing in richer
   metadata.
4. Write the closeout template early, including the skipped-checks field, and refuse to report completion
   without one. Add hash-chained journals only for operations that modify the runtime itself.

Where this leads: every record described here is only as good as the assumptions under which it was
collected, and those assumptions are the subject of the next chapter,
[security and trust model](16-security-and-trust-model.md) — who is trusted, what is treated as data
rather than instruction, and what this architecture explicitly does not defend against.

Related chapters: [policy and authority](05-policy-and-authority.md),
[guards, health, and restoration](14-guards-health-and-restoration.md), and
[runtime, releases, and promotion](10-runtime-releases-and-promotion.md).
