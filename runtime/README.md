# Reconstruct the reference runtime

This package supplies the complete customized source delta from a pinned official
OpenClaw release. Start with a separate upstream clone, verify and apply the patch,
then install dependencies and build. It does not copy an operator's account state
or install a service on your behalf.

The [manifest](manifest.json) is the current machine-readable authority. Use the
[source map](../docs/20-runtime-source-changes.md) to find the mechanisms and the
[adoption guide](../docs/17-adoption-guide.md) for workspace and host setup.
Earlier changes are summarized in the [history index](../docs/history/README.md).

The reference uses the official OpenClaw **v2026.10.4** base and the managed
**Codex 0.160.0** native package. Read exact custom commit/tree identities, patch and archive
digests, and tool versions from the manifest rather than inferring them from a
release name. Qualify the exact public commit you adopt; earlier results do not
qualify changed inputs. The separate `referenceDeployment` field describes an
installed reference. Reconstructing source does not establish that deployment or
ordinary-use acceptance on another host.

The selected and loaded external gateway reference is R96, activated on 13 November
2026. Mac companion build 508 retains its earlier source; the manifest declares
the finite companion delta needed to reconstruct it from the newer native
distribution. The companion build label identifies the intended source reference;
separate signing, installation and startup records establish the actual app
bundles and behavior. Source reconstruction does not establish live acceptance.

An owner-origin Discord goal on R86 verified its command acknowledgment, one
progress message, a separate final reply and completed delegated work. Native
`/goal` start and resume commands acknowledge
a saved goal before model work begins. Progress and
results then use the originating conversation's normal delivery path, so a long
task does not depend on the slash command's expiring interaction token. A private
command acknowledgment stays private and states where work updates will appear.
Status and other control replies keep their existing behavior.

The earlier objective-parsing repair remains included: an objective supplied
without an explicit action is literal text, including one that begins with
“Complete” or “Pause.” The native Codex archive is unchanged.

The Mac companion source resolves local dashboard credential
references through the existing endpoint owner. Primary and auxiliary dashboard
connections refresh those credentials when their own connection retires; unrelated
remote connections do not invalidate them. The previous native credential-entry
behavior is retained. Companion startup can install its bundled browser
extension, so the companion and external runtime must use matching extension
source. Updating only the stable extension directory is insufficient when a
retained app can replace it on its next launch. Reconstructing the matching source
does not sign or install the app, verify startup, or establish sustained browser
reliability on another host.

The bounded comparison did not establish an overall performance advantage over
native Codex. These targeted repairs do not establish consistently fast responses
or superiority over other harnesses.

## Source identities

The manifest separates the official base, customized source, native distribution,
companion source and installed reference. Read its exact identities before
building or comparing releases. The [source map](../docs/20-runtime-source-changes.md)
explains the retained context, compaction, continuation, delivery, account and
browser mechanisms and their code locations.

The latest reliability changes use existing execution paths:

- Cancelling a completed child retires its pending completion for the requesting
  parent without changing the child's recorded outcome or another parent's work.
  Remaining child results retain their continuation authority and final destination;
  a previously admitted continuation cannot deliver the cancelled result.
- Memory events enter the synchronization queue before the pending-event list is
  cleared. Failures and concurrent arrivals remain represented in that queue.
- Local worker timeouts retain their module, phase and local origin through error
  handling. They are not automatically treated as provider failures or replayed.
- Computer guidance includes already-discovered host names, IDs and discovery
  state. Discovery alone does not prove permission or reachability.
- Browser profile discovery distinguishes an unknown tab inventory from a
  confirmed empty inventory.
- An authenticated replacement makes the superseded browser relay yield across
  reconnects and worker revival. Explicit pairing can reclaim it. Existing
  network recovery and pending-command cleanup remain in place.
- Tab creation stays owned by the extension until its target reaches the
  requester. Failed delivery reclaims only unchanged, unclaimed tabs; delivered
  or user-modified tabs remain open even if the acknowledgment is lost.

The relay change requires matching runtime and extension components for its full
behavior. Older borrowed transports retain legacy closure behavior. A yielded
client can remain yielded after the replacement leaves; explicit pairing restores
it. Worker termination, older extensions or uncertain delivery can still leave
tabs requiring manual cleanup. These source changes
do not establish that every browser failure is fixed or that response latency has
improved. Verify the installed runtime through ordinary workflows.

