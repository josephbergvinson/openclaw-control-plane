# Memory pointer layer template

Copy to `<workspace-root>/MEMORY.md`. Rank 5, the lowest normative rank, and injected on every eligible session.

## Role boundary

This file is a compressed cue layer. It is **not** canonical policy, **not** approval authority, **not** a registry
of current facts, and **not** a secret store. It routes a reader to the file that owns a subject. Anything asserted
here is superseded by the owning source.

## Entry rules

1. **Hard cap of roughly a dozen entries.** Adding one past the cap means removing one, not raising the cap. The
   cost of this file is paid on every turn.
2. **Every entry is either a pointer or a uniquely valuable cue.** A pointer names the owning file. A cue is a fact
   that is short, stable, and not derivable from anywhere else. Nothing else qualifies.
3. **If a cue needs explanation, the explanation moves.** Put it in the file that owns the concept and leave only
   the pointer here. A cue layer that grows explanations has become a second, unversioned copy of its sources.
4. One line per entry. No nested structure, no narrative.

## Entries

- Operating contracts: `AGENTS.md` for behaviour, `TOOLS.md` for mechanism. Read those before acting on policy.
- Load policy and per-file sensitivity: `BOOTSTRAP.md`.
- Current topology, routes, and readiness: the structured registry and status files, not this file.
- `<surface-slug>` conventions and validation command: `memory/<surface-slug>.md`.
- Recurring operational procedure for `<job-name>`: `memory/<job-name>-protocol.md`.
- Dated notes live in `memory/YYYY-MM-DD.md`; find them by search, not by listing.

The entries above are shape examples: replace them with the pointers this workspace actually needs. The two-tier
design they belong to — a small always-loaded cue layer over a large corpus read on demand — is described in
[memory and context](../docs/08-memory-and-context.md).
