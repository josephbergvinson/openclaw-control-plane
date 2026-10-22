# Native runtime reconstruction

[The reconstruction helper](../scripts/reconstruct_runtime.py) keeps its ordinary source behavior: check the pinned upstream tag and source patch, or apply and stage the normalized runtime source with `--apply`. It uses an existing clean standalone clone, with its own Git directory and object store.

The optional native mode applies the separately bound distribution delta and provisions its exact native archive for **macOS on Apple silicon (Darwin arm64)**. Use ordinary source reconstruction for Linux; this archive does not contain a Linux executable. Native mode requires both `--apply` and `--native-distribution`:

```sh
python3 scripts/reconstruct_runtime.py /path/to/clean-upstream-clone --apply --native-distribution
```

Start this command from the pinned upstream commit. Native mode first reconstructs
the customized runtime source and verifies its normalized tree. It then applies
the separately bound distribution overlay, verifies the distribution tree, and
provisions the archive. Completion requires both the distribution tree and archive
to match the manifest. The helper leaves source changes staged; dependency
installation, checks and build follow the [runtime procedure](../runtime/README.md).

The reference targets official OpenClaw **v2026.10.4**. The manifest binds its
customized runtime source tree, distribution overlay, optional companion source
and exact managed **Codex 0.160.0** native archive. The [source and notices](../runtime/native160/README.md)
and [provenance metadata](../runtime/native160/native160-provenance.json) identify
the dependency archive and its source. The archive is included in the runtime's
release and recovery bindings. Complete qualification using the runtime procedure,
then use the [single activation owner](10-runtime-releases-and-promotion.md) for
the installed release transition. Reconstruction does not establish an installed
deployment or user acceptance.

## Optional companion source

The current packet records installed R43 Mac companion build 2610000502 at its
original source identity. Its canonical/native source reference has a later,
qualified test-only snapshot-iteration correction. That correction changed no
production or package inputs and did not rebuild the installed components. The older companion
has no supported reconstruction delta in this packet; select ordinary source or
native-distribution mode. The following command applies only to a manifest that
supplies one of the supported companion bindings below.

Select the Mac companion explicitly, from a new clean pinned-upstream clone:

```sh
python3 scripts/reconstruct_runtime.py /path/to/clean-upstream-clone --apply --native-distribution --native-companion
```

`nativeCompanion.sourceCommit` and `nativeCompanion.sourceTree` identify the
companion source. The helper accepts either of these finite cases:

| Case | Required binding and verification |
| --- | --- |
| Exact distribution identity | `delta` is absent; `sourceCommit` equals `nativeDistribution.referenceCommit`, and `sourceTree` equals both `nativeDistribution.referenceTree` and `nativeDistribution.normalizedTree`. No extra patch is needed or accepted as a substitute for equality. |
| Separate four-file Swift delta | `delta.distributionTree` equals `nativeDistribution.normalizedTree`; `delta.patch` names one nonempty regular patch with exact `file`, `bytes` and `sha256` bindings. The patch changes exactly the four declared macOS Swift source/test paths. |

Both cases first verify the distribution, then the complete companion source tree,
including modes, and provision the same bound archive. A historical descriptive
companion identity does not grant reconstruction when these conditions fail.
`--native-companion` requires both other options; ordinary source and native modes
remain unchanged. Use the [runtime procedure](../runtime/README.md) for subsequent
checks and build. A successful tree check is separate from app installation,
loaded-process verification and real-interface acceptance.

## Native distribution fields

`runtime/manifest.json` remains schema version 1. Its optional `nativeDistribution` object has these fields:

| Field | Required binding |
| --- | --- |
| `sourceTree` | Full Git SHA-1 of the customized source tree; must equal `source.normalizedTree`. |
| `referenceCommit`, `referenceTree` | Bound distribution source identity; exact companion identity must match these fields. |
| `normalizedTree` | Full Git SHA-1 of the runtime distribution tree. |
| `patch.file` | Regular `.patch` file directly inside `runtime/`. |
| `patch.bytes`, `patch.sha256` | Exact byte count and lowercase SHA-256 of the source-to-distribution patch. |
| `artifact.file` | Exact archive basename used under `native-package-artifacts/`, ending in `.tgz` or `.tar.gz`. |
| `artifact.url` | Actual HTTPS release download URL in `josephbergvinson/openclaw-control-plane`, with the same archive basename. |
| `artifact.bytes` | Exact positive byte count. |
| `artifact.sha256`, `artifact.sha512` | Lowercase hexadecimal digests of the unchanged archive bytes. |

The native patch must change exactly `pnpm-lock.yaml` and `pnpm-workspace.yaml`. The normalized tree bindings describe source contents independently of private commit identities. Both source patches remain separately hashed. The artifact filename must match the distribution's normal local tarball dependency; release publication must preserve the retained archive bytes.

Native mode refuses incomplete bindings, source mismatches, unexpected delta paths, nonmatching final trees, URL credentials or queries, other repositories, and traversal filenames. Ordinary source-only reconstruction does not read or fetch the native declaration.

## Archive provisioning

The helper requests only the declared public GitHub release URL and accepts its normal HTTPS redirect to GitHub's release asset host. It inherits no proxy credentials, authentication handlers, or cookies. It streams into an owned temporary file, checks exact size and both digests, keeps the verified descriptor through publication, and verifies the exposed regular file before reporting success.

Publication creates the exact `native-package-artifacts/<artifact.file>` entry without replacing an existing destination. Matching existing bytes are verified; differing files, symlinks, and changed namespace entries are retained and reported. The original clean-checkout requirement still applies. Temporary cleanup removes only a matching owned file and preserves the original failure if cleanup also fails.

If an apply or later archive operation fails, the isolated checkout may contain staged source changes. The helper reports failure and leaves that state available for inspection. A successful reconstruction proves source and archive identity; normal installation, build, runtime activation, and ordinary-use acceptance are subsequent operations.

The notice packet records a finite set of selected resources and notices. It does not claim a complete static dependency license inventory or a bit-for-bit rebuild of the native executables.

## Provision an explicitly unselected candidate dependency

The same archive owner can provision a reviewed candidate packet before that
candidate becomes the selected runtime reference. Keep the active runtime
manifest unchanged and supply the exact provenance file from a pinned
control-plane commit:

```sh
python3 scripts/reconstruct_runtime.py /path/to/candidate-checkout \
  --native-artifact-provenance /absolute/path/to/runtime/native160/native160-provenance.json
```

This mode validates the existing provenance package identity, exact public URL,
size and canonical SHA-512 integrity, then requires the checkout's workspace
override and frozen lock package/version/platform/integrity to match. The lock
snapshot must remain optional. It uses the same no-replace archive verifier as
native reconstruction and rechecks its inputs after provisioning. It never
applies a source patch, stages or commits files, loads the active runtime
manifest, installs dependencies, or selects a runtime. Source reconstruction
options cannot be combined with this mode. Its result reports the actual
provenance and dependency-input digests as well as the verified archive.

For dependency installation, CI provisions the Darwin-arm64 archive only on
matching macOS ARM64 runners; other platforms retain pnpm's native optional-package
selection. Source/archive reconstruction can verify the packet on other hosts
without executing its Darwin binaries. A candidate packet or hosted test result
does not change the installed reference or establish activation acceptance.
