# Public reference history

This index summarizes changes by mechanism. Use the current
[manifest](../../runtime/manifest.json) and
[reconstruction instructions](../../runtime/README.md) for installation.
Historical checks and an originating operator's live results do not qualify a
later source tree or a new host.

| Period | Publicly relevant changes | Current explanation |
| --- | --- | --- |
| September 2026 | Native task/goal continuation, queue steering, requester-bound completion, refresh persistence and model/usage ownership. | [Continuation](../06-execution-and-durable-lanes.md), [delivery](../13-delivery-and-control-surface.md), [source map](../20-runtime-source-changes.md) |
| Early October 2026 | Original-task context through restarts and scheduled work, opaque credential entry, bounded memory preparation and companion authentication. | [Context](../08-memory-and-context.md), [routing](../11-integrations-and-capability-routing.md), [security](../16-security-and-trust-model.md) |
| Mid October 2026 | Authenticated voice profiles and task consultation, playback/recording finalization, compaction recovery, reply ownership and bounded native interaction. | [Delivery and voice](../13-delivery-and-control-surface.md), [source map](../20-runtime-source-changes.md) |
| Public reconstruction | Complete official-base patch, a bound native overlay, published native archive, resource notices and fresh-clone CI. | [Runtime package](../../runtime/README.md), [native reconstruction](../21-native-runtime-reconstruction.md) |

## Source history and qualification

Public [commits](https://github.com/josephbergvinson/openclaw-control-plane/commits/main/)
identify published changes. [Workflow runs](https://github.com/josephbergvinson/openclaw-control-plane/actions)
retain checks for each public revision. A passing result belongs to its exact
commit and inputs; editing prose cannot transfer it to a successor.

The [queue regression package](../../runtime/diagnostics/adopted-steering/README.md)
retains focused fixtures and historical candidate overlays. Current CI tests
reconstructed source directly; those old overlays are not additional patches for
an adopter to apply.

Detailed host diagnostics, account records, activation receipts and meeting
acceptance belong to the operator's private evidence store. They are not inputs to
public reconstruction. Keep a concise current contract and use version history
when investigating an earlier reference.
