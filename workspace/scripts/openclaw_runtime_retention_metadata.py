#!/usr/bin/env python3
"""Shared lifecycle policy for OpenClaw runtime-promotion retention metadata.

Retention may ignore unresolved dependency fields only after the producing
operation is provably non-active. An ``active`` lock remains a global hard
block unless a strictly matching terminal receipt proves that the durable lock
is stale. Explicit retention dispositions continue to protect dependencies.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PROMOTION_OPERATION_LOCK_KIND = 'openclaw.runtime-promotion.operation-lock.v2'
PROMOTION_TERMINAL_RECEIPT_KIND = 'openclaw.runtime-promotion.terminal-receipt.v1'
PROMOTION_TERMINAL_STATES = frozenset({'completed', 'blocked', 'failed', 'cancelled', 'superseded'})
# Retired per-run drivers wrote these lock states. They are frozen residue,
# mapped explicitly so unknown non-active vocabulary fails closed.
LEGACY_OPERATION_LOCK_STATES = {
    'acquired': 'superseded',
    'build-claimed': 'superseded',
    'candidate-preparation-intent-recorded': 'superseded',
    'release-copy-claimed': 'superseded',
    'offline-store-coverage-claimed': 'superseded',
    'release-sealed-pending-activation-authority': 'superseded',
    'pre-discord-live-accepted': 'superseded',
    'blocked_preflight': 'blocked',
    'blocked_packaging_self_containment': 'blocked',
    'blocked_integration_acceptance_failed': 'blocked',
    'blocked_pre_mutation_symlink_closure': 'blocked',
    'terminal-failed': 'failed',
    'terminal-success': 'completed',
    'completed_live_proof': 'completed',
}
SAFE_TO_PRUNE_DISPOSITION = 'safe_to_prune_now'
RETAIN_DISPOSITION_PREFIXES = ('retain_', 'optional_cold_archive')

JsonLoader = Callable[[Path], dict[str, Any]]


def parse_utc_timestamp(value: Any, *, source: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{source} must be a non-empty timezone-aware timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(f'{source} must be an ISO-8601 timestamp: {value!r}') from exc
    if parsed.tzinfo is None:
        raise ValueError(f'{source} must include a timezone: {value!r}')
    return parsed.astimezone(timezone.utc)


def require_sha256(value: Any, *, source: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value.lower())
    ):
        raise ValueError(f'{source} must be a 64-character hexadecimal SHA-256')
    return value.lower()


def iter_json_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def classify_operation_lock(
    lock_path: Path,
    payload: dict[str, Any],
    *,
    load_json: JsonLoader,
) -> dict[str, Any]:
    """Classify one durable operation lock without weakening active safety.

    Canonical terminal states require the same receipt proof as a stale active
    lock. Retired-driver states are explicitly mapped; unknown vocabulary fails
    closed rather than silently becoming lifecycle-complete.
    """

    lock_state = payload.get('state')
    classification: dict[str, Any] = {
        'operation_root': lock_path.parent.name,
        'operation_lock_path': str(lock_path),
        'lock_state': lock_state,
        'classification': 'non_active_operation_lock',
        'terminal_receipt_path': None,
        'terminal_state': None,
    }
    if lock_state != 'active':
        if (
            lock_state in PROMOTION_TERMINAL_STATES
            and payload.get('kind') == PROMOTION_OPERATION_LOCK_KIND
        ):
            # Current coordinator terminalization: fall through to the same
            # physical receipt proof required for a stale active lock.
            pass
        elif lock_state is None or (
            lock_state in PROMOTION_TERMINAL_STATES
            and payload.get('kind') != PROMOTION_OPERATION_LOCK_KIND
        ):
            # Older dependency-only operation.lock.json files predate the v2
            # lifecycle contract and often have no kind or state at all. Keep
            # their explicit dependency/disposition semantics readable without
            # treating a new, explicit unknown state as terminal vocabulary.
            classification.update(
                {
                    'classification': 'legacy_unversioned_operation_lock',
                    'terminal_state': (
                        lock_state if lock_state in PROMOTION_TERMINAL_STATES else None
                    ),
                }
            )
            return classification
        elif lock_state in LEGACY_OPERATION_LOCK_STATES:
            classification.update(
                {
                    'classification': 'legacy_retired_driver_lock',
                    'terminal_state': LEGACY_OPERATION_LOCK_STATES[lock_state],
                    'legacy_lock_state': lock_state,
                }
            )
            return classification
        else:
            raise ValueError(
                f'{lock_path}: operation lock state is neither active, a canonical '
                f'terminal state, nor a mapped retired-driver state: {lock_state!r}'
            )

    terminal_path = lock_path.parent / 'terminal-receipt.json'
    if not terminal_path.exists() or terminal_path.is_symlink():
        raise ValueError(
            f'active promotion operation blocks runtime release retention until a '
            f'strictly matching physical terminal receipt exists: {lock_path}'
        )
    terminal = load_json(terminal_path)
    if payload.get('kind') != PROMOTION_OPERATION_LOCK_KIND:
        raise ValueError(f'{lock_path}: active operation lock kind is invalid')
    if terminal.get('kind') != PROMOTION_TERMINAL_RECEIPT_KIND:
        raise ValueError(f'{terminal_path}: terminal receipt kind is invalid')

    operation_id = payload.get('operationId')
    run_id = payload.get('runId')
    if not isinstance(operation_id, str) or not operation_id:
        raise ValueError(f'{lock_path}: active operationId must be a non-empty string')
    if operation_id != lock_path.parent.name:
        raise ValueError(f'{lock_path}: active operationId does not match its operation directory')
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f'{lock_path}: active runId must be a non-empty string')
    if terminal.get('operationId') != operation_id or terminal.get('runId') != run_id:
        raise ValueError(f'{terminal_path}: terminal receipt operationId/runId does not match active lock')

    terminal_state = terminal.get('state')
    if terminal_state not in PROMOTION_TERMINAL_STATES:
        raise ValueError(f'{terminal_path}: terminal receipt state is invalid: {terminal_state!r}')
    if (
        lock_state in PROMOTION_TERMINAL_STATES
        and terminal_state != lock_state
    ):
        raise ValueError(
            f'{terminal_path}: terminal receipt state does not match canonical lock state'
        )
    lock_authority = require_sha256(payload.get('authoritySha256'), source=f'{lock_path}:authoritySha256')
    terminal_authority = require_sha256(terminal.get('authoritySha256'), source=f'{terminal_path}:authoritySha256')
    if terminal_authority != lock_authority:
        raise ValueError(f'{terminal_path}: terminal receipt authoritySha256 does not match active lock')

    lock_sequence = payload.get('journalSequence')
    terminal_sequence = terminal.get('journalSequence')
    if not isinstance(lock_sequence, int) or lock_sequence < 0:
        raise ValueError(f'{lock_path}: journalSequence must be a non-negative integer')
    if not isinstance(terminal_sequence, int) or terminal_sequence < lock_sequence:
        raise ValueError(f'{terminal_path}: terminal journalSequence precedes the active lock')
    require_sha256(terminal.get('journalHeadSha256'), source=f'{terminal_path}:journalHeadSha256')

    acquired_at = parse_utc_timestamp(payload.get('acquiredAt'), source=f'{lock_path}:acquiredAt')
    recorded_at = parse_utc_timestamp(terminal.get('recordedAt'), source=f'{terminal_path}:recordedAt')
    if recorded_at < acquired_at:
        raise ValueError(f'{terminal_path}: terminal receipt predates the active operation lock')

    classification.update(
        {
            'classification': (
                'terminalized_stale_active_lock'
                if lock_state == 'active'
                else 'terminalized_operation_lock'
            ),
            'operation_id': operation_id,
            'run_id': run_id,
            'terminal_receipt_path': str(terminal_path),
            'terminal_state': terminal_state,
            'terminal_recorded_at': terminal.get('recordedAt'),
        }
    )
    return classification


def dependency_requires_retention(disposition: Any) -> bool:
    """Return whether an inactive operation explicitly retains a dependency."""

    if disposition is None or disposition == SAFE_TO_PRUNE_DISPOSITION:
        return False
    if not isinstance(disposition, str):
        raise ValueError('promotion dependency disposition must be a string when present')
    # Unknown non-empty dispositions fail closed instead of being silently
    # interpreted as prune authority.
    return bool(disposition.strip())
