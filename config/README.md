# Configuration

There are two different configuration owners:

| File | Owner and purpose |
|---|---|
| `operator.json` in the installed workspace | Shared adopter bindings for the exported Python/Node helpers: filesystem paths, account references, service identities and operational expectations. |
| `openclaw.json` in the selected native state/config location | Native OpenClaw models, sessions, plugins, tools, gateway and channels. |

The installer creates the first with minimal workspace/home bindings and renders
[openclaw.preferences.json](openclaw.preferences.json) into workspace
`config/openclaw.json`. That rendered file is a profile to merge after onboarding.
It is not a complete authenticated gateway configuration. Use the native
`config patch --dry-run` and `config patch` commands from the
[adoption guide](../docs/17-adoption-guide.md).

## Model and context preferences

The checked-in profile is an example configuration. It does not describe the
settings of every installation. The running configuration and supported user,
session and task overrides determine the effective selection.

| Setting | Value and purpose |
|---|---|
| Main model | `openai/gpt-6-astra`, OpenClaw runtime, no automatic fallback. |
| Main reasoning | `xhigh` for ordinary turns; supported conversation/task choices can override the defaults. |
| Fast mode | Global default `false`; inline, session and per-agent overrides take precedence. |
| Subagents | Delegation guidance is `suggest`; the configured child default is GPT-6 Astra/`xhigh`. Supported task overrides and native precedence below still apply. Limits remain eight concurrent subagents and four main runs. |
| Scheduled inference | This example selects GPT-6 Astra/`xhigh` in the separately owned heartbeat and dreaming fields. Saved agent-turn payloads retain their own selections; pure command jobs need no scheduler model. |
| Interactive helpers | This example explicitly selects GPT-6 Astra/`xhigh` for active memory. Utility calls use GPT-6 Astra and inherit its configured effort unless overridden. |
| Image understanding | GPT-6 Astra in `imageModel`, separate from image generation. |
| Image generation | `openai/gpt-image-2.5-flare` in `mediaModels.image`. |
| Bootstrap sizes | 180,000 characters per file and 400,000 total. These are loader limits, not a model's token window. |
| Compaction | Safeguard mode, strict identifier preservation, 48,000 recent tokens, six recent turns, explicit `low` summary effort, quality guard with one retry. |
| Compaction trigger | Native context pressure and overflow recovery; the optional active-transcript byte threshold is unset. |
| Compaction maintenance | 300-second safety window per built-in compaction model request; serial stages refresh it. Memory flush uses the native 4,000-token soft margin and an 8 MiB force-flush threshold. |
| Visibility | Routine automatic compaction notices are disabled with `notifyUser: false`. Explicit compact commands and actionable turn failures retain their normal responses. |
| Memory search | Local EmbeddingGemma model, no provider fallback; requires managed llama.cpp setup below. Native citation display is off; policy still requires support for factual claims. |
| Sessions | Per-channel peer scope; Discord, group and thread idle renewal after seven days. Thread bindings retain their separate 24-hour idle and 168-hour maximum-age limits. |

