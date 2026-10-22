# Reading the examples

These are fabricated explanatory records. They are not live receipts or installable
native state. Never insert them into OpenClaw's SQLite database.

- [Native run](native-run.example.json) projects the stable `subagents` tool's
  `src/agents/tools/subagents-tool.ts` list/wait observation. It shows execution
  `terminal` with outcome `ok`
  alongside delivery `pending`: finishing work and delivering it are separate facts.
  The accompanying schema checks only this illustrated subset, not every native field.
  Wait with the returned `runIds`; a wait timeout never cancels work or consumes
  completion delivery. The parent still owns its required post-child work and final.
- [Scheduler inventory](scheduler-entry.example.json) describes an operator's
  ownership and acceptance record. The native scheduler's own schema owns execution.
- [Known issue](known-issue.example.json) records a contained limitation, evidence and
  a condition for checking it again.

Actual capability registries and helpers ship in the workspace package. Actual runtime
contracts, parsers and tests ship through the [complete runtime patch](../runtime/README.md).
The former workspace status/lease files and phase-based promotion receipt are no longer
presented as current execution authority.
