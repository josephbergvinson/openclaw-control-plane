# Policy changelog template

Copy to `<workspace-root>/POLICY_CHANGELOG.md`. **Historical audit only.** This file is not a normative source, is
never default-loaded, and is read deliberately when someone needs to know why a rule exists.

## Row format

Append one line per change, newest last, in exactly this shape:

```text
<date> | <status> | <scope> | <summary> | <evidence>
```

| Field | Content |
|---|---|
| `date` | `YYYY-MM-DD`, the date the row was appended; on a `live` row that is the date the change took effect |
| `status` | `live`, `draft`, or `superseded`, describing the change at the moment the row was appended. `live` means the change is in force, `draft` that it is written but not in force, and `superseded` that the row's only job is to retire an earlier row. The value is never edited afterwards |
| `scope` | Slash-joined list of the contract, script, and test areas touched |
| `summary` | One clause. What changed, not why it was nice |
| `evidence` | A pointer to the artifact, event-log line, or test that substantiates it |

## Rules

1. **Append only.** Never rewrite a row, including its status. A row is a dated statement about the moment it was
   written, and editing one destroys the only property that makes the file auditable. A mistake, a change of
   state, or a retirement is recorded by a later row that names the earlier one.
2. **Status hygiene by appending.** A change recorded as `draft` that has since reached the live stack does not
   get reclassified in place; append a `live` row whose summary names the earlier row it promotes, or a row that
   states the residual gap. A draft left with no later row is worse than a missing entry: it makes the audit trail
   imply pending work that already shipped, and no reader can tell planned from forgotten. The current state of
   any rule is the newest row that names it, so the bar to hold is that no `draft` row describing landed work is
   left as the newest row on its subject.
3. **One row per change**, even when the change touches several files. The scope field carries the breadth.
4. **Evidence is a pointer, not a description.** "Verified manually" is not evidence.

## Entries

The rows below are format examples. Their value is the shape: a date, a lifecycle status, the areas touched, one
clause of what changed, and a pointer something can be checked against. The date column carries symbolic
placeholders here, ordered oldest to newest; a real file carries ISO dates. How the reference implementation reads
its own record is described in [architecture evolution](../docs/18-architecture-evolution.md).

```text
<date-1> | live | AGENTS/TOOLS | add a precedence tie-break clause to both normative contracts | tests/test_<area>.py
<date-2> | live | BOOTSTRAP/manifest | add a per-file rating column to the load-policy table | audit/events.jsonl entry <n>
<date-3> | draft | AGENTS | first draft of the <gate-name> exception | commit a1b2c3d
<date-4> | live | AGENTS/TOOLS/tests | supersedes the <date-3> row: narrow the <gate-name> exception and state what it does not authorize | tests/test_<area>.py
<date-5> | live | TOOLS | require a pre-edit identity check before a durable lane writes to a worktree | <artifact-root>/<run-id>/closeout.json
```

The third and fourth rows are the supersession pattern in full. The third keeps the status it was written with —
it really was a draft on that date, and it says nothing about a future it could not know. The fourth names it and
carries the outcome, which is why nothing ever has to reach back and edit the third. A reader who wants the state
of the `<gate-name>` exception reads forward to the newest row that names it and stops there. A retirement with no
replacement is written the same way, as a later row with status `superseded` naming the row it withdraws.
