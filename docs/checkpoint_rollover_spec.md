# Checkpoint rollover spec

Purpose:
- move context rollover checkpoints from narrow late-transcript heuristic extraction to reset-bounded replay grounding
- keep checkpoints as validated structured artifacts rather than free-form chat output
- reduce leaked wrapper text, stale objectives, and malformed next-step fields without changing ordinary assistant replies

## Scope

This spec applies only to context rollover checkpoints and their artifacts.
It does not change normal status updates or ordinary user-facing messages.

## vNext architecture

Target architecture:

`threshold monitor -> reset-bounded replay package -> structured checkpoint builder -> validator/repair -> render/send -> ledger update`

Key principle:
- the monitor is the trigger/orchestrator
- the builder is the checkpoint synthesizer
- the validator/renderer remain the canonical safety rail
- the ledger remains fallback-only, not the primary storyteller

### Automatic threshold-triggered behavior

Rules for automatic rollover emission in this phase:
- crossing the warn threshold arms `rolloverPending`; it does not emit a structured checkpoint immediately
- a `/new` or `/reset` boundary clears/rebaselines saved rollover state for that session; previously high usage must not auto-refire until there is a genuine post-reset warn-threshold crossing
- the monitor may send a lightweight pending alert when the threshold is crossed, but the checkpoint/handoff is deferred until the thread reaches a bounded idle/quiescent state
- automatic emission now requires both:
  - transport-level quieting, and
  - a grounded terminal assistant state showing the current slice is actually at a safe stop point
- current transport-level quieting signals are:
  - `updatedAt` older than the idle grace window (current default: 90 seconds)
  - or missing `updatedAt` plus unchanged token counters across consecutive polls
- current terminal-state signal is the runtime/session field `runtimeTurnState`
  - terminal values: `waiting-input`, `waiting-approval`, or `completed`
  - non-terminal values such as `executing` keep the pending state explicit
  - if the field is missing, auto emission fails closed and does not fall back to transcript heuristics for terminal readiness
- transcript grounding still powers checkpoint synthesis, but it no longer decides automatic terminal readiness
- if transport looks idle but the runtime marker is still non-terminal or missing, keep the pending state explicit and do not emit a checkpoint yet
- if the session is still active/recent, keep the pending state explicit (`armed` or `busy`) and do not pretend the thread is idle
- dropping back below the clear threshold clears the pending rollover without emitting a checkpoint

### Components

- `scripts/thread_context_rollover_monitor.py`
  - threshold detection
  - session routing
  - replay-package orchestration
  - render/send integration
- `scripts/checkpoint_session_window.py`
  - resolve the current replay window since the last `/new` or `/reset`
  - normalize turns and extract explicit status packets
  - surface boundary confidence and degraded reasons when needed
- `scripts/checkpoint_builder.py`
  - synthesize one structured checkpoint JSON object from the replay package
  - initial backend uses an isolated `openclaw agent` builder session over a local replay-package artifact
- `scripts/checkpoint_rollover.py`
  - shared schema, validation, and rendering helpers
- `scripts/checkpoint_validate.py`
  - CLI validator for checkpoint JSON artifacts
- `scripts/checkpoint_render.py`
  - CLI renderer that writes full markdown artifacts and prints the compressed chat summary
- `scripts/checkpoint_state_ledger.py`
  - fallback persistence for the last verified structured checkpoint per session

No gateway restart, model swap, or external service is required for the architecture itself.

## Canonical artifact

Source of truth is JSON, not markdown.

Schema version:
- `context-rollover.v1`

Required top-level fields:
- `schema_version`
- `session_id`
- `generated_at`
- `objective`
- `current_honest_state`
- `current_source_roots`
- `files_changed`
- `verified_evidence`
- `resume_safety`
- `blockers_approvals_needed`
- `pending_decisions_approval_gates`
- `next_atomic_step`

Optional top-level fields:
- `checkpoint_status`
  - used for degraded structured checkpoints
  - `kind`: `structured` or `degraded`
  - `reason`: explicit failure/degraded reason when `kind=degraded`
  - `missing_signal_fields`: canonical field names whose grounded signal was missing or synthesized
  - `validation_errors`: preserved validator failures when a would-be structured checkpoint had to degrade
