# Public reference history

This index summarizes changes by mechanism. Use the current
[manifest](../../runtime/manifest.json) and
[reconstruction instructions](../../runtime/README.md) for installation.
Historical checks and an originating operator's live results do not qualify a
later source tree or a new host.

| Period                | Publicly relevant changes                                                                                                                                 | Current explanation                                                                                                                                    |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| September 2026        | Native task/goal continuation, queue steering, requester-bound completion, refresh persistence and model/usage ownership.                                 | [Continuation](../06-execution-and-durable-lanes.md), [delivery](../13-delivery-and-control-surface.md), [source map](../20-runtime-source-changes.md) |
| Early October 2026    | Original-task context through restarts and scheduled work, opaque credential entry, bounded memory preparation and companion authentication.              | [Context](../08-memory-and-context.md), [routing](../11-integrations-and-capability-routing.md), [security](../16-security-and-trust-model.md)         |
| Mid October 2026      | Authenticated voice profiles and task consultation, playback/recording finalization, compaction recovery, reply ownership and bounded native interaction. | [Delivery and voice](../13-delivery-and-control-surface.md), [source map](../20-runtime-source-changes.md)                                             |
| Late October 2026     | Requester-bound progress/final handoff, native credential forms, indexed trajectory retention, account-renewal health and pre-upload confidentiality checks. | [Source map](../20-runtime-source-changes.md), [health](../14-guards-health-and-restoration.md), [publication checks](../../CONTRIBUTING.md#publication-confidentiality) |
| Public reconstruction | Complete official-base patch, a bound native overlay, published native archive, resource notices and fresh-clone CI.                                      | [Runtime package](../../runtime/README.md), [native reconstruction](../21-native-runtime-reconstruction.md)                                            |

## Source history and qualification

Public [commits](https://github.com/josephbergvinson/openclaw-control-plane/commits/main/)
identify published changes. [Workflow runs](https://github.com/josephbergvinson/openclaw-control-plane/actions)
retain checks for each public revision. A passing result belongs to its exact
commit and inputs; editing prose cannot transfer it to a successor.

Earlier queue-steering investigation used a disposable candidate overlay and
an isolated ten-case fixture. The
[historical package](https://github.com/josephbergvinson/openclaw-control-plane/commits/main/runtime/diagnostics/adopted-steering)
and its [workflow history](https://github.com/josephbergvinson/openclaw-control-plane/actions/workflows/steering-regression.yml)
remain in version history. Their results belong to the source generation tested
at that time. They are not extra installation steps or current-runtime proof.

The stable reference carries those behavioral contracts in the runtime's
existing adoption, queue, operation-registry and Codex publication tests, run by
the single reference workflow. Check the replacement assertions and reconstruct
the exact source selected by the current manifest. Historical results do not
qualify that source, its native package or an installed runtime.

Detailed host diagnostics, account records, activation receipts and meeting
acceptance belong to the operator's private evidence store. They are not inputs to
public reconstruction. Keep a concise current contract and use version history
when investigating an earlier reference.

## Historical visual examples

These dated images illustrate earlier behavior; they are not current installation
instructions or live acceptance evidence. The September UI images are synthetic
fixtures. The October pair is a cropped settings comparison. Use the current
[delivery](../13-delivery-and-control-surface.md) and
[context](../08-memory-and-context.md) chapters for present behavior and settings.

| Group | What it illustrates | Retained images |
| --- | --- | --- |
| Auth inbox, 24 September | Distinguishing a refreshable token from a required reconnect, and one account's failure from the rest of the pool. | Refreshable: [before](../assets/auth-inbox-20260924/before-refreshable.png), [after](../assets/auth-inbox-20260924/after-refreshable.png). Reconnect: [before](../assets/auth-inbox-20260924/before-reconnect.png), [after](../assets/auth-inbox-20260924/after-reconnect.png). |
| Discord progress, 24 September | An earlier sequential-post design. Its “after” layout is superseded: current progress edits one card, then posts a separate final. | [Before](../assets/discord-progress-20260924/before.png), [historical after](../assets/discord-progress-20260924/after.png). |
| Discord diffstat, 25 September | Keeping a tool-only file-change event out of ordinary conversation output. | [Before](../assets/discord-diffstat-20260925/before.png), [after](../assets/discord-diffstat-20260925/after.png). |
| Discord narration, 25 September | Replacing an invented worker problem with an observed useful status. | [Before](../assets/discord-narrator-20260925/before.png), [after](../assets/discord-narrator-20260925/after.png). |
| Voice bootstrap, 13 October | Including policy in the selected voice context. The pictured budget is historical, not a current configuration requirement. | [Before](../assets/discord-voice-bootstrap-context-20261013/before.png), [after](../assets/discord-voice-bootstrap-context-20261013/after.jpg). |
