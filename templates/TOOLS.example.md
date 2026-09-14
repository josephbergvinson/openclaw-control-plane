# TOOLS.md — Deliberate-read execution mechanics

This file supplies procedures referenced by `AGENTS.md`. It cannot independently authorize, forbid or override work. Load the relevant sections before applicable operations; it is not automatically injected merely because it exists. Use the versioned runtime and installed adapters declared by the reference. A command named here is not proof that its binary, account or dependency is installed.

## Tool state and execution posture

Keep tool availability, authenticated authority and actual execution distinct. A callable tool does not authorize its use; an authorized task does not make a missing binary work. A stored readiness result can be stale. Bind the target and account, then use the supported operation and authoritative readback to establish success.

Act on natural authenticated instructions within their objective, target, account/workspace, operation and risk envelope. `GO`, `STRONG GO`, `SEND` and similar phrases are understood affirmations, not required grammar. Preserve native action gates, human-presence authentication and the boundaries in AGENTS. Execution-security settings are chosen for the installation; they never create operator authority.

If the preferred route fails, inspect the real failure and use the next registered supported route when its source/account/effect boundary is unchanged. A route-specific stop condition wins over generic fallback. Do not repeat a failed call until something material changes. Ask only for a real missing choice, human-only step or unplanned external package.

## Commands, processes and working directories

Use the declared host, a canonical working directory and an absolute or clearly resolved binary. Prefer simple argv invocation. Avoid shell indirection, encoded payloads, long command chains and inline interpreters for substantive logic when an existing helper or reviewable script is clearer.

Capture the exit status and relevant output of material commands. Preserve a running session/process handle when execution continues; do not invent completion because the launching call returned. A timeout after possible side effects requires reconciliation, not blind replay. Use native represented workers for long task execution and direct exec for bounded commands, verification and supported local scripts.

Do not print environment dumps, authorization headers, cookies, tokens, connection strings or credential-bearing config. Secrets belong in sanctioned environment/Keychain/SecretRef/broker lanes, never prompts, command lines, logs, memory or config backups. Inspect secret readiness through supported value-free commands, not by reading values aloud.

## Source identity and worktrees

Before a non-trivial edit, resolve the surface through `registry/project_topology.json` and its surface record. Verify the repository root, standing branch, current branch/commit and Git common directory. Classify remotes by role. A registry row identifies a source; it grants no operation.

Use one task branch/worktree per reviewable unit. Preserve unrelated changes. Resolve duplicate roots, detached/ambiguous branches and overlapping writes before editing. A handoff, mirror, archive, built release or artifact directory is a separate role; never patch every apparent copy. Keep the canonical Workspace as an integration checkout.

