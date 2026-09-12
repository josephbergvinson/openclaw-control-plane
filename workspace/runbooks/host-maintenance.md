# Host maintenance installation and operation

The exported helpers run on macOS. They require explicit `operator.json` bindings,
Python's standard library, the pinned runtime/Node pair and the native utilities
used by each helper. The fixture suite additionally uses pytest. Installing the
workspace does not configure services or authorize a cleanup against another root.

The native CLI helpers bind the configured executable, Node, home, state root,
`state_root/openclaw.json` and gateway service label together. Conflicting inherited
state/config/service settings and a nonempty `OPENCLAW_PROFILE` are rejected. Keep
the same default-profile installation contract as the activation runbook.

## Prepare the bindings and definitions

Use the repository's `scripts/materialize_host.py` to render `host-templates.json`
into a new review directory. Supply the completed private operator configuration.
The materializer writes definitions and a digest manifest only. Compare every
argument, working directory, environment value and output path with the intended
installation. All native job inputs have `enabled: false`; every plist is disabled.

The resulting `control/root_drift_policy.json` and `registry/external_volume_guard.json`
belong at those relative paths in the installed workspace after review. The root
policy classifies known source, retained state and generated residue. Add any new
canonical source to that policy deliberately; unknown paths remain blockers. Make
the adopted workspace a Git checkout before scheduling its integrity check.

The external-volume contract binds the actual filesystem UUID and physical mount.
Its old active liveness observer is retained as a retired implementation: do not add
triggers or schedule it. The current storage helpers use their own read-only native
volume identity checks. Retain the untriggered context-warning definition too;
native runtime compaction owns rollover notification. These retained definitions
are not available background features waiting to be enabled.

## Install independent helpers

The Backblaze watchdog must remain on internal storage. Copy these four reviewed
files together into `paths.internal_control_root`: `backblaze_resource_watchdog.py`,
`backblaze_resources.py`, `external_volume_guard.py` and `operator_contract.py`.
`dependencies/backblaze-package.json` pins every file in this exported package.
Install the volume helper and operator contract before loading the resources module;
refresh files by reviewed atomic replacement and fsync under the existing admin lock.
Put its private bindings at
`paths.internal_operator_config` and set the rendered plist to that file. The shared
resource module measures all four source hashes when loaded. Compare all installed
hashes with the package manifest and require a later natural receipt containing the
same set before admitting work. Validate a staged internal copy with the selected
Python and an empty `PYTHONPATH`; importing the volume helper must not depend on the
external workspace. Its native UUID reader uses the standard library and Darwin
metadata; this import does not activate its retired probe CLI. Keep
its logs, hold, pause state and lock on internal storage too.

Retain any active installation hold while replacing a package. A previous receipt
with an incomplete source set proves only that older implementation ran. It cannot
admit the newer first-catalog path. The separate [Backblaze bootstrap runbook](backblaze-bootstrap.md)
describes the optional trial transition, exact publication bindings and effect gates.

The optional Claude stale-worker guard is the full signed-worker/parent-chain and
append-only receipt implementation. Its configured code and receipt roots are
passed as environment bindings. It only qualifies an unlinked old executable when
its actual parent process and a newer vendor-signed executable agree. Before a
single SIGTERM it rechecks process/file identity and publishes a durable attempt
barrier. A later invocation observes an existing barrier instead of sending another
signal. Its exact PID-only signalling boundary still requires local acceptance.
The disabled template retains the five-minute cadence and watched update directory.

The optional runtime-environment helper selects temporary/cache/state roots and
retrieves a gateway token through the login Keychain's configured service/account.
It then replaces its loader process with the configured desktop application. The
script contains no token. Review whether this compatibility bridge is required by
the adopted native application before enabling it. Do not log its environment or
copy Keychain values into the repository. The optional keepawake definition uses
macOS `caffeinate`; its power/session tradeoff is a separate adopter choice.

Copy a reviewed plist to its intended `paths.launch_agents` directory using its
`Label` as the filename. Keep it disabled until its complete environment, behavior
and permissions have been qualified. Disabling a file does not stop an already
loaded definition: inspect loaded state separately when replacing existing jobs.
Use native launchd tooling for deliberate installation or retirement; this package
never calls launchctl merely to render a template.

## Adopt the native scheduler inputs

`scheduler/maintenance-jobs.json` is an array of native `cron.add` parameter objects.
It preserves the nine reference command schedules and deadlines with new operator
bindings. Review each object and create it through the pinned runtime's native
scheduler API. Retain the returned ID; do not write old JSON stores or SQLite rows
by hand. Set `scheduler.maintenance_job_ids` to the actual IDs for daily retention,
workspace integrity, storage headroom, launch integrity and gateway recovery.
`scheduler.local_backup_job_id` is needed only when deliberately adopting the
retained local-backup path; it is not the cloud backup job ID.

Each command uses the pinned Python interpreter, `cron_python_entrypoint.py`, a
private receipt directory and the exact checked helper. The payload uses `argv`,
`cwd`, `env` and `timeoutSeconds`; it is not shell syntax. Explicit numeric
`timeoutSeconds: 0` disables this runtime's command wall timer; omission keeps the
ten-minute default and positive values retain their seconds-based deadlines.
No-output limits and cancellation remain independent. The supplied maintenance
objects preserve their positive deadlines; choose zero only when the intended job
contract requires it. Delivery uses the chosen channel and destination. Verify the registered definition after creation and keep
it disabled until a separately authorized manual run proves its real effect and
report. Enabling a schedule does not retroactively qualify older receipts from a
different payload.

## Check and report

Start with isolated fixtures:

```sh
python3 /path/to/reference/workspace/tests/run_periphery_tests.py
```

The runner creates disposable bindings and service stubs. It does not run native
cleanup, Backblaze control, launchd installation or network access. The source tests
exercise actual classification, receipts, archive/restore logic, subprocess custody
and controlled fixture deletion. They do not prove the adopter's accounts, disk,
scheduler, vendor client or application permissions.

A configured `openclaw_storage_prune.py --json` is a dry-run classifier; `--apply`
permits eligible removal. `storage_headroom_guard.py --check-only` measures capacity.
The retention wrapper runs four checked stages and must not report overall success
when any stage fails. Workspace integrity preserves known source edits and retained
state, removes only classified untracked residue, and reports unresolved conflicts
and unknown layouts. The health audit performs native task-ledger maintenance and
keeps unapproved or severity-changed security findings visible. Accepted findings
are adopter policy; the default public mapping is empty.

Record execution, observable effect and required delivery separately. After manual
acceptance, retain a naturally due receipt bound to the current payload and period.
Do not call a newly enabled job unattended-ready on the strength of a manual run.
