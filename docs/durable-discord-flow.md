# End-to-End Durable Discord Flow

This flow treats Discord as the operator-visible control plane and separates work into exactly one execution mode before side effects.

## Execution modes

Choose one mode before edits, external writes, scheduler changes, config changes, or long-running work.

| Mode | Use for | Avoid for |
|---|---|---|
| `inline` | quick read-only checks, short focused edits, small docs updates, final reports | long builds, broad audits, runtime repair |
| `durable isolated lane` | long implementation, broad audits, multi-step verification, background-style work | runtime recovery, config repair, task DB reconciliation |
| `terminal-side/manual containment` | build/install/restart, gateway recovery, provider/memory/timeout config, task DB reconciliation, large destructive filesystem work | ordinary docs/source edits |

Visibility labels such as `ack-then-inline` or `ack-then-delegate` are not execution modes. They describe Discord liveness behavior only.

## Durable lane lifecycle

1. **Classify** the request and choose the mode.
2. **Acknowledge visibly** in Discord before side effects.
3. **Create a status artifact** immediately, for example:

   ```text
   artifacts/discord_coding_slices/<run-id>/status.md
   ```

4. **Launch exactly one worker lane** when durable work is selected.
5. **Require isolated context** unless the operator explicitly approves shared context.
6. **Update the status artifact** at acceptance, meaningful checkpoints, blockers, and completion.
7. **Run the closeout gate** before claiming done.
8. **Deliver the final result** to the same Discord control plane.

## Status artifact lifecycle

A useful `status.md` includes:

```md
# Run status

runId: <run-id>
originChannel: <discord-channel-id>
originMessage: <discord-message-id>
state: initializing|executing|waiting-input|blocked|completed|failed
mode: inline|durable isolated lane|terminal-side/manual containment
objective: <one sentence>
lastCheckpoint: <timestamp>

## Progress
- ...

## Constraints
- ...

## Blockers
- none

## Result
- pending
```

Do not switch artifact paths mid-run unless the original artifact is unavailable and the operator is told exactly what changed.

## Final delivery rules

A durable Discord task is not complete just because the worker finished. Completion requires a human-readable update in the originating Discord control plane.

The final update should include:

- what changed
- verification run
- commit hash, if any
- closeout gate result, if applicable
- blockers or residual drift
- next action, if any

If an explicit proactive Discord acknowledgement was sent at the start, use an explicit proactive Discord final update too.

## Failure behavior

If durable lane creation fails:

- report the concrete blocker
- do not silently fall back inline
- do not spawn a second lane unless explicitly approved

If the preferred worker lane is unhealthy:

- report the lane failure
- use fallback only if policy allows it
- preserve Discord as the control plane
