# Durable preferences template

Copy to `<workspace-root>/USER.md`. Rank 4 in the precedence ladder.

## Authority boundary

This file holds **durable preferences and stable context only**. It informs defaults and interpretation. It decides
no policy question, grants no approval, and states no current fact about the machine. A preference that would change
a gate is a policy change and belongs in `AGENTS.md`.

## What does not belong here

This file is default-loaded, so it is a disclosure surface on every eligible session. Higher-sensitivity personal
context does **not** belong here, however convenient it would be. It belongs in a conditional-load memory file that
is read deliberately when a task actually needs it, and is recorded as such in `BOOTSTRAP.md`. Also excluded:
secrets and credential values, current facts about routes or hosts, and anything that would need updating weekly.

Rate this file honestly for sensitivity rather than assuming that "always-loaded" implies "low". Tier and
sensitivity are independent axes — see [memory and context](../docs/08-memory-and-context.md).

## Stable context

The entries in this section and the two that follow are shape examples, not recommendations. Replace them with the
adopting operator's own preferences; a preference nobody actually holds costs context on every turn and teaches the
agent a default that will later have to be argued with.

- Working timezone: `<timezone>`. Interpret bare dates and times in it unless told otherwise.
- Primary working surface: `<surface-slug>`. Assume it when a request names no surface.
- Task tracker of record: `<tracker>`.

## Durable working preferences

- Prefer plain text and Markdown over binary document formats for deliverables.
- Prefer a short answer plus a linked artifact over a long message.
- Prefer explicit units and ISO dates, `YYYY-MM-DD`.
- Prefer showing the command that was run and its real output over describing the result.
- When a request is ambiguous, ask one question rather than producing two variants.

## Preferred output forms

| Request kind | Default form |
|---|---|
| Comparison | Table |
| Procedure | Numbered steps with the verification step named |
| Investigation | Findings first, evidence path second, open questions last |
| Recurring report | Same section order every time, so runs can be compared |
