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

## Source identities

Read canonical source and native distribution identities from the manifest. The
current source includes these targeted reliability repairs:

- Current valid MCP OAuth bearers use a fresh canonical state read without taking
  the exclusive renewal or consent lease. Expired or rejected bearers and pending
  consent retain that leased path.
- Local gateway probes reuse compatible Mac client metadata for the same paired
  device key and current, unrevoked operator issuance. Explicit shared authentication
  does not require a matching cached device token; a cached token is reused only
  after native issuance verification. Remote targets and identity mismatches retain
  normal pairing checks.
- Stable identity and preference facts already explicit in trusted loaded profile
  context can be answered directly. Source and audience constraints still apply;
  missing, conflicting, historical and volatile facts require the appropriate source.
- Facts supplied in the current request or trusted provider context can be used
  directly. A search for one missing fact starts with one candidate and expands
  when the evidence is ambiguous or conflicting; configured score thresholds stay
  unchanged. Unstated personal preferences still require relevant memory.
- Memory helpers return evidence from completed work and identify incomplete
  results. Skipping deeper recall does not establish that prior context is irrelevant.
- Corrections arriving while an agent waits for a child or compacts its context
  remain attached to the same task. They wait for its continuation instead of
  starting a competing run. Task replacement cannot redirect them to a different
  run, and completion handoffs retain their original authority checks.
- A successful explicit yield with accepted completion children retains the same
  requester continuation and progress-card handoff. Internal prompt cleanup does
  not close the owed reply. Final delivery remains separately owned.
- An already confirmed progress card can pass to accepted background work even
  when the turn returns no new message. The handoff preserves its delivery
  association without sending another preview.
- A confirmed progress card keeps its role through the existing durable handoff
  and restart recovery. Explicit final delivery uses a separate message;
  corrections to completed answers remain supported.
- Session recovery honors a successfully started, current native invocation
  with no execution deadline. A quiet command stays owned while it runs; its actual exit starts
  the existing bounded result-collection allowance. Cancellation, expired
  deadlines and orphaned work retain their recovery paths.
- Compaction retries carry earlier confirmed delivery facts forward once.
  Progress stays distinct from a final answer, preserving pending child completion.
- Repeated projections of an accepted child retain its original completion
  obligation once per run identity. Real repeated message sends remain separate
  effects.
- Initial session context can refresh once if a prior command appends a terminal
  record while its unsent snapshot is being read. Rewrites, selection changes,
  cancellation and another append during the refreshed read remain errors.
- Read-only lifecycle artifact planning runs in the existing worker without
  reserving the transcript writer. Reclamation still rechecks selected state and
  native deletion authority before committing.
- Gateway start notification waits for both runner entry and live admission, so
  fast child completion can reach the final reply owed to its requester.
- Command cleanup and Gateway settlement still await required terminal saves.
  Deferred start observers inherit their owning execution context, so normal
  model-attempt completion cannot cancel a still-authorized final handoff.
  A later save failure cannot replace the original execution error or change its
  cancellation classification; a save-only failure is still reported.

The regression tests cover these behaviors in source. Verify delivery and memory
quality on the installed runtime before treating a deployment as accepted.

Trajectory retention aggregates group and byte totals through the canonical
covering index rather than materializing and sorting the runtime event table.
The existing writable database admission installs that index; retention limits,
whole-run ordering, active-run protection and transaction ownership are unchanged.

The existing registry can request an authored checkpoint after 45 seconds without
progress. Its 60-second sweep, tool safe boundaries, model/provider work and
transport can add delay. This is checkpoint eligibility, not a fixed delivery
interval or response-time guarantee.

The managed Codex 0.160.0 archive and the selected external gateway retain their
separate manifest bindings. The optional Mac companion target reproduces its
separately retained complete source tree. Its finite patch includes native
credential-form changes and every inverse gateway-only change needed to reach
that older component. These inverse changes do not roll back the selected
external gateway. New finite declarations list their exact paths; legacy packets
retain their original eight-path contract. The existing native owner still
verifies the Apple process, current host and local principal, exact form, focused
secure field and single-use entry scope. Its source commit, full tree and build
label are recorded in `nativeCompanion`.

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

The current manifest supplies a separate companion source delta and supports
`--native-companion`. Read its exact source, build and scope from `nativeCompanion`,
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
  equals both `nativeDistribution.referenceTree` and
  `nativeDistribution.normalizedTree`. No empty extra patch stands in for this
  equality.
- **Finite source delta:** bind the distribution tree and an exact nonempty patch
  in `nativeCompanion.delta`. The patch must change exactly the allowed paths
  enforced by the helper. The [source contract](../docs/21-native-runtime-reconstruction.md#optional-companion-source)
  covers the declared shared/macOS changes and all inverse gateway changes.
  Its bytes and SHA-256, base tree and complete final companion tree are checked.

The source commit and `sourceTree` retain the authentic component identity.
When fixture labels are normalized, a separate `normalizedTree` and exact
`normalization.paths` describe the public replay; those paths must match the
source normalization declaration. The helper verifies the complete replay tree
before retrieving the same declared native archive.
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