Trajectory retention aggregates group and byte totals through the canonical
covering index rather than materializing and sorting the runtime event table.
The existing writable database admission installs that index; retention limits,
whole-run ordering, active-run protection and transaction ownership are unchanged.

The existing registry can request an authored checkpoint after 45 seconds without
progress. Its 60-second sweep, tool safe boundaries, model/provider work and
transport can add delay. This is checkpoint eligibility, not a fixed delivery
interval or response-time guarantee.

The managed Codex 0.160.0 archive and the external gateway retain separate
manifest bindings. A companion may use the distribution's exact source identity
or a declared finite delta to a distinct complete tree. A distinct target includes
all required source and dependency changes; legacy packets retain their original
eight-path contract. The native credential-entry implementation still verifies
the Apple process, current host and local principal, exact form, focused secure
field and single-use entry scope. `nativeCompanion` records its source commit,
full tree and build label. Reconstructing it does not change the external gateway.

In the recorded service-hosted configuration, the selected external gateway
constructs text/voice agent prompts and resolves MCP tokens; the companion
provides native client/worker functions. Reconstructing the companion does not
change that external gateway's selected release or establish app-hosted gateway
acceptance. App-hosted gateway operation, explicit embedded local CLI/agent use
or changed worker roles require separate qualification.

The reference companion retains a separately qualified local Bun security
payload. This packet supplies neither that payload's artifact nor its
reconstruction binding. A normal build uses the source's tracked Bun staging
pin; it does not reproduce the retained security payload. Source and build
labels therefore do not establish identical installed app bytes. The optional
companion patch reconstructs the declared source tree; `referenceDeployment`
separately describes the installed external runtime. Neither proves installation
or ordinary-use acceptance on another host.

| Identity                                          | Manifest field                                              |
| ------------------------------------------------- | ----------------------------------------------------------- |
| Official repository, tag and annotated tag object | `upstream.repository`, `upstream.tag`, `upstream.tagObject` |
| Official source commit and tree                   | `upstream.commit`, `upstream.tree`                          |
| Custom reference commit and tree                  | `source.referenceCommit`, `source.referenceTree`            |
| Public reconstructed source tree                  | `source.normalizedTree`                                     |
| Complete patch filename, size and SHA256          | `patch.file`, `patch.bytes`, `patch.sha256`                 |
| Exact build tools and platform prerequisite       | `toolchain`                                                 |
| Native distribution tree, overlay and archive     | `nativeDistribution`                                        |
| Companion source identity and optional finite delta | `nativeCompanion`                                         |
| Scope of the existing installed reference         | `referenceDeployment`                                       |

Development-only corrections can advance the source and distribution references
without replacing the installed runtime. `referenceDeployment` retains the actual
installed commit. The optional companion patch still reconstructs the separately
recorded companion tree from the updated distribution reference.

The custom commit identifies source lineage; it need not exist in the public
upstream repository. Reconstruction uses the official tag plus the public patch.
The manifest declares deliberate fixture normalization. The current export replaces
identifying labels in two synthetic tests across all three source targets;
production code is unchanged. The resulting Git tree,
including file modes, is the comparison point; an adopter's commit ID will depend
on their own author and timestamp.

Choose the reconstruction target explicitly:

| Target | Options after the clean-checkout path | Final Git tree |
| --- | --- | --- |
| Customized runtime source | `--apply` | `source.normalizedTree` |
| Native distribution | `--apply --native-distribution` | `nativeDistribution.normalizedTree` |
| Mac companion source | `--apply --native-distribution --native-companion` | `nativeCompanion.normalizedTree`, or `sourceTree` when no normalization is declared |

These are source targets, not three running systems. The companion can share the
native distribution's exact commit and tree; a separate patch is required only
for the supported finite difference described below. Every selected target still
requires full Git tree equality, including file modes. Companion and deployment
metadata neither install an app nor attest that a new adopter is running it.

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

Before dependency installation, the reference workflow checks every literal
native source/test path against that reconstructed checkout. A missing path is
a failure requiring a reviewed behavior-to-owner
mapping; path existence alone does not establish equivalent assertions or a
passing test. This check does not change any manifest source or deployment
identity.

## Optional native distribution

The declared native archive targets **macOS on Apple silicon (Darwin arm64)**.
The ordinary source-reconstruction path also supports Linux; this native archive
does not provide a Linux executable.

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
[native source and notices](native160/README.md) for provenance and licensing.

## Optional Mac companion source

