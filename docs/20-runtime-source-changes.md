# Runtime source map

The public package contains the complete delta from the official source selected
by [the manifest](../runtime/manifest.json). This chapter maps its mechanisms to
source areas in the reconstructed checkout; it is not an exhaustive changed-file
inventory or a deployment diary. Use Git to inspect the exact patch and regression
tests. [Reconstruction](../runtime/README.md) checks the resulting tree and modes.

## Follow one request

```mermaid
flowchart LR
    Ingress[Authenticated channel request] --> Context[Profile, policy and source context]
    Context --> Run[Admitted model and tool run]
    Run --> Child[Optional represented worker]
    Child --> Resume[Parent continuation]
    Run --> Effects[Provider or file effects]
    Resume --> Effects
    Effects --> Readback[Verify requested result]
    Readback --> Delivery[Owned channel final]
```

Policy describes the intended behavior. Runtime source binds the requester,
execution, continuation and delivery. Workspace helpers own their checked
operations. No one layer substitutes for the others.

## Requests, tools and durable work

| Mechanism | Implementation area and behavior |
| --- | --- |
| Request admission | `src/auto-reply/reply` and channel ingress retain authenticated source/account/session context. Fetched text, a quoted message and a worker result do not create new requester authority. |
| Tool access | `src/agents` and gateway tool resolution carry the admitted permission mode and tool policy into execution. Authenticated-owner defaults do not become permissions for unrelated senders. |
| Visible workers | Native session/task tools represent delegated work and bind registered projects to their authorized source root/worktree. The caller supplies the task scope; workers retain its constraints. |
| Native goals | Goal/task accounting preserves the objective, accepted usage and terminal state across attempts. A model response ending does not itself satisfy the goal. |
| Yield and continuation | `extensions/lane-contract`, reply dispatch and native task scope preserve pending continuation while a child runs. The parent can resume and verify the result without an empty-response failure or a second mutation owner. |
| Original task context | Session creation, continuation and scheduled recovery retain the original request and source bindings. Later steering can correct ongoing work without silently replacing its objective. |
| Follow-up admission | Queue and steering owners distinguish live corrections, queued follow-ups and independent requests. Ownership is rechecked after preparation and replacement; a stale parent or source cannot adopt another run. |
| Child completion | Registry, announcement and requester-continuation paths keep result delivery owned through settlement and cleanup. A late completion does not justify another copy of an already delivered final. |
| Deadlines and cancellation | Spawn, backend and native process boundaries carry configured deadlines, including explicit zero where supported. Observation timeouts, execution deadlines and caller cancellation keep separate meanings. |
| Interrupted work | Durable task/session state and current execution handles inform recovery. Inspect effects before retrying; losing a wait or shortening a transcript does not prove an action never occurred. |

See [continuation](06-execution-and-durable-lanes.md) and
[authority](05-policy-and-authority.md) for the working contract.

## Context, memory and model requests

| Mechanism | Implementation area and behavior |
| --- | --- |
| Bootstrap selection | `src/agents/workspace.ts` owns recognized filenames, guarded reads and role/session filtering. Workspace manifests describe intended context but do not inject arbitrary files. |
| Durable context | Primary and company profiles carry stable identity, preferences and source pointers. Retrieval remains source- and audience-aware; stale memory cannot prove current provider facts. |
| Compaction | Native context-pressure handling, summary construction and memory flushing retain recent task context and writer custody. A failed later model request must remain distinguishable from successful compaction. |
| Optional recall | Memory preparation and search have bounded scope and usable fallbacks. Unavailable semantic search does not disable exact source lookup or justify claiming all context is missing. |
| OAuth refresh | `src/agents/auth-profiles` retains refresh ownership and durable profile adoption under queue/cross-process locking. A caller timing out does not authorize a competing refresh or identity substitution. |
| Request accounting | Transport metadata and accepted response identities distinguish configured model/reasoning preferences, outgoing requests, returned usage and goal totals. They do not expose hidden reasoning. |
| SDK and plugin contracts | Compiled SDK surfaces, generated schemas and plugin metadata travel with their consumers and tests. A source export must include the dependency closure rather than a few hand-selected fixes. |

[Memory and context](08-memory-and-context.md) explains loading boundaries and
[configuration](../config/README.md) describes the adopter's settings.

## Text, voice and meeting records

