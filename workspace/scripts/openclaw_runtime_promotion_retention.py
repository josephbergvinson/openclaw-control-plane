#!/usr/bin/env python3
"""Prune old OpenClaw runtime promotion/rebuild proof artifact directories.

Dry-run by default. Apply mode requires ``--apply`` or
``OPENCLAW_RUNTIME_PROMOTION_PRUNE_APPLY=1``. The helper only considers direct
children under ``artifacts/runtime_promotions`` from the explicitly admitted
producer naming families. It never restarts the Gateway and never deletes
outside that artifact root.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

try:
    from .operator_contract import load_operator_contract
except ImportError:  # direct script execution
    from operator_contract import load_operator_contract

OPERATOR = load_operator_contract()


try:
    from .openclaw_runtime_retention_metadata import (
        classify_operation_lock as classify_shared_operation_lock,
        dependency_requires_retention,
    )
except ImportError:  # direct script execution
    from openclaw_runtime_retention_metadata import (
        classify_operation_lock as classify_shared_operation_lock,
        dependency_requires_retention,
    )

APPLY_ENV = 'OPENCLAW_RUNTIME_PROMOTION_PRUNE_APPLY'
ANNOUNCE_NOOP_ENV = 'OPENCLAW_RUNTIME_PROMOTION_RETENTION_ANNOUNCE_NOOP'
DEFAULT_KEEP_LATEST = int(os.environ.get('OPENCLAW_RUNTIME_PROMOTION_KEEP_LATEST', '8'))
DEFAULT_MIN_AGE_DAYS = int(os.environ.get('OPENCLAW_RUNTIME_PROMOTION_MIN_AGE_DAYS', '3'))
LEGACY_PREFIXES = ('runtime-promotion-', 'runtime-rebuild-')
CURRENT_PRODUCER_PREFIXES = ('rebuild-to-', 'browser-control-runtime-promotion-')
ALLOWED_PREFIXES = (*LEGACY_PREFIXES, *CURRENT_PRODUCER_PREFIXES)
RMTREE_AVOIDS_SYMLINK_ATTACKS = bool(
    getattr(shutil.rmtree, 'avoids_symlink_attacks', False)
)
PROMOTION_OPERATION_DEPENDENCIES = {
    'previousReleasePath': 'previousReleaseDisposition',
    'rollbackReleasePath': 'rollbackReleaseDisposition',
}
REFERENCE_SCAN_SUFFIXES = {'.txt', '.md', '.log', '.out', '.err', '.json'}
LAST_OPERATION_CLASSIFICATIONS: list[dict[str, Any]] = []


@dataclass
class PromotionRecord:
    path: Path
    realpath: Path
    device: int
    inode: int
    directory_mtime_ns: int
    directory_ctime_ns: int
    size_bytes: int
    mtime_utc: datetime
    retained_reasons: list[str] = field(default_factory=list)
    action_state: str = 'pending'
    removal_started_at_utc: str | None = None
    removal_finished_at_utc: str | None = None
    after_exists: bool | None = None
    removed: bool = False
    remove_error: str | None = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def retained(self) -> bool:
        return bool(self.retained_reasons)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace('+00:00', 'Z')


def realpath(path: Path) -> Path:
    return Path(os.path.realpath(path))


def bytes_to_gib(value: int) -> float:
    return value / (1024 ** 3)


def format_gib(value: int) -> str:
    return f'{bytes_to_gib(value):.2f}GiB'


def du_bytes(path: Path) -> int:
    try:
        proc = subprocess.run(
            ['/usr/bin/du', '-sk', str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return int(proc.stdout.strip().splitlines()[0].split()[0]) * 1024
    except Exception:
        pass
    total = 0
    for root, _dirs, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(root) / filename).lstat().st_size
            except OSError:
                continue
    return total


def safe_promotion_realpath(path: Path, promotions_root: Path) -> Path:
    root_real = realpath(promotions_root)
    candidate_real = realpath(path)
    if candidate_real.parent != root_real:
        raise ValueError(f'unexpected promotion artifact path outside direct promotions root: {path} -> {candidate_real}')
    if not candidate_real.name.startswith(ALLOWED_PREFIXES):
        raise ValueError(f'unexpected promotion artifact directory name: {path}')
    return candidate_real


def list_promotion_records(promotions_root: Path) -> list[PromotionRecord]:
    try:
        root_info = promotions_root.stat()
    except FileNotFoundError as exc:
        if os.path.lexists(promotions_root):
            raise ValueError(
                f'promotions root is a dangling symlink: {promotions_root}'
            ) from exc
        return []
    except OSError as exc:
        raise ValueError(
            f'cannot stat promotions root {promotions_root}: '
            f'{type(exc).__name__}: {exc}'
        ) from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(
            f'promotions root must resolve to a directory: {promotions_root}'
        )
    records: list[PromotionRecord] = []
    for child in sorted(promotions_root.iterdir()):
        if not child.name.startswith(ALLOWED_PREFIXES):
            continue
        try:
            child_info = child.lstat()
        except OSError as exc:
            raise ValueError(
                f'cannot lstat promotion artifact {child}: '
                f'{type(exc).__name__}: {exc}'
            ) from exc
        if not stat.S_ISDIR(child_info.st_mode):
            raise ValueError(
                f'managed promotion artifact must be a physical directory: '
                f'{child}'
            )
        child_real = safe_promotion_realpath(child, promotions_root)
        records.append(
            PromotionRecord(
                path=child,
                realpath=child_real,
                device=child_info.st_dev,
                inode=child_info.st_ino,
                directory_mtime_ns=child_info.st_mtime_ns,
                directory_ctime_ns=child_info.st_ctime_ns,
                # Size is filled only for prune candidates after protection. Runtime
                # promotion artifacts may contain full source copies; sizing every
                # retained proof bundle makes the daily cron unnecessarily slow.
                size_bytes=0,
                mtime_utc=datetime.fromtimestamp(
                    child_info.st_mtime,
                    timezone.utc,
                ),
            )
        )
    return records


def current_release_tokens(current_symlink: Path | None = None) -> set[str]:
    current_symlink = current_symlink if current_symlink is not None else OPERATOR.require_path("paths.runtime_current_link")
    if not os.path.lexists(current_symlink):
        raise ValueError(
            f'authoritative current runtime symlink is absent: {current_symlink}'
        )
    if not current_symlink.is_symlink():
        raise ValueError(
            f'authoritative current runtime pointer is not a symlink: '
            f'{current_symlink}'
        )
    current_real = realpath(current_symlink)
    if not current_real.is_dir() or not current_real.name.startswith('openclaw-'):
        raise ValueError(
            f'authoritative current runtime symlink is dangling or invalid: '
            f'{current_symlink} -> {current_real}'
        )
    tokens: set[str] = set()
    tokens.add(str(current_real))
    tokens.add(current_real.name)
    # Runtime release names include the source commit near the end; keep this as
    # a broad reference token only when it is plausibly a commit-ish component.
    for part in current_real.name.replace('-', ' ').split():
        if len(part) >= 10 and all(ch in '0123456789abcdef' for ch in part.lower()):
            tokens.add(part[:10])
            tokens.add(part[:12])
    return {token for token in tokens if token}


def current_reference_retention_reason(
    record: PromotionRecord,
    tokens: set[str],
) -> str | None:
    if not tokens:
        return None
    haystack = record.name
    if any(token in haystack for token in tokens):
        return 'mentions-current-runtime-release'
    walk_errors: list[OSError] = []
    for root, dirs, files in os.walk(record.path, onerror=walk_errors.append):
        dirs[:] = [
            dirname for dirname in dirs
            if not (
                dirname.startswith('candidate-src-')
                or dirname in {'.git', 'node_modules', 'dist', 'build', '.venv', '__pycache__'}
            )
        ]
        for filename in files:
            path = Path(root) / filename
            if path.suffix not in REFERENCE_SCAN_SUFFIXES:
                continue
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    # Nested symlinks and special files are not authoritative
                    # retention metadata and must not permanently pin the
                    # entire promotion artifact.
                    continue
                if info.st_size > 200_000:
                    # Large build logs are evidentiary payload, not lifecycle
                    # authority. Explicit retention manifests and operation
                    # locks above own durable protection; do not let one large
                    # log turn age-based retention into retain-forever.
                    continue
                text = path.read_text(encoding='utf-8', errors='ignore')
            except OSError as exc:
                raise ValueError(
                    f'cannot inspect promotion current-runtime reference file '
                    f'{path}: {type(exc).__name__}: {exc}'
                ) from exc
            if any(token in text for token in tokens):
                return 'mentions-current-runtime-release'
    if walk_errors:
        exc = walk_errors[0]
        raise ValueError(
            f'cannot traverse promotion current-runtime reference artifact '
            f'{record.path}: {type(exc).__name__}: {exc}'
        ) from exc
    return None


def add_retained_reason(record: PromotionRecord, reason: str) -> None:
    if reason not in record.retained_reasons:
        record.retained_reasons.append(reason)


def load_metadata_object(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    flags = (
        os.O_RDONLY
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        return None, (
            f'{path.name}-invalid-fail-closed:{type(exc).__name__}'
        )
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino)
            != (current.st_dev, current.st_ino)
        ):
            return None, (
                f'{path.name}-invalid-fail-closed:not-a-physical-file'
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino)
            != (after.st_dev, after.st_ino)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            return None, (
                f'{path.name}-invalid-fail-closed:changed-during-read'
            )
        value = json.loads(b''.join(chunks).decode('utf-8'))
    except Exception as exc:
        return None, f'{path.name}-invalid-fail-closed:{type(exc).__name__}'
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        return None, f'{path.name}-invalid-fail-closed:not-an-object'
    return value, None


def iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_objects(child)


def manifest_declares_retain_long_term(payload: dict[str, Any]) -> bool:
    for entry in iter_json_objects(payload):
        if entry.get('class', entry.get('classification')) == 'retain_long_term':
            return True
        if 'retain_long_term' in entry and bool(entry['retain_long_term']):
            return True
    return False


def load_operation_json(path: Path) -> dict[str, Any]:
    payload, error = load_metadata_object(path)
    if error:
        raise ValueError(f'{path}: {error}')
    if payload is None:
        raise ValueError(f'{path}: operation metadata is absent')
    return payload


def operation_lock_retention_reasons(
    payload: dict[str, Any],
    classification: dict[str, Any],
) -> list[str]:
    lifecycle = classification.get('classification')
    if lifecycle not in {
        'legacy_unversioned_operation_lock',
        'legacy_retired_driver_lock',
        'terminalized_stale_active_lock',
        'terminalized_operation_lock',
    }:
        raise ValueError(f'unsupported promotion retention lifecycle classification: {lifecycle!r}')
    reasons: list[str] = []
    for path_field, disposition_field in PROMOTION_OPERATION_DEPENDENCIES.items():
        dependency = payload.get(path_field)
        if dependency is None:
            continue
        if not isinstance(dependency, str) or not dependency.strip():
            reasons.append(f'operation-lock-{path_field}-invalid-fail-closed')
            continue
        disposition = payload.get(disposition_field)
        if dependency_requires_retention(disposition):
            reasons.append(
                f'operation-lock-{path_field}-explicit-retention:{disposition}'
            )
    return reasons


def protect_durable_metadata(records: list[PromotionRecord]) -> None:
    global LAST_OPERATION_CLASSIFICATIONS
    LAST_OPERATION_CLASSIFICATIONS = []
    for record in records:
        manifest_path = record.path / 'retention-manifest.json'
        if os.path.lexists(manifest_path):
            manifest, error = load_metadata_object(manifest_path)
            if error:
                add_retained_reason(record, error)
            elif manifest is not None and manifest_declares_retain_long_term(manifest):
                add_retained_reason(record, 'retention-manifest-retain-long-term')

        lock_path = record.path / 'operation.lock.json'
        if os.path.lexists(lock_path):
            operation_lock, error = load_metadata_object(lock_path)
            if error:
                add_retained_reason(record, error)
                continue
            if operation_lock is None:
                add_retained_reason(record, 'operation.lock.json-invalid-fail-closed:absent')
                continue
            classification = classify_shared_operation_lock(
                lock_path,
                operation_lock,
                load_json=load_operation_json,
            )
            LAST_OPERATION_CLASSIFICATIONS.append(classification)
            for reason in operation_lock_retention_reasons(
                operation_lock,
                classification,
            ):
                add_retained_reason(record, reason)


def protect_records(records: list[PromotionRecord], *, now: datetime, keep_latest: int, min_age_days: int) -> None:
    protect_durable_metadata(records)

    newest = sorted(records, key=lambda record: record.mtime_utc, reverse=True)[:max(keep_latest, 0)]
    for record in newest:
        add_retained_reason(record, f'latest-{keep_latest}')

    min_age_seconds = max(min_age_days, 0) * 24 * 60 * 60
    for record in records:
        age_seconds = (now - record.mtime_utc).total_seconds()
        if age_seconds < min_age_seconds:
            add_retained_reason(record, f'younger-than-{min_age_days}d')

    tokens = current_release_tokens()
    for record in records:
        if record.retained:
            continue
        reason = current_reference_retention_reason(record, tokens)
        if reason:
            add_retained_reason(record, reason)


def size_candidates(records: list[PromotionRecord]) -> None:
    for record in records:
        if not record.retained:
            record.size_bytes = du_bytes(record.path)


def promotion_identity(
    record: PromotionRecord,
) -> tuple[str, int, int, int, int]:
    return (
        str(record.realpath),
        record.device,
        record.inode,
        record.directory_mtime_ns,
        record.directory_ctime_ns,
    )


def inventory_fingerprint(
    records: list[PromotionRecord],
) -> dict[str, tuple[str, int, int, int, int]]:
    return {record.name: promotion_identity(record) for record in records}


def retention_lock_path() -> Path:
    return OPERATOR.require_path("paths.runtime_retention_lock")


def acquire_shared_retention_lock() -> BinaryIO:
    """Acquire the nonblocking retention-writer lock.

    Promotion producers do not yet acquire this lock. It prevents overlapping
    retention writers; a full inventory/metadata reclassification immediately
    before each delete is therefore still mandatory. Current producer
    directories become candidates only after the independent latest, age,
    current-runtime-reference, active-operation, and explicit-retention gates.
    """

    path = retention_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
    )
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f'retention lock must be a physical regular file: {path}')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f'another runtime retention writer holds the shared lock: {path}') from exc
        return os.fdopen(descriptor, 'r+b', closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def prune_records(
    records: list[PromotionRecord],
    *,
    apply: bool,
    persist: Callable[[], None] = lambda: None,
    reclassify: Callable[[], list[PromotionRecord]] | None = None,
    expected_inventory: dict[str, tuple[str, int, int, int, int]] | None = None,
) -> None:
    if not apply:
        return
    expected = expected_inventory
    for record in records:
        if record.retained:
            continue
        if not RMTREE_AVOIDS_SYMLINK_ATTACKS:
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            record.remove_error = (
                'platform rmtree lacks symlink-attack resistance'
            )
            persist()
            continue
        record.action_state = 'removal_in_progress'
        record.removal_started_at_utc = utc_text()
        persist()
        if reclassify is not None:
            try:
                fresh_records = reclassify()
                fresh_inventory = inventory_fingerprint(fresh_records)
                if expected is not None and fresh_inventory != expected:
                    raise ValueError(
                        'promotion inventory or directory metadata changed '
                        'after classification'
                    )
                fresh = next(
                    (item for item in fresh_records if item.name == record.name),
                    None,
                )
                if fresh is None:
                    raise ValueError(
                        'promotion candidate disappeared after classification'
                    )
                if fresh.retained:
                    raise ValueError(
                        'promotion candidate became protected during full '
                        'metadata reclassification'
                    )
            except Exception as exc:
                record.action_state = 'remove_failed'
                record.after_exists = os.path.lexists(record.path)
                record.remove_error = f'{type(exc).__name__}: {exc}'
                persist()
                continue
        try:
            current = record.path.lstat()
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino)
                != (record.device, record.inode)
                or current.st_mtime_ns != record.directory_mtime_ns
                or current.st_ctime_ns != record.directory_ctime_ns
                or safe_promotion_realpath(
                    record.path,
                    OPERATOR.require_path("paths.runtime_promotions_root"),
                )
                != record.realpath
            ):
                raise ValueError(
                    'promotion candidate identity changed after '
                    'classification'
                )
        except Exception as exc:
            record.remove_error = f'{type(exc).__name__}: {exc}'
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            persist()
            continue
        try:
            shutil.rmtree(record.path)
            record.after_exists = os.path.lexists(record.path)
            if record.after_exists:
                raise ValueError('promotion candidate path exists after rmtree returned')
            record.removed = True
            record.action_state = 'removed'
            if expected is not None:
                expected.pop(record.name, None)
        except Exception as exc:  # pragma: no cover - defensive live safety path
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            record.remove_error = f'{type(exc).__name__}: {exc}'
        record.removal_finished_at_utc = utc_text()
        persist()


def relpath(path: Path) -> str:
    try:
        return path.relative_to(OPERATOR.require_path("paths.workspace")).as_posix()
    except ValueError:
        return str(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp'
    )
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def select_report_path(
    artifact_root: Path | None = None,
) -> tuple[str, str, Path]:
    started_at = utc_now()
    operation_id = (
        f'runtime-promotion-retention-'
        f'{started_at.strftime("%Y%m%dT%H%M%S%fZ")}-'
        f'{os.getpid()}-{uuid.uuid4().hex}'
    )
    root = artifact_root if artifact_root is not None else OPERATOR.require_path("paths.runtime_promotion_retention_artifacts")
    return operation_id, utc_text(started_at), root / f'{operation_id}.json'


def promotion_receipt(record: PromotionRecord) -> dict[str, Any]:
    return {
        'name': record.name,
        'path': str(record.path),
        'realpath': str(record.realpath),
        'device': record.device,
        'inode': record.inode,
        'directory_mtime_ns': record.directory_mtime_ns,
        'directory_ctime_ns': record.directory_ctime_ns,
        'before_exists': True,
        'before_size_bytes': record.size_bytes,
        'retained': record.retained,
        'retained_reasons': list(record.retained_reasons),
        'state': record.action_state,
        'removal_started_at_utc': record.removal_started_at_utc,
        'removal_finished_at_utc': record.removal_finished_at_utc,
        'after_exists': record.after_exists,
        'error': record.remove_error,
    }


def report_payload(
    records: list[PromotionRecord],
    *,
    apply: bool,
    keep_latest: int,
    min_age_days: int,
    operation_id: str,
    started_at_utc: str,
    state: str,
    terminal: bool,
) -> dict[str, Any]:
    retained = [record for record in records if record.retained]
    candidates = [record for record in records if not record.retained]
    removed = [record for record in records if record.removed]
    errors = [record for record in records if record.remove_error]
    return {
        'schema': 'openclaw.runtime_promotion_retention.v2',
        'operation_id': operation_id,
        'created_at_utc': started_at_utc,
        'updated_at_utc': utc_text(),
        'mode': 'apply' if apply else 'dry-run',
        'state': state,
        'terminal': terminal,
        'promotions_root': str(OPERATOR.require_path("paths.runtime_promotions_root")),
        'operation_classifications': list(LAST_OPERATION_CLASSIFICATIONS),
        'policy': {
            'allowed_prefixes': list(ALLOWED_PREFIXES),
            'automatic_age_prune_prefixes': list(ALLOWED_PREFIXES),
            'inactive_operation_dependencies': 'ignored unless an explicit retention disposition is present',
            'active_operation_lock': 'hard-block unless a strictly matching terminal receipt proves the lock stale',
            'keep_latest': keep_latest,
            'min_age_days': min_age_days,
            'current_runtime_symlink': str(OPERATOR.require_path("paths.runtime_current_link")),
            'producer_coordination': (
                'retention writers share a nonblocking lock; producers do not '
                'yet share it, so each deletion requires a full immediate '
                'reclassification'
            ),
        },
        'summary': {
            'total_artifacts': len(records),
            'retained_count': len(retained),
            'candidate_count': len(candidates),
            'removed_count': len(removed),
            'error_count': len(errors),
            'reclaimable_bytes': sum(record.size_bytes for record in candidates),
            'removed_bytes': sum(record.size_bytes for record in removed),
            'in_progress_count': sum(
                record.action_state == 'removal_in_progress'
                for record in records
            ),
        },
        'receipts': [promotion_receipt(record) for record in records],
        'retained': [
            {
                'name': record.name,
                'path': str(record.path),
                'realpath': str(record.realpath),
                'mtime_utc': record.mtime_utc.isoformat().replace('+00:00', 'Z'),
                'size_bytes': record.size_bytes,
                'size_gib': round(bytes_to_gib(record.size_bytes), 3),
                'reasons': record.retained_reasons,
                'state': record.action_state,
            }
            for record in retained
        ],
        'candidates': [
            {
                'name': record.name,
                'path': str(record.path),
                'realpath': str(record.realpath),
                'mtime_utc': record.mtime_utc.isoformat().replace('+00:00', 'Z'),
                'size_bytes': record.size_bytes,
                'size_gib': round(bytes_to_gib(record.size_bytes), 3),
                'action': record.action_state,
                'error': record.remove_error,
            }
            for record in candidates
        ],
        'removed': [record.name for record in removed],
        'errors': [{'name': record.name, 'error': record.remove_error} for record in errors],
        'gateway_restart': 'not_performed',
    }


def write_report(
    records: list[PromotionRecord],
    *,
    apply: bool,
    keep_latest: int,
    min_age_days: int,
    operation_id: str,
    started_at_utc: str,
    state: str,
    terminal: bool,
    report_path: Path,
) -> Path:
    payload = report_payload(
        records,
        apply=apply,
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        operation_id=operation_id,
        started_at_utc=started_at_utc,
        state=state,
        terminal=terminal,
    )
    atomic_write_json(report_path, payload)
    atomic_write_json(report_path.parent / 'latest.json', payload)
    return report_path


def status_line(records: list[PromotionRecord], *, apply: bool, report_path: Path) -> tuple[str, str]:
    retained = [record for record in records if record.retained]
    candidates = [record for record in records if not record.retained]
    removed = [record for record in records if record.removed]
    errors = [record for record in records if record.remove_error]
    marker = 'RUNTIME_PROMOTION_RETENTION_OK'
    result = 'no_prunable_promotion_artifacts'
    if errors:
        marker = 'RUNTIME_PROMOTION_RETENTION_BLOCKED'
        result = 'remove_errors'
    elif not apply and candidates:
        marker = 'RUNTIME_PROMOTION_RETENTION_DRY_RUN'
        result = 'dry_run_candidates'
    elif apply and removed:
        result = 'removed_old_promotion_artifacts'
    report_rel = relpath(report_path)
    detail = (
        f'STATUS | result: {result} | mode: {"apply" if apply else "dry-run"} '
        f'| total: {len(records)} | retained: {len(retained)} | candidates: {len(candidates)} '
        f'| removed: {len(removed)} | reclaimable: {format_gib(sum(record.size_bytes for record in candidates))} '
        f'| report: {report_rel} | gateway_restart: not_performed'
    )
    if errors:
        detail += ' | blockers: ' + '; '.join(f'{record.name}:{record.remove_error}' for record in errors)
    return marker, detail


def _run_locked(
    *,
    apply: bool,
    keep_latest: int = DEFAULT_KEEP_LATEST,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> tuple[int, str, str, Path]:
    records = list_promotion_records(OPERATOR.require_path("paths.runtime_promotions_root"))
    protect_records(records, now=utc_now(), keep_latest=keep_latest, min_age_days=min_age_days)
    size_candidates(records)
    for record in records:
        record.action_state = (
            'skipped_retained'
            if record.retained
            else ('pending' if apply else 'would_remove')
        )
        record.after_exists = (
            os.path.lexists(record.path)
            if record.retained or not apply
            else None
        )
    operation_id, started_at_utc, report_path = select_report_path(OPERATOR.require_path("paths.runtime_promotion_retention_artifacts"))
    expected_inventory = inventory_fingerprint(records)

    def persist_in_progress() -> None:
        write_report(
            records,
            apply=apply,
            keep_latest=keep_latest,
            min_age_days=min_age_days,
            operation_id=operation_id,
            started_at_utc=started_at_utc,
            state='apply_in_progress',
            terminal=False,
            report_path=report_path,
        )

    def reclassify() -> list[PromotionRecord]:
        fresh = list_promotion_records(OPERATOR.require_path("paths.runtime_promotions_root"))
        protect_records(
            fresh,
            now=utc_now(),
            keep_latest=keep_latest,
            min_age_days=min_age_days,
        )
        return fresh

    if apply:
        persist_in_progress()
        prune_records(
            records,
            apply=True,
            persist=persist_in_progress,
            reclassify=reclassify,
            expected_inventory=expected_inventory,
        )
        terminal_state = (
            'apply_blocked'
            if any(record.remove_error for record in records)
            else 'apply_complete'
        )
    else:
        terminal_state = 'dry_run_complete'
    write_report(
        records,
        apply=apply,
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        operation_id=operation_id,
        started_at_utc=started_at_utc,
        state=terminal_state,
        terminal=True,
        report_path=report_path,
    )
    marker, detail = status_line(records, apply=apply, report_path=report_path)
    rc = 1 if marker == 'RUNTIME_PROMOTION_RETENTION_BLOCKED' else 0
    return rc, marker, detail, report_path


def run(
    *,
    apply: bool,
    keep_latest: int = DEFAULT_KEEP_LATEST,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> tuple[int, str, str, Path]:
    lock = acquire_shared_retention_lock()
    try:
        return _run_locked(
            apply=apply,
            keep_latest=keep_latest,
            min_age_days=min_age_days,
        )
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Dry-run or apply OpenClaw runtime promotion artifact retention pruning.')
    parser.add_argument('--apply', action='store_true', help='Remove old unretained promotion artifacts. Without this flag, only writes a report.')
    parser.add_argument('--keep-latest', type=int, default=DEFAULT_KEEP_LATEST, help='Always retain this many newest promotion artifacts.')
    parser.add_argument('--min-age-days', type=int, default=DEFAULT_MIN_AGE_DAYS, help='Retain artifacts younger than this many days.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    apply = bool(args.apply or os.environ.get(APPLY_ENV) == '1')
    rc, marker, detail, _ = run(apply=apply, keep_latest=args.keep_latest, min_age_days=args.min_age_days)
    announce_noop = os.environ.get(ANNOUNCE_NOOP_ENV, '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if rc == 0 and apply and 'result: no_prunable_promotion_artifacts' in detail and not announce_noop:
        print('NO_REPLY')
        return 0
    print(marker)
    print(detail)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