- Do not work directly on `main` or another default branch for non-trivial work.
- Use a task-scoped branch such as `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, or project equivalent.
- Keep one material task per worktree unless the user explicitly combines work.
- If the repo is already dirty, report the drift before editing and either isolate, clean, or proceed with acknowledgement.
- If a worktree is broken, orphaned, or branch registration is inconsistent, do not edit it; repair it or create a clean worktree from the canonical repo.
- Keep commit identity visible at meaningful checkpoints for implementation work.
- If an operationally authoritative fix lands first on a non-standing branch, do not call the lane healthy until the fix is normalized onto the standing branch or the standing default is explicitly updated in the relevant source-of-truth map and verification path.
- Host-local operational data/output under a control-plane repo must be explicitly classified as tracked source, ignored operational state, or designated external artifact/output.

### Protected service lanes and prune preflight
- Before pruning, deleting, resetting, or repointing any worktree/ref, verify worktree clean/dirty state, merged/unmerged state, local/remote divergence, current worktree attachment, recent session/thread references, live process/port/cron references, and whether the lane is a protected service lane.
- Before deleting `.worktrees/openclaw-*` or any runtime-adjacent worktree, string/grep checks are insufficient. Build a realpath-closure manifest using portable macOS-safe resolution such as Python `os.path.realpath` over: installed runtime symlinks, `openclaw` binaries, LaunchAgent `ProgramArguments`, active process argv/cwd/open files, cron/scheduler/task-runner commands, current runtime symlinks, and config references. If any resolved path lands inside the deletion target, classify it as protected and block deletion.
- A protected service lane is any named worktree that backs a live process, port listener, cron/scheduler entry, or an active service baseline identified in recent session/checkpoint state.
- If a lane is protected or protection is ambiguous, default to preserve-and-inspect rather than prune; if it is unmerged, diverged, historically ambiguous, or was recently a runtime recovery/live release path, preserve it until a stable observation window and an exact deletion manifest exist.
- When more than one active non-trivial lane or any protected service lane exists, maintain a refreshable lane index or equivalent discoverability record. It is operational aid only; if it conflicts with live git/process/cron evidence, live evidence wins.
- After a task is merged, cherry-picked, normalized, or deliberately archived—and no protected service/session dependency remains—remove the worktree and stale refs in the same closeout slice rather than leaving them for later hygiene sweeps.

Local integration does not silently authorize remote publication or deployment.

## Filesystem and evidence

Resolve source, worktree, state, release and work-product roots through the registered layout. A coherent example uses `/Volumes/AgentData/Source`, `/Volumes/AgentData/Worktrees`, `/Volumes/AgentData/Runtime`, `/Volumes/AgentData/State` and `/Volumes/AgentData/WorkProduct`; these are distinct roles, not directories to merge. Generated work product belongs in the project work-product lane, not an improvised home-directory folder.

Keep diagnostics and receipts in internal work product. Source repositories hold source and required generated outputs. Use exact paths, indexed searches and bounded queries; do not recursively scan the entire workspace, release history or shared volume to discover which source might answer a question.

- Declaring a source boundary does not by itself expand write authority. An authenticated instruction that names or plainly implies a verified canonical repo/worktree authorizes its bounded writes; agent-initiated writes to another root do not.
- Generated reports, snapshots, manifests, audit bundles, handoff artifacts, exports, and other outputs must live in designated artifact/output locations, not mixed into source roots unless the repo already defines that pattern.
- Default scratch/output location is `./artifacts/<run-id>/` or an established repo-local output directory.
- Do not leave orphaned files in repo root.
- If a later step expects a temp/artifact file that was not produced, report the missing producer step rather than only the downstream `ENOENT`.
- For non-trivial artifacts or retained diagnostics, maintain a retention manifest or equivalent classification record using one of: `retain_long_term`, `retain_until_manual_archive_window`, `safe_to_prune_now`, `optional_cold_archive_only`.
- Retention class records storage intent only; it does not grant authority or make an artifact part of the live source-of-truth path.
- Installed runtime/package/bundle output edits outside the declared canonical repo are containment-only by default. Classify them as `live-installed-runtime-hotfix`, not canonical source edits.
- Do not patch installed dist/package/bundle output except when the operator instruction explicitly names emergency containment and the `live-installed-runtime-hotfix` record below is created first.
- A `live-installed-runtime-hotfix` must immediately record: exact files changed, reason for hotfix-in-place, backup/rollback path, verification performed, source-normalization target if known, committed yes/no, and normalized yes/no.
- If no source-normalization path is known, stop and report `blocker: source-normalization-path-unknown`.
- Until the hotfix is either source-normalized and committed or explicitly recorded as `live-installed-runtime-hotfix-not-normalized` with rollback instructions plus the remaining normalization blocker, do not report `completed`, `closed out`, `committed`, `healthy`, `blocker: none`, or equivalent.

### Backup-verified deletion mechanics
- When deletion is conditional on an existing or newly updated backup, first record the exact source path, backup path, intended backup form, and comparison predicate before mutation.
- Valid backup predicates must be directly checked, not inferred from a folder name or visible path. Acceptable checks include count/size/hash manifests, archive integrity tests, restore-list checks, or explicit, documented exclusions for re-creatable dependencies and transient metadata.
- File Provider/iCloud copies require extra caution: `dataless`, `compressed`, placeholder, package-tree, `Resource deadlock avoided`, partial materialization, or sync-error evidence means `backupVerified=false` until an alternate predicate passes.
- Do not proceed from sync/copy failure into deletion. If a backup attempt partially succeeds, immediately report `backupVerified=false`, the partial state, disk impact, and the next safe option.
- Exclusions change the backup contract. If excluding `.venv`, `node_modules`, `DerivedData`, `._*`, root-owned files, or any unreadable path, report the exclusions and ask one narrow confirmation before source deletion unless the original instruction explicitly authorized that exclusion shape.
- Prefer one of two clean fallback shapes after loose-file backup failure: create and verify a single archive object, or verify a partial backup with explicit exclusions. Do not silently switch between these shapes.
- A delete command after backup verification must re-check the source and backup immediately before deletion and must stop if the predicate no longer holds.

## Route and account selection

Read `registry/integration_routes.json` and resolve the typed facts already supplied by the request or authenticated session. An exact account takes precedence over a portfolio label; explicit workspace, network and principal facts must agree. Do not use keywords, channel names, a persistent global mode or whichever credential is convenient to select an account.

Derive the relevant portfolio without requiring the operator to enumerate every source. Use its bounded claim-relevant read set. Retain account and source identity with every result. Multiple read accounts do not create ambiguity by themselves and never select the single sender or write destination.

Prefer a supported native/upstream capability, then a maintained integration/API, then the registered managed browser, then host UI when needed. Check current target access when useful. Missing or stale readiness is not an automatic veto of an otherwise supported authorized operation. Verify every external mutation through the same route or an equally authoritative read path.

A registered team Discord guild may be a source-only portfolio route. That does not establish an inbound assistant binding or authority to post, reply, react or mutate there. Membership and an open logged-in tab are not operator authority.

Use the installed resolver with established typed facts: `python3 scripts/resolve_capability.py --compact --system <system> --intent <intent> --required-operation <operation>` plus the applicable `--account`, `--portfolio`, `--workspace`, `--network` and `--principal`. `--context` is a legacy exact route tag, not prompt text. Resolution does not perform the requested provider mutation. `--run-exact-probe` requests an optional bounded registry-bound read-only probe; `--native-operation-support unsupported` expresses an actual provider API gap, and `--authenticated-ui-required` expresses a task whose required state exists only in the signed-in UI.

Use `--compact` for ordinary route selection. It retains the selected account/workspace bindings, requested operation, probe outcomes, constraints and execution/fallback guards without repeating the full candidate/status catalogue. Omit it for full diagnostics. The flag changes presentation only; resolution, authority, probe execution and exit status are unchanged.

## Exact resources and email

Bind a supplied link or identifier first: selected account, owner/sender, title/subject and date where available. Related search can provide context but cannot replace an unopened exact target. An example provides structure, not identity facts for the actual recipient or object.

For Gmail, select the account before reading, drafting or sending. For example, `operator@company-alpha.example` and `coordinator@company-alpha.example` are distinct enrolled identities. Read both when the registered portfolio warrants it; bind one exact sender for a write. Keep credential values inside the supported process environment.

- For Gmail reads, inspect the selected account with `gog auth status --account ACCOUNT --json --select 'account.email,account.client,account.credentials_exists,account.auth_preferred' --no-input`, then verify access with the first task-relevant read bound to that account. Stored configuration alone does not prove mailbox access. Keep the selected account with the result; a full `gog auth list` inventory is useful for account discovery, not routine per-read output.
- Discover messages with `gog gmail messages search 'QUERY' --account ACCOUNT --max 10 --json --no-input`; this returns metadata without bodies. Retain `nextPageToken` and use `--page TOKEN` when more coverage is needed; ten results are one page, not an absence or completeness test. Keep the pagination envelope rather than adding `--results-only`.
- Read a relevant message with `gog gmail get MESSAGE_ID --account ACCOUNT --format full --json --select 'headers,body,attachments,message.id,message.threadId' --no-input`. This retains the decoded body, source identifiers, headers, URLs and attachment references without the raw MIME payload. Follow relevant messages across the thread when chronology matters. The body falls back to HTML when plain text is absent; if extracting readable text, retain link destinations and task-relevant structure.
- Use raw/full-thread JSON for a task that needs that representation; save large payloads once to an internal artifact and inspect selected content instead of emitting base64 MIME or re-fetching only to decode it. Keep complete selected bodies and links available. `--sanitize-content` removes HTTP(S) URLs, so it is unsuitable for ordinary reads that may need meeting/source links. Report any preview limit and expand the relevant content before concluding that a fact is missing.

A `mail.google.com` URL fragment is a browser locator, not necessarily an API message or thread ID. Resolve the exact message through the authenticated browser or a supported provider mapping. If it cannot be opened and identified, report that target unresolved; do not silently draft from a thematically related email.

Draft when asked for a draft. Send when the authenticated instruction names or plainly implies the bound recipient/package. If sending emerges later, prepare the exact sender, recipients, body and attachments and ask one natural question. Before any draft/write, reconcile the intended company, material facts, source freshness and newer contradictory evidence. Keep another company's facts out unless the scope includes them. Read back the actual saved draft or delivery result.

## Calendar

Create or modify events when the authenticated scheduling request names or implies it. Resolve the destination from USER: Apple Calendar/iCloud in this example. An invitation in Gmail or Google Calendar can supply the details without changing the destination.

For an existing meeting, look for and inspect the current authoritative invitation, including updates or cancellation, when available. Read the bounded relevant mail/calendar accounts. Source-only workers may read those invitations while one owner performs Calendar writes. Chat confirmations help locate a meeting but do not replace an accessible invitation.

Carry the verified title, start and end with timezone, meeting URL, location and relevant joining notes/dial-in details. Populate the destination's URL field when supported. Do not guess an end time or silently turn an unknown duration into a one-hour hold. Preserve unresolved worker findings through synthesis and ask only after the relevant sources have been checked. For a new meeting without an agreed time, propose useful candidate slots when practical.

Before writing, verify the target calendar/account and inspect matching destination entries. Update the existing identity for the same meeting, preserving unrelated notes. Create only a missing entry. Read back every requested meeting and compare times and joining details with its source; one successful event does not prove the whole request complete.

A supported Apple Calendar adapter may need an event-finder read plus a dedicated URL-property read, depending on its output contract. Verify the returned fields rather than assuming a title/time listing proves the URL. Reuse the installed adapter; do not introduce a second writer or raw database edit merely to obtain a field.

Use one bounded availability-window call through the registered Apple adapter. If it reports blocked, timed out or incomplete, preserve that boundary: do not automatically inspect `Calendar.sqlitedb`, switch automation frameworks or substitute Google Calendar. An explicitly requested alternate route remains subject to its own authority. Keep raw event titles, locations and descriptions private when the requested answer is simply availability.

## Documents, pages and task trackers

Resolve the exact account, workspace and parent/container before writing. Read the current target. Choose create, revise, replace, append, archive or delete from the intended outcome and current structure. Revise an existing reader-facing object in place where appropriate; append only when requested or when the destination is a real log/appendix. Do not create test shells or replacement objects simply to prove access.

For a shareable document, apply SOUL, deliberately read WRITING and use the work-product skill. Preserve the requested structure, meaning, numbers and commitments. Keep internal review notes, paths, source-process scaffolding and audit tables out unless the deliverable itself concerns those systems.

Verify the same object through an authoritative read: content, structure, placement, account/workspace and relevant collaboration properties. A returned link, block count, local file or success flag alone is not completion. Personal task tracking and company issue tracking remain separate registered destinations.

- Discover cards with `python3 scripts/trello_inspect.py board BOARD_ID --cards`, then read selected descriptions with `python3 scripts/trello_inspect.py card CARD_ID`; recent actions are opt-in via `--actions-limit N`. The board helper covers open cards, not historical completeness. Expand to task-relevant archived cards through the existing `trello_common` client when needed. The card helper does not include checklists; retrieve them with that client's `get('/cards/CARD_ID/checklists')` when relevant.
- For description-based discovery, use the existing client to fetch and match descriptions locally, then print matching IDs, titles, URLs, relevant dates and a short matching passage. Retrieve full relevant descriptions, checklists and links before synthesis; do not weaken the search to titles alone or print every matching description by default. Keep counts and disclose any preview limit, expanding coverage when the task requires it.

For Google documents and Drive, confirm the selected account and intended folder/shared drive before creating content. A working personal account does not substitute for an unavailable company route. Verify owner and placement metadata after the write. For Notion, use the registered bounded search, page and data-source reads, then recursively read back the same object after mutation; local exports do not replace current page content.

For company issue tracking, use the account-bound issue search or exact issue read, including relevant newest comments. Do not replace a supported provider reader with a recursive local or generic web search. For saved analytics questions, discover the bounded visible catalog and execute an exact supported saved query; do not silently switch to arbitrary SQL, native query bodies, parameters or analytics mutations. Return the business answer, not private raw rows or the helper envelope.

## Subject-scoped PostgreSQL recall

Use established context or relevant file memory for stable personal details. Query the verified subject-scoped PostgreSQL route when missing or change-sensitive facts require it. Prefer the smallest trustworthy read path: the checked-in reader or guard for the active surface first, then a transparent read-only `psql` query when the registered route supports it.

The reference Personal Data Project distinguishes `subject_profile_versions` for profile state from `context_daily` for date-scoped context. Bind the actual database/schema, subject and table from the adopter's registered route; those example table roles do not prove that a table exists or that its contents answer the question. Query only the surface that matches the claim and requested time window.

Verify the lane explicitly and lightly: inspect the structured registry/status layer or its generated capability view, confirm the applicable checked-in PostgreSQL reader exists, verify that the expected DSN environment variable is present without printing its value, and run a minimal read-only target-table probe when needed. Do not conclude that database availability is unknown merely because recent conversation did not mention the route. A connectivity probe is not proof that the relevant subject rows are present or current.

Keep reads narrow, subject-scoped and read-only. Never expose DSNs, secrets or unnecessary raw personal data in chat, logs, artifacts or audit messages. Personal-data writes are separate from recall: require an authenticated instruction naming or plainly implying the table, subject and attribute mutation; record before/after row IDs or stable hashes where useful, read back the effect authoritatively, and report only the minimum relevant values unless the operator requested the underlying detail.

## Browser and host UI

Use existing registered signed-in browser state. Reinspect the actual tab, origin, account and current UI before acting. Do not start a fresh login because an earlier observation or stale identifier failed. Keep credentials inside the managed browser/profile or sanctioned opaque broker boundary; never copy cookies or authentication values into another process.

Use state assertions, text/anchors and observable waits rather than blind clicks or fixed delays. Reconfirm app/window/focus after a context change. A click is not evidence of the requested effect; read back the target object or observable UI state. Unexpected layout or account changes require renewed identification, not guessing.

For `AGENTS.md` §11 cleanup, recheck the recorded tab/window or app-instance identity and current activity, use supported close controls on the same lane, and verify that only the intended resource closed.

CAPTCHA, passkey/biometric/hardware-key presence, one-time MFA, credential creation/reset/recovery and custody changes remain human-presence boundaries. An operator's permission to handle ordinary system dialogs does not invent a secure credential channel or bypass those native boundaries.

## Authorized wallet and economic operations

A request naming or plainly implying a QA flow, trade, order or other on-chain outcome can authorize the required connection, approvals, signatures and submission within its economic intent. Select the registered account and proportionate route/details from current context. Do not demand duplicate packages or magic wording for routine steps already authorized.

Verify the current origin, network and account. Check available human-readable transaction details against the requested action and use the trusted application's generated transaction. A staging hostname does not establish a test network. A separately enabled first-party QA grant is limited to its declared site/account/network scope and smallest practical transaction; it is not a universal trading grant.

Submit at most once and reconcile an ambiguous result against authoritative settlement before retrying. Never reveal credentials, create/reset custody material or satisfy a human-presence challenge. Ask one natural question if a missing choice would materially change economic intent or risk.

## Image generation

Use the configured Images 2.5 generation/editing route, `openai/gpt-image-2.5-flare`, through the existing OpenAI OAuth integration. Its configuration owner is `agents.defaults.mediaModels.image`, separate from the reasoning model's image understanding. Keep generation fallbacks empty: an unavailable route is not permission to silently use an older model or create API-key billing.

Preflight the complete prompt and intended reference images before the call. An `image_generate` call produces a user-visible delivery; default to one intended call. Inspect the result. An unplanned corrective second delivery needs one natural confirmation unless variants or multiple images were named or implied. Do not assume a hidden generation mode. The requested selector is not independent returned backend attestation if the provider does not supply that identity.

## Discord delivery and substantial work

Use natural prose, not fixed Result/Impact/Blocker schemas or compact JSON. Keep one logical answer together and use code fences when copying matters. Prefer conversation content; use a meaningful attachment when requested or when channel limits would make the answer unreadable. Do not expose absolute host paths or substitute an internal receipt for the answer.

For work that continues, acknowledge what is being undertaken and establish/resume the supported durable task before long computation, I/O or waiting. Keep the parent responsible for synthesis, source selection and external effects. Native subagents inherit supported model/thinking settings unless the task explicitly requires an override.

Relay user-relevant milestones and real waits within AGENTS' cadence. Tool commentary and harness notices are not useful task progress by themselves. Ordinary finals use native source delivery. Nonterminal updates use the native message route with `final: false` and inherited origin when supported. Never send the same completed final through both an explicit send and the automatic final path.

After interruption, reconcile the represented task, current effects and delivery state. A transcript final is not proof that Discord showed it; inspect the native delivery receipt and the user surface as appropriate. Do not create duplicate workers or replay side effects because a summary is incomplete.

## Coding, verification and closeout

Read the existing implementation and callers before editing. Search for an existing owner/helper before adding one. Keep each change reviewable; adjacent work needed for correctness is in scope, unrelated cleanup is not. Run the repository's appropriate checks from the declared worktree, inspect every failure and review the exact diff before committing. Do not weaken tests or substitute an unrelated green suite.

Distinguish source implemented/tested, integrated source selected, release built/selected, loaded process identity, accepted provider effect and visible user behavior. State exactly what was verified and what remains. A handoff binds the source/worktree/commit, changed unit, validation, relevant live state, remaining gate, rollback and cleanup. Keep detailed diagnostics private unless needed for review or resumption.

## Runtime maintenance and activation

Use the current upstream baseline with the tracked differentiated changes. Do not reinstall globally or revive a retired private updater. Build a separate self-contained candidate from one reviewed immutable source commit, preserve a rollback target, validate out of process and use the repository-owned activator for sealing, atomic selection, one gateway transition and post-switch verification.

Follow the current runtime-promotion runbook, including its write/network boundary, retained runtime outputs and external cache handling. Do not create a v10 admission, coordinator, operation UUID, phase driver or another lifecycle retry wrapper. Historical promotion artifacts are retention evidence, not active instructions.

Keep source, selector, service definition, loaded process and provider behavior distinct. Verify installed version/status, readiness, scheduler RPC, queue/task state and affected user paths. Native SQLite approval state is authoritative; keep a retired legacy JSON source absent. Preserve Node aliases, TCC identity and untouched acceptance windows.

Invoke the checked activator from its supported independent process boundary. Do not boot out/bootstrap the live gateway from the gateway's own process tree or stack an extra restart around activation. An ambiguous result is contained and reconciled; a rollback outcome means the predecessor recovered, not that the candidate activated.

## Schedulers, monitoring and retained state

Export or back up existing job definitions before an authorized change. Classify affected jobs as active, intentionally disabled or removed. Active commands target canonical paths or a documented host shim. Retired paths may remain historical evidence but do not belong in live integrity hard-fail expectations.

A scheduler process exiting zero proves transport, not the intended domain effect. Verify typed results, effects and delivery separately. Manual checks are labeled manual; a natural-dispatch claim requires untouched scheduled evidence. Preserve reserved natural windows and do not bulk retry ambiguous outbox entries.

Report material outcomes, failures, decisions and requested reminders. Keep no-change success silent unless the job promises a report. Resolve routine internal hygiene within authorized scope rather than asking the operator to adjudicate it. Archive terminal work through the supported nondestructive route, and never restore it into the live task root without a specific recovery decision.

## Data-pipeline troubleshooting

Choose one canonical wiring pattern and retain it until evidence disproves it. For user-generated JSON, attempt only conservative repairs of common formatting failures before escalating: unescaped newlines inside string values, or blank optional count-like values for count-type fields. Do not invent required values or silently change the meaning of valid data. Preserve the original input and identify any repair so parse success cannot conceal altered content.

Evaluate coverage against the active scoped contract, not a stale broader expectation. At each debugging checkpoint report the three distinct results: schema validity or the first concrete parse error; ingestion counts for found, ingested, invalid and skipped records; and observed coverage against the expected active scope. A successful parse does not establish ingestion, and ingestion does not establish complete coverage. These diagnostics belong in the applicable debugging result, not every ordinary agent response.

## Skill reliability

An actively used skill should state when to use it, when not to use it, its required inputs and preconditions, expected outputs or artifacts, success criteria, and failure handling or escalation. Check these contracts against the real route and current task before claiming the workflow is reusable. A named skill with an unavailable dependency has not passed its own execution requirements.

Out-of-scope skills and runbooks may supply examples; active interpretation must not depend on them. They do not create authority, replace the operator's request or excuse an incomplete deliverable. Keep operational instructions with their owning skill or runbook so a repair updates the actual execution path rather than only its description.
