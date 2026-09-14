# Reconstruct the reference runtime

This package reproduces the customized OpenClaw **2026.9.3** source used by this
reference architecture. It contains the complete patch from the official release,
including earlier retained capabilities, runtime fixes and their regression tests.
Two company-specific labels in one test fixture are replaced with Company Alpha.
Production source files are unchanged from the deployed source.

The [manifest](manifest.json) pins the upstream tag and commit, original deployed
source, sanitized source tree, patch checksum and toolchain. The
[source changes chapter](../docs/20-runtime-source-changes.md) explains the changes
and the boundary between source reconstruction and installation.

## Source identities

| Identity | Value |
|---|---|
| Official tag | `v2026.9.3` |
| Official commit | `1391f7cd2d40ab5bbcf2f5f831d3a64f520e72d7` |
| Deployed custom commit | `7fbb56e134a70c63d1404af547db71bd4870dc3c` |
| Sanitized source tree | `d300952e487e0979151a201fc5ec7225b4a0b890` |
| Patch SHA-256 | `bf70682c84cc0b84bdf653a9b6b1b4a9496591d61e3d5a5ea08dadc5f6465c5d` |
| Build tools | Node.js `24.16.0`, pnpm `12.3.4` |

The patch is 1,694,393 bytes and changes 518 paths across 62 local commits. The custom commit is a lineage
identifier; it is not a promise that GitHub's upstream repository contains that
commit. Reconstruction starts from the public official tag and uses this patch.

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

`git write-tree` must print `d300952e487e0979151a201fc5ec7225b4a0b890`.
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

The public patch now includes deployed source `7fbb56e`, including five commits
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

Fresh hosted reconstruction, native checks and build qualification for this updated
public pin remain pending. Earlier green workflow runs qualify their recorded
predecessors; they do not qualify this new package.

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
