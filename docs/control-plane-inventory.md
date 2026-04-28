# Control Plane Inventory

This repository is organized around reusable OpenClaw control-plane components.

## Contract templates

- `templates/AGENTS.md` — operator policy, task routing, lifecycle states, source-of-truth rules
- `templates/TOOLS.md` — execution mechanics, tool policies, filesystem/worktree rules
- `templates/SOUL.md` — voice/style only
- `templates/USER.md` — durable preference/profile scaffold
- `templates/MEMORY.md` — compressed memory pointer scaffold
- `templates/IDENTITY.md` — identity/defaults scaffold
- `templates/BOOTSTRAP.md` — bootstrap/load policy
- `templates/HEARTBEAT.md` — liveness/watchdog defaults
- `templates/capabilities.example.md` — generated capability/status view example
- `templates/policy_bootstrap_manifest.example.json` — machine-readable bootstrap manifest example

## Control registries

- `control/checkpoint_rules.example.json`
- `control/delegation_policy.example.json`
- `control/schedulers.example.json`
- `control/worker_entrypoints.example.json`

## Mechanics

- `scripts/discord_coding_slice.py` — durable Discord coding/control-plane slice helper
- `scripts/slice_closeout_gate.py` — closeout validation helper
- `scripts/policy_drift_check.sh` — policy drift check helper
- `scripts/capability_validation_runner.py` — capability validation harness
- `scripts/openclaw_health_audit_cron.py` — read-only health audit helper

Adapt placeholders and verify against your local OpenClaw runtime before installing.

## Implementation docs

- `docs/implementation-guide.md` — architecture, requirements, and expected repo layout
- `docs/durable-discord-flow.md` — end-to-end Discord execution lifecycle
- `docs/configuration-checklist.md` — registry/control/env/channel prerequisites
- `docs/validation-checklist.md` — probes, tests, closeout, and safety scans
- `docs/troubleshooting.md` — common failure modes and safe responses
- `docs/operator-adaptation-guide.md` — reusable mechanics vs local policy choices

## Examples

- `examples/status-artifact.example.md` — sample durable-lane status artifact
- `examples/final-discord-report.example.md` — sample final operator-visible update

## Install helper

- `scripts/install_template.sh` — dry-run/apply copy helper for template adoption
