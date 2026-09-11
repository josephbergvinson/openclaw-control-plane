# Adopted queue source steering baseline

Copy `test/isolated-queue` into the root of the reconstructed OpenClaw checkout after its frozen dependency install. The fixture resolves all source and native Vitest imports relative to that checkout. Run from the checkout root:

```sh
OPENCLAW_VITEST_MAX_WORKERS=1 node scripts/run-vitest.mjs run --config test/isolated-queue/vitest.queue-unit-dormant-boundary.config.ts test/isolated-queue/adopted-drain-steering.unit-dormant-boundary.test.ts --pool=forks
```

The current fixture combines finite stderr labels with seven pass-through `vi.spyOn` probes around input normalization, conversation preparation, runtime policy, thinking runtime, and native context/admission/execution preparation. Each probe forwards the original arguments and `this`, returns the original result, and rethrows synchronous errors. The three async probes attach settlement observers to the original returned promise; diagnostic write failures cannot reject those observers. The probes are restored before the existing `afterEach` cleanup. No timers or keepalive output are added. The four cases, assertions, lifecycle behavior and native 120-second no-output watchdog are unchanged.

`source-parity.json` retains the original baseline hash, the prior labels-only diagnostic hash, and the current fixture hash. The current diagnostic also splits initial argument construction from the native call and corrects stale comments. Recovering a prior fixture requires reversing the complete diagnostic delta; simply removing labels is no longer sufficient.

Two hosted attempts using the inherited `threads` pool ended with exit 143 from that watchdog and no completed case verdicts. In the second attempt, completed load/transform events stopped after about 15 seconds. A third attempt using `forks` completed collection and `beforeEach`, then entered the empty-queue control and stopped at initial request preparation, again with exit 143 and no case verdict. The original marker covers both argument construction and `runPreparedReply`; the current probes narrow that boundary. The cause remains unresolved.

The command above and the diagnostic workflow retain `--pool=forks`, one worker, isolation and the native runner. Switching pools did not eliminate the observed stall. A result under `forks` applies to that configuration and does not by itself resolve the original `threads` stall or establish its cause.

The four existing scenarios are unchanged: an empty-queue steering control, the adopted current source, an older ready predecessor, and a different tool-authority request. The adopted current source asserts steering; the suspected baseline defect is expected to fail that assertion while the controls pass. That outcome has not yet been demonstrated. Preserve the actual native exit code, case results and logs, including unexpected setup failures. A cancelled run or empty test report does not establish the defect.

The real queue, followup admission, operation registry, active steering and authority comparison run in process. A mock backend accepts message injection. This tests runtime routing and lifecycle callbacks; it does not prove provider transcript persistence or Discord behavior.

The config removes only the eager compiled-subprocess plugin. No scenario executes a child backend or reads compiled child artifacts. Future queued execution, fresh model execution and session reset callbacks record and throw if invoked. The remaining native schema plugin, aliases, worker setup and single-worker runner remain in use. Queue cleanup runs before the active operation is released.

`source-parity.json` records a bounded comparison between the canonical source and reconstructed source used to prepare this fixture. It does not cover the complete transitive source graph or establish a passing test result.
