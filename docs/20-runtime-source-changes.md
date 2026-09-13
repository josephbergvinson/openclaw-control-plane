# Runtime source changes

This architecture uses a customized OpenClaw **2026.9.3** build. Reproducing its
behavior requires both the runtime source changes and the surrounding configuration
and operating instructions. Copying workspace Markdown alone cannot add the native
ownership, persistence and delivery behavior described here.

The [runtime package](../runtime/README.md) contains the full consolidated patch,
license notices, exact identities and an offline reconstruction helper. It covers
57 local commits and 487 changed paths from the official release. It includes
capabilities retained during the upgrade and subsequent repairs across several
workstreams, rather than only the final Discord changes.

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
| OAuth refresh persistence | `src/agents/auth-profiles/oauth-manager.ts` retains refresh ownership and durable adoption after a caller's wait times out. Queue and cross-process locks prevent overlapping refresh owners; writes reject identity mismatch or credential regression. Failures remain associated with the credentials actually attempted. |
| Compaction and maintenance | Embedded compaction resolves the selected runtime/model policy and retains ingress/writer custody. Maintenance waits can be cancelled without releasing another operation's writer. Native notices describe supported lifecycle events. |
| Plain channel responses | Shared progress rendering and Discord/Telegram adapters omit synthetic emoji markers. Disabled tool progress also hides failed-tool progress rows while preserving approvals and final errors. Group prompt guidance respects reaction preferences. |
| Request evidence | The Responses transport records bounded model, reasoning-effort and service-tier metadata at outgoing requests. This separates configured preferences from request evidence and accepted responses without publishing hidden reasoning. |
| Memory and prompt preparation | Optional project-memory preparation is bounded, and search tools advertise only available or runtime-authorized corpora. The agent receives fewer misleading options and avoids unbounded optional preparation. |
| State, approvals and backup compatibility | Native state-root migration, approval inspection/migration gates, read-only state access and backup resource inventory preserve the owners of persisted configuration and approval state. |

The patch also carries regression tests, Plugin SDK compatibility documentation,
the lane-contract lockfile importer and generated protocol/schema changes. These
are part of the reconstruction. Omitting tests or selecting a few recent commits
would produce a different reference package.

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

The manifest starts at official tag `v2026.9.3`, commit
`1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`, and records deployed source
`39d61ba70ec77e39ead3a51cf22f66fc6321068f`. Its public derivative changes only two
company-specific strings in one regression fixture. All production source bytes
and file modes match that deployed source. The resulting tree is
`7df03421b5eb7d267c111c24321b7f333883b5e3`.

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

The newly pinned `39d61ba` source passed native checks, a complete build, direct
runtime import and sealed-release activation on 13 September. Fresh health, channel
and scheduler reads passed, as did a manually invoked Journal capture with visual
acceptance and renewed process binding. These checks retain their exact scope. The
new Discord `/goal` acknowledgement and delivery behavior still requires a fresh
user-path run after browser sign-in; regression evidence is recorded separately.
See the [runtime evidence](../runtime/README.md) for the current results and limits.

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
