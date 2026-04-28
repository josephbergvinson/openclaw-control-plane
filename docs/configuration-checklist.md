# Configuration Checklist

Use this checklist when adapting the control-plane template to a real OpenClaw setup.

## Workspace files

Required root files:

- [ ] `AGENTS.md`
- [ ] `TOOLS.md`
- [ ] `SOUL.md`
- [ ] `USER.md`
- [ ] `MEMORY.md`
- [ ] `IDENTITY.md`
- [ ] `BOOTSTRAP.md`
- [ ] `HEARTBEAT.md`

Optional examples to adapt:

- [ ] `templates/capabilities.example.md`
- [ ] `templates/policy_bootstrap_manifest.example.json`
- [ ] `templates/POLICY_CHANGELOG.example.md`

## `registry/`

Use `registry/` when you want machine-readable source-of-truth records. Common entries:

- [ ] project surfaces
- [ ] canonical source roots
- [ ] standing branches
- [ ] local authoritative remotes
- [ ] mirror/handoff roots
- [ ] validation entrypoints
- [ ] capability readiness records

If you do not use `registry/`, keep equivalent facts in a generated capability/status view and verify them before edits.

## `control/`

Adapt the example files only if they match your workflow:

- [ ] `control/checkpoint_rules.example.json`
- [ ] `control/delegation_policy.example.json`
- [ ] `control/schedulers.example.json`
- [ ] `control/worker_entrypoints.example.json`

After editing, remove `.example` only when the file is live for your setup.


## Required model/context settings

The full control-plane template assumes the runtime can keep the operating contracts live during serious work. Treat these as must-have settings or explicit adaptation decisions:

- [ ] Use a large-context model/worker lane for the main control-plane assistant.
- [ ] Prefer a model/provider lane with effective prompt caching for repeated policy injection.
- [ ] Increase or permit larger bootstrap/context budgets so `AGENTS.md`, `TOOLS.md`, and the supporting profile/style/memory scaffolds are available when needed.
- [ ] Avoid aggressive auto-compaction during long-running implementation, audit, or recovery-adjacent work.
- [ ] Prefer context retention over token thrift while a durable lane is actively executing.
- [ ] Emit a compact rollover/checkpoint before any unavoidable compaction or thread rotation.
- [ ] Verify that durable worker prompts receive the same execution-mode, approval, source-of-truth, and closeout rules as the parent control plane.
- [ ] If using a smaller-context provider, create a compressed policy profile before enabling durable worker flows.

Recommended minimum behavior:

```text
large context window: required for full template
prompt caching: strongly recommended
bootstrap budget: large enough for AGENTS.md + TOOLS.md + core scaffolds
long-run compaction: deprioritized; checkpoint before unavoidable compaction
worker context: isolated by default, but still policy-complete
```

Do not compensate for small context by silently dropping safety rules. Compress wording first; preserve routing, approvals, source-of-truth, and closeout semantics.

## Environment and secrets

Do not commit secrets. Configure secrets through OpenClaw-supported config, keychain, environment, or your deployment mechanism.

Checklist:

- [ ] OpenClaw gateway config exists and is readable by the service
- [ ] provider keys or OAuth credentials are configured outside the public repo
- [ ] Discord token configured outside the repo
- [ ] Telegram token configured outside the repo, if used
- [ ] Notion/GitHub/Google/etc. credentials configured only if needed
- [ ] no tokens in shell history, docs, artifacts, test fixtures, or logs committed to Git

## Discord targets

For Discord-based control-plane work, define:

- [ ] operator-visible control channel(s)
- [ ] audit-log channel, if used
- [ ] allowed sender/user policy
- [ ] group/channel behavior
- [ ] whether thread-bound sessions are allowed
- [ ] durable lane status artifact path pattern
- [ ] final delivery rules

## Gateway and channel prerequisites

Before relying on the control plane:

- [ ] `openclaw gateway status` reports running/reachable
- [ ] `openclaw gateway probe` succeeds
- [ ] `openclaw channels status --probe` succeeds for expected channels
- [ ] Discord inbound works
- [ ] outbound Discord sends work when explicitly approved
- [ ] memory status is known
- [ ] task DB active/queued/running state is known

## Placeholders to replace

Search for placeholders:

```bash
grep -RIn "<[^>][^>]*>" AGENTS.md TOOLS.md SOUL.md USER.md MEMORY.md IDENTITY.md BOOTSTRAP.md HEARTBEAT.md control scripts docs
```

Common placeholders:

- `<OPENCLAW_WORKSPACE>`
- `<OPERATOR_HANDOFF_ROOT>`
- `<PROJECT_OR_ORG>`
- `<PROJECT_HANDOFF>`
- `<LOCAL_SOURCE_ROOT>`
- `<AUTHORIZED_DISCORD_USER_ID>`
- `<POLICY_AUDIT_LOG_CHANNEL_ID>`
- `<DISCORD_CHANNEL_ID>`
- `<PORT>`