- `source_strategy`
  - observability-only field describing which source won final checkpoint selection
  - expected values in vNext include:
    - `reset-window-replay`
    - `reset-window-replay+ledger`
    - `builder-repaired`
    - `busy-forced-last-verified`
    - `boundary-uncertain`
    - `transcript-unavailable`
  - legacy values such as `status-packet`, `transcript-slice`, and `ledger` may remain during rollout for compatibility
- `confidence`
  - optional coarse confidence bucket for reporting and rollout observation
  - expected values: `high`, `medium`, or `low`
- `checkpoint_quality`
  - optional quality signal separate from `source_strategy` and `confidence`
  - intended for rollout observation and fail-closed decisions when structure alone is not enough
  - fields:
    - `tier`: `high`, `medium`, or `low`
    - `score`: numeric 0-100 quality score
    - `signals`: quality wins such as `reset_window_resolved`, `full_replay_window_used`, `builder_synthesized`, `builder_repaired`, `live_evidence_merged`, `ledger_fallback_used`
    - `issues`: optional quality concerns such as `degraded`, `boundary_uncertain`, `builder_timeout`, `builder_invalid_json`, `live_conflict`, `fallback_without_live_evidence`

Canonical structured artifact path:
- `artifacts/context_rollover_handoffs/<slug>.json`
- markdown is rendered from that JSON artifact; it is not the source of truth

Degraded checkpoints:
- still serialize the full canonical schema
- still pass the shared validator/renderer
- preserve the degraded/failure reason in `checkpoint_status.reason`
- preserve missing or synthesized signal information in `checkpoint_status.missing_signal_fields`
- may preserve prior validation failures in `checkpoint_status.validation_errors`

## Reset-bounded replay window

The primary checkpoint source is the current session window since the last `/new` or `/reset` boundary.

### Reset boundary resolution priority

1. explicit reset/new boundary metadata when present
2. active transcript segment known to begin after reset
3. latest reset snapshot / rollover boundary marker
4. current active transcript with degraded `boundary-uncertain`

Rules:
- do not silently read pre-reset history as if it were current scope
- if the reset boundary is ambiguous, degrade explicitly rather than widening the replay window without disclosure
- when an active transcript exists, prefer it over a reset snapshot even if the reset snapshot is richer
- reset/deleted snapshots are fallback evidence, not the default live replay source

### Replay package contents

The replay package should contain at minimum:
- `session_id`
- `thread_label`
- `boundary`
- `replay_window`
- `status_packets`
- `live_evidence`
- `last_verified_checkpoint`

### Structured builder contract

The builder must:
- read the whole replay window, not only the last 10-20 turns
- privilege later narrowing and later explicit transitions over early scope
- treat explicit assistant status packets as strong anchors, not absolute truth
- merge live evidence when it is stronger than transcript text
- return exactly one JSON object matching `context-rollover.v1`
- never emit markdown, commentary, or wrapper text
- degrade instead of inventing when critical fields cannot be grounded

### Initial backend decision

The initial builder backend uses an isolated `openclaw agent` builder session driven by a local replay-package artifact.

Implementation shape:
1. write replay package JSON to an artifact file under `artifacts/context_rollover_handoffs/`
2. invoke `openclaw agent --session-id <synthetic-builder-session> --message <builder-instructions> --json`
3. require the reply to contain exactly one JSON object matching `context-rollover.v1`
4. parse and validate that JSON
5. if validation fails, run exactly one repair pass with validator errors
6. if repair still fails, emit degraded fallback

## Field-specific source priority rules

Source priority is field-specific, not one flat global order.

### Objective

Priority:
1. builder synthesis over the full reset-bounded replay window
2. explicit objective from the latest qualified assistant status packet
3. later narrowing/phase transitions in the same replay window
4. latest substantive user ask in the same replay window
5. last verified structured checkpoint fallback

