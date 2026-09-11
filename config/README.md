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

The profile carries these reference choices:

| Setting | Value and purpose |
|---|---|
| Main model | `openai/gpt-6-astra`, OpenClaw runtime, no automatic fallback. |
| Main reasoning | `ultra`; inspect the actual provider request to verify normalization and execution. |
| Fast mode | Global default `false`; inline, session and per-agent overrides take precedence. |
| Alternate model | `openai/gpt-5.6-sol` mapped to the Codex runtime when explicitly selected. |
| Subagents | Astra with `max` reasoning; maximum eight concurrent subagents, four main runs. |
| Image understanding | Astra in `imageModel`. |
| Image generation | `openai/gpt-image-2.5-flare` in `mediaModels.image`. |
| Bootstrap sizes | 180,000 characters per file and 400,000 total. These are loader limits, not a model's token window. |
| Compaction | Safeguard mode, strict identifier preservation, 120,000 recent tokens, six recent turns, quality guard with one retry. |
| Compaction trigger | Native context pressure and overflow recovery; the optional active-transcript byte threshold is unset. |
| Compaction maintenance | 1,800-second timeout; enabled memory flush with the native 4,000-token soft margin and an 8 MiB force-flush threshold. |
| Visibility | Native compaction notification enabled; path-specific background behavior still needs verification. |
| Memory search | Local EmbeddingGemma model, no provider fallback; requires managed llama.cpp setup below. Native citation display is off; policy still requires support for factual claims. |
| Sessions | Per-channel peer scope, long-lived Discord sessions and explicit thread binding limits. |

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

## Permission and host choices

Gateway authentication, remote exposure, execution approvals, filesystem grants,
browser profile permissions and channel allowlists are deliberate adopter choices.
They are not inherited from the source host. Review the native
[authentication guide](https://docs.openclaw.ai/gateway/authentication) and the pinned
source's configuration schema for those choices.

For service changes, keep the interpreter, state root, configuration path and selected
release consistent across the CLI, gateway, node and scheduler. Activation expects
adopter-derived authentication order and extension bindings; its runbook describes
how to establish those inputs without recording token values.

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
