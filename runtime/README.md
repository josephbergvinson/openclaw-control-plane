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
| Deployed custom commit | `f31686e33ce3fa8564cecf97e3f36f39a69f57dd` |
| Sanitized source tree | `cc032d12126f13922aad858d6104ec1c62e71abc` |
| Patch SHA-256 | `c358067965fe7079a0ea6bfcd2c31635517a8bc85c509a3b3835578fde85ae6d` |
| Build tools | Node.js `24.16.0`, pnpm `12.3.4` |

The patch is 1,511,568 bytes and changes 477 paths. The custom commit is a lineage
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

`git write-tree` must print `cc032d12126f13922aad858d6104ec1c62e71abc`.
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


The steering and cron changes in this source were qualified together in
[run 34659951151](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/34659951151):
ten steering regression cases, 525 tests across twelve native test files, all 34
native check commands, and the full build passed against this normalized tree.
The corresponding canonical source also completed a full macOS build. These are
source and build results; they do not establish another host's channel or device
acceptance. The [steering regression workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/steering-regression.yml)
now exercises the included fix directly, while the reference workflow retains the
complete native-check and build requirements.
