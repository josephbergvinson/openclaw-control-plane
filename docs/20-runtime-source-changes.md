# Runtime source changes

This architecture uses the customized OpenClaw build pinned in the runtime
manifest. Reproducing its behavior requires both the runtime source changes and
the surrounding configuration
and operating instructions. Copying workspace Markdown alone cannot add the native
ownership, persistence and delivery behavior described here.

The [runtime package](../runtime/README.md) contains the full consolidated patch,
license notices, exact identities and an offline reconstruction helper. Its
manifest records the complete changed-path and commit counts from the pinned
official release. It includes
capabilities retained during the upgrade and subsequent repairs across several
workstreams, rather than only the final Discord changes.

## September 22 upgrade

The 2026.9.5 port starts from official commit
`ec9c1a13db8938e5a3eaa51fca2e981cde2395a9`. It retains the target's current owners
and data contracts while carrying forward the differentiated behavior. The final
source identity and gateway deployment status belong to the manifest. The
[September 22 record](../runtime/README.md#september-22-upgrade) distinguishes verified
gateway activation and the Mini installation from subsequent component and
credential acceptance. Source `1fb1f66cfe29`, including the native credential
compatibility reader and native-goal repair, remains installed as signed build
`2609000592` on the originating Mini. A bounded native-goal final has exact Discord
readback. The newer exported and activated Gateway source
`085711060c84` corrects existing Discord thread receipts so visible finals can
settle their backend delivery obligation. It activated at `12:37:05.013053Z`;
fresh live settlement acceptance remains pending after a provider rate limit
interrupted the diagnostic before child creation. The qualified companion retains its matching `1fb`
private worker. MacBook installation and actual account entry remain separate
gates. The behavior and acceptance boundaries below retain their dated scopes.

| Change family | Source behavior and acceptance boundary |
|---|---|
| Task ownership and steering | Native Codex completion tracking preserves the target's per-assignment run identity, session incarnation and requester custody. Owned child follow-ups retain a stable logical completion identity; an accepted send is not proof that the parent received its result. |
| OAuth settlement | Durable refresh claims and settlement retain the admitted owner beyond a caller's observation deadline. A stale authentication failure cannot poison a renewed credential generation. Configuration and unit checks do not prove a particular provider sign-in. |
| Provider recovery after tool progress | A fully settled successful tool batch can begin a fresh transient-outage window without resetting the run's total retry budget. Existing continuation reuses the committed transcript; commentary, duplicate receipts, failed tools and active work do not renew that window. |
| Native credential entry | The Mac companion resolves a host-local enrolled alias into the observed native secure field through a bounded private signed-app child. Direct reads disable legacy Keychain UI. Existing creator-trusted items can use the fixed Apple-signed reader after exact metadata, item, host, unlocked-store and stored-ACL checks; a class-only read materializes a legacy reference without requesting password data. That compatibility reader can briefly prompt on a lock/ACL race, while the parent deadline bounds its process group. The app validates the helper before launch and the child validates its direct app parent. `credential_prompts` returns an opaque execution-bound reference; `type_secret` rechecks account, host, Apple-signed prompt owner, field and execution authority after awaiting resolution. Browser credentials retain their separate origin-bound route. Entry and successful authentication are separate receipts. |
| Native goal delivery | Continuing-goal finals select the existing durable channel owner once and retain the actual command-target session. Completed progress drains before the final; ordinary private slash replies retain their interaction hooks. Physical sends, cancellation and multipart receipts are tested independently of goal completion. A bounded native-goal final passed on `1fb`; the later existing-thread receipt correction is activated but still needs live backend-settlement acceptance. |
| Project and source-reading guidance | Existing registered project/worktree delegation may create the visible native task required by that owner. Read-only source CLI routes remain available when selected by the registered capability; outbound messaging and coordination retain their own tool rules. |
| Native packaging pins | Swift snapshot preparation restores committed dependency pins after temporary dependency editing and uses the existing forced-resolution contract for native compilation. The verified package remains a separate artifact from the live gateway. |
| Recovery reporting | Existing versioned effect predicates may carry one closed failure cause. The runtime keeps structured/private values out of public notification text, including truncated and split-line output. A producer's current-status projection must correlate a later same-target verified effect; successful no-op execution alone cannot erase a historical failed invocation. |
| Diagnostic attribution | The local deep probe uses existing identity-bound pinned platform metadata, without widening device admission. An unresolved non-environment SecretRef is reported as an unresolved comparison rather than a proved token mismatch. Read-only metadata inspection preserves source database artifacts. |

Native app signing, host-specific credential enrollment and actual secure entry
remain separate from gateway source/build checks. Recovery report fixtures do not
rerun authentication or prove newly delivered scheduler notifications. Historical
completion reconciliation preserves failures and intentional silence instead of
bulk-resending old work or converting every terminal task to delivery success.

## September 21 follow-up repair

The [repair record](../runtime/README.md#september-21-follow-up-repair) pins
committed source `daf5771a7d868903ada1a99cf595a587027f018c`. The complete patch
includes the following additions, now deployed with a bounded live Discord check:

| Change | Source boundary |
|---|---|
| Owned child follow-ups | `src/agents/tools/sessions-send-owned-child.ts` and the subagent registry bind an eligible follow-up to the parent's current turn, preserve logical task identity, and fence changes to the child session or execution owner. |
| Completed-session reactivation | `src/gateway/session-subagent-reactivation.ts` rechecks the current owner after runtime loading and completes the existing replacement transition before a follow-up promises a completion handoff. |
| Harness completion receipts | `src/agents/accepted-session-spawn.ts`, embedded tool completion and Codex dynamic tools preserve the stable completion identity separately from the accepted execution ID. |
| Rejected-wait presentation | The sessions-yield tool and `src/agents/embedded-agent-runner/run/tool-error-warning.ts` explain an unconfirmed handoff while retaining the error and the existing visible-answer rules. |
| Retained dreaming effort | The preceding `8ad9b4113ab` change carries managed reasoning effort through memory dreaming and the background completion bridge; the complete export retains it. |

The package keeps the original official release as its reconstruction base.
Focused regression results, reconstructed-source qualification, activation and
actual channel delivery are recorded separately. Hosted qualification passed for
`daf5771a7d86`; the older companion acceptance retains its original scope.

The later [active-child correction](../runtime/README.md#active-child-follow-up-correction),
source `18b432ca2f83a107b1bc329cedf2221f20e61639`, is included in the manifest and patch.
It changes the existing owned-send
producer to inherit the active execution's delivery mode and the shared terminal
owner to defer stale failure warnings during confirmed continuation. Its tests
exercise the real registered backend validator and actual payload rendering.

## What is included

Every row below is **runtime-backed**: the implementation is in the supplied source
patch. Configuration and operator policy decide how those capabilities are used.
The source paths are relative to the reconstructed OpenClaw checkout.

| Change family | Implementation and effect |
|---|---|
| Browser credential handling | `extensions/browser/src/browser-credential-fill.ts`, credential configuration and origin-policy modules, plus browser/node route callers. Credentials resolve through configured secret references and approved origins. Adopters supply their own secrets and origin rules. |
| Detached work presentation | `extensions/lane-contract/` presents detached Discord work through the native task owner. A pending checkpoint send delays only that task's progress delivery; task execution and other tasks' progress delivery continue. A missing receipt preserves later distinct commentary; the uncertain checkpoint is not replayed. The 60-second observer deadline does not cancel the underlying send, which can still arrive late. Failed attempts retain the last confirmed receipt as the progress clock. Checkpoint requests use the exact active backend and stable queue identity. `src/tasks/agent-harness-task-runtime-scope.ts` and SDK continuation methods enforce the underlying ownership. |
| Native goal acknowledgement and delivery | `src/auto-reply/reply/commands-goal.ts` acknowledges a saved start/resume before continuing its work. The Discord native adapter keeps that control receipt at the configured slash-command privacy, then sends progress and results to the originating conversation through ordinary channel delivery. Work remains deliverable after interaction expiry; status, validation errors and unrelated control replies retain their normal privacy. |
| Goal accounting | `src/agents/session-goal-usage.ts` and `src/config/sessions/goals-usage.ts` count accepted provider input, output and cache usage once across attempts. Compaction changes context size without resetting accumulated usage. A bound final response remains charged after goal completion; later unrelated work does not. Unknown mid-run usage is reported rather than fabricated. |
| Yield and continuation | Reply classification carries pending continuation through dispatch and Discord native command settlement. An intentional yield without acknowledgment does not become an empty-answer failure. Transcript projection completes before the writer yields. |
| Follow-up steering | The reply queue binds an adopted source to its exact active operation. That source no longer blocks its own correction; older queued work and different authority remain blockers. Ownership and queue order are rechecked before injection and after parked admission. |
| Visible child deadlines | The initial visible-session timeout reaches the existing execution owner in milliseconds. Explicit zero, default/omitted values and seconds compatibility retain their distinct contracts. Generated protocol types and regression cases travel with the patch. |
| Cron and child custody | Native cron, subagent registry and task runtime changes retain parent custody through child completion, retries, cancellation and restart recovery. A parent does not lose responsibility merely because execution moved into a child. In `src/cron/command-runner.ts`, explicit numeric `timeoutSeconds: 0` disables the command wall timer; omission, positive deadlines, no-output limits and cancellation keep their separate contracts. |
| Automation removal | `src/cron/service/timer-outcomes.ts` and `timer-outcome-finalization.ts` retain terminal history for a removed run without generating a new failure alert. A replacement with the same ID retains its own state. Existing alerts for execution and required-delivery failures remain enabled. |
| Retained cron configuration | `src/cron/task-run-history.ts` adds `configRevision` only when a retained execution receipt identifies that run's configuration unambiguously. Later job edits cannot rewrite it; missing or ambiguous receipts leave the field absent. The gateway schema and regression tests carry the same contract. |
| Exact Discord reads | `extensions/discord/src/actions/runtime.messaging.messages.ts` returns the specified message or an error for `read --message-id`, preserving target authorization and rejecting conflicting history selectors. It cannot silently substitute channel history. |
| Continuation presentation | `src/agents/command/continuation-presentation.ts` and the Discord message handler retain eligible progress at yield and route resumed commentary and typing through native presentation settings. Hidden child, cron and heartbeat work does not acquire that visible continuation path. |
| Optional silent heartbeat results | `src/auto-reply/reply/get-reply-run-context.ts` and the embedded attempt path allow an intentionally silent heartbeat to settle without a retry demanding a visible answer. Ordinary requests retain their answer requirement. |
| Yielded requester ownership | Subagent registry reads, heartbeat preflight and reply admission recheck durable and live requester ownership through cleanup and settlement backoff. Announcement socket fallback registers the canonical child task before dispatch while preserving a separate silent CLI task row. |
| OAuth refresh persistence | `src/agents/auth-profiles/oauth-manager.ts` retains refresh ownership and durable adoption after a caller's wait times out. Queue and cross-process locks prevent overlapping refresh owners; writes reject identity mismatch or credential regression. Failures remain associated with the credentials actually attempted. |
| Compaction and maintenance | Embedded compaction resolves the selected runtime/model policy and retains ingress/writer custody. Maintenance waits can be cancelled without releasing another operation's writer. Native notices describe supported lifecycle events. |
| Plain channel responses | Shared progress rendering and Discord/Telegram adapters omit synthetic emoji markers. Disabled tool progress also hides failed-tool progress rows while preserving approvals and final errors. Group prompt guidance respects reaction preferences. |
| Request evidence | The Responses transport records bounded model, reasoning-effort and service-tier metadata at outgoing requests. This separates configured preferences from request evidence and accepted responses without publishing hidden reasoning. |
| Memory and prompt preparation | Optional project-memory preparation is bounded, and search tools advertise only available or runtime-authorized corpora. The agent receives fewer misleading options and avoids unbounded optional preparation. |
| Shared memory-search ownership | `extensions/memory-core/src/memory/manager.ts`, the memory tool and the optional SDK `withSearchOperation` contract retain provider and database ownership through search result/status finalization. Closing drains already-admitted nested work, expires callback admission and lets later callers acquire a replacement. |
| Registered-project source workers | `src/agents/tools/sessions-spawn-visible.ts` forwards a native `projectId` to Gateway admission, which resolves the authorized registered root and managed worktree. Project binding rejects conflicting placement inputs and preserves inherited permission limits. |
| Discord host context through preflight | `extensions/discord/src/monitor/message-handler.preflight-context.ts` preserves the injected host `buildContext` function into ordinary message processing. This retains the exact Gateway ownership binding; raw builders and retired owners remain unbound. |
| Run-bound delegated tool context | `src/auto-reply/reply/agent-runner-execution.ts` uses the admitted reply operation's Gateway context resolver for tools. An unbound operation cannot fall back to ambient authority. |
| Authenticated owner access defaults | `agents.defaults.ownerPermissionMode` supplies the configured access mode only to an authenticated owner turn. Explicit session modes win; the default is not written into shared session rows or granted to unrelated senders and background work. Child authority follows the admitted parent and native revalidation. |
| Accepted-profile steering authority | `src/agents/embedded-agent-runner/run/attempt-stream.ts` captures the selected authentication profile only after provider acceptance, invalidates capture on abort and preserves operation ownership. `src/auto-reply/reply/agent-runner-steer-adoption.ts` also requires current tool authority before live steering injection. |
| Foreground checkpoint custody | `src/auto-reply/reply/agent-runner-memory.ts` and the execution owner extend existing ingress and deferred-lifecycle custody through the pre-compaction memory checkpoint. Native presentation can show the checkpoint; cleanup releases ownership on all settlement paths. |
| Observed macOS permission denials | The native Mac input path checks Accessibility and Event Posting separately before input dispatch and reports the observed missing capability. Screen Recording remains a separate capture check; a denial is not attributed to an unverified stale app build or TCC record. |
| Local Mac gateway authentication | `apps/macos/Sources/OpenClaw/GatewayLocalAuthResolver.swift` and `src/node-host/local-gateway-auth.ts` resolve configured local gateway SecretRefs through the app's matching bundled worker. The exchange uses bounded private pipes and checks the current configuration before returning credentials. The accepted connection owns in-memory reuse; disconnect or a changed configuration retires it. |
| State, approvals and backup compatibility | Native state-root migration, approval inspection/migration gates, read-only state access and backup resource inventory preserve the owners of persisted configuration and approval state. |

The patch also carries regression tests, Plugin SDK compatibility documentation,
the lane-contract lockfile importer and generated protocol/schema changes. These
are part of the reconstruction. Omitting tests or selecting a few recent commits
would produce a different reference package.

## Mac companion authentication

A local Mac app launched from Finder may not inherit the environment available to
the gateway service or a terminal. A configured token or password SecretRef therefore
needs the existing runtime secret resolver, even while the gateway itself is healthy.
The companion asks its own bundled `node worker --resolve-local-auth` helper to
resolve that reference. Packaged apps use their signed worker payload, including
after relocation; they do not depend on a source worktree or a globally installed
CLI having the same private command.

The helper accepts only a bounded pipe request matching the current local gateway
authentication configuration. Its native verifier checks the owning app or
boolean-only diagnostic against the matching signed bundle before and after secret
resolution; pipe type alone does not authorize a caller. The helper also checks
the configuration again before returning credentials through the private output
pipe and suppresses provider diagnostics. No credential export file or new
persistent credential copy is created. The app, native CLI and private Node binary
must share their signing identity; unsigned or ad-hoc builds cannot use this bridge.
Literal and environment-string settings retain their native paths; a selected
structured SecretRef remains authoritative over ambient credentials. Remote gateway
configuration remains outside this local resolver.

Source reconstruction, Mac app installation and gateway deployment have separate
identities. An app and its matching worker can advance together while the running
gateway remains on its previously accepted release. The manifest records that
production difference explicitly; a newer reference commit alone is not evidence
that the gateway was rebuilt or activated.

The companion at `275f120c13b` passed signed installation, matching worker and local
authentication checks, native screen/cursor/cleanup probes, and an ordinary
registered-project agent read. The [acceptance record](../runtime/README.md#historical-september-15-qualification)
binds those checks to the app build and exact source-qualified package. It does not
claim live token rotation, iOS acceptance or protected native password entry.

Reference `34982936353` adds only the production Knip classification for the
resolver export used by focused tests. The full-tree audit still checks those
test consumers. Every executable source blob remains identical to the accepted
companion at `275f120c13b`; this tooling correction requires no app rebuild.

## Models, harnesses and goals

The reference preferences use `openai/gpt-6-astra`, ultra reasoning, fast mode off,
and no automatic model fallback. Image generation uses
`openai/gpt-image-2.5-flare`. These are configurable preferences, not credentials or
mandatory model choices for every adopter. Choose models your account can access.

Astra and the GPT-5.6 family are public model names in the
[OpenAI model catalog](https://developers.openai.com/api/docs/models).
[Astra's API documentation](https://developers.openai.com/api/docs/models/gpt-6-astra)
documents `max` reasoning effort. In the observed native execution path, the local
ultra setting maps to outgoing `max`; an absent service-tier observation is not
evidence that priority processing was requested.

The model harness is the selected execution backend inside OpenClaw. The verified
reference flow used OpenClaw's embedded Responses path. Codex-backed execution is
another supported backend and should not be inferred merely from the use of a
ChatGPT-connected account. Preserve the selected backend's capability and authority
contracts when configuring a different setup.

Native `/goal` keeps one durable objective on the session, with status and model
tools for creating, reading and completing it. It is not a replacement for tasks,
cron or standing orders. `/new` and `/reset` clear the current goal/context boundary;
the logical session identifier and retained history may remain. The reconstructed
OpenClaw `docs/tools/goal.md` documents the full command and accounting contract.

On Discord, `/goal start` and `/goal resume` confirm the saved goal before work
continues. That confirmation follows the slash command's privacy setting and tells
the requester that progress and results will appear in the conversation. Those
work messages use ordinary channel delivery and remain deliverable after the
interaction expires. Goal status, validation errors and other native control
replies keep their normal privacy.

## Compaction and connected accounts

Native compaction owns context maintenance. The reference enables
`agents.defaults.compaction.notifyUser`; foreground/preflight and in-attempt events
can emit lifecycle notices, while manual `/compact` reports its command outcome.
Background maintenance is not guaranteed a separate channel start/end message.
Do not add an independent context-percentage monitor and treat its advisory as
proof of native compaction state.

OAuth refresh repairs preserve current credentials across the identified
concurrency, timeout and account-rotation failures. They cannot make provider
revocation or an expired external login impossible. Onboarding, profile ordering,
supported refresh, durable state readback and recovery remain separate checks.
Never copy another operator's account database into a fresh installation.

## Reproduction and verification boundaries

The manifest identifies the exact official base, custom commit, normalized tree,
patch checksum and export/deployment status. CI consumes its tag and toolchain
values, then independently checks the annotated tag object and resulting source
tree. Prior gateway and companion acceptance keeps its original identity; it is
never transferred to a new candidate by changing the reference manifest.

The public derivative changes only the two established company strings in the
lane-contract fixture. All other source blobs and file modes are identical to the
committed custom reference. Production differences are never described as
fixture-only normalization.

Use the [reconstruction instructions](../runtime/README.md) to apply and verify the
patch before dependency installation. The helper checks a caller-supplied standalone
clone, refuses an unexpected ref or dirty state, and performs no network access,
build or activation. It does not operate on a running installation.

The predecessor `f31686e`, with normalized tree
`cc032d12126f13922aad858d6104ec1c62e71abc`, passed [combined qualification run 34659951151](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34659951151):
all ten steering regression cases, 525 tests across twelve native test files, all
34 native check commands and a complete build. That predecessor canonical source also
completed a full macOS build. The public export separately verifies reconstruction
and production parity; these historical results do not qualify the newly pinned
source or grant another host live acceptance.

On an earlier deployed predecessor, a fresh Discord Calendar goal completed after
an intentional no-acknowledgment yield and requester continuation; accepted usage
matched its counter and one final was observed. That historical flow did not test
the newer steering correction or expire a child deadline. Child-deadline behavior
remains supported by the identified regression evidence, without a live expiry test.

On 12 September, the adopted `f31686e` release also passed one naturalistic Discord
steering case: a source-location follow-up arrived during the original document
request, both messages stayed in the same active run, and one final response with
the requested PDF was visible. The run ended with no pending inputs and no premature
compaction observed, using `ultra` reasoning with fast mode off. The file supplied for delivery
matched an independent raw fetch from the source provider, and the attachment's
name and size were observed in Discord. CDN bytes were not downloaded again.
This verifies that specific user path on the originating host; it does not cover
every future steering, compaction or device case or replace adoption checks.

The historical `39d61ba` source passed native checks, a complete build, direct
runtime import and sealed-release activation on 13 September. Fresh health, channel
and scheduler reads passed, as did a manually invoked Journal capture with visual
acceptance and renewed process binding. These checks retain their exact scope. The
Discord `/goal` acknowledgement and delivery path was not repeated in that
qualification; browser sign-in was pending at that time.

The predecessor `7fbb56e` source subsequently passed local native checks, production/test
type checks, the full build and sealed-release activation. Four fresh Discord
checks verified calendar read and verification, same-run document steering, and a
controlled two-worker yield/resume with final delivery. See the
[delivery evidence](13-delivery-and-control-surface.md#successor-live-retest) for
the exact scopes, timings and remaining attachment-byte and resumed-typing limits.
These ordinary-request checks do not establish fresh `/goal` or interaction-expiry
acceptance. Those results qualify the predecessor only; historical green runs above retain
their original source identity.

The preceding `a9aa626e7db` reference passed independent reconstruction from a clean
official-tag checkout, with all 39,490 tracked paths and file modes compared against
source. Every production blob matched; the two established fixture labels are the
only changes. Focused regressions and scoped independent review accompany the new
repairs. Deployed `b3ee068` passed required native checks, including production/test
types, all 17
core-test type graphs, extension checks, lint and the remaining native guards. The
full build, sealed-release activation and current-process health/readiness checks
passed. A fresh Discord task then passed visible progress, same-run steering,
registered-project delegation, verified reversible file effects and final delivery;
see the [scoped live result](13-delivery-and-control-surface.md#activated-successor-check).
A separate browser-credential check passed native opaque entry, configured-account
verification, live application data and requested tab cleanup. At that check, other credentials
remained unprovisioned. Hosted qualification is established separately
by the matching public commit’s workflow results. Predecessor `1f38d05` activated
successfully but failed its
ordinary Discord test before provider execution; the successor preserves the
omitted host context builder, as recorded in the [delivery diagnosis](13-delivery-and-control-surface.md#discord-host-context-regression). That earlier qualification gave the macOS diagnostic change source-level
regression evidence; it did not establish a full Mac application installation
or permission-dialog acceptance.

Reference `a9aa626` corrects only the question-recovery fixture discovered in the
first hosted ownership suite. The original failure reproduced locally; all 20
cases in the corrected file, selected native checks and scoped review passed. The
correction tests admitted permission and separately tests shared-row changes. Its
[qualification explanation](13-delivery-and-control-surface.md#hosted-permission-fixture-correction)
retains the failed hosted result and the distinction between deployed production
source and the corrected test reference. Hosted checks for the matching public
commit establish that reference's result without implying another activation.

The repository's operating policies, service layout, scheduled maintenance,
integrations and backup procedures remain necessary alongside this source package.
Build output, configured state, running identity and successful user workflows are
distinct evidence. Keep each claim attached to the check that establishes it.

## Attribution

The included OpenClaw source remains MIT licensed under its existing
[license](../runtime/LICENSE) and
[third-party notices](../runtime/THIRD_PARTY_NOTICES.md). Preserve these notices when
copying substantial source. The consolidated patch avoids exporting private Git
history or author metadata; it does not replace upstream attribution.
