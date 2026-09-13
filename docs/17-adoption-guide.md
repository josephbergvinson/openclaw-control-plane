# Adoption guide

This guide builds a similar installation from the pinned source and the exported
workspace. It covers the runtime, policy, integrations and host operations. Adopting
only the Markdown gives the model instructions; it does not install the runtime
changes or operational helpers.

Use a separate directory for the reference repository, runtime source and new
workspace. A service must never point at the disposable clone used to test a patch.
The examples below use explicit paths; replace them with the intended local paths.

## 1. Reconstruct and check the runtime

Follow [the runtime package](../runtime/README.md) to clone official tag `v2026.9.3`,
verify and apply the patch, and record the resulting tree in a local commit. The
expected tree is `7df03421b5eb7d267c111c24321b7f333883b5e3`.

Use Node.js 24.16.0 and pnpm 12.3.4. Run the documented frozen dependency installation,
native changed-source checks and build from that checkout. The source package
contains the complete delta from the official release. A new commit hash is expected
because the adopter supplies its author and timestamp; the tree is the source check.

Read the reconstructed source's `AGENTS.md`, `docs/testing.md` and command-specific
CLI documentation before invoking platform checks. The online
[getting-started guide](https://docs.openclaw.ai/start/getting-started) is useful
background, but the pinned source defines the exact version being installed.

## 2. Install a separate workspace

The installer copies the full `workspace/` implementation, policy templates and the
nested analytical-workbook template. It also installs the reference chapters, diagrams,
runtime package and supporting documentation, rebasing their links to the installed
layout. `REFERENCE.md` is the local entry point. It writes an installation manifest, a private
`operator.json` and a rendered preferences file. It refuses an existing destination.
It neither installs dependencies nor invokes providers or host services.
The installed `.gitignore` excludes private operator bindings, generated preferences
and installation receipts, private memory, operational artifacts and caches. Source
policies, adapters, registries and the reference configuration remain trackable when
the workspace is initialized as a local Git checkout. Review the staged files before
committing; publishing an adopted workspace requires its own audience/privacy review.

```bash
python3 /path/to/openclaw-control-plane/scripts/install_workspace.py /absolute/new-workspace --dry-run
python3 /path/to/openclaw-control-plane/scripts/install_workspace.py /absolute/new-workspace
```

A minimal install derives only the workspace and current home. Add the relevant
host and account settings to its private `operator.json` before using a helper.
Alternatively prepare that file first and pass its absolute path with
`--operator-config`. Its `paths.workspace` must match the destination.

`operator.json` is the shared binding for paths, identities and service expectations.
A helper reads only the settings it needs. Missing credentials or account bindings
must block that route without preventing unrelated configured routes. Registry
values such as `${operator:identifiers.accounts.personal_google}` resolve as whole
JSON values; they are not shell expressions. See [configuration](../config/README.md).

Read the installed AGENTS, SOUL, USER and TOOLS files as substantive working
instructions. Adapt the example companies, source ownership and standing grants to
the intended operator. Keep current personal facts in private memory and source
systems. Do not convert examples into assertions that integrations are ready.

## 3. Initialize native state and connect a model

Choose one state directory and keep it consistent for the CLI, gateway, node and
scheduled helpers. Use `OPENCLAW_STATE_DIR` and, when needed,
`OPENCLAW_CONFIG_PATH` to bind native commands to it. The workspace's
`config/openclaw.json` is a **preferences profile**, not a complete authenticated
installation: it intentionally has no gateway token, channel IDs or provider secrets.

For the commands below, set `source_checkout` to the absolute reconstructed and built
checkout, `node_executable` to the pinned Node binary, and `agent_workspace` to the
installed workspace. Set `OPENCLAW_STATE_DIR` to the chosen state root and
`OPENCLAW_CONFIG_PATH` to its `openclaw.json`; the activation owner requires that
same config location. Run native onboarding explicitly from the built source:

```bash
unset OPENCLAW_PROFILE
export OPENCLAW_LAUNCHD_LABEL=ai.openclaw.gateway
"$node_executable" "$source_checkout/openclaw.mjs" onboard --classic --no-install-daemon
```

Select the intended local gateway, workspace and provider accounts. The explicit
option leaves gateway service installation for the reviewed sequence below. Targeted
`configure` is also supported when continuing an already initialized installation.
Do not use reset flags on an existing installation.

After onboarding, apply the rendered preferences through the native merge operation.
This preserves unrelated gateway and channel configuration rather than replacing the
whole file:

```bash
"$node_executable" "$source_checkout/openclaw.mjs" config patch --file "$agent_workspace/config/openclaw.json" --dry-run
"$node_executable" "$source_checkout/openclaw.mjs" config patch --file "$agent_workspace/config/openclaw.json"
"$node_executable" "$source_checkout/openclaw.mjs" config validate --json
```

These commands use the reconstructed installation explicitly and must keep the same
state directory as onboarding. Inspect the dry-run diff: arrays in a config
patch replace that setting's array. Model fallbacks and external plugin paths in the
reference are deliberately empty. Keep any different adopter requirement as an
explicit reviewed change. Omitted profile keys remain unchanged in an existing
configuration. If adopting the reference's native compaction triggers, review the
[two-key deletion patch](../config/README.md#existing-compaction-overrides) to remove
any earlier byte-threshold and memory-flush-margin overrides explicitly.

The profile uses `openai/gpt-6-astra` for the main agent and image understanding,
`ultra` for main reasoning and `max` for subagents. Generation uses
`openai/gpt-image-2.5-flare`. It includes the alternate Codex-backed model mapping,
local memory search, compaction and session settings described in
[configuration](../config/README.md). Check account availability and native runtime
selection on an actual request; a saved model ID is not proof of execution.

### Configure and check local memory

The profile enables local embeddings with no provider fallback. Complete the
bundled llama.cpp setup using the same pinned CLI and native state directory:

```bash
"$node_executable" "$source_checkout/openclaw.mjs" configure --section model
```

Select **Local llama.cpp**, then **Managed local server**. Decline the chat-model
proposal and accept the separate **embedding-only setup**. Native setup installs
the managed server and embedding model and writes its command, arguments and
loopback endpoint. The selected Astra chat model should remain unchanged. The
reference supplies the canonical EmbeddingGemma URI; a bare cache filename from
another installation would require that file to exist locally. See the
[official llama.cpp setup guide](https://docs.openclaw.ai/plugins/llama-cpp).

Verify the resulting configuration and exercise a known, nonsecret memory source:

```bash
"$node_executable" "$source_checkout/openclaw.mjs" config validate --json
"$node_executable" "$source_checkout/openclaw.mjs" config get agents.defaults.model.primary
"$node_executable" "$source_checkout/openclaw.mjs" config get models.providers.llama-cpp.localService --json
"$node_executable" "$source_checkout/openclaw.mjs" memory status --agent main --deep --json
"$node_executable" "$source_checkout/openclaw.mjs" memory index --agent main
"$node_executable" "$source_checkout/openclaw.mjs" memory search "QUESTION ABOUT A KNOWN NONSECRET MEMORY FACT" --agent main --max-results 3 --json
```

Confirm that Astra remains selected, deep status reports local embedding readiness,
and retrieval returns the expected source with no stale or unavailable status. An
empty corpus cannot prove retrieval. The [memory CLI guide](https://docs.openclaw.ai/cli/memory)
describes indexing and status. These steps initialize the adopter's local service;
they are separate from this repository's offline fixture validation.

The profile also sets the global fast-mode default to off. Per-agent, stored session
and inline overrides can supersede it. Check `/fast status` in the channel used for
acceptance; `/fast default` clears a stored session override when configuration
should apply. That status reports the resolved policy, while request evidence is
needed to establish what an upstream provider actually received.

## 4. Enroll accounts and bind preservation state

Connect each provider using its supported credential store or OAuth flow. The
reference patch preserves native refresh state and serializes compatible refresh
attempts. Keep account selection and current token state in that native owner.
Copying an old runtime credential file over a newer store can reintroduce stale state.
The [OAuth documentation](https://docs.openclaw.ai/concepts/oauth) describes the
upstream model; the [source chapter](20-runtime-source-changes.md) identifies the
custom persistence changes.

Follow the [activation runbook](../workspace/runbooks/runtime-activation.md#native-state-prerequisites)
to initialize native execution approvals, select the persisted OpenAI account order,
and derive its count/digest from the read-only native inspector. Declare the historical
session-reservation mode explicitly; a new installation ordinarily declares absence.
Do not create an empty historical file to satisfy the old host's condition.

For each connected account, verify identity and a permitted read now; after the first
installation below, verify persistence across a service restart and the next actual refresh. When rotating model accounts, retain the
other accounts' state and verify that a subsequent request uses the intended identity.
A successful login screen or one completion is not refresh acceptance. Never print
refresh tokens, copy them into policy files or store them in the public reference.

## 5. Bind integrations and device capabilities

Use the exported capability resolver and registries. Supply the selected route's
account, service and executable references in `operator.json`; keep secret values in
the provider store or restricted secret file named by the route. Run a read-only probe
for the actual operation and identity, then verify the result in the authoritative
source. Do not borrow another account's readiness status.

The workspace includes procedures for company collaboration tools, personal sources,
Apple applications, browser work and wallet/network-specific tooling. Company Alpha's
mainnet staging site remains mainnet: a staging label never grants testnet treatment.
Wallet mutations require the intended account, network and operation to be established.

For native applications, grant the required macOS permissions to the actual executable
or application used by the route. For phone/node access, pair the intended node and
operator roles separately where the client requires them. Test the actual capability;
a tunnel or visible mirrored screen establishes connectivity, not every device grant.

Connect a chat channel through native configuration. Use the operator's own allowlists
and destinations. In that channel, run an ordinary request, inspect its result in the
target application and confirm that one coherent final response is visible.

## 6. Establish the first installed runtime

The activator promotes an already installed runtime. This section creates and verifies
that real first installation; it does not fabricate a predecessor receipt. Perform
these steps from a separate local terminal as the configured macOS operator. If a
service already exists, inspect and preserve it and use the upgrade path instead of
applying this first-install recipe over it.

### Package and seal the initial release

Choose a new physical `initial_release` directly under `paths.runtime_releases_root`,
with a name such as `openclaw-2026.9.3-initial`. Set `source_commit` to the complete
commit recorded after reconstruction, not the original host's commit. This must match
the build's `dist/build-info.json`. The already completed frozen install and build
must include the full dependencies and required bundled plugins.

Keep the completed source checkout exclusive while staging. The exported staging
helper requires its clean Git HEAD and `dist/build-info.json` to match `source_commit`.
It copies the completed build with independent regular-file identities, retaining
internal relative symlinks and excluding the Git database and known build caches.
Generated executable `node_modules/.bin` shims that contain the exact source path
are relocated to the candidate path; ordinary source files are not rewritten.
Escaping, absolute or dangling symlinks fail staging.

If local verification placed regenerable caches inside the source checkout, identify
their top-level directory explicitly, for example by adding
`--exclude-root .reference-build` to the staging command. The option is repeatable,
accepts only simple basenames, and rejects protected build/dependency roots and any
root containing tracked source. The receipt records these extra exclusions. It does
not exclude a nested directory merely because it has the same name.

```bash
python3 "$agent_workspace/scripts/stage_runtime_release.py" \
  --source "$source_checkout" --candidate "$initial_release" \
  --source-commit "$source_commit" --receipt "$staging_receipt"
export OPENCLAW_OPERATOR_CONFIG="$agent_workspace/operator.json"
python3 "$agent_workspace/scripts/openclaw_runtime_activate.py" seal \
  --candidate-release "$initial_release" --source-commit "$source_commit" \
  --output "$initial_seal"
```

Both `staging_receipt` and `initial_seal` are new absolute paths outside the source
and release directories. The staging helper verifies the copied files, rechecks source
identity and contents, makes the candidate read-only and records file and shim hashes.
An interrupted or failed copy remains visible for inspection and is not overwritten
by a retry. Staging does not execute or seal the candidate.

The activator independently
checks the tree, source commit, entrypoints, plugins and every dependency symlink; it
refuses links into the build checkout or shared stores. A failed seal is not a release:
inspect the reported closure issue and rebuild the candidate through the supported
package/build path. Do not weaken the seal or point a service at an unsealed copy.
Retain the source checkout as source, not as a second running installation.

### Stage exact gateway and node definitions

Populate the path and runtime fields listed in the
[activation runbook](../workspace/runbooks/runtime-activation.md#explicit-installation-bindings).
`paths.host_home` must match `identifiers.host_user`. `paths.node_binary` names the
pinned executable; `paths.runtime_node_alias` names a symlink to its grandparent.
The CLI/package/current links each have a distinct declared role.
This first-install recipe uses the native default labels `ai.openclaw.gateway`
and `ai.openclaw.node`, with their matching filenames under
`host_home/Library/LaunchAgents`. The pinned native node controller always selects
`ai.openclaw.node`; arbitrary custom labels would make its status/stop commands
inspect a different service. The staging mode rejects that mismatch. Keep
`OPENCLAW_PROFILE` unset and `OPENCLAW_LAUNCHD_LABEL=ai.openclaw.gateway` for these
native lifecycle commands. Existing custom installations require an explicit
service-identity review before adopting the promotion contract.

```bash
python3 "$agent_workspace/scripts/materialize_host.py" \
  "$agent_workspace" "$runtime_review_directory" --runtime-services --dry-run
python3 "$agent_workspace/scripts/materialize_host.py" \
  "$agent_workspace" "$runtime_review_directory" --runtime-services
```

The review directory must be new. This mode stages only the gateway and node, so an
unconfigured backup account cannot block first runtime installation. Both definitions
are disabled, with `RunAtLoad=false` and `KeepAlive=false`. Their arguments already use
the exact pinned Node and package selector expected by the activator. They bind native
config/state, operator config, working directory and log paths. No service or link is
installed or started by materialization.

### Install the reviewed selectors and definitions

Set the following shell variables from the corresponding private operator bindings:
`current_link`, `package_link`, `cli_link`, `node_alias`, `gateway_plist`, `node_plist`,
`gateway_label` and `node_label`. `cli_link` is `paths.openclaw_cli`. Verify that the
state root and its config already exist and that both launchd service labels are
absent. The new selector/link and plist paths must be absent, including dangling
symlinks. The following bounded preparation uses exclusive operations and refuses any
existing destination; it never replaces a working installation:

```bash
python3 - "$agent_workspace" "$initial_release" "$runtime_review_directory" <<'PYCODE'
import importlib.util, os, pathlib, plistlib, sys
workspace, release, staged = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('operator_contract', workspace / 'scripts/operator_contract.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
c = module.load_operator_contract(workspace / 'operator.json')
node = c.require_path('paths.node_binary')
current = c.require_path('paths.runtime_current_link')
links = {
    current: release,
    c.require_path('paths.runtime_package_link'): current,
    c.require_path('paths.openclaw_cli'): current / 'openclaw.mjs',
    c.require_path('paths.runtime_node_alias'): node.parent.parent,
}
if len(links) != 4:
    raise SystemExit('The four first-install selector/support paths must be distinct')
plists = {}
for role in ('gateway', 'node'):
    target = c.require_path(f'paths.{role}_plist')
    value = plistlib.loads((staged / 'launchd' / target.name).read_bytes())
    value.update(Disabled=False, RunAtLoad=True, KeepAlive=True)
    plists[target] = plistlib.dumps(value, sort_keys=True)
if len(plists) != 2 or set(links) & set(plists):
    raise SystemExit('The two service definition paths must be distinct from each other and all links')
for target in (*links, *plists):
    if os.path.lexists(target):
        raise SystemExit(f'First-install destination already exists: {target}')
for target, destination in links.items():
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(destination)
for target, payload in plists.items():
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
(c.require_path('paths.state_root') / 'logs').mkdir(mode=0o700, exist_ok=True)
PYCODE
```

This deliberately enables only the two reviewed runtime definitions. A partial file
preparation failure stays visible for inspection and does not start anything. Verify
all four literal symlink targets, plist contents and their `0600` modes. The gateway
must have `WorkingDirectory=host_home`, `RunAtLoad=true`, `KeepAlive=true` and the exact
`gateway --port` vector; the node uses `node run --host 127.0.0.1 --port` with the same
port. Both use external supervisor/repair mode. Native node authentication resolves
from the initialized local gateway configuration; do not paste tokens into a plist.

### Start and qualify the real predecessor

After checking the exact installed files, bootstrap each service once from the local
GUI user's domain. These are actual service starts, unlike the preceding staging:

```bash
launchctl enable "gui/$(id -u)/$gateway_label"
launchctl bootstrap "gui/$(id -u)" "$gateway_plist"
"$node_executable" "$cli_link" gateway status --json
"$node_executable" "$cli_link" gateway call health --json
launchctl enable "gui/$(id -u)/$node_label"
launchctl bootstrap "gui/$(id -u)" "$node_plist"
"$node_executable" "$cli_link" node status --json
"$node_executable" "$cli_link" devices list --json
"$node_executable" "$cli_link" nodes status --json
```

If the new node needs pairing, inspect the exact pending identity and use native
`devices approve <requestId>` for that request, then repeat the status read. Do not
approve an unrelated pending device. Inspect loaded launchd arguments, working
directory, process executable/entrypoint, selected release, health endpoints and the
actual node connection. Retain a dated first-install record and the seal, then run a
small authorized model/channel request and verify its result. That working installed
release and state are now the predecessor for a subsequent promotion. No activation
success receipt is claimed for this separate first-install operation.

### Use the activator for later promotions

For a later candidate, retain this healthy predecessor, build and seal the new release,
and drain active work. Stop gateway and node through their native `stop --json`
commands from the separate terminal. The activator independently requires both
services and the gateway listener to be absent. Then capture the real stopped state:

```bash
"$node_executable" "$cli_link" node stop --json
"$node_executable" "$cli_link" gateway stop --json
python3 "$agent_workspace/scripts/openclaw_runtime_activate.py" snapshot \
  --output-root "$snapshot_directory" --manifest "$snapshot_directory/stopped-snapshot.json" \
  --candidate-release "$candidate_release" --expected-commit "$candidate_commit" \
  --candidate-seal "$candidate_seal"
python3 "$agent_workspace/scripts/openclaw_runtime_activate.py" activate \
  --candidate-release "$candidate_release" --expected-commit "$candidate_commit" \
  --candidate-seal "$candidate_seal" \
  --stopped-snapshot-manifest "$snapshot_directory/stopped-snapshot.json"
```

All artifact paths are new absolute paths. A version upgrade additionally requires
`--upgrade-config` with its reviewed immutable target config; omit it for the
same-version case shown. Follow the [activation runbook](../workspace/runbooks/runtime-activation.md)
for exact preservation, consumed-start recovery and receipt retirement. Do not
bootstrap manually after a consumed activation failure.

If adopting the Journal workflow, complete its [native capture acceptance and
permission binding](../workspace/runbooks/runtime-activation.md#journal-capture-acceptance-and-permission-continuity)
after the initial activation. Configure the resulting private binding for later
activations. Subsequent process or activation changes require fresh capture
evidence before capability readiness can be claimed; this does not require
replaying the scheduled Journal synchronization job.

### Stage maintenance separately

For the configured host operations, run `materialize_host.py` without
`--runtime-services` into another new review directory. Every staged LaunchAgent has
`Disabled=true`, and it omits `RunAtLoad` or sets it false; retired definitions retain
no launch triggers. Inspect each interpreter, working directory, environment and
log path before separately enabling an intended service or schedule. Select one
scheduler owner for each recurring purpose.

Follow [scheduling](12-scheduling-and-background-work.md) and
[host operations](19-host-operations-and-backups.md). Run a permitted manual fixture or
bounded acceptance first, enable the intended schedule, and record a separately
identified naturally scheduled result. Do not schedule an old local backup producer
alongside Backblaze merely because both helpers are present.

Backblaze requires native enrollment, selected volumes and an actual upload/restore
qualification. Local archives require metadata-preserving extraction and application
readability. Docker/OrbStack requires the correct engine context and a tested export
and reconstruction path. Keep these recovery claims separate.

## 7. Test the experience

Use ordinary prompts with independently established ground truth. Useful examples:

- “Add today's Company Alpha and Company Beta calls to Work Calendar.” Verify exact
  invitations, timezone, duration, conference details and absence of duplicates.
- “Check the latest staging changes and tell me what still needs fixing.” Establish
  the site's actual network and source revision before any transaction.
- “What did I eat three weeks ago?” Require dated source evidence or an honest gap.
- “Write a considered essay about the tradeoffs in this design.” Review argument,
  source support and prose; a completed file alone does not establish quality.
- Start a real goal spanning tool calls and context pressure. Check continued work,
  task state, cancellation when requested, and visible completion or a specific blocker.

Confirm reasoning and runtime selection from request evidence, not the preference
file. Check progress during long tools, foreground compaction notification and a
terminal result on the actual channel path. Repeating an already-correct operation
tests idempotency; creating from an empty state is a separate test.

## Installation acceptance

Keep a dated local record of source and build identity, rendered configuration,
account identity and refresh checks, service bindings, channel/device results,
scheduler receipts and restore evidence. Record any failures alongside fixes and
rerun the affected user path. The [evidence chapter](15-evidence-audit-and-verification.md)
explains what each receipt can establish.

The objective is a useful assistant whose work can be inspected and resumed. Add
integration breadth as its real account and effect paths become verified, keeping
one coherent source of policy and operational ownership.
