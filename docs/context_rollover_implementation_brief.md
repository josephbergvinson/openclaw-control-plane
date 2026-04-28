# Context Rollover Reliability — Implementation Brief

## Objective

Materially improve context rollover checkpoint quality and reliability by moving from ad hoc transcript scraping toward replay-tested, field-specific, structured checkpoint generation.

## Outcome target

The system should become operationally trustworthy enough that rollover checkpoints are usually resumable, rarely polluted, and only fall back to `insufficient signal` for genuinely barren/noisy transcripts.

## Ship criteria

- every rollover writes a canonical JSON artifact
- zero leaked wrapper/system/untrusted envelope text in replay fixtures
- replay corpus passes at >= 90% structured-or-degraded structured output
- objective extraction acceptable on >= 85% of replay corpus
- next-step extraction acceptable on >= 85% of replay corpus
- blocker false-positive rate near zero on curated replay corpus
- live monitor dry-run passes and launch agent reloads cleanly

## Recommended patch order

1. WP0 — replay corpus + quality gate
2. WP1 — unify degraded/failure path into canonical structured artifacts
3. WP2 — field-specific extractor refactor
4. WP3 — explicit status-packet-first source strategy
5. WP4 — session-side checkpoint state ledger
6. WP5 — observability, reporting, and rollout gate

## Work packages

### WP0 — Replay corpus + quality gate

Goal:
- stop debugging rollover failures by anecdote
- create replay fixtures and a deterministic replay harness over recent real failure/success classes

Initial deliverable for this slice:
- fixture-driven replay harness
- expected output assertions per fixture
- unit test coverage for the harness
- replay summary metrics including kind counts, reason buckets, tag buckets, and known-gap fixture counts

Current WP0 fixture baseline:
- wrapper complaint contamination
- approval-gated next step
- conditional follow-up clause failure
- stale user objective override
- waiting-input polluted-field structured output
- waiting-approval status-packet success

Exact file touch list:
- `docs/context_rollover_implementation_brief.md`
- `scripts/checkpoint_replay.py`
- `tests/test_checkpoint_replays.py`
- `tests/fixtures/rollover_replays/wrapper-complaint.jsonl`
- `tests/fixtures/rollover_replays/wrapper-complaint.expected.json`
- `tests/fixtures/rollover_replays/approval-gated-next-step.jsonl`
- `tests/fixtures/rollover_replays/approval-gated-next-step.expected.json`
- `tests/fixtures/rollover_replays/conditional-followup-clause.jsonl`
- `tests/fixtures/rollover_replays/conditional-followup-clause.expected.json`
- `tests/fixtures/rollover_replays/stale-user-objective.jsonl`
- `tests/fixtures/rollover_replays/stale-user-objective.expected.json`

Acceptance tests:
- `python3 scripts/checkpoint_replay.py --fixtures tests/fixtures/rollover_replays --json`
- `python3 -m unittest tests/test_checkpoint_replays.py`

### WP1 — Unify degraded/failure path into canonical structured artifacts

Goal:
- remove the split between structured success and ad hoc insufficient markdown

Exact file touch list:
- `scripts/checkpoint_rollover.py`
- `scripts/checkpoint_validate.py`
- `scripts/checkpoint_render.py`
- `scripts/thread_context_rollover_monitor.py`
- `tests/test_checkpoint_validate.py`
- `tests/test_checkpoint_render.py`
- `tests/test_thread_context_rollover_monitor.py`
- `docs/checkpoint_rollover_spec.md`

Acceptance tests:
- degraded fixtures still produce canonical JSON artifacts
- validator/render coverage passes for success and degraded paths

### WP2 — Field-specific extractor refactor

Goal:
- replace generic fragment mining with field-specific extraction logic

Exact file touch list:
- `scripts/checkpoint_extract.py`
- `scripts/thread_context_rollover_monitor.py`
- `tests/test_checkpoint_extract.py`
- `tests/test_thread_context_rollover_monitor.py`

Acceptance tests:
- approval-gated next step replays cleanly
- conditional follow-up clause chooses the follow-up clause
- stale user ask does not override newer assistant objective
- blockers/pending fields reject completed-state pollution

### WP3 — Explicit status-packet-first source strategy

Goal:
- treat recent explicit assistant status packets as the primary source of checkpoint truth

Exact file touch list:
- `scripts/checkpoint_extract.py`
- `scripts/thread_context_rollover_monitor.py`
- `tests/test_checkpoint_extract.py`
- `tests/test_thread_context_rollover_monitor.py`
- `docs/checkpoint_rollover_spec.md`

Acceptance tests:
- latest assistant status packet dominates older user asks
- labeled status fields survive into output without transcript override

### WP4 — Session-side checkpoint state ledger

Goal:
- reduce reliance on threshold-time transcript scraping by maintaining a lightweight session state ledger

Exact file touch list:
- `scripts/checkpoint_state_ledger.py`
- `scripts/thread_context_rollover_monitor.py`
- `tests/test_checkpoint_state_ledger.py`
- `tests/test_thread_context_rollover_monitor.py`
- `docs/checkpoint_rollover_spec.md`

Acceptance tests:
- earlier good status packet can still drive a good checkpoint even if final turns are noisy
- newer valid state overrides older state

### WP5 — Observability, reporting, and rollout gate

Goal:
- make checkpoint quality measurable and enforce a release gate

Exact file touch list:
- `scripts/checkpoint_rollover_report.py`
- `scripts/thread_context_rollover_monitor.py`
- `tests/test_checkpoint_rollover_report.py`
- `docs/checkpoint_rollover_spec.md`

Acceptance tests:
- report computes structured/degraded/insufficient counts correctly
- fail reasons and source strategies are visible in report output
- rollout gate is data-backed (coverage threshold + insufficient-count threshold), not prose-only

## Release gate

```bash
python3 -m unittest \
  tests/test_checkpoint_validate.py \
  tests/test_checkpoint_render.py \
  tests/test_checkpoint_extract.py \
  tests/test_checkpoint_replays.py \
  tests/test_checkpoint_state_ledger.py \
  tests/test_checkpoint_rollover_report.py \
  tests/test_thread_context_rollover_monitor.py

python3 scripts/checkpoint_replay.py --fixtures tests/fixtures/rollover_replays --json

python3 <OPENCLAW_WORKSPACE>/scripts/thread_context_rollover_monitor.py --dry-run --json

python3 scripts/checkpoint_rollover_report.py \
  --outbox tmp/context_rollover_monitor/OUTBOX.jsonl \
  --json
```

## Notes

- WP0 is allowed to bootstrap with expected-output assertions over current checkpoint text while canonical degraded JSON is not yet universal.
- WP1 should convert degraded outputs to canonical structured artifacts so replay fixtures can graduate from text assertions to fully structured expected JSON.
- LaunchAgent / runtime reload should happen only after code validation on the live checkout.
