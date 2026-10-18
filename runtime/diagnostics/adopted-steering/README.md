# Adopted queue source steering regression

The reference runtime includes the adopted-source steering fix and the separate
explicit-zero command timeout correction. The workflow tests the reconstructed
source directly. The patch files and original baseline records in this directory
remain historical qualification evidence; do not apply those overlays on top of
the current runtime package.

## Current regression command

After reconstructing the source and completing its frozen dependency install,
copy `test/isolated-queue` into the reconstructed checkout's `test` directory. Run
from that checkout root:

```sh
OPENCLAW_VITEST_MAX_WORKERS=1 node scripts/run-vitest.mjs run --pool=forks --config test/isolated-queue/vitest.queue-candidate.config.ts test/isolated-queue/adopted-drain-steering.unit-dormant-boundary.test.ts test/isolated-queue/adopted-drain-steering.owner-fifo.test.ts --reporter=verbose --reporter=junit --reporter="$PWD/scripts/lib/vitest-resource-reporter.mts" --outputFile.junit=steering-candidate.xml
```

The ten cases cover an empty-queue control, the adopted current source, an older
ready predecessor, different authority, owner replacement after preparation, a
new predecessor, source cancellation, a parked correction retaining priority over
a later waiter, owner replacement while parked, and distinct collected sources.
The real queue, follow-up admission, operation registry and authority comparison
run in process. A mock backend accepts message injection. This verifies routing
and ownership, not provider transcript persistence or Discord delivery.

The original fixtures, assertions and cleanup are retained. They use the native
Vitest setup, schema plugin and aliases, one worker, isolation, the `forks` pool
and the native 120-second no-output watchdog. The configuration removes only an
eager compiled-subprocess plugin; no scenario executes a child backend. Future
queued execution, fresh model execution and reset callbacks record and throw if
invoked. Probe wrappers forward native arguments/results and restore spies during
cleanup. No timing deadline or assertion is weakened to obtain a pass.

Both fixture requests supply the resolved default `ReplyToMode: "off"` for their
empty Discord configuration, avoiding unrelated bundled-channel lookup. The two
partial mocks preserve native session-path and authority-policy exports through
`importOriginal` while retaining their explicit fixture overrides. This setup
does not assert that real Discord ingress always supplies the same field.

The workflow requires native exit zero, exactly the ten named cases passing, no
skipped cases and no unhandled errors. It retains native output, JUnit and the
existing verifier result. The verifier's `candidate` phase name is retained for
compatibility; the input is now the reconstructed reference source.

After these cases, the existing cleanup helper removes only the verified,
untracked diagnostic copy and its caches. The workflow then runs its twelve
explicit native ownership/queue/cron test files through `pnpm test:serial`.
Native failures remain failures. The separate [reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/verify-reference.yml)
owns the complete native-check plan and full build against the runtime manifest;
this regression workflow no longer repeats the historical overlay/build sequence.

## Historical overlays

`candidate.patch`, `cron-zero-timeout.patch`, `candidate-overlay.json` and
`source-parity.json` describe the earlier isolated candidate. They remain
reference inputs for that diagnostic package, not additional installation steps.
The current complete runtime patch already incorporates the applicable fixes.

Use the exact public commit's workflow results for qualification. Earlier fixture
results do not establish provider delivery, an installed runtime or a new source
revision. See the [history index](../../../docs/history/README.md) for context.
