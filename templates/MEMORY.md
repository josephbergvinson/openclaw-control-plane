# MEMORY.md — Compressed Cues and Source Pointers

Role boundary:
- Short cache of high-value cues and source pointers only.
- Never canonical policy, approval authority, runtime fact registry, or secret store.

Rules:
- Keep this file to 12 bullets or fewer.
- Each bullet must be either a source pointer or a uniquely high-value cue.
- If a cue needs explanation, move the explanation to its source file and leave only the cue or pointer here.

Cues / pointers:
- `USER.md` -> durable preferences, support context, dated user context.
- `SOUL.md` -> voice and style defaults.
- `capabilities.md` -> current host/runtime facts, readiness evidence, probe commands.
- Memo-first; explicit assumptions; minimal fluff.
- Evidence and second-order effects beat slogans.
- Keep sensitive or highly personal details in conditional memory files, not in the default bootstrap profile.
- Structure is protective infrastructure.
- Pair interpretation with a concrete next step.
- <PROJECT_OR_ORG> / <DOMAIN> are core recurring domains.
- Prefer `<OPERATOR_HANDOFF_ROOT>` for user-facing handoff paths; `<LEGACY_HANDOFF_ROOT>` is legacy unless a surface-specific policy explicitly says otherwise.
- Deep sources when needed: `memory/<conditional-context>.md` and other private, untracked memory files.
