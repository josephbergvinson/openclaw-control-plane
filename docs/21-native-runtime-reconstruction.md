# Native runtime reconstruction

[The reconstruction helper](../scripts/reconstruct_runtime.py) keeps its ordinary source behavior: check the pinned upstream tag and source patch, or apply and stage the normalized runtime source C with `--apply`. It uses an existing clean standalone clone, with its own Git directory and object store.

The optional native mode applies the separately bound C→D distribution delta and provisions the exact native archive used by D. It requires both `--apply` and `--native-distribution`:

```sh
python3 scripts/reconstruct_runtime.py /path/to/clean-upstream-clone --apply --native-distribution
```

Start this command from the pinned upstream commit. Native mode applies C internally, verifies its normalized tree, applies the native delta, verifies normalized D, then provisions the archive. Completion requires both D and the archive to be verified. The helper leaves source changes staged and never installs dependencies, builds, commits, or activates a runtime.

The current runtime manifest binds canonical C, native D and the exact published release archive. The [native154 source and notices](../runtime/native154/README.md) and [provenance metadata](../runtime/native154/native154-provenance.json) record the archive and its source. The helper verifies those bindings; it does not infer an installed deployment or ordinary-use acceptance from a successful reconstruction.

## Optional manifest field

`runtime/manifest.json` remains schema version 1. Its optional `nativeDistribution` object has these fields:

| Field | Required binding |
| --- | --- |
| `sourceTree` | Full Git SHA-1 of normalized C; must equal `source.normalizedTree`. |
| `normalizedTree` | Full Git SHA-1 of normalized distribution D. |
| `patch.file` | Regular `.patch` file directly inside `runtime/`. |
| `patch.bytes`, `patch.sha256` | Exact byte count and lowercase SHA-256 of that C→D patch. |
| `artifact.file` | Exact archive basename used under `native-package-artifacts/`, ending in `.tgz` or `.tar.gz`. |
| `artifact.url` | Actual HTTPS release download URL in `josephbergvinson/openclaw-control-plane`, with the same archive basename. |
| `artifact.bytes` | Exact positive byte count. |
| `artifact.sha256`, `artifact.sha512` | Lowercase hexadecimal digests of the unchanged archive bytes. |

The native patch must change exactly `pnpm-lock.yaml` and `pnpm-workspace.yaml`. The normalized tree bindings describe source contents independently of private commit identities. Both source patches remain separately hashed. The artifact filename must match D's normal local tarball dependency; release publication must preserve the retained archive bytes.

Native mode refuses incomplete bindings, source mismatches, unexpected delta paths, nonmatching final trees, URL credentials or queries, other repositories, and traversal filenames. Ordinary C-only reconstruction does not read or fetch the native declaration.

## Archive provisioning

The helper requests only the declared public GitHub release URL and accepts its normal HTTPS redirect to GitHub's release asset host. It inherits no proxy credentials, authentication handlers, or cookies. It streams into an owned temporary file, checks exact size and both digests, keeps the verified descriptor through publication, and verifies the exposed regular file before reporting success.

Publication creates the exact `native-package-artifacts/<artifact.file>` entry without replacing an existing destination. Matching existing bytes are verified; differing files, symlinks, and changed namespace entries are retained and reported. The original clean-checkout requirement still applies. Temporary cleanup removes only a matching owned file and preserves the original failure if cleanup also fails.

If an apply or later archive operation fails, the isolated checkout may contain staged source changes. The helper reports failure and leaves that state available for inspection. A successful reconstruction proves source and archive identity; normal installation, build, runtime activation, and ordinary-use acceptance are subsequent operations.

The notice packet records a finite set of selected resources and notices. It does not claim a complete static dependency license inventory or a bit-for-bit rebuild of the native executables.