Rules:
- objective must describe the remaining deliverable, not the original kickoff scope if part of the work is already complete
- later narrowing beats earlier broad framing
- completed prerequisites must not be restated as the active objective

### Current honest state

Priority:
1. current live evidence snapshot when cheaply verifiable
2. later explicit transcript transitions after the latest status packet
3. latest qualified assistant status packet
4. earlier replay-window turns
5. last verified structured checkpoint fallback

Rules:
- later state transitions clear stale earlier state
- `waiting-approval` cannot survive later approval + execution evidence
- `waiting-input` cannot survive later returned-patch / QC / merge-closeout evidence
- if work may still be active, `in_flight` must say so explicitly

### Source roots / branch / commit / files changed / verified evidence

Priority:
1. live verifiable evidence
2. explicit structured packet fields in the replay window
3. explicit commit/artifact/path/process/job mentions in the replay window
4. last verified structured checkpoint fallback

Rules:
- live evidence beats transcript when they conflict
- if transcript and live evidence disagree on a critical fact and the conflict cannot be reconciled safely, degrade
- generic “work was done” language never counts as verified evidence
- tool counts and file-touch summaries are not evidence

### Blockers / approvals needed

Priority:
1. latest unresolved approval/input state in the replay window
2. explicit latest status-packet blocker/pending fields
3. last verified structured checkpoint fallback

Rules:
- later approval clears stale blocker state
- later execution evidence clears stale `waiting-approval`
- empty means `none`, not boilerplate
- old reminders, quoted user text, and wrapper text are never blockers

### Next atomic step

Priority:
1. builder synthesis from unresolved state + in-flight state + resume safety
2. later explicit phase transition in the replay window
3. explicit latest status-packet next step
4. last verified structured checkpoint fallback

Rules:
- must name the first safe substantive action after `/new`
- must not be meta-only (`run /new`, `paste this checkpoint`, `continue above`, `resume`, `wait`)
- if writes may already be in flight, the next step becomes inspect/verify first, not rerun

## Session-side checkpoint state ledger

The monitor keeps a lightweight per-session ledger at:
- `tmp/context_rollover_monitor/checkpoint_state_ledger.json`

Ledger rules:
- store only the latest verified structured checkpoint state for the session
- use the ledger to preserve resumable state when live replay/build is unavailable, invalid, timed out, or boundary-uncertain
- precedence is:
  1. valid reset-window replay result
  2. builder-repaired replay result
  3. ledger-backed last verified checkpoint when replay/build is unavailable or weaker
- stale ledger state must not override fresher replay-grounded state
- ledger-backed selection is a fallback/resumability aid only; it does not replace canonical replay-window grounding

## Failure and degraded behavior

Degraded behavior still emits a full structured checkpoint artifact. It must not fall back to loose prose.

Degraded reason buckets expected in this phase:
- `boundary-uncertain`
- `builder-timeout`
- `builder-invalid-json`
- `live-conflict`
- `transcript-missing`
- `no-verified-fallback`

Hard degraded rules:
- degraded checkpoints must still pass the shared validator
- degraded reason must be explicit and ideally single-cause
- `resume_safety.do_not_rerun` is mandatory whenever duplicate writes are plausible
- wrapper text, tool envelopes, and untrusted metadata must never leak into degraded fields

## Validator rules

Hard failures:
- missing required fields
- wrong `schema_version`
- empty `objective`
- empty `next_atomic_step`
- empty `verified_evidence`
- no concrete evidence kind (`command`, `artifact`, `db_fact`, `commit`, `process`, or `job`)
- malformed `current_honest_state`
- malformed `current_source_roots`
- malformed `resume_safety`
- leaked envelope/system text such as:
  - `<<<EXTERNAL_UNTRUSTED_CONTENT`
  - `UNTRUSTED Discord message body`
  - `Conversation info`
  - `Sender (untrusted metadata)`
  - `message_id`
  - `Exec completed` / `Exec failed`
  - `NO_REPLY`
- meta-only next-step values such as:
  - `run /new`
  - `paste this checkpoint`
  - `continue above`
  - `resume`
  - `wait`

