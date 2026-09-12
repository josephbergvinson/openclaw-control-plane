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

## Qualification history

The original four-case baseline reproduced the source defect in
[run 34657404147](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34657404147):
three controls passed and `adopted-current` failed because injection was called
zero times instead of once. Native exit was one with no unhandled errors and all
four cleanup paths completed. This is the expected assertion failure, not a
passing baseline. Earlier setup failures and pool observations remain recorded in
`source-parity.json`; they do not establish a production-incident cause.

The eight-file steering overlay passed all ten cases in
[run 34658769048](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34658769048).
The combined steering and two-file cron overlay then passed
[run 34659951151](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34659951151)
at reference commit `6795d19633907166fcac2c24dfbff99bd8186321`:

- The unchanged baseline produced its exact expected assertion failure.
- All ten steering cases passed with no unhandled errors.
- All 525 tests across twelve native files passed, including fourteen cron tests.
- The complete native plan ran all 34 commands successfully.
- The full build passed against the same normalized source tree.

That qualified tree is `cc032d12126f13922aad858d6104ec1c62e71abc`, the sanitized
derivative of canonical source `f31686e33ce3fa8564cecf97e3f36f39a69f57dd`. The
canonical source subsequently completed a full macOS build. Source qualification,
local packaging and live acceptance remain distinct facts.

`candidate-overlay.json` preserves the exact original base and ten before/after
hashes. `candidate.patch` and `cron-zero-timeout.patch` remain separate historical
overlays. `apply_candidate.py` and the prepare/verify modes of `freeze_candidate.py`
are tied to that older base and fail closed on a different generation. To replay
the complete historical qualification, use reference commit `6795d196` and its
manifest/workflow together. Do not replace the historical base hashes with the
current reference tree.

The 91-file source-parity record describes the original bounded comparison, not
the complete import graph. A successful regression or build does not prove that
the original Discord incident took this path, that a provider saved the steering
message, or that an adopter's channels and devices work.
