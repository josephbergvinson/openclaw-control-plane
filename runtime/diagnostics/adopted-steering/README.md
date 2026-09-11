# Adopted queue source steering baseline

Copy `test/isolated-queue` into the root of the reconstructed OpenClaw checkout after its frozen dependency install. The fixture resolves all source and native Vitest imports relative to that checkout. Run from the checkout root:

```sh
OPENCLAW_VITEST_MAX_WORKERS=1 node scripts/run-vitest.mjs run --config test/isolated-queue/vitest.queue-unit-dormant-boundary.config.ts test/isolated-queue/adopted-drain-steering.unit-dormant-boundary.test.ts
```

The four existing scenarios are unchanged: an empty-queue steering control, the adopted current source, an older ready predecessor, and a different tool-authority request. The adopted current source asserts steering; the suspected baseline defect is expected to fail that assertion while the controls pass. That outcome has not yet been demonstrated. Preserve the actual native exit code, case results and logs, including unexpected setup failures. A cancelled run or empty test report does not establish the defect.

The real queue, followup admission, operation registry, active steering and authority comparison run in process. A mock backend accepts message injection. This tests runtime routing and lifecycle callbacks; it does not prove provider transcript persistence or Discord behavior.

The config removes only the eager compiled-subprocess plugin. No scenario executes a child backend or reads compiled child artifacts. Future queued execution, fresh model execution and session reset callbacks record and throw if invoked. The remaining native schema plugin, aliases, worker setup and single-worker runner remain in use. Queue cleanup runs before the active operation is released.

`source-parity.json` records a bounded comparison between the canonical source and reconstructed source used to prepare this fixture. It does not cover the complete transitive source graph or establish a passing test result.
