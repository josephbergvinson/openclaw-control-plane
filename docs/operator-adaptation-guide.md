# Operator Adaptation Guide

The template contains reusable control-plane mechanics and example policy choices. Adapt both before using it as your own operating contract.

## Reusable mechanics

These parts are generally reusable:

- explicit execution modes: inline, durable isolated lane, terminal-side/manual containment
- approval gates for external mutation, destructive work, and outbound messages
- source-of-truth separation between canonical repo, worktree, handoff, mirror, and backup
- Discord liveness rules for long work
- status artifact lifecycle
- closeout gate expectations
- memory sensitivity boundaries
- bootstrap-success silence
- fresh vs historical evidence discipline
- public-safety scanning before publishing templates

## Operator-specific policy

These parts should be adapted:

- trusted sender model
- Discord channels and audit-log destinations
- preferred models and agent runtimes
- local workspace paths
- project surfaces and canonical roots
- handoff destinations
- external integrations
- security posture
- allowed tools and approval thresholds
- memory sensitivity policy
- writing style and communication preferences



## Compressed-profile adaptation

If your model/provider cannot comfortably carry the full contract stack, build a compressed profile rather than weakening the control-plane semantics. Keep these invariants:

- classify execution mode before side effects
- require approvals for external mutation and destructive work
- keep canonical source/worktree/handoff distinctions
- preserve durable-lane status artifacts and final-delivery rules
- checkpoint before compaction or thread rotation
- report blockers instead of silently substituting execution modes

Good candidates for compression:

- examples and historical rationale
- repeated Discord-specific wording if you do not use Discord
- unused integration policies
- project-specific scaffolding
- verbose explanatory sections in favor of short mandatory rules

Bad candidates for removal:

- approval gates
- terminal-side/manual containment boundaries
- source-of-truth rules
- closeout verification
- memory sensitivity boundaries
- token/secret redaction

## Model/provider adaptation

Do not treat the model lane as an implementation detail. A dense control plane is partly a context-economics bet. The reference setup uses Codex/GPT-5.5 via ChatGPT Pro OAuth; if you use another model/provider, verify:

- practical context window
- prompt caching behavior
- tool-call reliability under long instructions
- latency under full contract injection
- whether durable worker prompts retain the relevant policy details

If the lane feels brittle, reduce injected policy surface before weakening safety rules. Prefer shorter contracts over implicit behavior.

## Security posture choices

Choose a posture deliberately.

### Personal trusted-operator mode

Useful for one operator controlling their own local assistant. May allow broader filesystem and exec access, but still needs redaction, approvals, and backup gates.

### Shared trusted-team mode

Useful for a small trusted team. Requires clearer role separation, channel permissions, and external mutation controls.

### Untrusted or multi-tenant mode

Do not use the template unchanged. Harden filesystem access, sandboxing, exec policy, channel membership, and credential boundaries. Consider separate gateways, OS users, or hosts.

## Project integration

Use `docs/example-project-template.md` as the pattern. For each project, define:

- surface slug
- canonical source root
- active worktree strategy
- standing branch
- local authoritative remote
- mirror/handoff/backup paths
- validation command
- closeout expectations
- mutation approvals

Keep project facts out of generic policy when they belong in capability/registry files.

## Style adaptation

`SOUL.md` controls voice only. It should not contain tool policy, approval gates, runtime facts, or source-of-truth rules.

`USER.md` should contain durable preferences and working context, not secrets or high-sensitivity private history unless you intentionally keep it local and private.

`MEMORY.md` should be a compressed pointer layer, not a full personal data dump.

## Adoption process

1. Read `AGENTS.md` and `TOOLS.md` fully.
2. Remove policies that do not fit your risk model.
3. Replace placeholders and example project surfaces.
4. Run tests and scans.
5. Start with read-only tasks.
6. Add mutation permissions gradually.
7. Commit policy changes and keep a changelog.
