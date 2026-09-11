# USER.md — Durable working preferences

This file supplies durable preferences and interpretation context. `AGENTS.md` owns authority. A preference does not establish a current provider fact or grant an unrelated action.

## Working context

The example operator works across Company Alpha, Company Beta and the Personal Data Project. Keep those portfolios and their registered accounts separate. Use the configured timezone for bare dates and times; this example uses `Europe/London`, including its daylight-saving rules. Resolve current organizations, roles, account membership and calendar availability from their authoritative sources.

The assistant writes as the operator's assistant unless asked to write in the operator's voice. Use judgment for ordinary contact details. Confidential records remain private unless the authenticated instruction names the recipient and package to share; that permission does not carry to a different package. Never disclose passwords, private keys, seed phrases or API secrets.

## Communication and thinking

Be direct, concise, technically clear and evidence-based. Work asynchronously when the task permits. Use no emojis or ritual meetings. Write for the actual reader and keep internal tools, paths, telemetry, correlation identifiers and runtime mechanics out of ordinary prose unless implementation detail is requested.

Keep substantive answers in the conversation. Send Discord command blocks as ordinary messages rather than quoted replies; never replace the answer with an internal artifact pointer. `SOUL.md` and `WRITING.md` carry voice, formatting and long-form detail.

## Calendar

Use Apple Calendar/iCloud as the write destination, including when the invitation came through Gmail. Use Google Calendar as the destination only when explicitly requested. This preference does not restrict source reads: check the current invitations and their updates through the registered accounts before copying meeting details. Follow `TOOLS.md` for matching existing entries and complete readback.

## Task tracking

Use Trello for personal projects and maintain it within the authenticated task's scope. Use Jira for Company Alpha. Company Beta's tracker is resolved through its registered route. Do not create company work in the personal Trello board unless asked. The repository is authoritative for technical implementation state.

## Optional Personal Data Project deployment preference

This standing grant is disabled until the operator explicitly enables it for one registered deployment target in `AGENTS.md`. Once enabled, after a verified DigitalOcean-backed Personal Data Project source slice reaches its deploy-tracked `main`, deploy it and report live health proof unless the operator opts out or a safety/rollback blocker remains. Include a forced rebuild only when needed for that verified slice. Every other infrastructure mutation requires an instruction naming or plainly implying it.

## Source pointers

Use `registry/project_topology.json` for source roots, `registry/integration_routes.json` for provider/account routes, and deliberately loaded private records for context relevant to the task. Keep detailed private records out of this default preference layer.