The manifest supports `--native-companion` for a matching distribution source
or an explicitly declared finite delta. Read the exact source, build and scope from `nativeCompanion`,
including the retained Bun payload limitation under [source identities](#source-identities).
Use a new clean official-tag clone to reconstruct that declared source target:

```bash
git clone --depth 1 --branch "$reference_tag" https://github.com/openclaw/openclaw.git /path/to/openclaw-companion-reference
python3 "$reference_root/scripts/reconstruct_runtime.py" /path/to/openclaw-companion-reference --apply --native-distribution --native-companion
cd /path/to/openclaw-companion-reference
git write-tree
git switch -c reference/openclaw-companion
git commit -m "Apply the pinned OpenClaw Mac companion source"
```

Before committing, `git write-tree` must match `nativeCompanion.normalizedTree`
when declared, otherwise `nativeCompanion.sourceTree`.
Recording the reconstructed target in `HEAD` lets the validation below compare
its changes with the official upstream commit, using your own Git identity.

The helper first reconstructs and verifies the native distribution. It admits two
companion cases:

- **Same source:** omit `nativeCompanion.delta` only when the companion's
  `sourceCommit` equals `nativeDistribution.referenceCommit` and its `sourceTree`
  equals `nativeDistribution.referenceTree`. Its replay tree must equal
  `nativeDistribution.normalizedTree`: use the declared `normalizedTree` when
  fixture normalization is present, otherwise `sourceTree`. No empty extra
  patch stands in for this equality.
- **Finite source delta:** bind the distribution tree and an exact nonempty patch
  in `nativeCompanion.delta`. The patch must change exactly the allowed paths
  enforced by the helper. The [source contract](../docs/21-native-runtime-reconstruction.md#optional-companion-source)
  covers the declared shared/macOS changes, inverse gateway changes and the two
  exact pnpm files when needed to restore the companion's dependency declarations.
  Its bytes and SHA-256, base tree and complete final companion tree are checked.

The source commit and `sourceTree` retain the authentic component identity.
When fixture labels are normalized, a separate `normalizedTree` and exact
`normalization.paths` describe the public replay; those paths must match the
source normalization declaration. This also supports the same-source case without
an additional delta. The helper verifies the complete replay tree
before retrieving the same declared native archive. The archive's verified
presence does not establish that the companion lockfile selects it; dependency
installation follows the reconstructed lockfile. Dependency files cannot be
fixture-normalization targets.
A descriptive companion identity that differs without a valid delta is
not reconstructable in this mode. Ordinary source and native-distribution modes
keep their existing targets; do not apply another mode on an already modified
checkout. This command reconstructs source, not an installed or accepted Mac app.

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

Local changed-file checks use the same import-graph analysis as CI to select
prompt-snapshot validation when prompt-producing source changes. Generated
snapshots capture the current tool descriptions and schemas.

The reference workflow also runs the runtime's native steering, queue and
publication tests against that same reconstructed source. It uses the runtime's
normal test runner and fixtures; no diagnostic overlay or copied test harness is
an installation or qualification step. The assertions cover source ownership,
FIFO ordering, cancellation, authority changes and provider input custody.
The existing serial terminal-continuation step also runs
`src/agents/tools/message-tool-final-edit.test.ts`, covering direct and nested
final edits, negative controls and a later input. Workspace helper tests retain
their separate scope.

Browser qualification uses the pinned Playwright-managed Chromium and verifies
sandbox-enabled startup before the browser cases. On the hosted Ubuntu runner,
the [workflow](https://github.com/josephbergvinson/openclaw-control-plane/blob/main/.github/workflows/verify-reference.yml) admits user namespaces
for that exact executable through an AppArmor profile while retaining the global
restriction. This is a CI prerequisite, not a production host setting or a reason
to disable the browser sandbox when a test cannot launch.

A changed source target requires the corresponding behavioral assertions to be
reviewed against its maintained owner before qualification. Retiring an old path
does not by itself retire the behavior it checked.

Check all required jobs on the exact public commit being adopted. Command plans,
results and build logs are workflow artifacts; a cancelled run or partial log is
not successful qualification. Linux CI does not replace macOS installation,
device permissions or real channel/account acceptance.

## License

The incorporated OpenClaw source remains MIT licensed. Retain [LICENSE](LICENSE),
[third-party notices](THIRD_PARTY_NOTICES.md) and native resource notices when
redistributing source or artifacts. Private credentials, conversations and live
operational receipts are outside this reference package.
