# Runtime activation and the first installation

`../scripts/openclaw_runtime_activate.py` is the exported lifecycle owner. Its
operations are `seal`, `snapshot`, `activate`, `restore`, `retire-receipts`,
`screen-capture-enroll` and `screen-capture-check`.
It runs as the configured macOS operator account. It does not install Node,
onboard accounts, invent a predecessor, stop a working service, or turn an
incomplete installation into a successful activation receipt.

Follow the repository's [adoption guide](../../docs/17-adoption-guide.md) to
reconstruct and build the pinned source, run native onboarding, and stage and
qualify the initial gateway/node definitions. First installation establishes the state directory,
credentials, service definitions and actual working predecessor. Subsequent
candidate promotions use this helper. A fresh installation can use the same
source version as the reference without possessing any historical repair files.

## Explicit installation bindings

Set `OPENCLAW_OPERATOR_CONFIG` to the absolute path of the adopter's private
`operator.json`. The shared contract never expands shell syntax or unresolved
placeholders. All these paths must describe the same installation:

| Binding | Meaning |
| --- | --- |
| `paths.host_home`, `identifiers.host_user` | The home and account that own the gateway; the CLI verifies they match the account database. |
| `paths.state_root` | One physical runtime state directory, shared by the predecessor and candidate. |
| `paths.runtime_releases_root` | Physical parent of immutable, self-contained release directories. |
| `paths.runtime_current_link` | Symlink to the selected absolute release directory. |
| `paths.runtime_package_link` | Compatibility package symlink, whose literal target is `runtime_current_link`. |
| `paths.openclaw_cli` | CLI symlink, whose literal target is `runtime_current_link/openclaw.mjs`. |
| `paths.node_binary` | Executable Node inside the pinned installation. |
| `paths.runtime_node_alias` | Symlink whose literal target is the grandparent of `node_binary`. |
| `paths.gateway_plist`, `paths.node_plist` | The real service definitions captured with state. |
| `paths.activation_lock`, `paths.activation_result` | Shared activation lock and immutable terminal receipt path. |
| `paths.legacy_exec_approvals` | Legacy approvals-file location checked for absence; for a new installation use `state_root/exec-approvals.json`. Its parent must match the persisted approvals socket directory. |
| `paths.session_store`, `paths.session_reservations` | Historical session-store and reservation paths within `state_root`; these identify a preservation constraint, not a newly created store. |
| `paths.screen_capture_binding` | Private immutable Journal continuity binding. Initially `null` for an installation that has not enrolled this route; set the exact path after acceptance to require the guard on subsequent activations. |
| `paths.python_binary`, `paths.peekaboo_binary` | Executables used by the explicit Journal capture route. Use the same Python and Peekaboo executables as the installed Journal workflow. |

Set `runtime.gateway_label`, `runtime.node_label`, `runtime.gateway_port` and
`runtime.required_extensions` to the actual installation. The reference expects
bundled `discord` and `lane-contract`; Discord must resolve to the bundled plugin,
with no stale installed-plugin record shadowing it. The gateway plist must use
the pinned Node, `runtime_package_link/dist/index.js`, `gateway`, `--port` and the
configured port, with `WorkingDirectory=host_home`, `RunAtLoad=true` and
`KeepAlive=true`. Both service environments must name `OPENCLAW_STATE_DIR` and
set `OPENCLAW_SUPERVISOR_MODE=external` and
`OPENCLAW_SERVICE_REPAIR_POLICY=external`. See
[release architecture](../../docs/10-runtime-releases-and-promotion.md).

## Journal capture acceptance and permission continuity

The source installation protects its Journal route across runtime activations.
When `paths.screen_capture_binding` is configured, the activator checks that
binding before touching either launchd job and again after the transition. It
pins the responsible Node and Peekaboo executable bytes, filesystem identities
and signatures, and the exact read-only ScreenCapture TCC row. The compiled TCC
`csreq` must pass `codesign --test-requirement` against the exact Node binary;
a valid self-signature alone is insufficient. A changed, revoked or unknown
prerequisite stops activation. The helper never repairs permissions.

A fresh installation first establishes its normal activation receipt with this
optional binding unset. Then run the explicit native acceptance below and set
`paths.screen_capture_binding` to the verified output. Keep it configured for
every later activation when using the Journal workflow. Do not unset it to bypass
a failed prerequisite, or fabricate a first-install receipt or TCC grant.

Open the existing Journal app before invoking the capture. Read-only TCC log
observation requires already configured noninteractive sudo access and fails
before dispatch when unavailable. It does not prompt for credentials or change
system logging privacy settings. Use private durable evidence storage under the
configured state root, not a disposable source checkout or scratch directory.
The following variables are absolute paths from the adopter's private contract:
`$PYTHON`, `$WORKSPACE`, `$STATE`, and `$OPENCLAW_OPERATOR_CONFIG`.

