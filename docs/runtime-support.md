# Runtime support and portable fallback

This repository is primarily a control-plane template: policy files, routing rules, lifecycle expectations, status artifacts, and closeout discipline. Those pieces are portable across OpenClaw installations.

Some workflows described here are runtime-backed. They require the running OpenClaw build to support the relevant worker/session, Discord, bootstrap, or delivery behavior. This repo does not assume those changes will land upstream, and it does not require you to run a custom fork to get value from the template.

## Capability matrix

| Capability | If runtime support exists | Portable fallback |
|---|---|---|
| Durable Discord worker lanes | Worker is launched from the control plane, writes a status artifact, and returns completion to the same Discord surface. | Keep Discord as the control plane, but run the worker manually or with a normal local command. Record state in `artifacts/<run-id>/status.md` and paste final completion back to Discord. |
| Worker/session routing | Assistant can create/resume isolated worker sessions with explicit execution mode and status path. | Use a manually named run id and a status artifact. Treat any separate terminal, agent CLI, or assistant thread as the worker lane. |
| Bootstrap-success suppression | Successful bootstrap/status handling stays invisible unless there is a blocker. | Keep the same rule in `AGENTS.md`/`BOOTSTRAP.md`; if the runtime leaks status text, remove it manually from public-facing drafts and issue reports. |
| Status-artifact delivery | Runtime or worker can initialize and update deterministic status files. | Create the artifact yourself before starting work and require every worker/operator update to edit it. |
| Closeout reporting | Runtime or helper scripts enforce final delivery, verification, and residual-drift reporting. | Run the checklist in `docs/validation-checklist.md` manually and include the closeout fields in the final report. |
| Same-surface final delivery | Completion returns to the original Discord thread/channel automatically. | Copy the final report into the original control-plane surface and link the status artifact or commit. |

## Portable operating pattern

If your OpenClaw runtime does not support the full workflow, use this sequence:

1. Choose a run id, for example `manual-control-plane-YYYYMMDD-HHMM`.
2. Create `artifacts/<run-id>/status.md` before work begins.
3. Record:
   - objective
   - execution mode: `inline`, `durable/manual`, or `terminal-side/manual containment`
   - source root / worktree / branch if repo work is involved
   - approval boundary
   - next atomic step
4. Run the work in the best available lane: current assistant, local terminal, external coding agent, or manual operator flow.
5. Update the status artifact at meaningful phase boundaries.
6. Before claiming completion, verify:
   - changed files or external state
   - tests/checks run
   - commit or artifact identity
   - residual drift
   - final delivery back to the original control plane
7. Paste or send the final completion report to the same surface where the request started.

## What not to do

Do not treat the templates as a substitute for missing runtime behavior. If the runtime cannot create durable workers, suppress bootstrap chatter, or auto-deliver final reports, the portable fallback is explicit manual discipline: status artifacts, visible checkpoints, and final delivery by the operator or assistant.

Do not claim parity with the reference setup until you have verified the relevant runtime-backed behavior in your own installation.

## Optional source patch path

Advanced users can maintain their own fork or patch series for runtime-backed behavior, but this repo intentionally does not depend on upstream acceptance or a specific custom runtime branch. If you publish runtime patches, document:

- OpenClaw base version or commit
- patch branch or patch-file location
- features enabled by the patches
- build/install/restart steps
- verification commands
- rollback path

The control-plane template should remain useful even when those patches are absent.
