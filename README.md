# OpenClaw Control Plane Template

A public template for running OpenClaw as a disciplined personal-agent control plane rather than a loose chatbot.

This repo contains policy and operating-contract templates for an OpenClaw setup:

## Start here

If you are evaluating or adopting this repo, read these first:

1. `docs/implementation-guide.md` — architecture, requirements, and layout
2. `docs/configuration-checklist.md` — what to configure before use
3. `docs/validation-checklist.md` — how to verify the setup

## Who this is for

- personal power users running OpenClaw as a local operations assistant
- engineers who want explicit agent routing, source-control, and closeout rules
- small trusted teams adapting OpenClaw into a control plane

This is not a plug-and-play hosted service. Treat it as a template for your own trusted runtime boundary.

## Architecture sketch

```text
Discord / chat control plane
        │
        ▼
OpenClaw gateway and channel runtime
        │
        ▼
Policy contracts: AGENTS.md + TOOLS.md + profile/style/memory scaffolds
        │
        ├── inline work
        ├── durable isolated worker lane ──► status artifact ──► final Discord update
        └── terminal-side/manual containment
```


- `templates/AGENTS.md` — behavior, approvals, routing, lifecycle, source-of-truth policy
- `templates/TOOLS.md` — execution mechanics, tool/runtime/filesystem/worktree rules
- `templates/SOUL.md` — voice/style only
- `templates/BOOTSTRAP.md` — bootstrap/load discipline
- `templates/HEARTBEAT.md` — liveness/watchdog defaults
- `templates/USER.md`, `templates/MEMORY.md`, `templates/IDENTITY.md` — profile, memory-pointer, and identity scaffolds
- `templates/capabilities.example.md`, `templates/policy_bootstrap_manifest.example.json` — capability/bootstrap examples
- `control/*.example.json` — delegation, checkpoint, scheduler, and worker-entrypoint registries
- `scripts/` — optional helper scripts for durable slices, closeout checks, policy drift, capability validation, and dry-run installation
- `docs/` — deployment, inventory, and rollover/control-plane notes
- `examples/` — sample status artifact and final Discord report
- `tests/test_templates_public.py` — example invariant checks


## What this is for

Use this when you want your OpenClaw assistant to behave like an operations partner with explicit rules for:

- inline vs durable vs manual/terminal-side execution
- external-system mutation approvals
- Discord/channel liveness
- source/worktree governance
- memory and privacy boundaries
- bootstrap silence
- closeout/verification expectations
- runtime recovery containment

## Quick install

Dry-run first:

```bash
scripts/install_template.sh --target /path/to/OpenClawWorkspace
```

Apply after review:

```bash
scripts/install_template.sh --target /path/to/OpenClawWorkspace --apply
```

Or copy manually from your OpenClaw workspace:

```bash
mkdir -p ./control-plane-backup
cp AGENTS.md TOOLS.md SOUL.md USER.md MEMORY.md IDENTITY.md BOOTSTRAP.md HEARTBEAT.md ./control-plane-backup/ 2>/dev/null || true
cp templates/AGENTS.md templates/TOOLS.md templates/SOUL.md templates/USER.md templates/MEMORY.md templates/IDENTITY.md templates/BOOTSTRAP.md templates/HEARTBEAT.md ./
```

Then edit placeholders before use:

- `<AUTHORIZED_DISCORD_USER_ID>`
- `<POLICY_AUDIT_LOG_CHANNEL_ID>`
- `<OPENCLAW_WORKSPACE>`
- `<OPERATOR_HANDOFF_ROOT>`
- `<PROJECT_OR_ORG>`
- any repo, channel, model, or provider assumptions that differ from your host

## Safety notes

Do not blindly install these files into a shared or production OpenClaw gateway. The templates assume a trusted personal-operator model unless you harden them.

Before adopting:

1. Review all approval gates.
2. Remove host-specific paths.
3. Set your own channel/account IDs.
4. Decide whether full exec, broad filesystem access, browser automation, durable lanes, or external mutations are acceptable in your threat model.
5. Run any local policy invariant tests you keep.

## More docs

- `docs/implementation-guide.md`
- `docs/durable-discord-flow.md`
- `docs/configuration-checklist.md`
- `docs/validation-checklist.md`
- `docs/troubleshooting.md`
- `docs/operator-adaptation-guide.md`
- `docs/deployment.md`
- `docs/control-plane-inventory.md`
- `docs/example-project-template.md`

## Deployment model

The intended model is:

1. Start from the templates.
2. Customize for your host and trust boundary.
3. Commit your local policy stack in your OpenClaw workspace.
4. Treat later changes as control-plane changes: review, test, commit, and audit-log them.

## Model and context assumptions

This template was developed against Codex/GPT-5.5 through a ChatGPT Pro OAuth lane. The control-plane pattern is portable, but the full template is intentionally context-heavy: it keeps policy, execution mechanics, routing rules, source-governance rules, and durable-lane behavior live in the prompt surface.

For the full version, treat a large-context worker lane, expanded bootstrap budgets, good prompt-cache behavior, and non-aggressive compaction as must-have runtime assumptions. If your model has a smaller practical context window, weaker prompt caching, aggressive auto-compaction, or less tolerance for long operational instructions, start with the routing model and contract separation pattern, then trim the templates before enabling durable worker flows.

Reference environment: this template was developed and tested against OpenClaw 2026.4.24 in a source-built local runtime. The control-plane pattern is portable, but some behaviors described by the templates are runtime-backed rather than policy-only. Discord durable-lane handling, worker/session routing, bootstrap suppression, status-artifact delivery, and closeout reporting require OpenClaw runtime support. If your running OpenClaw build does not implement those code paths, the templates can still express the desired operating discipline, but they cannot make the runtime behave that way by themselves.

For runtime-backed behavior, treat the full dependency chain as: supporting source commit present -> source normalized onto the intended runtime branch -> runtime built/installed from that source -> gateway restarted or reloaded if required -> live behavior verified. If you are using a stock or older OpenClaw install, validate those capabilities before assuming the full workflow works unchanged. When runtime support is absent, use the portable fallback in [`docs/runtime-support.md`](docs/runtime-support.md) rather than assuming upstream patches or custom forks.