```sh
CAPTURE_ROOT="$STATE/capability-evidence/journal"
mkdir -p "$CAPTURE_ROOT"
chmod 700 "$CAPTURE_ROOT"
CAPTURE_DIR="$CAPTURE_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
"$PYTHON" -B "$WORKSPACE/scripts/journal_screen_capture_acceptance.py" \
  capture --directory "$CAPTURE_DIR"
```

This creates one disabled temporary scheduler command job, manually invokes it
once, records a private Peekaboo image and TCC attribution, then removes the job.
The Python parent and two new-session children preserve the installed scheduler
route. It does not export or ingest Journal content, replay a production job,
change TCC, or restart the gateway. An uncertain result retains command receipts
for reconciliation and never retries the capture automatically.

Inspect `journal.png` and confirm it depicts the actual Journal window. Pass the
reported image hash only after that review; acceptance rejects a replaced image,
responsible process or activation receipt. Then enroll and check the evidence:

```sh
"$PYTHON" -B "$WORKSPACE/scripts/journal_screen_capture_acceptance.py" \
  accept --directory "$CAPTURE_DIR" \
  --reviewed-journal-image-sha256 "$REVIEWED_IMAGE_SHA256"
BINDING_OUTPUT="$CAPTURE_DIR/continuity-binding.json"
"$PYTHON" "$WORKSPACE/scripts/openclaw_runtime_activate.py" \
  screen-capture-enroll --acceptance "$CAPTURE_DIR/acceptance.json" \
  --acceptance-sha256 "$ACCEPTANCE_SHA256" \
  --permission-database '/Library/Application Support/com.apple.TCC/TCC.db' \
  --output "$BINDING_OUTPUT"
"$PYTHON" "$WORKSPACE/scripts/openclaw_runtime_activate.py" \
  screen-capture-check --binding "$BINDING_OUTPUT" --require-current-process
```

`$ACCEPTANCE_SHA256` is the hash returned by the acceptance command. After the
check passes, bind `paths.screen_capture_binding` to `$BINDING_OUTPUT` in private
`operator.json`. Preserve each previous immutable receipt when renewing the
binding. Include the evidence directory and configured binding in private state
backups. No original host receipt or permission state is supplied by this repo.

For a corresponding capability status record, set `screen_capture_binding` to
an object containing that absolute `path` and its `sha256`. The resolver and
configuration-invalidation helper recheck the native binding. A new responsible
process or activation makes current capability unknown and requires a fresh
explicit capture, even when code and TCC prerequisites still match. The focused
`capability_validation_runner.py --screen-capture-binding "$BINDING_OUTPUT"`
command performs the same current-process check without provider probes.
Other native capture routes keep their own evidence. This manual acceptance
proves current Journal capture capability; natural scheduled synchronization
requires a separate successful scheduler and data-delivery observation.

## Native state prerequisites

Use the initial runtime's native setup before sealing and stopping it. The
following are real commands in the pinned source, not SQL repair recipes. Here
`$OPENCLAW` is the built checkout's `openclaw.mjs` before first installation, or
the selected CLI path afterward. `$NODE` is the pinned Node executable and
`$STATE` the configured state root.
Every command must receive `OPENCLAW_STATE_DIR="$STATE"` (and the matching
`OPENCLAW_CONFIG_PATH` when that was explicitly configured).

```sh
"$NODE" "$OPENCLAW" config validate
"$NODE" "$OPENCLAW" doctor --lint
"$NODE" "$OPENCLAW" models auth login --provider openai
"$NODE" "$OPENCLAW" models auth list --provider openai
"$NODE" "$OPENCLAW" models auth order set --provider openai --agent main 'openai:your-profile-id'
"$NODE" "$OPENCLAW" models auth order get --provider openai --agent main --json
```

Login is interactive. Repeat it for the accounts the adopter actually uses,
then set the ordered list of their real profile IDs. These IDs belong in private
local state. The native order command writes persisted auth state and refreshes
the running gateway's auth view. Do not put an `openai` order override or copied
OpenAI profile records back into `openclaw.json`. API-key-only deployments do
not meet this reference's OAuth-account continuity contract.

When a real legacy state migration is required, inspect `doctor` first and use
`"$NODE" "$OPENCLAW" doctor --repair --non-interactive` for the reviewed native repair.
Do not run it as a recurring substitute for an unexplained activation failure.
This can change state, so preserve the existing installation first. The activator
requires no pending legacy approvals source or doctor claim after migration.

Initialize the adopter's host execution policy through the native approvals
command. Prepare a private JSON policy file with `version: 1`, the adopter's
chosen `defaults`, and optional agent rules. These settings determine what the
agent can execute; the public reference does not claim a particular example is
the original host's policy. Then run:

```sh
"$NODE" "$OPENCLAW" approvals set --file "$REVIEWED_APPROVALS_POLICY"
"$NODE" "$OPENCLAW" approvals get --json
```

