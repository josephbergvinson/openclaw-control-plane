# A worked example

This example uses fictitious companies, events and identifiers. It illustrates the actual operating decisions without requiring the original operator's private records.

The operator sends an ordinary request in Discord:

> Add tomorrow's calls with the Company Alpha and Company Beta leads to my work calendar.

## Establish the intended sources and destination

The assistant resolves the company identities through the project and integration registries. It verifies which Google Workspace accounts contain the invitations and which Apple Calendar is the intended destination.

Company Alpha's invitation is a thirty-minute meeting. A chat message mentions the start time but says nothing reliable about the finish. Company Beta's latest invitation includes a joining link and telephone details. An older message contains a superseded time.

The authoritative invitations determine the event details. Chat context helps locate them but does not replace them. A Google source is compatible with an Apple Calendar destination: account selection follows the operation, not a blanket restriction to one provider.

## Divide reads without creating competing writers

The parent can ask a worker to inspect Company Beta's invitation and updates while it checks Company Alpha. The worker receives the relevant read-only scope and returns source identities, exact times and joining details. The parent remains the only calendar writer.

If the parent yields, the runtime retains the represented worker and its completion path. The operator receives useful progress when the work is substantial, without raw tool arguments or internal task identifiers.

## Reconcile existing events

The assistant reads the destination before creating anything. Company Alpha already has an event, but its duration is provisional and the joining link is absent. Company Beta has no corresponding event.

The assistant updates the existing Alpha event using its identity and creates one Beta event. It carries across the verified title, start and end, timezone, joining URL and useful invitation notes. It does not infer a one-hour duration merely because that is convenient.

If a tool returns an ambiguous result, the assistant checks the destination before retrying. A second create call is not the default response to an observation timeout.

## Verify the actual outcome

Read both destination events independently after the writes. Check that the Alpha identity was preserved, Beta exists once, the event times match the current invitations and joining details remain intact. Search for duplicates at the relevant destination.

The final answer can then be short:

> Both calls are in your Work calendar with the current times and joining details. I updated the existing Alpha entry and added Beta; there are no duplicates.

This is example wording, not a claim that the repository itself performed these writes. An adopter must run the workflow against their own test data and verify the destination.

## Repeat the request

A second identical request should find both correct entries and report them without creating additional events. This is the idempotent path. It is worth testing separately from the first create/update path: success on one does not prove the other.

## Carry the pattern to other work

For a company QA request, replace invitations with the exact deployed site, repository and issue records. For a personal-data question, replace them with the relevant read-only data source and its freshness/completeness evidence. For a writing request, bind claims to current source material and verify the delivered attachment.

The reusable sequence is: interpret the request, bind authoritative sources and destinations, divide independent work, make the authorized changes, verify their effects and deliver the requested result. [Integration routing](11-integrations-and-capability-routing.md), [execution](06-execution-and-durable-lanes.md) and [verification](15-evidence-audit-and-verification.md) supply the detailed rules.
