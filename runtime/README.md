# Reconstruct the reference runtime

This package supplies the complete customized source delta from a pinned official
OpenClaw release. Start with a separate upstream clone, verify and apply the patch,
then install dependencies and build. It does not copy an operator's account state
or install a service on your behalf.

The [manifest](manifest.json) is the current machine-readable authority. Use the
[source map](../docs/20-runtime-source-changes.md) to find the mechanisms and the
[adoption guide](../docs/17-adoption-guide.md) for workspace and host setup.
Earlier changes are summarized in the [history index](../docs/history/README.md).

## Source identities

| Identity | Manifest field |
| --- | --- |
| Official repository, tag and annotated tag object | `upstream.repository`, `upstream.tag`, `upstream.tagObject` |
| Official source commit and tree | `upstream.commit`, `upstream.tree` |
| Custom reference commit and tree | `source.referenceCommit`, `source.referenceTree` |
| Public reconstructed source tree | `source.normalizedTree` |
| Complete patch filename, size and SHA256 | `patch.file`, `patch.bytes`, `patch.sha256` |
| Exact build tools and platform prerequisite | `toolchain` |
| Native distribution tree, overlay and archive | `nativeDistribution` |
| Separately identified native companion | `nativeCompanion` |
| Scope of the existing installed reference | `referenceDeployment` |

The custom commit identifies source lineage; it need not exist in the public
upstream repository. Reconstruction uses the official tag plus the public patch.
The manifest describes deliberate fixture normalization. The resulting Git tree,
including file modes, is the comparison point; an adopter's commit ID will depend
on their own author and timestamp.

Companion and existing-deployment fields identify separate components and scope.
They neither install a companion nor attest that a new adopter is running it. The
reconstruction trees and patch/asset bindings remain the actual public inputs.

## Prerequisites

Use Git, Python 3 and the exact Node.js/pnpm versions in `toolchain`. Internet
access is required to obtain repositories and dependencies. Native macOS checks
also require the upstream project's pinned Swift tools. Run heavy checks serially
on a host with enough memory and disk headroom.

Keep three locations separate: this reference repository, reconstructed runtime
source and the eventual installed release. No service should use the disposable
checkout in which you test patch application.

## Check and apply

Run from this reference repository. Replace the destination with a new absolute
path:

```bash
reference_root="$PWD"
reference_tag=$(python3 -c 'import json; print(json.load(open("runtime/manifest.json"))["upstream"]["tag"])')
git clone --depth 1 --branch "$reference_tag" https://github.com/openclaw/openclaw.git /path/to/openclaw-reference
python3 "$reference_root/scripts/reconstruct_runtime.py" /path/to/openclaw-reference
python3 "$reference_root/scripts/reconstruct_runtime.py" /path/to/openclaw-reference --apply
```

The first invocation verifies the patch digest, exact official commit/tag object,
checkout identity and applicability. The second applies and stages the patch,
then checks `source.normalizedTree`. The helper refuses dirty checkouts, including
untracked or ignored files, linked worktrees and shared object stores. It performs
no fetch and disables Git's lazy fetching.

Ordinary mode does not install packages, build, commit, activate services or reset
an unsuccessful checkout. An error after application leaves that isolated checkout
available for inspection. Keep it exclusive throughout reconstruction.

After successful application, record the source under your own Git identity:

```bash
cd /path/to/openclaw-reference
git write-tree
git switch -c reference/openclaw-custom
git commit -m "Apply the pinned OpenClaw reference runtime"
```

`git write-tree` must match the manifest's `source.normalizedTree`.

## Optional native distribution

To reproduce the separately bound distribution instead, start from another clean
official-tag clone and use:

```bash
git clone --depth 1 --branch "$reference_tag" https://github.com/openclaw/openclaw.git /path/to/openclaw-native-reference
python3 "$reference_root/scripts/reconstruct_runtime.py" /path/to/openclaw-native-reference --apply --native-distribution
cd /path/to/openclaw-native-reference
git write-tree
git switch -c reference/openclaw-native
git commit -m "Apply the pinned OpenClaw native distribution"
```

Use this instead of ordinary `--apply`, not on an already reconstructed checkout.
Native mode applies the source patch and declared two-file distribution overlay,
then provisions the public archive at the lockfile's local dependency path. It
checks the distribution tree, archive size, SHA256 and SHA512. The local dependency
spelling does not require a private or unpublished package.

Here `git write-tree` must match `nativeDistribution.normalizedTree`, and the build
commands below run in this native checkout instead of the source-only checkout.

This mode accesses the declared public release asset. Ordinary source-only mode
does not fetch it. See [native reconstruction](../docs/21-native-runtime-reconstruction.md)
for the artifact and failure contracts, and
[native source and notices](native154/README.md) for provenance and licensing.

## Validate and build

Inside the reconstructed checkout, use the manifest's exact tool versions:

```bash
node --version
pnpm --version
pnpm install --frozen-lockfile
reference_base=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["upstream"]["commit"])' "$reference_root/runtime/manifest.json")
node scripts/check-changed.mjs --base "$reference_base" --head HEAD
pnpm build
```

Dependency installation uses package registries and upstream installation hooks;
it is separate from patch reconstruction. Follow reconstructed `AGENTS.md`,
`docs/help/testing/suites.md` and platform prerequisites for native checks. Keep actual command
outcomes rather than weakening a check to match an earlier report.

Source-tree equality is reproducible. Build timestamps, native dependencies and
operating systems can change compiled bytes. This package does not claim a
byte-identical rebuild of every native executable. Continue with the
[adoption guide](../docs/17-adoption-guide.md) for installation and user acceptance.

## Hosted qualification

The [reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/verify-reference.yml)
uses a fresh official-tag checkout and the same manifest bindings. Independent
repository, native-check and build jobs feed `reconstruct-and-build`. Its native
plan adapter only adds bounded full-lint shard selection; it preserves the other
planned commands and their failures.

The [steering regression workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/steering-regression.yml)
checks reconstructed queue/ownership behavior. Historical diagnostic overlays
are not applied on top of current source. Runtime regression tests and workspace
helper tests have separate owners and retain their actual scope.

Check all required jobs on the exact public commit being adopted. Command plans,
results and build logs are workflow artifacts; a cancelled run or partial log is
not successful qualification. Linux CI does not replace macOS installation,
device permissions or real channel/account acceptance.

## License

The incorporated OpenClaw source remains MIT licensed. Retain [LICENSE](LICENSE),
[third-party notices](THIRD_PARTY_NOTICES.md) and native resource notices when
redistributing source or artifacts. Private credentials, conversations and live
operational receipts are outside this reference package.