Without `--gateway` or `--node`, these commands target local state. `set` writes
the current SQLite approvals record and generates the socket token through the
native store. `get` alone is a read and is not an initializer. The native command
preserves or creates socket defaults; do not invent a token, copy another
operator's token, or hand-edit the database. For a new installation the default
socket and `paths.legacy_exec_approvals` should both be under `state_root`.

The activation config additionally requires `auth` to be an object without
legacy `cooldowns` or OpenAI profile shadows, `skills.entries.goplaces.enabled`
to be false without an embedded API key, required bundled plugins enabled, and
`plugins.load.paths=[]`. The preference profile supplies the reference choices;
validate the merged native config rather than substituting the profile for all
installation-specific configuration.

## Bind the adopter's auth order without copying credentials

The exact runtime contains a read-only approvals/auth inspector at
`dist/infra/exec-approvals-inspection.js`. Run it against the initialized state
before the stopped snapshot. In the snippet, `$NODE`, `$RELEASE`, `$STATE` and
`$LEGACY_APPROVALS` are absolute paths read from the private operator contract.
It emits counts, digests and validity flags, not profile names or credentials.

```sh
OPENCLAW_STATE_DIR="$STATE" \
OPENCLAW_LEGACY_EXEC_APPROVALS_PATH="$LEGACY_APPROVALS" \
OPENCLAW_APPROVALS_INSPECTOR="$RELEASE/dist/infra/exec-approvals-inspection.js" \
"$NODE" --input-type=module <<'JS'
import { pathToFileURL } from 'node:url';
const { inspectExecApprovalsState } = await import(
  pathToFileURL(process.env.OPENCLAW_APPROVALS_INSPECTOR).href
);
const report = inspectExecApprovalsState({
  stateDir: process.env.OPENCLAW_STATE_DIR,
  legacyPath: process.env.OPENCLAW_LEGACY_EXEC_APPROVALS_PATH,
});
process.stdout.write(JSON.stringify(report, null, 2) + '\n');
JS
```

Review all validity flags, then bind `runtime.expected_auth_order_count` and
`runtime.expected_auth_order_sha256` to that report's `openAIProfileOrderCount`
and `openAIProfileOrderSha256`. The count must be positive. These are an
attestation of the adopter's own accounts, never values copied from the reference
host. Changing accounts requires deliberately refreshing this preservation
binding; an observed login screen is not persistence proof.

## Historical reservation state: required or explicitly absent

The original installation contains a historical session-quarantine reservation
record. No native first-install command produces that record. The export adds an
explicit adopter setting, `runtime.session_reservations_mode`:

- `required`: the existing file must be a physical JSON file with mode `0600`,
  its `storePath` must equal `paths.session_store`, and its exact bytes must match
  the adopter-supplied `runtime.session_reservations_sha256`.
- `absent`: no filesystem entry may exist at `paths.session_reservations`,
  including a directory or dangling symlink. There is no digest to fabricate.

Both paths must stay within the physical state root. The mode and observed
presence are part of the snapshot and the preflight, post-migration, post-boot,
repeat-invocation and restore comparisons. A missing required file fails. It
never selects absent mode automatically. Switching the setting cannot make a
snapshot captured under the other setting valid. Restore extracts the actual
archive and verifies its original mode and presence. Do not manufacture an empty
historical reservation record to make a gate pass.

This is a deliberate portability adaptation to the exported lifecycle helper;
it is not a change to the pinned OpenClaw runtime patch.

## Promotion and recovery

Prepare a self-contained candidate with no symlinks escaping its release root,
make all physical release contents non-writable, and retain the build's exact
commit in `dist/build-info.json`. The activator checks these properties itself.
Use the candidate and stopped-snapshot command sequence in the
[adoption guide](../../docs/17-adoption-guide.md), with a unique snapshot directory.
A version change also requires `--upgrade-config` naming a reviewed immutable
mode-`0400` target config. A same-version change must omit that argument.

A successful candidate consumes a single start fence and writes an immutable
receipt. Repeating the command reads and verifies that receipt; it does not
start again. A failed or uncertain consumed start requires the matched snapshot
restore. `restore --after-success` applies only to an explicitly stopped rollback
of a successful version upgrade and leaves the restored queue stopped. Receipt
retirement has its own exact terminal and live verification gates; deleting
receipt files manually is not a valid way to retry.

The retention children share `paths.runtime_retention_lock`; release retention
also uses the activation lock and result. Configure
`paths.runtime_release_retention_artifacts`,
`paths.runtime_promotion_retention_artifacts`, `paths.runtime_promotions_root`,
`paths.cli_root`, `runtime.launchagent_label_markers`, and, if retaining the
historical privileged-build cleanup, `paths.approval_a_execution_root`.
A missing historical build root is an accepted no-op. The latter helper keeps
its root-ownership checks and must run in a separately authorized privileged
maintenance invocation. It is not part of ordinary user activation.
