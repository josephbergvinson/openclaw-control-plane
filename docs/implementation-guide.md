# Implementation Guide

This guide describes the control-plane architecture in this repository and how to adapt it to an OpenClaw workspace.

## Architecture

The control plane is a small operating layer around OpenClaw. It is not a plugin and does not replace OpenClaw runtime behavior. It provides:

- contract files that define agent behavior and execution rules
- control registries that record reusable workflow state and routing expectations
- helper scripts for durable Discord work, closeout checks, and validation
- documentation for deployment, validation, troubleshooting, and operator adaptation

The recommended stack is:

```text
OpenClaw workspace
├── AGENTS.md                     # behavior, approvals, routing, lifecycle
├── TOOLS.md                      # execution mechanics and tool policy
├── SOUL.md                       # voice/style only
├── USER.md                       # durable operator preferences/profile scaffold
├── MEMORY.md                     # compressed memory pointer scaffold
├── IDENTITY.md                   # assistant identity/defaults
├── BOOTSTRAP.md                  # bootstrap/load discipline
├── HEARTBEAT.md                  # liveness/watchdog behavior
├── control/                      # optional machine-readable workflow registries
├── registry/                     # optional source/capability registries for your host
├── scripts/                      # optional validation and workflow helpers
└── docs/                         # local runbooks and implementation notes
```



## Context budget requirements

This control plane works by keeping operational contracts in the model's active working context. That is the feature, but it has runtime implications. The full version expects:

- enough context window for policy + task evidence + tool results
- enough bootstrap budget to load the important contracts without truncating their mechanics
- prompt caching or equivalent economics so repeated contract injection is tolerable
- compaction behavior that does not discard live task state during long work
- rollover/checkpoint behavior before unavoidable context loss

For long-running work, the preferred order is:

1. retain context and continue;
2. if context pressure rises, send a lightweight rollover warning or checkpoint;
3. if compaction/rotation is unavoidable, emit a safe resume checkpoint first;
4. only then compact or rotate.

The template intentionally deprioritizes compaction for efficiency alone. If your runtime compacts aggressively, expect to tune the contracts down or move more state into explicit artifacts before relying on durable lanes.

## Model and context assumptions

The reference setup for this template uses Codex/GPT-5.5 through a ChatGPT Pro OAuth lane. That matters because the control plane injects substantial policy and operating context into serious tasks. Large context budget and effective prompt caching make the full-density setup feel natural.

The pattern can be adapted to other providers, but do not assume the included contracts are an optimal prompt payload everywhere. For smaller or more expensive context windows, compress first:

1. Keep the execution-mode taxonomy.
2. Keep source-of-truth and approval gates.
3. Keep closeout and final-delivery rules.
4. Move project-specific detail into generated capability/registry files.
5. Disable or summarize optional sections until the worker lane proves reliable.

## Required OpenClaw capabilities

At minimum, your setup should support:

- file reads/writes inside the OpenClaw workspace
- shell execution for local validation commands
- Git for versioning the control-plane files
- a reachable OpenClaw gateway
- `openclaw status` / `openclaw gateway status` style diagnostics

For the Discord durable-lane workflow, add:

- Discord channel integration
- subagent or worker-session support
- task/run visibility sufficient to distinguish active, queued, running, stale, and failed work
- durable artifact paths under the workspace, usually `artifacts/...`

For external writes, add only the integrations you actually use, and define their approval gates in `AGENTS.md` and `TOOLS.md`.

## Expected repo layout

This public repo is a template. In your own OpenClaw workspace:

1. Copy files from `templates/` to the workspace root.
2. Copy `control/*.example.json` into `control/` and remove `.example` only after editing.
3. Copy only the scripts you intend to maintain.
4. Keep private memory files, secrets, runtime DBs, and generated artifacts outside your public template repo.
5. Commit local changes in your private/workspace repo.

## Component responsibilities

| Component | Role | Mutability |
|---|---|---|
| `AGENTS.md` | policy authority for behavior, approvals, routing, lifecycle | edit intentionally, test after changes |
| `TOOLS.md` | mechanics authority for tools, filesystem, worktrees, verification | edit intentionally, test after changes |
| `SOUL.md` | voice/style only | low-risk edits |
| `USER.md` | durable operator preferences/profile scaffold | adapt privately |
| `MEMORY.md` | compressed memory pointers | adapt privately |
| `BOOTSTRAP.md` | bootstrap/load discipline | rarely changes |
| `control/*.json` | workflow and registry examples | adapt to your host |
| `scripts/*` | helper implementation mechanics | maintain like source code |
| `docs/*` | operator runbooks | keep aligned with policy |

## Installation sequence

1. Review the templates.
2. Back up your current control-plane files.
3. Copy templates into the workspace root.
4. Replace placeholders.
5. Run tests and scans.
6. Run OpenClaw read-only probes.
7. Commit the adopted control-plane state.
8. Start with read-only tasks before allowing mutation-heavy workflows.

See `docs/deployment.md` and `docs/configuration-checklist.md` for the detailed checklist.