| Mechanism | Implementation area and behavior |
| --- | --- |
| Channel routing | Discord/Telegram adapters preserve the intended account, channel and thread. Exact-message reads return that message or an error instead of substituting general history. |
| Progress | Reply presentation preserves authored milestones during long work and legitimate yields. Optional tool narration is separate from useful commentary and final delivery. |
| Final ownership | Direct replies, command settlement, workers and resumed runs share native delivery ownership. Implicit reply references follow the configured route while explicit targets and source checks remain binding. |
| Delivery reconciliation | A model result, internal terminal record and provider-visible message are distinct observations. Unknown sends require inspection before retry; an unconditional exactly-once guarantee is not implied. |
| Voice identity and context | `extensions/discord/src/voice` resolves the authenticated speaker and selected primary/room context before admitting speech. Guests retain room context without inheriting another speaker's private profile. |
| Wake name and task handoff | Room/guild voice settings control when the bot responds and whether it may consult the normal agent for records or actions. A spoken acknowledgment and eventual result keep the original request's owner. |
| Playback and interruptions | Realtime and speech pipelines retain queued replies, PCM/playback ownership and configured interruption behavior. Room/provider replacement cannot reopen stale playback or reconnect indefinitely after a terminal error. |
| Two-sided recording | Assistant audio enters the meeting recorder through played output, separately from conversational ingress. Generated but unheard text is not recorded as speech, and the bot does not respond to its own recording. |
| Capture finalization | Recorder drains and lifecycle ownership keep pending speech/output associated with the right capture through stop/leave. Summary and destination delivery follow the configured capture/follow owner. |
| Minutes quality | The LLM drafts readable decisions, owners, deadlines and open questions from the actual record, using relevant authoritative names/facts. It should preserve uncertainty and coverage gaps rather than invent content. |

[Delivery and voice](13-delivery-and-control-surface.md) connects these mechanisms
to configuration and real-interface acceptance. Test fixtures establish their
specified scenarios; listening to a real call and reading its saved record are
separate checks.

## Native application and browser interaction

The Mac companion and bundled worker have their own build and identity. Local
application authentication resolves the configured gateway SecretRef through the
matching native helper, using bounded private pipes and current configuration.
A terminal's inherited environment is not a substitute for the Finder-launched
application's actual authentication path.

Browser and Computer tools are complementary. Use the supported browser interface
for structured page operations and direct Computer interaction for native prompts
or workflows whose normal interface needs it. The requested account and provider
rules remain the same whichever route performs the action. A denied OS permission
should identify the observed capability; it does not prove the whole app or account
is inaccessible.

Opaque credential bindings identify an existing protected consumer. They support
secret entry without placing password values in prompts, logs or shell arguments.
Native authentication, autofill and recovery remain separate from the provider's
current account authorization. Bounded diagnostics and cleanup must retain the
original failure and close only resources owned by that operation.

Source areas include `apps/macos`, `src/node-host`, browser plugin interaction and
credential consumers, and the native worker's tool lifecycle. The
[security chapter](16-security-and-trust-model.md) describes the boundaries.
The [native packet](../runtime/native154/README.md) identifies the separately
published dependency, source patch and resource notices.

## Scheduling, releases and host helpers

| Owner | Contract |
| --- | --- |
| Native cron | A persisted definition binds schedule, command/model settings, tools, deadline and delivery. Run results keep their admitted configuration identity; later edits do not rewrite history. |
| Command supervision | Python entrypoint receipts distinguish child exit, termination and residual processes. Native exit success alone does not prove the command's domain effect or provider delivery. |
| Existing recovery | A configured repair retains the original deadline and independently verifies the repaired operation. Agent prose is not the verification command. |
| Release activation | Workspace activation seals a complete candidate, captures compatible recovery state, consumes one start and verifies selected/loaded process identities. A rebuilt source checkout is not an installed release. |
| Health and access | Existing health owners check their declared runtime and provisioned account routes. Normal provider refresh and task-time recovery retain exact identity; missing optional bindings do not fail unrelated accounts. |
| Maintenance and recovery | Classified cleanup, retention and backup helpers keep producer/lease, identity and recovery checks. They must report partial work honestly and preserve unknown or active data. |

The runtime patch and exported Workspace are different source owners. See
[scheduling](12-scheduling-and-background-work.md),
[release promotion](10-runtime-releases-and-promotion.md) and
[host operations](19-host-operations-and-backups.md) for their procedures.

## Reproduction and qualification

The complete patch retains source, regression tests, SDK/schema changes and
required dependency metadata. Deliberate public fixture normalization is declared
in the manifest; it cannot hide a production difference. Current source and
optional native distribution trees are separately bound and checked.

Use the public [reconstruction procedure](../runtime/README.md), then inspect
[hosted checks](https://github.com/josephbergvinson/openclaw-control-plane/actions)
for the exact commit. Source equality, tests, build, activation and user-visible
behavior establish different claims. An adopter supplies their own model,
accounts, device permissions, schedules and acceptance checks.

## Attribution

Retain OpenClaw's [license](../runtime/LICENSE),
[third-party notices](../runtime/THIRD_PARTY_NOTICES.md) and the
[native resource notices](../runtime/native154/README.md). A consolidated patch
preserves the complete source delta without publishing the originating operator's
private Git history, configuration or operational records.
