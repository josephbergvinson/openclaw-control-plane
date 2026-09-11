# Capability routing package

`resolve_capability.py` retains the complete route-selection, identity,
operation, probe-custody and evidence-validation logic of the reference build.
`capability_registry_contract.py` checks the registry before selection. The
registry includes 41 routes; unused integrations can remain unconfigured.

Operator-specific account names, IDs, paths, domains and provider identity
digests are whole-value `${operator:key}` references. `operator_contract.py`
validates the installation contract. `routing_operator_bindings.py` holds one
configuration snapshot per process, so an account binding cannot change in the
middle of a probe. A later invocation reads the updated configuration.

The distributable probe registry represents command arguments as
`{"$operator_argv": [...]}`. A bounded `{"$operator_env": "NAME", "value": ...}`
entry represents one environment assignment. The materializer resolves only
whole values; `{"$operator_prefix": "channel:", "value": ...}` supplies one
Discord target argument. It uses `shlex.join`; the original resolver compares the
parsed argument vector with its supported probe. It never executes a shell.
Historical descriptive commands containing shell operators remain descriptions
and have no exact-probe transport unless an explicit parser supports them.

Missing optional bindings remain unconfigured during registry inspection.
Typed path and digest sentinels allow the schema to be inspected; they carry
no readiness and the selected-route check rejects them before transport.
Provider responses are always parsed as data, never as operator references.
The Node adapters materialize their own project-registry references at the
consumer. Standalone signer probes use the exact configured secret-file paths;
the wallet adapter loads its signer files from the configured state root.
Configure those standalone paths to the same files when both surfaces use the
same signer. Host ownership binds to `identifiers.host_user` and the process UID.

Trello has its own `services.trello.username` and `board_name`; they do not
inherit a GitHub identity. `services.trello.board_id` is the short board ID used
in its URL and API request. Linear's provider workspace is
`services.linear.workspace_id`. The Linear and Mercury `auth_reference` fields
locate their adopter-owned OAuth configuration; they contain references, not
tokens. The coordinator Gmail probe is an explicitly manual account inspection,
so it cannot produce an authoritative exact-probe receipt by executing prose.

The shipped capability status starts unverified. Tests use disposable,
synthetic installations and fake provider responses. They do not write the
shipped status or authenticate real accounts. The preserved controls include
account isolation, unknown-operation rejection, bounded output and deadlines,
process-group cleanup, strict response identity and evidence scope, and the
separation of routing permission from completion evidence.

Read the dependency instructions in `../dependencies/README.md` before using
the PostgreSQL or WalletConnect adapters. Project-specific repositories and
their own validation commands remain adopter inputs; their names and paths in
the topology registry do not install those projects.
