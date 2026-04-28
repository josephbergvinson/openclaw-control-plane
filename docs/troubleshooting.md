# Troubleshooting Guide

This guide covers common control-plane failures and the safest first response.

## Lane creation failure

Symptoms:

- worker/session creation returns an error
- durable lane never appears
- status artifact remains at `initializing`

Response:

1. Do not silently fall back to another execution mode.
2. Report the concrete error in the Discord control plane.
3. Preserve the status artifact.
4. Check worker/subagent capability and task DB state.
5. Retry only after explicit approval or after correcting the lane shape.

## Thread binding failure

Symptoms:

- Discord worker cannot bind to a thread
- persistent session requires thread binding but the current policy disallows it

Response:

1. Distinguish runtime limitation from policy failure.
2. Prefer an allowed unthreaded one-shot run lane if the policy and tool support it.
3. If no allowed lane shape exists, stop and report the blocker.
4. Do not create nested Discord threads unless explicitly approved.

## Worker degradation

Symptoms:

- preferred coding worker is unavailable
- worker starts but cannot authenticate or load context
- repeated worker-specific runtime errors

Response:

1. Keep Discord as the control plane.
2. Report the preferred-lane failure.
3. Use documented fallback only if policy allows it.
4. Do not substitute a stronger/more dangerous execution mode without approval.

## Missed final updates

Symptoms:

- worker completed but Discord thread did not receive a final human-readable report
- only internal/task completion notice exists

Response:

1. Convert the worker result into a normal operator-visible update.
2. Send it to the originating Discord target.
3. Include verification and closeout status.
4. Update the status artifact.
5. Add or tighten tests if this recurs.

## Dirty root or closeout failure

Symptoms:

- root checkout has unrelated drift
- closeout gate reports incomplete
- task branch merged but cleanup or verification is incomplete

Response:

1. Classify drift as task-related or unrelated.
2. Stage/commit only task-related files.
3. Do not absorb unrelated drift into the slice.
4. Report the exact closeout blocker.
5. Preserve the lane if verification failed or branch preservation is required.

## Task DB noise

Symptoms:

- active/running rows remain after completion
- queued/running counts disagree with visible work
- maintenance audit reports issues

Response:

1. Treat task DB reconciliation as terminal-side/manual containment.
2. Do not mutate task DB from a routine inline/durable lane.
3. Export or inspect current state before changes.
4. Apply maintenance only after explicit approval.

## Historical log residue

Symptoms:

- logs contain old missing-dist, no-space-left, timeout, or plugin errors
- fresh probes pass but retained logs look severe

Response:

1. Classify evidence as fresh-window, mixed-window, or historical-residue.
2. Do not diagnose current outage from stale lines alone.
3. Run fresh gateway/channel/memory/task/disk probes.
4. Report retained residue separately from live failures.