Warnings only:
- empty `files_changed`
- empty `blockers_approvals_needed`
- empty `pending_decisions_approval_gates`

## Rendered markdown field order

The renderer emits fields in this order:
1. `Session ID`
2. `Status` / `Reason` / degraded notes when `checkpoint_status.kind=degraded`
3. `Objective`
4. `Current honest state`
5. `Current source roots`
6. `Branch / commit`
7. `Files changed`
8. `Verified evidence`
9. `Resume safety`
10. `Blockers / approvals needed`
11. `Pending decisions / approval gates`
12. `Next atomic step`

The renderer writes:
- full markdown artifact: `artifacts/context_rollover_handoffs/<slug>.md`
- short alias artifact: `artifacts/context_rollover_handoffs/<slug>-current.md` or a hashed alias when needed
- compressed chat summary with final pointer line only

Pointer line requirements:
- final line of the compressed summary
- repo-relative path only
- no absolute host path
- 120 characters or fewer

## Reporting and rollout gate

Reporting entrypoint:
- `scripts/checkpoint_rollover_report.py`

Expected report outputs:
- `structured` / `degraded` / `insufficient` counts
- fail-reason buckets
- source-strategy distribution
- confidence buckets when present
- checkpoint-status-kind distribution when present
- checkpoint-quality tier buckets and average score when present

Classification for reporting:
- `structured`: canonical checkpoint artifact validates and has no degraded status
- `degraded`: canonical checkpoint artifact validates with `checkpoint_status.kind=degraded`
- `insufficient`: canonical checkpoint artifact is missing, unreadable, or invalid

Default rollout gate implemented by the report script:
- measured corpus must have `structured + degraded` coverage rate >= `0.90`
- `insufficient` count must be `0`
- when replay fixtures are evaluated, replay assertion failures must also be `0`

Runtime outbox observability should preserve when available:
- `checkpointArtifactPath`
- `sourceStrategy`
- `confidence`
- `checkpointStatusKind`
- `checkpointReason`
- `replayWindowTurns`
- `replayBoundaryKind`
- `builderBackend`
- `builderRepairCount`

## Manual-force checkpoint modes

Manual-force entrypoint:
- `scripts/context_rollover_force_checkpoint.py`

Mode contract:
- `idle`
  - may ask the live target session to emit its own grounded checkpoint immediately when the session can still answer coherently from current live state
  - relay the resulting checkpoint through the manual-force path so orchestration-known metadata can be labeled honestly
- `busy-forced`
  - if the live request times out, fails, or is explicitly bypassed, run the same replay/builder/validator path used by threshold-triggered checkpointing
  - if that shared path still cannot ground a fresher checkpoint and a verified fallback exists, emit a degraded checkpoint from the last verified state already preserved in the checkpoint ledger or prior rollover artifacts
  - this is an honesty-preserving fallback, not a fake fresh checkpoint

Busy-forced requirements:
- preserve the last verified checkpoint fields as the factual base when the last-verified fallback wins
- add an explicit degraded reason explaining that fresh live grounding was not available because the target session was still active
- surface current busy-session evidence in `current_honest_state.in_flight`, `verified_evidence`, and `resume_safety.background_work`
- append a clear `do_not_rerun` warning to avoid duplicate writes or replaying older mutation attempts while work may still be in flight
- set `next_atomic_step` to the next safe inspection step needed before new writes or reruns
- annotate observability with `source_strategy=busy-forced-last-verified` when the last-verified fallback wins
- render a busy-forced status label when that source strategy is selected rather than the generic insufficient-signal label

Implementation rule:
- manual-force and threshold-triggered checkpointing should converge on the same replay/builder/validator/degraded path wherever practical; they should differ mainly in trigger source, not synthesis logic

## Intended rollout

Phase 1:
- align spec and implement reset-bounded replay-window loading (`checkpoint_session_window.py`)

Phase 2:
- add structured builder and one-pass repair behavior

Phase 3:
- switch the monitor to replay-package orchestration as the primary path
- expand replay/report gates and keep ledger fallback during rollout

Phase 4:
- make the replay-first validator gate mandatory once the expanded replay suite passes
