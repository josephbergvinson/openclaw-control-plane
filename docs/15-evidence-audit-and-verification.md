# Evidence and verification

Completion is a claim about the requested outcome. Evidence should make that claim inspectable by someone who did not watch the task unfold.

The smallest useful verification is the one that covers the actual requirement. A narrow test is valuable when its scope is stated; it becomes misleading when used to support a broader claim.

## Start with the requirement

For each requested outcome, identify the authoritative observable. A source change is checked in the repository and through relevant tests. A runtime activation is checked against the actual release and processes. A calendar change is checked in the destination calendar. A scheduled producer is checked through the schedule and its domain effects.

Record the source version, relevant configuration, operation identity, observation time, result and limits. Keep private account identifiers, detailed logs and personal content in the operator's private evidence store.

## Verify the reference itself

The repository needs several different checks:

1. **Content review:** current code and configuration support the architectural claims; private examples have become genuinely synthetic examples without losing their useful behavior.
2. **Source reconstruction:** the pinned official base and supplied patch produce the advertised normalized Git tree.
3. **Configuration and installation:** generated files use valid native settings, installed instructions resolve their referenced helpers and registries, and the installer preserves existing files unless an overwrite is explicitly selected.
4. **Behavioral checks:** relevant tests cover the exported runtime and helper changes, including negative and interruption paths where applicable.
5. **Documentation checks:** links and anchors resolve, schemas match examples, and Mermaid diagrams actually parse/render.
6. **Publication review:** every outgoing public file and commit is approved for that audience; remote visibility and published content match the reviewed result.

A structural validator cannot decide whether a policy is current or a privacy transformation preserves meaning. Independent review remains necessary.

## Match evidence to its boundary

| Claim | Necessary evidence |
| --- | --- |
| Patch reproduces the reference | Verified upstream commit, patch digest, successful application and normalized tree equality. |
| Built distribution is usable | Successful build plus required artifact/import checks under the stated toolchain. |
| Release is active | Matching configured selectors, loaded service definitions, process identity, build identity and readiness. |
| Worker deadline is enforced | A test exercising the deadline at execution, not just a registry field or normally completed child. |
| Goal continued correctly | Retained objective, actual yield/continuation events, terminal state and observed result. |
| Reasoning effort was requested | Configured setting plus actual outgoing request metadata. Accepted provider output is a separate observation. |
| Usage accounting is correct | Accepted response identities and normalized usage reconcile to the appropriate goal phases. |
| OAuth state persists | Durable profile state and compatible identity survive the relevant refresh/rotation boundary. A successful login screen alone is insufficient. |
| Calendar action succeeded | Independent destination readback of each event's identity, times, URL and relevant notes; duplicate check. |
| Scheduled workflow succeeded | The current job definition admitted the run, the producer completed, domain effects passed and required delivery was observed. |
| Backup is recoverable | Actual restored content and required metadata match the declared source scope. |

## Preserve failed attempts honestly

A later successful check does not change the earlier command's exit status. Keep the original failure and record the recovery separately. If a long validation sequence stops because a tool is missing, do not manufacture an aggregate successful receipt after running the remaining commands.

When combining evidence from several invocations, establish that the source, toolchain, command plan and relevant environment stayed compatible. Repeat a passed check when a later change can affect its result, not merely to produce a cleaner-looking story.

Likewise, a test that required coaching or repair is not an unassisted first attempt. Record the intervention and use a fresh natural request for subsequent acceptance. Do not overspecify the prompt until it tells the agent how to pass.

## Separate execution speed from responsiveness

Time to the final result, time to first useful progress, waiting time and operator interventions measure different parts of the experience. A complex answer delivered eventually can still be a responsiveness failure.

Compare like workloads under comparable settings before claiming a general latency improvement. Preserve model and reasoning preferences while investigating avoidable coordination, retrieval or delivery overhead.

## Keep review independent

A reviewer should inspect source and destination evidence rather than accept the implementation author's summary. For a public export, review both disclosure risk and fidelity: stripping every specific instruction can produce a safe but useless reference.

For a private archive, check captured scope, consistency, credential handling and restoration. A manifest listing files is not proof that their contents are present or decryptable. A backup tool exiting successfully is not a full restore test.

The [adoption guide](17-adoption-guide.md) turns these principles into an installation sequence. The [capability matrix](03-capability-provenance.md) prevents the resulting evidence from being generalized beyond its scope.
