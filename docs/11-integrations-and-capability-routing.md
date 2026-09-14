# Integrations and capability routing

A request can involve several sources and one destination. “Add the Company Alpha and Company Beta calls to my calendar” may require two organizations' invitation sources, an update in a third account and a final answer in the originating conversation. The agent must discover the relevant sources without making the operator enumerate every connector, while keeping their identities and permissions separate.

The reference uses structured routes and a supported resolver for that work. A route identifies an account, workspace, principal and mechanism; it is not authorization. The authenticated request supplies authority, and the provider's current response establishes whether the operation actually worked.

## A concrete multi-account example

The example operator has personal accounts, Company Alpha accounts and Company Beta accounts. Company Alpha has two distinct mail identities: `operator@company-alpha.example` and `coordinator@company-alpha.example`. The first may be the intended sender while the second holds the current invitation. Reading both relevant mailboxes is useful; using whichever one answered first to send a message is a routing error.

| Question | Source of the decision |
|---|---|
| Which company's facts matter? | Trusted request/session context and the registered portfolio |
| Which mailboxes may contain the current invitation? | That portfolio's bounded authoritative read set |
| Which account should send the reply? | The exact requested sender or an unambiguous registered account binding |
| Where should the event be written? | The operator's destination preference and the requested calendar/account |
| Did the event actually contain the joining link? | Readback from the written destination, compared with the current invitation |

A read set does not force a question simply because it contains multiple accounts. The agent asks when unresolved ambiguity would materially change the answer, destination or risk, after bounded discovery has used the facts already available.

## Registry and resolver

`registry/integration_routes.json` is the current structured route owner. Its top-level records include system aliases, routing defaults, opaque credential handles, portfolios, portfolio routes and concrete routes. The route records retain the system, provider adapter, required account/principal, supported operations, capability scope, native lane, probe/status identifiers and browser fallbacks.

The supported `scripts/resolve_capability.py` joins those records with the capability registry contract and applicable status/probe data. Its provider-specific guards matter: exporting only a small generic selector would not reproduce the same account and operation checks. The adoption bundle must include the resolver's dependency closure and consistently rendered route identities.

The interface accepts typed facts:

```text
python3 scripts/resolve_capability.py \
  --compact \
  --system gmail \
  --intent read_search \
  --required-operation gmail-search \
  --account operator@company-alpha.example \
  --portfolio company-alpha
```

The operation spelling must match the installed route's supported operation list. The example demonstrates the argument contract; it does not provision the fictional account or grant access. Additional selectors are `--workspace`, `--network` and `--principal`. `--context` is a legacy exact route tag, not a place to paste the user prompt.

For ordinary route selection, `--compact` retains the selected lanes, account/workspace bindings, requested operation, probe outcomes, constraints and execution/fallback guards. It omits the full candidate/status catalogue and unrelated operations. Omit the flag for full diagnostics. This is a presentation option: `resolve()` and the default JSON contract remain unchanged, as do route selection, authority, probe execution and exit status. The selected lane information is sufficient to perform the next bounded operation without reprinting the full registry.

An exact account wins over a portfolio label or legacy context. Explicit workspace, network and principal facts must still agree. Selectors narrow candidates; they never authorize the operation. Do not infer them through keyword matching, a channel name, a persistent portfolio mode or a convenient signed-in tab.

The resolver does not perform the requested provider mutation. `--run-exact-probe` is an optional bounded read-only diagnostic for supported registry-bound probes. `--native-operation-support unsupported` records a real provider API gap; `--authenticated-ui-required` identifies work whose required state is available only in an authenticated interface. These facts guide the supported fallback path rather than introducing a second approval vocabulary.

## Readiness is useful evidence, not an execution veto

Keep three states separate: a declared route, a stored readiness observation and a current operation. A status record can be stale, and a general “Google is connected” result cannot identify which account answered. Probe the exact route when useful and retain the account identity with the result.

A missing or failed readiness probe does not by itself make a valid authorized operation forbidden. Inspect what actually failed. A route may support the requested operation even if a diagnostic is stale; conversely, a successful identity probe may prove no access to the requested document. The real operation and authoritative readback settle the effect.

Do not call an entire capability unavailable after trying one mismatched lane. Continue through the next registered supported route when that preserves the account, target and authority. A specific adapter stop condition takes precedence over the generic fallback order. Repeated identical calls without changed evidence are not a recovery strategy.

## Supported fallback order

Prefer the native/upstream capability, then a maintained API/integration or supported CLI, then the registered managed browser, then host UI where necessary. The sequence is about a verifiable way to complete this operation. It does not permit transferring cookies, switching accounts or bypassing a native access boundary.

An existing signed-in browser can be the correct route. Inspect its actual account and current state; do not reauthenticate blindly or assume that an open tab belongs to the task. Human-presence challenges remain human steps, and credentials stay inside the supported browser/profile or opaque broker.

