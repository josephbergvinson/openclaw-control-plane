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
| Deployed custom commit | `81a38d9766169ddade96b44e43fafab78ec5589d` |
| Sanitized source tree | `aa9a540d48dc21fbbd9047001d136a4eaddfda31` |
| Patch SHA-256 | `202e3d6a2ac08706cab41a68220804e4d68f53d8dfe242d150d36b65dbf8d8d9` |
| Build tools | Node.js `24.16.0`, pnpm `12.3.4` |

The patch is 1,493,941 bytes and changes 470 paths. The custom commit is a lineage
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

`git write-tree` must print `aa9a540d48dc21fbbd9047001d136a4eaddfda31`.
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