Flare is the general-purpose Images 2.5 generation option in the
[OpenAI release announcement](https://openai.com/index/introducing-chatgpt-images-2-5/).
Model availability and authentication must be established on the adopter's account.
The profile explicitly sets `agents.defaults.fastModeDefault` to `false`. Existing
per-agent, stored session or inline `/fast` overrides take precedence; inspect
`/fast status` on the actual channel path. Use `/fast default` to clear a stored
session override when the intended behavior is to inherit configuration. Its plugin
entries preserve the reference's enabled capability set; enabling a plugin does not
enroll a service.

Large bootstrap and retention settings have real context and latency costs. Verify
the compiled prompt and compaction behavior using the exact model and channel path.
Do not label a configured reasoning level or a copied memory file as runtime proof.

## Background model and existing overrides

The ordinary model needs an authenticated route with GPT-6 Astra available on the
adopter's account. Preserve provider enrollment, model metadata and credentials
when applying this preference profile. Verify the actual model and reasoning on
both an interactive request and a scheduled request; a catalog row alone proves
neither. This profile does not change the account or require a new API key. Its explicit
`openai-chatgpt-responses` model row retains GPT-6.1 Sol compatibility metadata.
The ordinary, image-understanding and utility selectors use Astra. The Sol
row declares an 872,000-token ceiling and a 272,000-token default, with text/image
input and 128,000 output tokens. Its declared efforts are low, medium, high, xhigh
and max, with temperature disabled. Verify Astra's own catalog metadata and
effective accepted selection separately. Use the authenticated route's metadata
rather than substituting a Platform API model window. Account enrollment is still
required.

Heartbeat owns its `model` and `thinking` fields; dreaming owns its model and
`phases.light/deep/rem.execution.thinking` fields. These settings require the
matching reconstructed runtime and compatible companion workers. Active memory
owns its model and effort separately. Utility calls inherit the GPT-6 Astra
model's `params.thinking: "xhigh"` unless they supply an
override. Preserve separately owned scheduler and helper settings when changing
ordinary effort. Realtime voice has its own model configuration;
specialized image generation, local embeddings and speech retain their own providers.
Autonomous heartbeat settings do not override admitted user-task continuations.
Configured failures follow their actual fallback and spending policy and report
the owning workflow's outcome rather than silently changing model or effort.

Autonomous heartbeats use `isolatedSession: true`: each run starts with a fresh
transcript while retaining its originating conversation's delivery policy.
Background checks do not reuse an interactive conversation's history or replace
its latest run status. Recorded outcomes remain available for diagnosis and
subsequent conversation context. This setting neither clears past failures nor
changes the configured heartbeat destination.

### Child model and effort

Ordinary effort resolves supported turn/session choices before defaults. Within
configured defaults, an agent's `thinkingDefault` precedes model `params.thinking`,
then the global `thinkingDefault`. Changing only the global value may leave a
per-agent or stored-session selection effective.

Native child selection follows these separate precedence chains:

| Selection | Priority, highest first |
|---|---|
| Thinking | Explicit `sessions_spawn.thinking`; requester-agent `subagents.thinking`; target-agent `subagents.thinking`; global `agents.defaults.subagents.thinking`; effective caller effort. |
| Model | Explicit spawn model; target-agent `subagents.model`; global `agents.defaults.subagents.model`; same-agent caller model when available, otherwise the target agent's primary/default chain. |

When thinking is inherited, the active caller turn takes precedence over saved
requester preferences. Lowering caller effort therefore lowers newly inherited
children unless a child setting applies. Already accepted child selections remain
stored in their sessions; changing ordinary defaults does not retune them.
Use supported task/session or spawn overrides when a particular job needs them.
`sessions_spawn.thinking` is unavailable with `visible:true`; follow the selected
route's supported interface. Inspect accepted child settings and the installed
runtime's native consumers rather than assuming every worker matches its parent.

Required fresh repository-native code review follows that repository's workflow
and its own model/effort options. Ordinary agent defaults neither waive that
review nor select the review helper's settings. Delegation remains useful for
independent work, review and continuity; `AGENTS.md` owns the execution choice.

A merge retains omitted keys. If an authorized change deliberately restores
caller inheritance, review existing global child pins before removing them with
this native deletion patch. Retain pins that the operator still intends:

```json
{
  "agents": {
    "defaults": {
      "subagents": { "model": null, "thinking": null }
    }
  }
}
```

When calling native `config.patch`, include the current `baseHash`. If the deleted
model object contains a `fallbacks` array, also pass
`replacePaths: ["agents.defaults.subagents.model.fallbacks"]` alongside `raw` and
`baseHash`; the native owner requires the exact destructive array path even when
its parent is deleted. Review and name any other reported array removal explicitly.

Review per-agent `thinkingDefault`, model parameters, stored-session and per-task
overrides through their native owners during an authorized migration; changing
defaults alone does not rewrite them. Preserve deliberate `/think ultra`
orchestration and active task ownership. Use `/think default` to restore inherited
reasoning only where intended.

Preserve deliberately selected `payload.model` and `payload.thinking` on existing
operator-owned `agentTurn` jobs; changing defaults does not rewrite payloads.
Keep job IDs, enablement, schedules, prompts, budgets and delivery settings.
System-owned skill-review, heartbeat and inference declarations retain their
native ownership and inheritance. Where that owner exposes execution-session
preferences, use supported `sessions.patch` on its exact
`agent:<agent-id>:cron:<job-id>` key instead of rewriting a projected payload;
verify intended disabled state remains unchanged. Inspect command implementations
for nested inference. See [scheduling and background work](../docs/12-scheduling-and-background-work.md).

## Local memory setup

The profile uses the native EmbeddingGemma Hugging Face URI rather than a cache
filename from an existing installation. The pinned runtime resolves this URI using
its recorded model revision and checksum. A model URI alone does not install the
managed llama.cpp service. Follow the [local-memory setup and acceptance steps](../docs/17-adoption-guide.md#configure-and-check-local-memory)
after merging preferences. Native setup supplies the server command, arguments,
loopback endpoint and embedding file while preserving the selected chat model.

## Existing compaction overrides

The profile leaves `agents.defaults.compaction.maxActiveTranscriptBytes` and
`agents.defaults.compaction.memoryFlush.softThresholdTokens` unset. Native
context-pressure compaction remains enabled. The selected `memory-core` plugin
uses a 4,000-token margin before the blocking compaction threshold for its optional
memory flush. The separate 8 MiB force-flush setting remains explicit.

A config merge preserves existing keys omitted from the profile. To remove earlier
adopter overrides of these two settings, save this native deletion patch locally,
review it, and use `config patch --file <absolute-patch-path> --dry-run` followed by
the same command without `--dry-run` against the intended native configuration:

```json
{
  "agents": {
    "defaults": {
      "compaction": {
        "maxActiveTranscriptBytes": null,
        "memoryFlush": { "softThresholdTokens": null }
      }
    }
  }
}
```

The optional byte threshold measures logical transcript storage, including runtime
control records; it is not a model-token measurement. Enable it only with a measured
storage requirement and an appropriate limit. Removing that override keeps native
token-pressure and overflow protection, retention, summary quality checks and
reasoning preferences in place. See [memory and context](../docs/08-memory-and-context.md).

## Shared helper bindings

Python helpers read the shared binding contract through
`workspace/scripts/operator_contract.py`; Node adapters use
`workspace/scripts/routing_operator_bindings.mjs`. The default file is workspace
`operator.json`; an absolute
`OPENCLAW_OPERATOR_CONFIG` overrides it. Loading is read-only. Apart from the current
home and script-derived workspace, required settings have no source-host defaults.

Fields are read only when the selected operation needs them. Missing, placeholder or
malformed required settings raise a configuration error. Paths must be absolute and
must not contain shell expansion or parent traversal. Selectors are not blindly
resolved through symlinks because their own identity can matter.

A registry may reference one complete JSON value:

```json
{
  "account": "${operator:identifiers.accounts.personal_google}"
}
```

The reader preserves the referenced JSON type. References cannot be embedded into
arbitrary strings or used as shell expansion. An adapter constructs an argument vector
from the validated settings. Do not add another environment-file parser or duplicate
account setting to get around a missing binding.

Keep credentials in native/provider stores or permission-restricted files referenced
by the settings. The operator contract contains identities and references, not secret
values. Configure only the integrations being adopted and leave other routes visibly
unverified until their actual probes and effect checks pass.

The worker profile sets `agents.defaults.subagents.runTimeoutSeconds: 0`, so an open-ended delegated task has no arbitrary worker deadline. A positive per-run limit is reserved for an explicit operator timebox. Individual I/O timeouts and stalled-work recovery remain separate controls.

## Permission and host choices

The profile carries the operator's full-access execution choices: `tools.profile`
and `tools.exec.mode` are `full`; `tools.fs.workspaceOnly` and
`tools.exec.applyPatch.workspaceOnly` are `false`; `agents.defaults.sandbox.mode`
is `off`. Ordinary file access and patch access are distinct controls.

`agents.defaults.ownerPermissionMode: "full"` applies at native authenticated owner
admission and is inherited by delegated workers. It does not store an owner grant
in a shared-channel session or overwrite an explicit session permission mode.
`agents.defaults.elevatedDefault: "full"` is a separate execution preference; it does
not identify an owner or replace native elevated-tool authorization.

Keep owner identities in the adopter's native configuration. For example, replace
the parameter below with the intended Discord user's stable ID before applying it:

```json
{
  "commands": {
    "ownerAllowFrom": ["discord:<owner-user-id>"]
  }
}
```

The preferences profile omits `commands.ownerAllowFrom` so merging it preserves
onboarding's verified identity bindings. A display name, channel membership or
channel/guild command allowlist does not establish owner identity. Bind other native
authentication, remote exposure, browser permissions and channel access to the
installation; none are copied from the source host. Review the native
[authentication guide](https://docs.openclaw.ai/gateway/authentication) and the pinned
source's configuration schema.

For service changes, keep the interpreter, state root, configuration path and selected
release consistent across the CLI, gateway, node and scheduler. Activation expects
adopter-derived authentication order and extension bindings; its runbook describes
how to establish those inputs without recording token values.

## Scoped maintenance warnings and cache cleanup

`maintenance.accepted_security_findings` keeps the existing exact severity/title
acceptance for ordinary findings. The Discord broad-member warning additionally
requires `maintenance.accepted_company_posture` and fresh scope proof; adding its
check ID or rendered title to the ordinary mapping cannot bypass that proof.
The optional company posture is absent or disabled by default. The example's
CompanyAlpha identities are fictional: replace every identity, the workspace path
and read-only tool namespace before explicitly enabling an accepted posture.
This records a reporting disposition; it does not grant access or change native
Discord, agent, memory or tool configuration.

An enabled posture binds one guild, its wildcard and one exact channel, one owner,
one company agent/workspace, exactly three distinct named engineering senders,
and one read-only tool namespace. The health helper accepts only the warning's
exact structured target detail, at WARN severity, after two equal bounded native
config reads prove mention gating, company-only memory, workspace file access,
private-session restrictions and owner-only elevation. It accepts either the
read-only sender predecessor or the exact three-engineer tool successor. Changed
accounts, targets, sender privileges, private-context controls, missing evidence,
text-only audit output and higher or unknown severity remain blocking. Daily and
weekly deep audits each collect their own fresh proof. Malformed enabled bindings
fail explicitly instead of silently accepting the warning.

The storage helper recognizes the audited public pnpm metadata layouts under
`Library/Caches/pnpm/v11`, including the exact encoded npm registry directory,
and dated OpenClaw Vitest generated-module caches under `.cache`. Only these
new developer-cache kinds use 48-hour retention. Existing package downloads and
npm logs retain their 14-day rule. Root and every child must be old enough, match
the admitted format and inode inventory, and have no active producer or open
handles. Cleanup rechecks them after capture and validates each leaf immediately
before removal. Package stores, private registries, application/browser caches,
source files and unknown layouts are outside this scope.

## Field inventory and staged host files

[operator.example.json](operator.example.json) lists the bindings consumed by the
exported helpers and host templates. It deliberately contains unresolved values.
A new workspace does not need every integration configured. Fill the fields required
by the selected route or operational family; the consuming helper validates them.
A required-mode session-reservation installation additionally supplies its exact
`runtime.session_reservations_sha256`; the fresh-install example declares absence.

Render the host definitions into a separate directory before installing any of them:

```bash
python3 /path/to/openclaw-control-plane/scripts/materialize_host.py /absolute/new-workspace /absolute/host-review --dry-run
python3 /path/to/openclaw-control-plane/scripts/materialize_host.py /absolute/new-workspace /absolute/host-review
```

This uses the explicit workspace `host-templates.json` manifest. Outputs include
rendered scheduler JSON, launchd plists and guard contracts, with hashes in a
materialization manifest. The command does not load launch agents or create scheduled
jobs. Review their paths, grants, cadence and disabled state before following the
relevant host runbook.
