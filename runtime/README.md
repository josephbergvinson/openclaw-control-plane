# Reconstruct the reference runtime

This package reproduces the customized OpenClaw source identified by this
reference architecture. It contains the complete patch from the official release,
including earlier retained capabilities, runtime fixes and their regression tests.
Two company-specific labels in one test fixture are replaced with Company Alpha.
Production source files are unchanged from the pinned source.

The [manifest](manifest.json) pins the upstream tag and commit, deployed and reference
source, sanitized source tree, patch checksum and toolchain. The
[source changes chapter](../docs/20-runtime-source-changes.md) explains the changes
and the boundary between source reconstruction and installation.

## September 22 upgrade

The upgrade targets official `v2026.9.5`, commit
`ec9c1a13db8938e5a3eaa51fca2e981cde2395a9`, with the retained custom changes
ported to the target's task, database and provider owners. The complete export is
rebuilt from that official base, rather than stacking the old patch on a stock
installation. See the [source map](../docs/20-runtime-source-changes.md#september-22-upgrade).

The originating gateway first activated custom source
`287be510c328262a8b093fd2aea1f07d51c8508e` on September 22 at
`05:57:49.051232Z`. That upgrade completed native migration to state schema 17
and agent schema 21. Its dated activation and capture evidence remain historical.

The preceding repair source `1fb1f66cfe29267902a768417b35e4a8c5c4e439` activated at
`11:22:01.365523Z` through the same-version release owner. Successor
`085711060c8477df71066402330eb5a2902d6824` activated at `12:37:05.013053Z`.
The manifest records this exact release and activation-receipt hash. Fresh postflight
verified the selected process, health/readiness and preserved configuration, account
ordering and scheduled-job definitions. Journal capture was visually accepted and
enrolled for the new process; new-process scheduled synchronization remains unproven.

The signed app from source `1fb1f66cfe29`, build `2609000592`, was separately installed,
launched and observed connected on the originating Mac Mini. Signature and contents
were verified. One macOS-added app-root metadata attribute was explicitly reconciled;
unchanged metadata throughout is not claimed. MacBook installation, native
credential/account acceptance and hosted checks for this public revision remain
pending. A bounded native-goal final passed with exact Discord readback on `1fb`;
longer-workflow delivery and backend settlement remain separate acceptance scopes.

The currently activated Gateway source is `085711060c8477df71066402330eb5a2902d6824`.
It retains the native credential correction, native-goal progress and final-delivery
repair, registered-project delegation guidance, source CLI guidance and packaging
pin repair. It additionally fixes existing Discord thread-channel send receipts:
the provider records the actual thread identity for every chunk and the final
aggregate, allowing strict source-delivery custody to recognize the visible final.
Fourteen focused producer and composed settlement cases pass. The successor is
activated; its fresh Discord diagnostic stopped on a provider rate limit before
child creation, so actual live settlement acceptance remains pending. The signed companion and its
matching private worker remain at qualified source `1fb`; this Gateway channel
producer change does not require another native build.

The reconstructible candidate is now `189ec1fad1e017b8b14b54b2ce364cefcaf9f7f1`.
It corrects quota recovery after completed tool work and account-bound WebSocket
reuse. Eligible recovery advances through the existing authorized account order,
keeps the same model and committed transcript, and does not replay the original
task. User-pinned accounts, cancellation, active tool work and exhausted candidates
retain their existing boundaries. Both prompt-level and assistant-level failures
preserve the aggregate side-effect guard after continuation.

The transport now binds reuse to the effective endpoint and handshake headers.
An account or credential change opens a fresh connection with the completed tool
history and without the previous connection's response ID or account-bound
encrypted reasoning. The identity digest stays private in memory. Focused tests
include five production-runner cases with exactly one physical write each and
59 owning transport cases. Full hosted qualification and live acceptance of this
candidate remain pending. CI corrections remove stale size and assertion counts, distinguish test fixture
wrappers from production functions, and align the existing settle-wake expectation.
The fixture rename preserves all call sites and passed its 135 owning tests.

The candidate also restores visible activity when a completed worker resumes its
original requester. That continuation retained final-delivery custody but could
bypass typing, progress drafts and narration while the model continued working.
The repair carries a run- and session-bound presentation capability through the
existing internal dispatch. Discord reuses its configured progress card and
activity narrator; private worker completion prompts are excluded from narration.
Every send or edit, including delayed writes, rechecks current ownership. Abort,
replacement and completion stop pending updates without changing the final owner.
The seven owning test files passed 90 focused cases. CI now runs those exact
agent and Discord suites and preserves their logs separately. These source tests
do not establish a fresh live progress or final-delivery result for the candidate.

Credential resolution runs in a bounded private child of the signed Mac app.
Direct Keychain access disables interaction inside that child. Existing items that
trust Apple's reader can use the fixed signed `/usr/bin/security` path after exact
item, metadata, host, unlocked-store and stored-ACL checks. A metadata-only class
read materializes a legacy persistent reference before ACL inspection. The app
validates its helper before launch and rechecks prompt and execution authority
after resolution; secret values remain in the private native pipe and secure field.
The compatibility reader has no no-UI switch: a lock or ACL race can briefly prompt.
Its process group has a deadline and is joined on cancellation or timeout.

The final native product/test-target build, 28 focused native cases and four
bounded-process cases passed. The disposable Keychain checks preserved six exact
byte variants, rejected locked or untrusted items, revalidated metadata and removed
their private store without changing canonical settings. The lock-after-admission
case refused without a value and joined at its deadline. Visible OS-dialog
disappearance was not inspected and is not claimed. Eighteen runtime cases passed
for native-goal progress, final custody and dispatch. These source and process
results are distinct from the now-verified signed package and Mini installation;
actual account entry and complete workflow delivery remain separate acceptance
steps. The bounded native-goal readback above proves only its stated scope.
Earlier native source `c4bee4279fdf` retains its original 18-test and four-case
synthetic qualification; those results alone did not qualify the subsequent
compatibility reader.

In the manifest, `source.referenceCommit` and `source.normalizedTree` identify what
CI reconstructs, while `source.deployed*` retains the verified `085711` gateway
activation. `candidateOnly` is true and `source.referenceDeploymentStatus` is
`candidate-not-activated` until this successor has its own activation receipt.
The existing `components.macCompanion.candidate` record now distinguishes the
verified originating Mini installation from pending other-host and account checks.
Prior gateway and companion acceptance records retain their dates and scopes.

## Active-child follow-up correction

A later incident exposed two gaps in the earlier repair. The owned follow-up
helper supplied a new agent-to-agent reply-delivery mode when steering an already
active child. The real execution owner rejected the conflicting mode. The parent
then successfully yielded to its remaining child work, but shared terminal
preparation rendered the earlier mutating-tool error as a final warning.

The correction lets the active child retain its admitted delivery mode; it does
not change restrictions for generic agent-to-agent requests. Terminal preparation
defers the final warning only when the attempt succeeded, yielded and has
core-confirmed continuation custody. Original error, mutation and trace facts
remain intact. Failed, cancelled and unowned turns retain their warning behavior.

The send regression now uses real registered execution handles and the backend
validator; the previous test mocked that boundary. Four send cases and three
warning cases reproduced the defects before the production changes. Focused
verification passed 111 session tests, 31 steering tests, 51 terminal-preparation
tests, 50 terminal-resolution tests, two terminal-state tests and 34 payload-error
tests. An unrelated capacity-warning assertion also failed on the untouched prior
source; only its expected prefix changed to the existing plain-text `Warning:`.

The correction is committed as `18b432ca2f83a107b1bc329cedf2221f20e61639` and included
in the complete patch. The native changed-source checks passed in 739.9 seconds,
including typechecks, lint and dead-code checks, and scoped independent review
found no actionable findings. The pinned full build passed in 232.79 seconds, and
sealed-release activation selected this exact source. A bounded live check of active
and completed child follow-ups passed, followed by a separate live check retaining
a real tool error through a successful yield without a stale final warning.
**Hosted qualification of this source remains pending.** Earlier workflows do not qualify the
correction. The workflow adds the full terminal-preparation suite in its own
invocation against the reconstructed build.

## September 21 follow-up repair

The earlier repair used source `daf5771a7d868903ada1a99cf595a587027f018c`.
It was the deployment baseline before the active-child correction above.
For that earlier source, local source checks, focused regressions, the complete
build, fresh reconstruction, activation and the bounded live Discord handoff check
passed. Its [complete reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641128711)
and [steering workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641129009)
also passed at public commit `9a04c7fd5121c05c5b9cd9b13c579f9159746f24`.

The repair addresses a rejected wait after a parent sends more work to an
existing child. Completion ownership must follow the parent's current turn,
including when the child keeps the same logical task but starts another execution.
The retained execution identifier and completion identifier have different roles.
An active child receives a guarded follow-up; an idle completed child resumes
through the existing session owner. An unrelated send or watch does not confer
completion ownership.

A failed wait remains an error. The repair replaces the ambiguous “Yield
failed” presentation with an explanation that no handoff was confirmed and that
background work may still be running. It does not report delivery or completion
that has not been observed.

The complete export retains the managed dreaming-effort repair in source
`8ad9b4113ab316cd6f84fc4486ea2a7c9dff887f`, following the previous public reference
`3498293635372bd128eb4d3cddd520ffb0cfb6ae`. Reconstruction starts at official commit
`1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7`; this is not an incremental patch from 8ad9.

The updated workflow adds focused follow-up, ownership, warning and harness
receipt tests, followed by the requester-wake end-to-end suite using the runtime
built in the same job. The embedded receipt check covers its two changed cases;
it is not a passing result for that entire tool-handler suite. Results must identify
the newly generated manifest and normalized tree. Runtime activation, the controlled
live handoff and destination readback are recorded separately below; those results
do not replace hosted qualification or acceptance on another installation.

## Source identities

The [manifest](manifest.json) carries the exact identities without requiring a
second manually synchronized version table:

| Identity | Manifest field |
|---|---|
| Official repository, tag and annotated tag object | `upstream.repository`, `upstream.tag`, `upstream.tagObject` |
| Official source commit and tree | `upstream.commit`, `upstream.tree` |
| Custom reference commit and tree | `source.referenceCommit`, `source.referenceTree` |
| Public reconstructed tree | `source.normalizedTree` |
| Patch filename, bytes and SHA-256 | `patch.file`, `patch.bytes`, `patch.sha256` |
| Exact build tools | `toolchain.node`, `toolchain.pnpm` |
| Candidate/deployment status | `candidateOnly`, `source.referenceDeploymentStatus` |
| Separately scoped companion evidence | `components` |

The custom commit identifies source lineage; it need not exist in the public
upstream repository. Reconstruction uses the official tag plus the complete patch.
The only export normalization replaces two labels in one lane-contract test
fixture. Every other blob and every file mode must match the committed custom
source. Previous deployment evidence remains separately dated; an export never
advances a runtime selector or changes a live service.

## Check and apply

Use a separate clone that no service or other task is using. Do this before installing
dependencies. The helper refuses dirty checkouts, including untracked and ignored
files, linked worktrees and shared object stores. It requires the exact official
commit and annotated tag object.

The clone below accesses GitHub. The Python helper performs no fetch or other network
operation and disables Git's lazy fetching. Obtain this reference repository and
review its manifest, patch and helper before running them.

```bash
# Run in the reference repository; the manifest selects the official source.
reference_root="$PWD"
reference_tag=$(python3 -c 'import json; print(json.load(open("runtime/manifest.json"))["upstream"]["tag"])')
git clone --depth 1 --branch "$reference_tag" https://github.com/openclaw/openclaw.git /path/to/openclaw-reference
python3 /path/to/openclaw-control-plane/scripts/reconstruct_runtime.py /path/to/openclaw-reference
python3 /path/to/openclaw-control-plane/scripts/reconstruct_runtime.py /path/to/openclaw-reference --apply
```

The first helper invocation checks the checksum, checkout identity and patch
applicability. `--apply` applies and stages the patch, then verifies the resulting
Git tree. It does not commit, install packages, build, activate OpenClaw, copy account
state or alter another checkout. Keep this clone exclusive while applying the patch.
An error after application leaves the isolated checkout available for inspection;
the helper never resets it or attempts automatic rollback.

After a successful application, record the source under your own Git identity:

```bash
cd /path/to/openclaw-reference
git write-tree
git switch -c reference/openclaw-custom
git commit -m "Apply the pinned OpenClaw reference runtime"
```

`git write-tree` must match `source.normalizedTree` in the manifest.
Your commit ID will differ because commit author, timestamp and history are local.
The pinned source tree is the reproducibility check.

## Validate and build

Use the exact Node.js and pnpm versions in the manifest. The following commands
run inside the reconstructed checkout. Dependency installation accesses package registries and runs the upstream
installation process; it is separate from the offline reconstruction helper.

```bash
node --version
pnpm --version
pnpm install --frozen-lockfile
reference_base=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["upstream"]["commit"])' "$reference_root/runtime/manifest.json")
node scripts/check-changed.mjs --base "$reference_base" --head HEAD
pnpm build
```

Follow the reconstructed repository's `AGENTS.md` and native testing guidance for
focused tests and platform prerequisites. macOS checks require upstream's pinned
Swift tooling, including SwiftLint 0.65.1. Run resource-heavy checks serially on
memory-constrained hosts. Retain actual command results; do not weaken checks to
make a new installation match a historical report.

The source tree is reproducible. Build timestamps, operating systems, native
dependencies and local configuration can change output bytes. A successful build
also does not demonstrate connected accounts, device permissions, channel delivery,
scheduled execution or restoration on your host. Continue with the
[adoption guide](../docs/17-adoption-guide.md) for that setup.

The [reference verification workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/verify-reference.yml)
also performs a fresh reconstruction, frozen dependency installation, native
changed-source checks and full build on a standard Ubuntu runner. It uses the same
pinned source and commands above, with no operator accounts or host state. Inspect
the actual workflow result for the commit being adopted; the presence of the
workflow is not a passing build. Linux build results do not replace the macOS
installation and permission checks in the adoption guide.

## License

The incorporated OpenClaw source is MIT licensed. Retain its [LICENSE](LICENSE) and
[third-party notices](THIRD_PARTY_NOTICES.md). This package contains no runtime
credentials, host configuration, conversation history or compiled dependencies.

## Hosted qualification

The reference workflow executes the pinned runtime's native changed-source plan.
Its small adapter preserves every planned command and environment, adding only
`--split-core --threads=1` to the full `lint` command. Native shard deadlines,
Control UI i18n verification, extension and script lint, Stylelint, and checks after
lint remain in the plan. An unexpected plan shape fails before execution.

A separate job reconstructs the same pinned source and runs `pnpm build`. The final
`reconstruct-and-build` check requires repository tests, all native checks, and the
build to succeed. Command plans, actual command outcomes and build logs are retained
as workflow artifacts; environment values are not recorded in the plan. A cancelled
run or partial command log does not qualify the reference.

### Current source qualification

Reference `18b432ca2f83a107b1bc329cedf2221f20e61639` passed all 279 focused tests
listed above and the native changed-source checks for its six changed paths.
The unchanged public helper passed preflight and application in a fresh standalone
clone of the exact official release. Comparison of all 39,499 tracked entries
confirmed matching production blobs and every file mode; only the two established
fixture labels differ. The pinned full build passed in 232.79 seconds on the clean
source, with build-info SHA-256
`f7f5c94110df6ea497843194c8fe7d3e6924ba7850a9a1feac76962a7617e4af`.
Both bounded live checks passed as recorded below. Hosted qualification of this
source remains pending.

### Active-child correction deployment

The sealed release `openclaw-2026.9.3-18b432ca2f83-20260921T2140Z-selfcontained`
was activated at 21:52:46 UTC. The loaded gateway was verified on the exact
reference source. The activation receipt has SHA-256
`b3c8ef5b28b254606637e838ee4ccc3b155bfc118cde340a98c1b8b5f00c0f19`.
Readback confirmed unchanged configuration, scheduled-job definitions,
authentication-profile ordering and native companion process identities. Discord
and Telegram were connected. A capture check qualified the current gateway process;
it does not establish natural scheduled synchronization.

The first controlled live check steered a running child through its original
execution, then sent another follow-up to that completed child. The second follow-up
started a new execution with the same completion-task identity. Its successful
`yielded` result was followed by child completion and requester continuation.
Exactly one final message was read back from Discord at 21:55:24.807 UTC,
103.310 seconds after acceptance, with no intermediate failure warning.
The first wait was skipped because completion was already queued; this was one
successful yield.

The deliberately nonzero shell exit in that first check was returned with
`isError: false`; it did not establish warning deferral after a retained tool error.
A separate focused check exercised that exact boundary. A follow-up steered the
original child execution, then a send to an absent synthetic target returned
`isError: true`. The trace retained that `sessions_send` failure with
`mutatingAction: true` and `executionStarted: true` when the parent successfully
yielded. The persisted yield and successful attempt emitted no final warning.
The child completed through the original execution, the parent resumed automatically,
and exactly one final Discord message was read back at 22:01:36.345 UTC,
117.275 seconds after acceptance. Both the readback during the yield and the final
readback contained no intermediate failure warning; the synthetic target remained
absent. These controlled checks cover the exercised handoffs and warning boundary,
not every concurrent or long-running workload.

### Earlier September 21 qualification

Reference `daf5771a7d868903ada1a99cf595a587027f018c` passed the unchanged native
changed-source checks for its 24 changed paths in 883.1 seconds. Focused verification
passed 507 tests across the selected full suites, four separately filtered cases,
and all 16 requester-wake end-to-end cases. The broad embedded tool-handler suite
is not covered by a passing-suite claim. Two review findings were corrected with
failing-then-passing regressions and independently inspected after correction.
The complete pinned `pnpm build` also passed in 225.79 seconds on that clean source.

A fresh standalone clone of the exact official tag passed the unchanged public
helper's preflight, apply and normalized-tree checks. Comparison of all 39,499
tracked entries confirmed matching production blobs and every file mode against
the committed source. Only the two established fixture labels differ. The resulting
tree and patch digest are retained in the [manifest at public commit `9a04c7f`](https://github.com/josephbergvinson/openclaw-control-plane/blob/9a04c7fd5121c05c5b9cd9b13c579f9159746f24/runtime/manifest.json).

The [complete reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641128711)
and [steering workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641129009)
passed at public commit `9a04c7fd5121c05c5b9cd9b13c579f9159746f24`, whose manifest
pins source `daf5771a7d86` and normalized tree
`5048932811a2d8acbdd4e52bc7ad0e7e7d056da9`. These results qualify that earlier source,
not the active-child correction above.

### September 21 deployment and live check

The sealed release `openclaw-2026.9.3-daf5771a7d86-20260921T1821Z-selfcontained`
was activated at 18:39:11 UTC. Readback confirmed the selected commit and process
entrypoints, healthy endpoints, preserved configuration, scheduled-job definitions,
authentication-profile ordering and native companion process identities. Discord
and Telegram were connected. Current-process native screen capture was qualified;
this was not a new companion installation or proof of scheduled capture.

A bounded live diagnostic completed phase one in one native child, then sent a
second request to that same child. The follow-up receipt retained a stable completion
identity and explicitly promised completion delivery. Its `sessions_yield` returned
`yielded`; phase-two completion resumed the parent and exactly one final message
was read back from Discord at 18:41:39.824 UTC, 35.974 seconds after acceptance.
The first attempted wait was skipped because phase-one completion was already
queued; the check demonstrates one successful yield, not two.

This establishes the exercised follow-up, continuation and delivery path. It does
not establish every concurrent-child, transport-failure or long-running workload.
The [delivery chapter](../docs/13-delivery-and-control-surface.md#september-21-follow-up-delivery-incident)
connects the result to the incident and source repair.

### Historical September 15 qualification

The earlier package reconstructed reference `34982936353`, retaining the
previously accepted gateway repairs and adding the local Mac SecretRef
bootstrap described in the [source map](../docs/20-runtime-source-changes.md#mac-companion-authentication).
The following qualification is bound to that earlier source.

A fresh standalone clone of the exact official tag passed the unchanged public
helper's preflight, apply and normalized-tree checks. Comparison of all 39,495
tracked entries confirmed matching production blobs and every file mode against
the new source. Only the two established fixture labels differ.

The signed companion and its matching bundled worker were activated on 15 September
from source `275f120c13b`, build
`2026.9.3-275f120c13b8-2026-09-15T11-39-13.000Z`. All 18 post-activation metadata
checks passed. Native executable lookup, a valid PNG screen capture, cursor query
and execution cleanup passed. An ordinary registered-project agent read completed
in 23.494 seconds and verified the requested path, source commit and heading.
Rejected probe setup attempts are excluded from these accepted results.
The gateway process remained on `b3ee068c6c6` throughout the handover.

The retired standalone CLI node is persistently disabled in launchd; its plist
and pairing identity are retained. The native app and gateway remain enabled,
and their processes were unchanged by that startup correction. To roll back,
first retire the native app and its workers, then enable the retained CLI service
before bootstrapping its plist. A bootout alone does not prevent startup at the
next login.

These checks do not establish live gateway-token rotation, iOS operation or
protected native Apple/Mac password entry. Rotation and connection invalidation
have focused synthetic test coverage; broader production readiness is not claimed.

The first companion reference candidate's [hosted native plan](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34964946596)
passed full typechecking but rejected the resolver's intentional test export in
its production-only dead-export scan. Its build and [steering checks](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34964946780)
passed; those results do not qualify this corrected reference. The two-line Knip
classification correction is recorded in source `34982936353`.

The corrected package passed the [complete reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34968616919)
and [steering workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34968616962)
at [candidate manifest `38b00d7`](https://github.com/josephbergvinson/openclaw-control-plane/blob/38b00d7dcc2226c28bcbb83a9edaf518befafea2/runtime/manifest.json).
The preserved results contain all 34 native commands with zero exit codes, the
complete runtime build, all ten steering cases and the focused ownership and cron regression suites.
Both the build and native-plan artifacts identify normalized tree
`4afd5efe52ae0ba136f738538477f4256500bc5a`.
The candidate manifest binds that tree to source `34982936353` and the exact patch
digest. Its later qualification follow-up changed only prose; it did not represent
a separate full CI run on the prose-only follow-up commit or the current reference.

The companion acceptance covers local registered-project routing. Remote
node-backed Codex placement requires its own verification.

The following gateway evidence retains its earlier deployment identity.

Deployed `b3ee068c6c6` passed all required native checks, including production
and test types, all 17 core-test type graphs, extension checks, lint and the
remaining state, schema, media, sidecar, cycle and authorization guards. The new
Discord preflight regression covers valid host ownership, an unbound builder and a
retired owner. Scoped independent review passed.

The complete build, including the UI, passed. On 15 September `b3ee068c6c6` passed
sealed-release activation, current-process health/readiness checks and protected
configuration, authentication and approval-state verification. The private
operator source was published and its exact remote revision verified. A fresh
Journal capture and process-binding renewal passed on the activated release.

A subsequent real Discord request passed visible progress, in-flight steering,
a native registered-project helper, verified temporary file effects across two
projects, requester continuation and final delivery. No timeout or compaction was
observed. The [delivery chapter](../docs/13-delivery-and-control-surface.md#activated-successor-check)
retains the scope and timings. A separate Discord-directed browser check used the
native opaque credential alias, verified the configured account and two pages with
live data, and closed its test tab after the requested follow-up. No timeout or
compaction was observed. At that check, other credentials remained unprovisioned; those
checks did not establish every account or workflow. Hosted qualification is separate:
inspect the matching public commit's reference and steering workflow results.

Predecessor `1f38d05` passed checks, build and activation but failed its ordinary
Discord test before the provider request. That failure remains recorded alongside
the correction; earlier passing live results below retain their original revision.

The first hosted ownership suite exposed an outdated test that changed only a
shared session row instead of the incoming turn's admitted permission. Reference
`a9aa626` corrects that fixture while retaining rejection for guarded admission and
adds a case proving that a later shared-row value cannot replace admitted authority.
The old failure was reproduced locally; all 20 cases in the corrected test file,
its selected native checks and scoped independent review passed. No production
change or second activation was needed. Hosted qualification of that test reference
belongs to its matching public commit, not an earlier green job.

### September 14 predecessor qualification

The predecessor public patch included deployed source `7fbb56e`, including five commits
after the former `39d61ba` pin: retained cron configuration revisions, exact Discord
message reads, continuation progress and typing, optional silent heartbeat results,
and durable requester ownership through child completion. The normalization still
changes only two labels in the same test fixture; production source is preserved.

A fresh independent clone of the official tag passed the published reconstruction
helper's apply and normalized-tree checks. Comparison of all 39,482 tracked entries
with deployed source confirmed identical production blobs and all file modes; the
sole difference was the two established test-label substitutions. The repository's
41 unit tests and seven native-plan adapter tests also passed. These checks verify
the package and reconstruction mechanics, before hosted runtime qualification.

The originating host passed the current source's production/test type checks,
required native checks, full build and sealed-release activation. Four subsequent
Discord checks verified the observed calendar, document-steering and controlled
worker delivery paths. The [delivery qualification](../docs/13-delivery-and-control-surface.md#successor-live-retest)
retains their timings and the limits on attachment-byte and resumed-typing evidence.

Hosted results for that predecessor qualify only its recorded source identity;
they do not qualify the successor package.

### Historical qualification

The steering and cron changes in predecessor source `f31686e` were qualified together in
[run 34659951151](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34659951151):
ten steering regression cases, 525 tests across twelve native test files, all 34
native check commands, and the full build passed against its normalized tree
`cc032d12126f13922aad858d6104ec1c62e71abc`.
That predecessor canonical source also completed a full macOS build. These are
source and build results; they do not establish another host's channel or device
acceptance. The [steering regression workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/steering-regression.yml)
now exercises the included fix directly, while the reference workflow retains the
complete native-check and build requirements.

Published base `707dc70` also passed [qualification run 34669121956](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34669121956):
reference checks, the full native plan, the runtime build and the aggregate gate.
That qualification covered the predecessor runtime; it is historical evidence for
the changes retained here, not qualification of the newly pinned source.

The originating host separately passed a naturalistic two-message Discord steering
case on the adopted `f31686e` release on 12 September, with one visible final response and
no premature compaction. See the [source chapter](../docs/20-runtime-source-changes.md)
for the observed scope and limits; this does not replace an adopter's live checks.

The predecessor candidate, source `a8cbb37`, passed on 12 September 2026 the originating host's native
changed-source check against `f31686e`; every selected guard completed. One full
`pnpm build`, including the UI, also passed. The self-contained staged release
passed the direct runtime policy import and was sealed against its complete file
inventory. Source identity and a clean checkout were verified throughout.
These were local source and candidate-artifact results. Hosted qualification of
that update was pending at the time; the earlier workflow results above retain
their original scope.

Source `39d61ba` additionally prevents failure alerts when an active automation is removed, while retaining cancellation history and isolating a replacement with the same ID. Its 62 focused tests passed, and independent P0–P2 review found no actionable issues. On 13 September 2026, this source passed the originating host's full native changed-source check against `a8cbb37`, followed by one complete build including the UI. The self-contained release passed the direct runtime policy import and was sealed; the source commit and clean checkout remained unchanged. These results qualify the local candidate, with activation and live acceptance recorded separately below.

Source `39d61ba` was activated on 13 September 2026 after an idle native suspension
and a fresh stopped-state rollback snapshot. The activation receipt verified the
selected release and directly owned gateway/node processes. Subsequent health and
readiness checks passed; Discord and Telegram were connected with successful
probes, and supported scheduler listing and history reads succeeded. The configured
authentication profile order was preserved.

A fresh Journal window capture passed through the native scheduled-command route;
the actual image was inspected and its permission/process binding renewed. This
establishes that manual capture route after activation. Natural scheduled Journal
synchronization remains a separate check.

The 13 September qualification did not repeat the updated Discord `/goal`
acknowledgement, privacy, progress and final-delivery user path on `39d61ba`;
browser sign-in was pending then. The later ordinary-request checks on `7fbb56e`
are recorded above, but do not establish fresh slash-command or interaction-expiry
acceptance. Those paths retain their separate live-check requirement.
Adopters should complete the [live acceptance checks](../docs/17-adoption-guide.md)
against their own installation.
