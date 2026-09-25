# Reconstruct the reference runtime

This package reproduces the customized OpenClaw source identified by this
reference architecture. It contains the complete patch from the official release,
including earlier retained capabilities, runtime fixes and their regression tests.
Two company-specific labels in one test fixture are replaced with Company Alpha.
Production source files are unchanged from the pinned source.

The [manifest](manifest.json) pins the upstream tag and commit, deployed and reference
source, sanitized source tree, patch checksum and toolchain. The
[source changes chapter](../docs/20-runtime-source-changes.md) explains the changes
and the boundary between source reconstruction and installation.

## September 22 upgrade

The upgrade targets official `v2026.9.5`, commit
`ec9c1a13db8938e5a3eaa51fca2e981cde2395a9`, with the retained custom changes
ported to the target's task, database and provider owners. The complete export is
rebuilt from that official base, rather than stacking the old patch on a stock
installation. See the [source map](../docs/20-runtime-source-changes.md#september-22-upgrade).

The originating gateway first activated custom source
`287be510c328262a8b093fd2aea1f07d51c8508e` on September 22 at
`05:57:49.051232Z`. That upgrade completed native migration to state schema 17
and agent schema 21. Its dated activation and capture evidence remain historical.

The preceding repair source `1fb1f66cfe29267902a768417b35e4a8c5c4e439` activated at
`11:22:01.365523Z` through the same-version release owner. Successor
`085711060c8477df71066402330eb5a2902d6824` activated at `12:37:05.013053Z`.
The dated record binds this exact release and activation-receipt hash. Postflight at that activation
verified the selected process, health/readiness and preserved configuration, account
ordering and scheduled-job definitions. Journal capture was visually accepted and
enrolled for the new process; new-process scheduled synchronization remains unproven.

The signed app from source `1fb1f66cfe29`, build `2609000592`, was separately installed,
launched and observed connected on the originating Mac Mini on September 22. Signature and contents
were verified. One macOS-added app-root metadata attribute was explicitly reconciled;
unchanged metadata throughout is not claimed. That checkpoint did not establish
MacBook installation or native credential/account acceptance. Later source
qualification is recorded separately below. A bounded native-goal final passed with exact Discord readback on `1fb`;
longer-workflow delivery and backend settlement remain separate acceptance scopes.

The September 22 activation record verified Gateway source `085711060c8477df71066402330eb5a2902d6824`.
It retains the native credential correction, native-goal progress and final-delivery
repair, registered-project delegation guidance, source CLI guidance and packaging
pin repair. It additionally fixes existing Discord thread-channel send receipts:
the provider records the actual thread identity for every chunk and the final
aggregate, allowing strict source-delivery custody to recognize the visible final.
Fourteen focused producer and composed settlement cases pass. The successor
activated on September 22; its fresh Discord diagnostic stopped on a provider rate limit before
child creation, so actual live settlement acceptance remains pending. The September 22 qualification recorded the signed companion and its
matching private worker at source `1fb`; this Gateway channel
producer change does not require another native build.

## September 23 qualified source

The source qualified at this checkpoint is `5b5942def95c29e75aaca6c3bc3ba6f084ae1dc8`.
Its [complete reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35891835806)
and [Steering regression](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35891835793)
passed at public commit `bbd7a78e2f8c2426d7a53d5cb8e4b75fbaf280e4` on September 23.
These are exact source-qualification results, separate from Gateway activation,
companion installation, live workflow acceptance and checks of later public commits.

The originating Gateway activated that exact source on September 23 at
18:59:26.156345 UTC as release
`openclaw-2026.9.5-5b5942def95c-20260923T1834Z-selfcontained`. The checked owner
made one candidate attempt with no rollback; its activation receipt SHA-256 is
`961d04a7e7f60c65bbd3811741ca127c862bb78d354efc0e3548cdbbe6bcf35f`.
The stopped-state snapshot was retained. Postflight verified the selected
Gateway and node, successful health/readiness responses, unchanged configuration,
account ordering and scheduled-job definitions, and capture for the current
process. Scheduled capture synchronization and individual workflow results remain
separate acceptance scopes.

The remaining model defaults were applied afterward through acknowledged native
configuration updates, without a restart. Readback matched the five intended
settings for utility completion, autonomous heartbeat, active memory, memory
dreaming and its model-override permission, with no unexpected leaf changes.
This is configuration acceptance; each real inference path still needs its own
observed model/effort evidence.

It retains the preceding candidate’s corrections to quota recovery after completed tool work and account-bound WebSocket
reuse. Eligible recovery advances through the existing authorized account order,
keeps the same model and committed transcript, and does not replay the original
task. User-pinned accounts, cancellation, active tool work and exhausted candidates
retain their existing boundaries. Both prompt-level and assistant-level failures
preserve the aggregate side-effect guard after continuation.

The transport now binds reuse to the effective endpoint and handshake headers.
An account or credential change opens a fresh connection with the completed tool
history and without the previous connection's response ID or account-bound
encrypted reasoning. The identity digest stays private in memory. Focused tests
include five production-runner cases with exactly one physical write each and
59 owning transport cases. Hosted qualification passed for the current source;
live acceptance of these recovery paths remains separate. CI corrections remove stale size and assertion counts, distinguish test fixture
wrappers from production functions, and align the existing settle-wake expectation.
The fixture rename preserves all call sites and passed its 135 owning tests.

The source also restores visible activity when a completed worker resumes its
original requester. That continuation retained final-delivery custody but could
bypass typing, progress drafts and narration while the model continued working.
The repair carries a run- and session-bound presentation capability through the
existing internal dispatch. Discord reuses its configured progress card and
activity narrator; private worker completion prompts are excluded from narration.
Every send or edit, including delayed writes, rechecks current ownership. Abort,
replacement and completion stop pending updates without changing the final owner.
The seven owning test files passed 90 focused cases. CI now runs those exact
agent and Discord suites and preserves their logs separately. These source tests
do not establish a fresh live progress or final-delivery result for the candidate.

The September 23 source also separates interactive and autonomous model policy.
The example configuration selects Astra Max for ordinary conversation, with
`/think ultra` retaining opt-in orchestration, and Sol High for model-backed
maintenance. Utility completion, autonomous heartbeat and all memory-dreaming
phases use the selected model’s existing reasoning parameters. A wake that
continues an admitted user task retains that task’s model and effort. Children
inherit their initiating task unless explicitly overridden. Script-only jobs do
not acquire an LLM dependency. This uses the existing configuration schema, so
the retained signed companion can continue reading it. Focused routing and
compatibility tests and the complete hosted source qualification passed. Activation
and live acceptance of this combined source are recorded separately.

The native command transport now waits for an explicitly bounded command to
finish, allowing five seconds for the response after that command deadline. Its
previous fixed 20-second socket wait could report an unavailable companion while
the app was still running. Existing authentication, cancellation and no-replay
checks remain in place. The repair runs in the companion's bundled worker, so
activating the Gateway alone does not install it. A matching signed companion
package and host installation are required. The matching signed package is build
`2609000593`; direct bundled-worker inspection confirms the same source and build
identity. This signed app was installed and observed connected on the originating
Mini on September 23. Full resources and metadata matched the qualified package,
except one macOS-added app-root `com.apple.macl` attribute that was explicitly
reconciled.

At 18:51:02 UTC, a bounded native execution returned its expected output after
23.01 seconds with `isError: false`, beyond the previous 20-second cutoff. The
native route does not expose a separate exit-code or timed-out field; none is
inferred. The acceptance receipt SHA-256 is
`0db88de11699fc73d9242f8e3b87c492caaa2cee714264f8ab0d6a9a12dabac8`.
This verifies the installed Mini command path. The same signed build and private
worker were installed on the MacBook and observed connected on September 23. The
full 43,919-entry installation inventory matched, with only the same explicitly
reconciled macOS-added app-root attribute. Actual process identities, executable
hashes and the app and CLI Apple-anchor/exact-leaf signature checks passed. Its
installed connection acceptance receipt SHA-256 is
`95466686cb3cf1841aaee786f9a3dcfea9995dd5b5a689a6bdac1a8724fdcb12`.
These installation and connection checks do not establish credential entry or
successful authentication.

The existing daily retention job was manually admitted after the copied-cache
mount-identity repair on September 23. All four cleanup children returned zero,
all 64 effect predicates passed, no timeout or residual process remained, and its
normal Discord result was read back. There were no eligible removals, so this is
a verified no-op with no reclaimed-space claim. The original failed invocation
is retained; the next natural daily tick has not been observed. The effect and
delivery receipt SHA-256 is
`2378edc972d469055a12d4c9b33df3b059bb66bae0e85c3d920eced46cecca17`.
The [host chapter](../docs/19-host-operations-and-backups.md#guarded-storage-cleanup)
describes the preserved volume and cleanup guards.

A separate September 23 Reminders authorization attempt through build `2609000593`
reached the correct app, with its purpose text present. macOS deferred the prompt;
the single bounded 90-second command timed out without a grant or denial. The
reason for the delay is not established. A previously verified native Reminders UI
route remains the fallback, and policy avoids repeating the same authorization
wait for every reminder until an observed permission-state change warrants a
recheck. CLI permission remains pending. Time-correlated OS logs for the existing
UI fixture show alarm firing and muted Notification Center delivery during screen
sharing, with no sound. Association with that fixture is inferred from the matching
edit, due and cleanup times; the log does not expose its title. Visible banner and
iPhone delivery remain unverified. The read-only notification disposition receipt
SHA-256 is
`ec38036fcdc21b79ac9ad8b3185bed37b2acdf8e20058f4ce1731816922f8d20`.

Credential resolution runs in a bounded private child of the signed Mac app.
Direct Keychain access disables interaction inside that child. Existing items that
trust Apple's reader can use the fixed signed `/usr/bin/security` path after exact
item, metadata, host, unlocked-store and stored-ACL checks. A metadata-only class
read materializes a legacy persistent reference before ACL inspection. The app
validates its helper before launch and rechecks prompt and execution authority
after resolution; secret values remain in the private native pipe and secure field.
The compatibility reader has no no-UI switch: a lock or ACL race can briefly prompt.
Its process group has a deadline and is joined on cancellation or timeout.

The final native product/test-target build, 28 focused native cases and four
bounded-process cases passed. The disposable Keychain checks preserved six exact
byte variants, rejected locked or untrusted items, revalidated metadata and removed
their private store without changing canonical settings. The lock-after-admission
case refused without a value and joined at its deadline. Visible OS-dialog
disappearance was not inspected and is not claimed. Eighteen runtime cases passed
for native-goal progress, final custody and dispatch. These source and process
results are distinct from the signed package and Mini installation verified on September 22;
actual account entry and complete workflow delivery remain separate acceptance
steps. The bounded native-goal readback above proves only its stated scope.
Earlier native source `c4bee4279fdf` retains its original 18-test and four-case
synthetic qualification; those results alone did not qualify the subsequent
compatibility reader.

At this September 23 checkpoint, `source.referenceCommit` and
`source.normalizedTree` matched the verified `5b5942def95c` Gateway activation.
The companion record bound signed build `2609000593` and its verified Mini and
MacBook installations and connections, with credential entry and authentication
still separate. The September 24 successor below changes the reconstructible
companion source without changing the recorded Gateway deployment. The prior `085711` Gateway and `1fb`/`2609000592` Mini records, plus
earlier acceptance, retain their dated historical scopes.

## September 24 native prompt successor

At this checkpoint, the reconstructible source was `f29bb229c5c8f033fd3a7966dbc8ecf1f6cab572`,
normalized tree `b8d24593af90951eb74769c1849a2b09d0c1cd2b`. Its only delta from
`5b5942def95c` is the native credential prompt owner and its regression tests.
The Gateway remained on `5b5942def95c`; no Gateway activation was needed for this
Mac-only correction. The signed companion and bundled worker use source
`f29bb229c5c8`, build `2609000594`. They are installed and connected on both
the originating Mini and MacBook.

A real macOS authentication agent presented a valid native password prompt but
had no AppKit `launchDate`, so the previous matcher rejected it. The repair pins
the kernel process incarnation: PID, user ID, start seconds and start
microseconds. It rechecks that identity before entry. Missing, truncated or
inconsistent kernel metadata refuses admission, and a reused PID cannot inherit
an older prompt's authority. The existing Apple code-signature, foreground app,
focused window, secure field, account evidence and single-use execution-bound
reference checks remain in place.

Thirty focused native tests pass, including identity changes, invalid kernel
metadata and replacement while credential resolution is awaited. An independent
standalone reconstruction compared all 44,895 tracked entries and modes against
the committed source; only the two established fixture-label substitutions differ.
The normal signed package owner completed successfully with matching native app
and private-worker identities. On September 24 at 00:53:35 UTC, the installed
originating Mini passed actual native credential acceptance: the real Apple
LocalAuthentication prompt had no AppKit launch date, the opaque `type_secret`
route entered the enrolled credential, and macOS reported successful
authentication. No secret was passed through ordinary tool arguments, no account
password changed, the QA process exited and its execution scope closed.

The installed bundle's content, permissions, signature and links matched the
qualified package; only an OS-created app-root metadata attribute required
explicit reconciliation. Its native node was connected with Accessibility and
Screen Recording granted. The installation acceptance receipt SHA-256 is
`b8f56e1e9d5a779cc343685454f0f994068cdd4769b484e289400b63915056ba`; native entry and
authentication acceptance SHA-256 is
`3879cbbfe36e1054f74e99c670abbff321dc0dd63b848387772b219627809169`.
The package receipt SHA-256 is
`a0eaa0d7ac9d6c832b75b037c3ec197c03ab3b27486a2ca203288f68cee94574`.

The MacBook installation and postflight passed on September 24 at 01:29:42 UTC.
All 43,919 bundle entries matched except the same explicitly reconciled OS-added
app-root attribute. App and CLI signatures, actual process/source identities,
node connection and preservation of the existing host credential bindings were
verified. Its actual fresh process still reported Accessibility and Screen
Recording denied. MacBook native interaction and credential entry remain
unproven until those OS permissions are granted and the route is checked; the
Mini's success does not substitute for that host's result. The MacBook receipt
SHA-256 is
`1f2a6e12c286e64b730e0aa1cc1851ba2754349ac6d4a1d6236554cbb846da99`.
The earlier bounded admission attempt stopped before signaling or replacing the
app while scheduled work was active; the successful continuation reused verified
stage and rollback copies, preserved the first failure and released its exact
native exclusion lease. At that companion checkpoint, the Gateway stayed at source
`5b5942def95c` with its process and configuration unchanged. Prior installation and test records retain their dated
scopes. Visible Reminders banner delivery is a nonblocking, unverified effect
limit; iPhone work is excluded from this goal.

### Native prompt source qualification

The preceding complete [September 23 source qualification](#september-23-source-qualification)
remains scoped to `5b5942def95c`. The `f29bb229c5c8` patch checksum is
`0e449135dd38e55132041a0f8d1689920fb7c1fb23e15310f77d13173b6ff340`.
Hosted checks must reconstruct the successor tree from the updated manifest.
The native Swift regression and package evidence remain separate from the Linux
reference workflow and from each host's actual authentication result.

## September 24 scheduled-run recovery successor

Source `2c102f590298efa7294e6fcea7e8147aeca22efe` retains the companion correction
and changes the isolated cron exception owner. A provider failure before any work
could previously leave delivery unknown and strand an unstaged announcement
checkpoint. The owner now records a positive no-send fact only when the settled,
complete canonical transcript proves that the exact run failed before tools or
assistant output. Fenced, partial, branched or unknown records, fallback,
continuations and observed work preserve uncertainty. The original error remains
visible. The private collector consumes the persisted proof without sending a
replacement or advancing covered source history; its export boundary is unchanged.

The focused native tests, production and owning test types, type-aware lint, and
245 control-plane cases passed. A fresh official-tag reconstruction compared all
44,897 tracked entries and modes; only the two established fixture labels differ.
The normalized tree is `950818a3cc1530dc6545090c3b1ade6ac7a025af`, and the patch
SHA-256 is `2c1abf0e1d195fc91a9bf539b9a050ea65ecd9ff677aee90a622c1a03c835136`.
The originating Gateway activated this exact source on September 24 at
10:03:56.892630 UTC as
`openclaw-2026.9.5-2c102f590298-20260924T0939Z-selfcontained`. The activation
receipt SHA-256 is
`791b0a8e6db29cbcddf41fa232aa1b96200e8738a558b618a6b463c7e80930b8`.
Postflight verified health/readiness, connected Discord and Telegram, and capture
for the new process. Configuration, account ordering, execution approvals,
scheduled-job definitions and native companion processes were preserved. The
initial missing-window capture attempt was retained and reconciled before capture
acceptance. Final-head hosted CI remains a separate required qualification; the
pull request records its actual result.

The signed companion stays at `f29bb229c5c8`, build `2609000594`; this cron-only
correction does not require rebuilding it. The MacBook reconnected at 10:04 UTC
and again reported Accessibility and Screen Recording denied. Its actual native
interaction remains unverified; the Mini's earlier accepted authentication result
retains its own host-specific scope.

The related control-plane repair recognizes the deliberately disabled retired
native launcher while retaining the other integrity checks. Autonomous heartbeat
uses its own isolated session. The existing guard, daily health and source-change
announcement jobs each passed one manually admitted execution with confirmed
delivery after recovery; these checks do not claim a later natural scheduled run.

## Active-child follow-up correction

A later incident exposed two gaps in the earlier repair. The owned follow-up
helper supplied a new agent-to-agent reply-delivery mode when steering an already
active child. The real execution owner rejected the conflicting mode. The parent
then successfully yielded to its remaining child work, but shared terminal
preparation rendered the earlier mutating-tool error as a final warning.

The correction lets the active child retain its admitted delivery mode; it does
not change restrictions for generic agent-to-agent requests. Terminal preparation
defers the final warning only when the attempt succeeded, yielded and has
core-confirmed continuation custody. Original error, mutation and trace facts
remain intact. Failed, cancelled and unowned turns retain their warning behavior.

The send regression now uses real registered execution handles and the backend
validator; the previous test mocked that boundary. Four send cases and three
warning cases reproduced the defects before the production changes. Focused
verification passed 111 session tests, 31 steering tests, 51 terminal-preparation
tests, 50 terminal-resolution tests, two terminal-state tests and 34 payload-error
tests. An unrelated capacity-warning assertion also failed on the untouched prior
source; only its expected prefix changed to the existing plain-text `Warning:`.

The correction is committed as `18b432ca2f83a107b1bc329cedf2221f20e61639` and included
in the complete patch. The native changed-source checks passed in 739.9 seconds,
including typechecks, lint and dead-code checks, and scoped independent review
found no actionable findings. The pinned full build passed in 232.79 seconds, and
sealed-release activation selected this exact source. A bounded live check of active
and completed child follow-ups passed, followed by a separate live check retaining
a real tool error through a successful yield without a stale final warning.
Hosted qualification of that exact historical public revision was still pending
at this checkpoint. The current source retains this correction and its full
terminal-preparation suite; its separate hosted result is recorded below.

## September 21 follow-up repair

The earlier repair used source `daf5771a7d868903ada1a99cf595a587027f018c`.
It was the deployment baseline before the active-child correction above.
For that earlier source, local source checks, focused regressions, the complete
build, fresh reconstruction, activation and the bounded live Discord handoff check
passed. Its [complete reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641128711)
and [steering workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641129009)
also passed at public commit `9a04c7fd5121c05c5b9cd9b13c579f9159746f24`.

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

The [manifest](manifest.json) carries the exact identities without requiring a
second manually synchronized version table:

| Identity | Manifest field |
|---|---|
| Official repository, tag and annotated tag object | `upstream.repository`, `upstream.tag`, `upstream.tagObject` |
| Official source commit and tree | `upstream.commit`, `upstream.tree` |
| Custom reference commit and tree | `source.referenceCommit`, `source.referenceTree` |
| Public reconstructed tree | `source.normalizedTree` |
| Patch filename, bytes and SHA-256 | `patch.file`, `patch.bytes`, `patch.sha256` |
| Exact build tools | `toolchain.node`, `toolchain.pnpm` |
| Candidate/deployment status | `candidateOnly`, `source.referenceDeploymentStatus` |
| Separately scoped companion evidence | `components` |

The custom commit identifies source lineage; it need not exist in the public
upstream repository. Reconstruction uses the official tag plus the complete patch.
The only export normalization replaces two labels in one lane-contract test
fixture. Every other blob and every file mode must match the committed custom
source. Previous deployment evidence remains separately dated; an export never
advances a runtime selector or changes a live service.

## Check and apply

Use a separate clone that no service or other task is using. Do this before installing
dependencies. The helper refuses dirty checkouts, including untracked and ignored
files, linked worktrees and shared object stores. It requires the exact official
commit and annotated tag object.

The clone below accesses GitHub. The Python helper performs no fetch or other network
operation and disables Git's lazy fetching. Obtain this reference repository and
review its manifest, patch and helper before running them.

```bash
# Run in the reference repository; the manifest selects the official source.
reference_root="$PWD"
reference_tag=$(python3 -c 'import json; print(json.load(open("runtime/manifest.json"))["upstream"]["tag"])')
git clone --depth 1 --branch "$reference_tag" https://github.com/openclaw/openclaw.git /path/to/openclaw-reference
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
git switch -c reference/openclaw-custom
git commit -m "Apply the pinned OpenClaw reference runtime"
```

`git write-tree` must match `source.normalizedTree` in the manifest.
Your commit ID will differ because commit author, timestamp and history are local.
The pinned source tree is the reproducibility check.

## Validate and build

Use the exact Node.js and pnpm versions in the manifest. The following commands
run inside the reconstructed checkout. Dependency installation accesses package registries and runs the upstream
installation process; it is separate from the offline reconstruction helper.

```bash
node --version
pnpm --version
pnpm install --frozen-lockfile
reference_base=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["upstream"]["commit"])' "$reference_root/runtime/manifest.json")
node scripts/check-changed.mjs --base "$reference_base" --head HEAD
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

### September 23 source qualification

Source `5b5942def95c29e75aaca6c3bc3ba6f084ae1dc8`, normalized tree
`56a9bcae6a403eadf5aa48949cfe4a9c767a4ed8`, passed
[Verify reference](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35891835806)
at public commit `bbd7a78e2f8c2426d7a53d5cb8e4b75fbaf280e4` on September 23 at
18:25:01 UTC. The artifact records all 35 native planned commands completing
successfully, including all 92 hosted lint shards. The native plan took 90 minutes
36 seconds; the separate reconstructed build and owned delivery, quota and
continuation regression suites also passed. The
[Steering workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35891835793)
passed all ten steering cases and its ownership tests.

The hosted reconstructed tree matched that September 23 checkpoint's normalized
source; its patch SHA-256 was
`4f007edf5de1d7cf4c93593c12eff245cc84a0b09e949cbc54a36f9dfdeee28c`.
An independent standalone reconstruction also matched all 44,895 entries and modes,
with only the two documented fixture-label substitutions. Failed or cancelled
predecessor runs remain historical and are not counted as passing qualification.
These results qualify the named source and public commit, not later documentation
revisions or an unverified deployment.

### Historical September 21 local source qualification

Reference `18b432ca2f83a107b1bc329cedf2221f20e61639` passed all 279 focused tests
listed above and the native changed-source checks for its six changed paths.
The unchanged public helper passed preflight and application in a fresh standalone
clone of the exact official release. Comparison of all 39,499 tracked entries
confirmed matching production blobs and every file mode; only the two established
fixture labels differ. The pinned full build passed in 232.79 seconds on the clean
source, with build-info SHA-256
`f7f5c94110df6ea497843194c8fe7d3e6924ba7850a9a1feac76962a7617e4af`.
Both bounded live checks passed as recorded below. Hosted qualification of that
exact historical public revision remained pending at this checkpoint.

### Active-child correction deployment

The sealed release `openclaw-2026.9.3-18b432ca2f83-20260921T2140Z-selfcontained`
was activated at 21:52:46 UTC. The loaded gateway was verified on the exact
reference source. The activation receipt has SHA-256
`b3c8ef5b28b254606637e838ee4ccc3b155bfc118cde340a98c1b8b5f00c0f19`.
Readback confirmed unchanged configuration, scheduled-job definitions,
authentication-profile ordering and native companion process identities. Discord
and Telegram were connected. A capture check qualified the current gateway process;
it does not establish natural scheduled synchronization.

The first controlled live check steered a running child through its original
execution, then sent another follow-up to that completed child. The second follow-up
started a new execution with the same completion-task identity. Its successful
`yielded` result was followed by child completion and requester continuation.
Exactly one final message was read back from Discord at 21:55:24.807 UTC,
103.310 seconds after acceptance, with no intermediate failure warning.
The first wait was skipped because completion was already queued; this was one
successful yield.

The deliberately nonzero shell exit in that first check was returned with
`isError: false`; it did not establish warning deferral after a retained tool error.
A separate focused check exercised that exact boundary. A follow-up steered the
original child execution, then a send to an absent synthetic target returned
`isError: true`. The trace retained that `sessions_send` failure with
`mutatingAction: true` and `executionStarted: true` when the parent successfully
yielded. The persisted yield and successful attempt emitted no final warning.
The child completed through the original execution, the parent resumed automatically,
and exactly one final Discord message was read back at 22:01:36.345 UTC,
117.275 seconds after acceptance. Both the readback during the yield and the final
readback contained no intermediate failure warning; the synthetic target remained
absent. These controlled checks cover the exercised handoffs and warning boundary,
not every concurrent or long-running workload.

### Earlier September 21 qualification

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
tree and patch digest are retained in the [manifest at public commit `9a04c7f`](https://github.com/josephbergvinson/openclaw-control-plane/blob/9a04c7fd5121c05c5b9cd9b13c579f9159746f24/runtime/manifest.json).

The [complete reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641128711)
and [steering workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/35641129009)
passed at public commit `9a04c7fd5121c05c5b9cd9b13c579f9159746f24`, whose manifest
pins source `daf5771a7d86` and normalized tree
`5048932811a2d8acbdd4e52bc7ad0e7e7d056da9`. These results qualify that earlier source,
not the active-child correction above.

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

## September 24 OAuth authentication health successor

Source `5233aa69090d540f73af8258e39718c20495c806` prevents refreshable OAuth access-token expiry
from producing a false sign-in warning. It also preserves actionable per-account
warnings beside a healthy provider pool and derives their age from known expiry
rather than the latest status poll. The [source map and synthetic before/after
proof](../docs/20-runtime-source-changes.md#oauth-authentication-health-and-inbox)
show the behavior and its evidence boundary.

This exact source was activated on September 24 at `14:24:24.542477Z` as
`openclaw-2026.9.5-5233aa69090d-20260924T1407Z-selfcontained`, in one attempt with no
rollback. The activation receipt SHA-256 is
`07a4703f0300ac5026f73d1a406eff844f49273ea73cb6e5b1fec2b691d5ed1d`.
The preceding `2c102f590298` deployment and its receipts remain historical.

All nine live profiles report authenticated, refreshable credentials without a
required sign-in; model capacity and quota availability remain separate. The eight
JavaScript and CSS assets served by the Gateway match the activated build, and the actual native Mini Inbox has no authentication warning.
Before activation, the existing credential owner successfully renewed the affected
profile and extended its persisted expiry while preserving shared and agent profile
order. That renewal and the post-activation UI observation have separate receipts;
neither is a new account sign-in result.

Native companions and their private workers remain at `f29bb229c5c8`, build
`2609000594`; no native rebuild, installation or restart was performed for this
central change. Both native nodes are connected. At `14:28:09 UTC`, the MacBook
reported Screen Recording granted and Accessibility denied. Its Inbox was not
directly inspected, and native credential-entry acceptance remains pending
Accessibility and an actual interaction check. The existing UI reconnect path
refreshes the Gateway-served build without reinstalling the app.

All 33 mechanical postflight checks passed. The raw approval-state difference was
reconciled to usage bookkeeping; policy was unchanged. The acceptance receipt SHA-256
is `cc5ea281a5f42f9fb6497c7b8e34e8d0837de6508685eb5798a91f4647e2ebf1`.
The later public `25e1d5881e5d` qualification passed its full build but failed
only the auth-status test growth ratchet; the successor below retains that history.
Current-process screen capture
passed; no automation was replayed and no later natural scheduled success is
inferred from this change.

## September 24 Discord progress successor

Source `c42af6f66b8abd23620aa9a8a110b9813c1bf5de` corrects two causes of invisible
long-running work: foreground commentary that only edited an earlier Discord post,
and ordinary interactive child tasks that did not opt into the existing yielded
progress owner. Foreground updates produce a new post after a three-minute
checkpoint; native background progress starts after a 15-second coalescing window
and repeats every three minutes while the exact yield remains authoritative.
Operator notification overrides, cancellation and the original final-delivery
owner remain intact. See the [source and synthetic before/after proof](../docs/20-runtime-source-changes.md#long-running-discord-progress-checkpoints).

Public revision `25e1d5881e5dff77a524a7826b31c363292d4a15` passed the complete
reconstruction build, reference checks and steering tests, but its native checks
stopped at the auth-status test file growth ratchet. The successor extracts those
unchanged cases into a focused test file. The original
[reference workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/36014726278)
and [steering workflow](https://github.com/josephbergvinson/openclaw-control-plane/actions/runs/36014726303)
retain that dated outcome; it is not qualification of this successor.

This exact source activated on September 24 at `15:49:57.891411Z` as
`openclaw-2026.9.5-c42af6f66b8a-20260924T1528Z-selfcontained` in one attempt with no rollback.
The activation receipt SHA-256 is
`6a0666726f5895087c0111d17ffde2804ca74ab01e0c279ae4b187061a084758`. All 33 mechanical postflight
checks passed: selected source and processes, health/readiness, configuration,
account ordering, scheduled-job definitions, terminal sessions and retained native
companions were verified. Current-process Journal capture was visually inspected
and enrolled; a later natural scheduled sync is not inferred.

The preceding `5233aa69090d` auth/Inbox activation retains its dated receipts.
Native companions and private workers remain `f29bb229c5c8`, build `2609000594`;
this central change does not rebuild or install them. MacBook native credential
acceptance remains separate. The subsequent real Discord review on this c42 deployment passed foreground
checkpoint, repeated yielded progress, requester resumption and final delivery.
It also identified the two source edge cases corrected by the successor below.
Public revision `fd3fc75f8580` subsequently passed all five hosted checks,
including all 35 native commands, 92 lint shards and the full runtime build;
all six artifact digests and exact source identities were verified. Those results
retain their c42 scope and do not qualify the successor below. Successful channel
delivery does not imply that those source defects were absent.

## September 24 progress and trigger follow-up

Source `205ba711eb3d9fda86db4ea8bd4d7ebb66d50d80` adds three bounded corrections: canonical
normalization before a foreground checkpoint, consistent ownership of queued
native task notifications, and a fresh tool-refresh scope for scheduled scripts.
See the [source explanation](../docs/20-runtime-source-changes.md#progress-ownership-and-scheduled-trigger-scope).
Source `205ba711eb3d9fda86db4ea8bd4d7ebb66d50d80` activated on September 24 at
`2026-09-24T17:15:56.505319Z` as
`openclaw-2026.9.5-205ba711eb3d-20260924T1657Z-selfcontained` in one attempt with no rollback.
All 33 mechanical postflight checks passed. The activation receipt SHA-256 is
`0609079db56f3ea6bc07000557bc9f892f99a0af713b3fc3529c2c2b3e759101`; the acceptance receipt is
`8015885b3eb062adf8892891aa950229d41316b414443774c01eae647ec4ef7b`. The dated auth, Journal and native companion
receipts retain their original scope. Native apps and workers remain at
`f29bb229c5c8`, build `2609000594`; this central successor does not install or restart
them. The original 18:17 BST scheduled trigger then completed naturally through
Sol in 104.677 seconds. Its exact summary reached Discord at 17:18:43.559 UTC,
with one match among the 12 messages read; error counters returned to zero through
normal scheduler behavior. Configuration and schedule were unchanged, with no
forced run, checkpoint mutation, business replay or failure-history reset.
This verifies the reported trigger incident, not every automation. Hosted
qualification remains separate and binds the exact public revision.

## SDK contract qualification reference

Source reference `db4ae8b2e6e0cbb6fdd14d4dd4ebb9e99b0c2f6c` is a qualification-only successor to the
installed runtime `205ba711eb3d`. Only SDK contract documentation, direct SDK tests
and exact hand-maintained export counts differ; every production blob and mode
is unchanged. The runtime and native companions were not rebuilt or reactivated.
The installed runtime's accepted activation and natural scheduled delivery remain
recorded above and in the manifest.

The preceding public `24dcc596e2ef` passed its complete reconstruction build and
owned regressions but failed native command 19's SDK surface inventory check.
The existing canonical sanitizer contributes two qualified public/callable exports
through `channel-outbound` and its compatibility barrel, plus one deprecated
`channel-message` projection. The exact inventory is documented and tested without
changing guard evaluation or unrelated allowances. See the
[qualification explanation](../docs/20-runtime-source-changes.md#sdk-contract-qualification-reference).
Hosted checks and artifacts determine qualification of this exact reference;
no preceding source result substitutes for it.

## Native credential discovery diagnostics

Reference `bbc4d8d806a56c706ca1813862e62e89a0c192d8` adds diagnostic reason codes to the existing native
`credential_prompts` action. The result now distinguishes an unavailable focused
window, incomplete Accessibility metadata, an unobserved or ambiguous secure
field, an unsupported form context, and absent or unmatched local bindings.
Counts and closed codes use the same bounded discovery pass; native text and
credential values are not returned. Account matching, prompt custody, secret
resolution and entry checks remain unchanged.

The source compiled and passed all 17 isolated native credential-owner tests,
focused Swift lint and formatting, documentation formatting, native schema and
localization verification. The generated localization inventory also synchronizes
one inherited permission-description row with text already present in source;
it introduces no additional permission behavior or new settings interface.

This is a companion diagnostic source candidate. Packaging, installation and
actual password entry require their own receipts. The installed Gateway remains
`205ba711eb3d9fda86db4ea8bd4d7ebb66d50d80`; its executable source and deployment were not changed by this
four-file successor. Existing native companion and channel acceptance retain their
dated scope. No successful MacBook credential entry is claimed here. The public
successor receives qualification through the existing reference workflows.

## Discord source replies and authentication blockers

Reference `70601fb9c5e4f9173c73ab36662b0f60c973e810` keeps ordinary task updates, blockers and requests for
human action in the originating conversation through the shared messaging
instructions. A user's private-delivery request applies to that item; it does
not make subsequent authentication notices private. Short-lived codes and
verification URLs retain their existing private handling. Both automatic reply
mode and message-tool-only mode receive the same source-conversation guidance.
This changes prompt guidance, not destination identifiers or transport rules.

The shared credential prompt also requires a fresh check of the authorized
service's session and current challenge before repeating a sign-in blocker.
Available authorized native credential tools must be inspected when browser
controls cannot handle the visible challenge. The tool reference explains how
an offered password fallback can use an enrolled host-local alias without
exposing its value; actual OS/provider human presence remains a boundary.

Quiet progress now excludes file-change counts from draft staging, so a write
before any meaningful progress cannot create a standalone file-count message
or retain that draft on yield. Later human-readable checkpoints still render.
The same contract applies to ordinary output and native draft snapshots.

Resumed requester turns also register the existing heartbeat lifecycle flag.
This lets a new ordinary child retain factual progress in its source conversation
after requester settlement. The existing cron, heartbeat, nested-requester and
explicit `done_only` or `silent` exclusions remain. A regression uses actual
command preparation, native registration and yield to verify updates at 15 and
195 seconds without child-authored prose or new tool events. The 46-case owning
suite and core typecheck passed. The Gateway is activated and its 33 mechanical
postflight checks passed; actual channel acceptance remains separate and pending.

The local qualification reported 75 prompt cases and 81 progress/Discord cases
passing. The source export preserves every production blob and mode; only the
established two private test labels are normalized. This Gateway source was
activated once on September 25 at 14:47:26 UTC, with no rollback. All 33 mechanical
postflight checks passed, including current-process capture and preservation of
configuration, auth ordering, jobs, terminals and companion processes. Hosted
qualification and live Discord canaries remain pending. MacBook opaque credential
entry is a separate pending acceptance; existing native receipts retain their
dated source and host scope.

### Observed progress status

Reference `490a24de638ac28f94bd136a6653116d9de56e26` replaces utility-model progress inference with fixed status
text derived from observed tool names and lifecycle outcomes. An unrelated failed
command cannot produce a claim that a worker never started. A completed launch
request does not establish completion of the worker's task. No request text, tool
arguments, paths, results or private child content enters generated status.

Authored preambles retain priority. Existing visibility, 12-second coalescing,
unchanged-text suppression, turn cancellation and explicit quiet settings remain.
The retired narration model module is removed; utility-model routing for titles
and recaps remains unchanged. Qualification includes 39 cases across the owner,
requester continuation and Discord transport, followed by 28 owning cases after a
mechanical closure-binding correction and a production typecheck. These counts
are overlapping qualification runs, not a unique-test total.

The public pair under `docs/assets/discord-narrator-20260925/` is explicitly a
synthetic rendering of the owner regression: a mocked baseline utility response
versus the repaired latest activity. It is not captured live model text or a live
Discord screenshot. The same source was activated once on September 25 at
15:50:25 UTC, without rollback. All 33 mechanical postflight checks passed,
including current-process capture and preservation of configuration, auth
ordering, jobs, terminals and companion processes. Exact-head hosted checks and
live Discord canaries remain pending. MacBook opaque credential-entry acceptance
remains a separate pending check; dated native receipts retain their host scope.

### Bounded waits in active worker tasks

Reference `d83fa84b316c2bb162060ba0ea035425e185ade0` clarifies the existing exec, process and cron descriptions:
a bounded wait between steps of an active worker task stays within that worker's
tracked tool call or session. Detached reminders and future follow-ups still use
the scheduler. This changes model-facing guidance only; scheduler execution,
authorization and explicit notification modes remain unchanged. The complete
owning tool-description and cron suites passed 213 cases, including finalized
available-tool descriptions.

This reference also refreshes the canonical generated config-doc hash for the
previously reviewed utility-model help wording. The exact owning check passed;
schema fields, count budgets and executable code are unaffected by that generated
metadata correction. The original public `ac60b68` check retains its failed stale
baseline receipt rather than treating its passing build as complete qualification.

The deployed Gateway remains `490a24de638ac28f94bd136a6653116d9de56e26` at this publication.
The new source's activation, exact-head hosted qualification and live Discord
acceptance remain separate and pending. Native credential-entry acceptance retains
its independent host-specific scope. All ten existing synthetic image assets are
unchanged; these tool-description changes add no new rendered UI.

### Cron description test layout qualification

Reference `c4467dff8d3483c9ee8e38e662a2e331ce48288f` moves the complete cron description regression into a
coherent sibling test file. All 200 cron cases pass, and both line-cap growth
checks pass against the official release base without a limit exception. The
original `c2c0d99` hosted failure remains recorded; its passing jobs do not
substitute for the successor's complete exact-head CI qualification.

All production files and modes are byte-identical to the built `d83fa84` runtime.
This test-only reference requires no rebuild or redeployment. The deployed
Gateway remains `490a24` at publication; activation of the already built `d83fa84`
candidate, live Discord acceptance and native credential entry remain separately
tracked. The ten existing synthetic screenshot assets are unchanged.

### Guild admission, queued configuration and paused workers

Reference `513be0562e914cd940cc216255232f8099cc5cfa` corrects three reproduced owner failures. Ordinary guild
messages use inbound admission; configured member restrictions and actual command
authorization remain enforced. A paused worker is reported as unfinished, with
its existing continuation identity, while completed siblings keep their results.
Queued runs that belonged to the live runtime refresh their configuration before
secret resolution and policy admission. Explicit scoped configurations stay pinned;
the exact catalog guard remains enforced and no completed work is retried.

Qualification includes 77 paused-wake tests, 30 guild ingress tests, 104 existing
Discord preflight controls and 47 queued-admission/secret-resolution tests. The
strengthened five-case queued regression was also rerun after final mechanical
changes; it is included in the 47, not an additional unique suite count. It
reproduces the actual catalog-generation rejection through queued admission and
checks the repaired model-input read. Current exact-head hosted CI, activation and
live behavior remain separate gates. Ten earlier synthetic images are unchanged.

The manifest's `490a24` deployment fields retain their dated publication-time
receipt. A later accepted activation installed `d83fa84` on September 25 at
17:18:59 UTC with all 33 mechanical checks accepted. This new source successor is
not yet activated at this preparation checkpoint. Its actual deployment and live
results will be recorded after acceptance. The queued reader does not replace a
retired plugin-record Gateway resolver or claim to repair that separate boundary.
MacBook opaque native credential entry remains unproven.
