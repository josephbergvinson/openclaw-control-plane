# Capability provenance

A capability can depend on model instructions, local helpers, runtime code and external services at the same time. This chapter identifies the owner of each responsibility so an adopter can reproduce the mechanism and test the correct boundary.

## Four evidence labels

| Label | Meaning |
| --- | --- |
| **policy-only** | An instruction guides the agent's judgment. Compliance must be evaluated through behavior. |
| **helper-backed** | A concrete script or service implements the stated check or operation. Its inputs and dependencies must be supplied. |
| **runtime-backed** | The pinned OpenClaw source implements the mechanism. Copying Markdown does not install it. |
| **live-proven** | An operator has verified a particular behavior on a particular installation and retained dated evidence. It is a status for that observation, not a permanent property conferred by this repository. |

These labels are not a ranking of importance. Correct account selection often needs judgment, while a timeout needs implementation. A reliable workflow requires both.

## Responsibility matrix

| Capability | Owner and backing | What to verify on adoption |
| --- | --- | --- |
| Natural authorization within the request | AGENTS; policy-only, with native authentication/approval enforcement beneath it | Ordinary authorized reversible work proceeds; quoted external instructions do not grant authority; real scope changes remain bounded. |
| Voice, no emoji and useful progress | AGENTS/SOUL plus channel presentation changes; policy-only and runtime-backed | Inspect actual channel output, including failure and continuation paths. This is not a universal Unicode filter. |
| Source and destination selection | AGENTS, project/integration registries and resolver; policy-only and helper-backed | Exact source identity, selected account and destination readback agree. |
| Current invitation details | Calendar instructions and provider/native application tools; policy-only and tool-backed | Correct start/end/timezone, joining URL and notes; existing event identities preserved; no duplicates. |
| Durable session goals | Native goal tools and persisted session-goal state; runtime-backed | Objective, status and accepted usage survive the relevant continuation; reset behavior is explicit. |
| Detached work and follow-up routing | Native task ownership and lane-contract integration; runtime-backed | A child is represented, the parent retains the right return route, and later input reaches the intended work. |
| Yield and requester continuation | Native reply dispatch and worker completion path; runtime-backed | A legitimate yield does not become a false empty-answer failure; the requester eventually receives the result. |
| Visible worker timeout | Spawn, create-session and execution dispatch chain; runtime-backed | Configured timeout reaches actual execution, including explicit zero. Test expiration separately from normal completion. |
| Streamed message identity | Shared/channel streaming implementation; runtime-backed | Incremental output edits the intended message and does not create extra terminal messages. |
| Accepted goal usage | Session-goal accounting and provider response correlation; runtime-backed | Accepted attempts reconcile once; retries, compaction and later unrelated work do not corrupt the counter. |
| Model effort and provider evidence | Configuration, provider adapter and bounded prompt observer; runtime-backed | Distinguish selected effort, outgoing request, accepted response and visible delivery. |
| OAuth refresh persistence | Auth-profile refresh/persistence and admission custody; runtime-backed | Refresh adopts compatible durable state and cannot overwrite a newer or different-account credential state. External revocation still requires recovery. |
| Compaction and continuity | Existing runtime compaction owner plus checkpoints; runtime-backed and policy-only | Context reduction retains task constraints; notice coverage matches the execution path. Background maintenance may be silent. |
| Memory retrieval and availability | Workspace memory, registered context sources and runtime tool preparation | Tools accurately advertise available corpora; source freshness and audience boundaries survive retrieval. |
| Scheduled child custody | Native cron/task/subagent ownership; runtime-backed | Parent custody persists through child completion, interruption and cancellation. Domain success is checked separately. |
| Host maintenance | Installed scheduler definitions and maintained helpers; helper-backed | Exact command, interpreter, state root, schedule and terminal effect match the current definition. |
| Release activation | Build/seal/snapshot/activation helpers plus native service controls; helper-backed and runtime-backed | Candidate identity, stopped-state snapshot, selectors, actual process identity and readiness converge. |
| Backup and restoration | Local recovery tools, cloud backup client and independent verification; helper-backed/external | Coverage, completed upload, restored content and required metadata are each proved at their own boundary. |
| Project data access | Separately deployed project service and registered tools | Correct subject/account, freshness, permissions and domain-specific completeness; transport health alone is insufficient. |

## Code that accompanies the claims

The [runtime source package](20-runtime-source-changes.md) supplies the complete normalized diff from the official release, with hashes and a source map. It includes tests and generated contract changes rather than only the final production methods. The [adoption guide](17-adoption-guide.md) explains configuration and host integration.

The public policy is intentionally substantive. It preserves reusable operating behavior while using fictitious identities and routes. It cannot supply an adopter's credentials, account relationships, device permissions or external project data.

## What the reference's validation establishes

Source reconstruction can prove that the supplied patch produces the advertised tree. Tests can prove the behavior covered by their inputs and assertions. A successful build can prove that the source produces a runnable distribution under the tested toolchain. A live test can prove the observed user path.

Keep those records separate. Do not turn a check count into a claim of universal reliability, or cite a successful normal worker completion as proof that cancellation at its deadline works. The [verification chapter](15-evidence-audit-and-verification.md) gives the acceptance procedure.
