# Reconstruct the reference runtime

This package reproduces the customized OpenClaw **2026.9.3** source used by this
reference architecture. It contains the complete patch from the official release,
including earlier retained capabilities, runtime fixes and their regression tests.
Two company-specific labels in one test fixture are replaced with Company Alpha.
Production source files are unchanged from the pinned source.

The [manifest](manifest.json) pins the upstream tag and commit, deployed and reference
source, sanitized source tree, patch checksum and toolchain. The
[source changes chapter](../docs/20-runtime-source-changes.md) explains the changes
and the boundary between source reconstruction and installation.

## September 21 follow-up repair

The deployed and reconstructed source is `daf5771a7d868903ada1a99cf595a587027f018c`.
The manifest and complete patch reconstruct that source with the two established
fixture-label substitutions. Local source checks, focused regressions, the complete
build, fresh reconstruction, activation and the bounded live Discord handoff check
passed. **Hosted qualification remains pending.** Earlier passing workflows do not
qualify this source.

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

| Identity | Value |
|---|---|
| Official tag | `v2026.9.3` |
| Official commit | `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7` |
| Deployed gateway source | `daf5771a7d868903ada1a99cf595a587027f018c` |
| Historical Mac companion and bundled worker acceptance; not revalidated | `275f120c13b84f150a1bd2c1f9129183e535f2a3` |
| Reconstructed reference source | `daf5771a7d868903ada1a99cf595a587027f018c` |
| Reference source tree before fixture normalization | `5ab5fa2727e07e87cc5649da439ec18074874753` |
| Sanitized source tree | `5048932811a2d8acbdd4e52bc7ad0e7e7d056da9` |
| Patch SHA-256 | `3ea096fbc14d49a90e42a17753557d4d88969ef2ab21e614b846c70b7553acba` |
| Build tools | Node.js `24.16.0`, pnpm `12.3.4` |

The patch is 2,021,780 bytes and changes 602 paths across 78 local commits. The custom commit is a lineage
identifier; it is not a promise that GitHub's upstream repository contains that
commit. Reconstruction starts from the public official tag and uses this patch.

The gateway and reference now use the same source. The manifest explicitly scopes
`deployedCommit` to the gateway and records exact paths, blobs and file modes
under `productionDelta`, `testOnlyDelta`, `documentationDelta` and
`toolingOnlyDelta`. These endpoint deltas are empty because deployed and reference
commits match. The `components.macCompanion` identity retains the
separately accepted September 15 app and worker as historical evidence; it is not
a new companion installation or revalidation claim. Production differences are
not labeled test-only.

The two privacy substitutions in the lane-contract fixture remain the only
export normalization; every other exported blob and file mode matches the
new pinned reference source. Deployment and scoped live verification are recorded
below, separately from reproducible source export.

## Check and apply

Use a separate clone that no service or other task is using. Do this before installing
dependencies. The helper refuses dirty checkouts, including untracked and ignored
files, linked worktrees and shared object stores. It requires the exact official
commit and annotated tag object.

The clone below accesses GitHub. The Python helper performs no fetch or other network
operation and disables Git's lazy fetching. Obtain this reference repository and
review its manifest, patch and helper before running them.

```bash
git clone --depth 1 --branch v2026.9.3 https://github.com/openclaw/openclaw.git openclaw-reference
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
git switch -c reference/openclaw-2026.9.3
git commit -m "Apply the OpenClaw 2026.9.3 reference runtime"
```

`git write-tree` must print `5048932811a2d8acbdd4e52bc7ad0e7e7d056da9`.
Your commit ID will differ because commit author, timestamp and history are local.
The pinned source tree is the reproducibility check.

## Validate and build

Use Node.js 24.16.0 and pnpm 12.3.4. The following commands run inside the reconstructed
checkout. Dependency installation accesses package registries and runs the upstream
installation process; it is separate from the offline reconstruction helper.

```bash
node --version
pnpm --version
pnpm install --frozen-lockfile
node scripts/check-changed.mjs --base 1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7 --head HEAD
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
tree and patch digest are pinned above.

Hosted qualification remains pending. The workflow runs the focused lifecycle suites, the two
changed embedded receipt cases and the requester-wake end-to-end suite against its
own reconstructed build. A passing run must identify this manifest and normalized
tree; the historical results below do not qualify the new reference.

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
