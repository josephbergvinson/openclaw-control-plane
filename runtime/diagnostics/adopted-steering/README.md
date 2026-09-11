# Adopted queue source steering baseline

Copy `test/isolated-queue` into the root of the reconstructed OpenClaw checkout after its frozen dependency install. The fixture resolves all source and native Vitest imports relative to that checkout. Run from the checkout root:

```sh
OPENCLAW_VITEST_MAX_WORKERS=1 node scripts/run-vitest.mjs run --config test/isolated-queue/vitest.queue-unit-dormant-boundary.config.ts test/isolated-queue/adopted-drain-steering.unit-dormant-boundary.test.ts --pool=forks
```

The current fixture combines finite stderr labels with eleven pass-through probes around input normalization, conversation preparation, runtime policy, thinking runtime, native context/admission/execution preparation, current images, transcript-recorder construction, reply threading and bundled-channel lookup. Ten probes wrap native unmocked exports. The image probe wraps the existing async mock by capturing its implementation before replacement, avoiding recursive invocation of the mutable spy. Each probe forwards the original arguments and `this`, returns the original result, and rethrows synchronous errors. The four async probes attach settlement observers to the original returned promise; diagnostic write failures cannot reject those observers. Before the existing `afterEach` cleanup, native spies are restored and the image mock regains its captured implementation. No timers or keepalive output are added. The four cases, assertions, lifecycle behavior and native 120-second no-output watchdog are unchanged.

`source-parity.json` retains the original baseline hash, the prior labels-only and preparation diagnostic hashes, and the current fixture hash. The current diagnostic also splits initial argument construction from the native call and corrects stale comments. Recovering a prior fixture requires reversing the complete diagnostic delta; simply removing labels is no longer sufficient.

Two hosted attempts using the inherited `threads` pool ended with exit 143 from that watchdog and no completed case verdicts. In the second attempt, completed load/transform events stopped after about 15 seconds. A third attempt using `forks` completed collection and `beforeEach`, then entered the empty-queue control and stopped at initial request preparation. A fourth attempt, also using `forks`, completed argument construction and both native context and admission preparation. Execution preparation returned a promise that did not settle before the watchdog. Both forks attempts also ended with exit 143 and no completed case verdict. The current probes distinguish the image await, transcript-recorder construction and synchronous reply-threading/plugin lookup. The cause remains unresolved; no reply-policy value or production source has been changed.

The command above and the diagnostic workflow retain `--pool=forks`, one worker, isolation and the native runner. Switching pools did not eliminate the observed stall. A result under `forks` applies to that configuration and does not by itself resolve the original `threads` stall or establish its cause.

The four existing scenarios are unchanged: an empty-queue steering control, the adopted current source, an older ready predecessor, and a different tool-authority request. The adopted current source asserts steering; the suspected baseline defect is expected to fail that assertion while the controls pass. That outcome has not yet been demonstrated. Preserve the actual native exit code, case results and logs, including unexpected setup failures. A cancelled run or empty test report does not establish the defect.

The real queue, followup admission, operation registry, active steering and authority comparison run in process. A mock backend accepts message injection. This tests runtime routing and lifecycle callbacks; it does not prove provider transcript persistence or Discord behavior.

The config removes only the eager compiled-subprocess plugin. No scenario executes a child backend or reads compiled child artifacts. Future queued execution, fresh model execution and session reset callbacks record and throw if invoked. The remaining native schema plugin, aliases, worker setup and single-worker runner remain in use. Queue cleanup runs before the active operation is released.

`source-parity.json` records a bounded comparison between the canonical source and reconstructed source used to prepare this fixture. It does not cover the complete transitive source graph or establish a passing test result.