Source-only Discord routes are a useful case. A registered company guild may be readable through the existing account-bound CLI while remaining outside the assistant's inbound bindings. A delegated message-tool rejection does not prove that supported source route is unavailable. Use the exact registered guild/channel bounds; do not add inbound bindings or post there merely to make a read easier.

## Exact objects and current content

When the operator supplies a URL or identifier, bind that object first. Check its selected account, owner/sender, title/subject and date where available. Related discovery can help locate it but cannot silently substitute another object. This rule applies before drafting as well as before a final write: the wrong source can produce a confidently wrong draft.

A Gmail browser fragment is not necessarily an API message ID. Resolve it through the authenticated browser or a supported provider mapping before invoking message/thread reads. An inaccessible direct target stays unresolved rather than being replaced with a search result that merely concerns the same topic.

For Notion, read current page blocks or data-source records through the registered bounded reader. For an issue, inspect the exact issue and relevant newer comments. For analytics, discover visible saved questions and use the supported bounded saved-query operation. Current provider readers are preferable to recursive filesystem searches of old exports. A saved-query route does not silently authorize arbitrary SQL or an analytics mutation.

## Subject-scoped personal data

Personal recall uses the registered Personal Data Project reader when a missing or changing fact needs database evidence. Stable preferences can come from established context. For a current or date-scoped claim, bind the actual subject, database/schema, table and time window before reading; profile-version records and daily context answer different questions.

The [mechanics template](../templates/TOOLS.example.md#subject-scoped-postgresql-recall) preserves the checked-in-reader-first route, lightweight identity/readiness checks, narrow read-only PostgreSQL fallback, and the separate authority required for writes. A DSN's presence or a successful connectivity probe does not prove that the requested subject data is present. Report a bounded missing-data result without exposing credentials or unrelated rows.

## Calendar: different source and destination

Apple Calendar/iCloud is the example destination preference. Invitations in Gmail or Google Calendar remain valid sources. The assistant searches the relevant portfolio accounts, inspects the current invitation and any update/cancellation, and preserves exact start/end/timezone, joining URL, location and relevant notes. It does not infer a one-hour duration merely because chat supplied a start time.

Read-only workers may gather the invitation sources; one owner performs the Calendar writes. Before mutation, inspect matching destination entries. Preserve the existing event identity and unrelated notes, updating only the needed details. Create a missing meeting once. Then read back every requested meeting and compare its fields with the source.

A Calendar listing that returns title/start/end may omit the dedicated URL field. Use the supported adapter's corresponding property read when necessary; do not assume the URL from a successful listing. If the registered availability helper returns blocked, timed out or incomplete, report that boundary instead of automatically inspecting raw Calendar databases, switching frameworks or changing the destination provider. The [mechanics template](../templates/TOOLS.example.md#calendar) carries the complete procedure.

## External writes and delivery

Before creating a document, confirm the intended account and parent folder/shared drive. A working personal route is not a substitute for a company destination. Read the current object and choose create, revise, replace, append or delete from the actual requested outcome. Do not create test objects to establish access when a supported read can do so.

After a write, read back the same object or use an equally authoritative path. Verify content, structure, account, placement and relevant collaboration state. A returned link or a local copy of the request is not effect proof. After a possible message send or transaction submission, reconcile the destination before retrying.

A same-origin result to the requester is part of the conversation. Third-party communication requires an instruction that names or plainly implies that recipient/package. If it emerges later, prepare the exact sender, recipients, body and attachments before asking one natural question. Source membership and available credentials never grant a new recipient.

## OAuth state and configuration

Model selection and authentication profiles are separate configuration concerns. A profile order chooses among enrolled accounts without changing the requested model or reasoning setting. Use the native provider's failure classification, rotation and cooldown behavior; do not restore a second `auth.cooldowns` system around it.

Credential values remain in sanctioned state and SecretRef/environment/broker lanes. Public configuration contains references and selectable account placeholders, not OAuth tokens, browser profiles or live host identifiers. A successful chooser or login screen does not prove that refreshed state persists across subsequent runtime use. Acceptance must check the supported stored state, the provider operation and the later use relevant to account rotation.

The reference distinguishes the main reasoning model, its image-understanding capability and the separate Images 2.5 generation route. The [configuration and runtime chapters](10-runtime-releases-and-promotion.md) describe the pinned model settings. An unavailable generation route is not permission to silently switch to an older model or a new billing mechanism.

## Verifying an adopted route

Use an isolated fictional profile for structural/rendering tests, then the adopter's exact authorized accounts for real acceptance. Confirm resolver output, provider identity, current object access and the requested operation. For a write, verify the real object afterward. For a user-facing workflow, verify the delivered answer as well.

Keep any remaining distinction explicit: schema-valid, helper executable, provider authenticated, effect accepted and user-visible behavior accepted. The route registry helps choose a path; it cannot compress those observations into a single green status.
