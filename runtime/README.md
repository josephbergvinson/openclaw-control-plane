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

## Source identities

| Identity | Value |
|---|---|
| Official tag | `v2026.9.3` |
| Official commit | `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7` |
| Deployed gateway source | `b3ee068c6c6b1fe4f90b5313c7b07a4cb0647a47` |
| Deployed Mac companion and bundled worker source | `275f120c13b84f150a1bd2c1f9129183e535f2a3` |
| Reconstructed reference source | `3498293635372bd128eb4d3cddd520ffb0cfb6ae` |
| Sanitized source tree | `4afd5efe52ae0ba136f738538477f4256500bc5a` |
| Patch SHA-256 | `7af8cdaf648be9f3ae08e5729667b3426559551fbf1b68425b0372a4eeeb7538` |
| Build tools | Node.js `24.16.0`, pnpm `12.3.4` |

The patch is 1,919,055 bytes and changes 582 paths across 76 local commits. The custom commit is a lineage
identifier; it is not a promise that GitHub's upstream repository contains that
commit. Reconstruction starts from the public official tag and uses this patch.

The gateway remains on `b3ee068c6c6`. The reference advances the native Mac
app and its bundled private worker. The manifest explicitly scopes
`deployedCommit` to the gateway and records exact paths, blobs and file modes
under `productionDelta`, `testOnlyDelta`, `documentationDelta` and
`toolingOnlyDelta`. The
`components.macCompanion` identity records the separately accepted app and worker.
These production differences are not labeled test-only.
The reference is one tooling-only commit beyond the deployed companion:
`config/knip.config.ts` models an intentional focused-test export in the production
scan, while the full-tree scan still audits the test consumers. Every executable
source blob and file mode is unchanged from the accepted app source.

The two privacy substitutions in the lane-contract fixture remain the only
export normalization; every other exported blob and file mode matches the
new pinned reference source. The package does not claim a gateway rebuild.

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

`git write-tree` must print `4afd5efe52ae0ba136f738538477f4256500bc5a`.
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

The package reconstructs reference `34982936353`, retaining the
previously accepted gateway repairs and adding the local Mac SecretRef
bootstrap described in the [source map](../docs/20-runtime-source-changes.md#mac-companion-authentication).
Hosted qualification is bound to this source, independently of earlier green
workflows and the subsequent acceptance record.

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
classification correction is recorded in source `34982936353`. Qualification of
that exact new reference remains pending; no gate is skipped or disabled.

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
