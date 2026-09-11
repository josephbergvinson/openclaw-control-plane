#!/usr/bin/env python3
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import os
import shutil
import subprocess
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .root_drift_state import classify_item, load_policy, parse_porcelain_line
else:
    from root_drift_state import classify_item, load_policy, parse_porcelain_line


@dataclass(frozen=True)
class SurfaceSpec:
    slug: str
    root: Path
    required: tuple[str, ...]


@dataclass(frozen=True)
class GitDrift:
    slug: str
    root: Path
    entries: tuple[str, ...]
    disposition: str = 'actionable'
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedResidueScan:
    paths: tuple[Path, ...]
    attention: tuple[str, ...]


@dataclass(frozen=True)
class CleanupResult:
    removed: tuple[str, ...]
    errors: tuple[str, ...]
    attention: tuple[str, ...]


@dataclass(frozen=True)
class ArtifactResult:
    artifact_id: str
    json_path: Path
    text_path: Path
    json_relpath: str
    text_relpath: str


SURFACE_SPECS = (
    SurfaceSpec(
        slug='control-plane',
        root=Path((str(OPERATOR.require_path('paths.workspace')))),
        required=(
            '.git',
            'AGENTS.md',
            'runbooks/host-maintenance.md',
            'scripts/cron_python_entrypoint.py',
            'scripts/workspace_integrity_guard.py',
            'scripts/gateway_autoheal_cron.py',
        ),
    ),

)

CONTROL_PLANE_ROOT = SURFACE_SPECS[0].root
ARTIFACT_ROOT = CONTROL_PLANE_ROOT / 'artifacts' / 'workspace_integrity'
SAFE_GENERATED_DIR_NAMES = frozenset({'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'})
SKIP_GENERATED_SCAN_DIR_NAMES = frozenset(
    {'.git', '.worktrees', '.venv', 'venv', 'env', 'node_modules', 'artifacts'}
)
SKIP_GENERATED_SCAN_DIR_PATTERNS = (
    ('artifacts', 'openclaw-recovery-backups', '*', 'bad-dist-openclaw-selfref'),
)
ACTIONABLE_DRIFT = 'actionable'
RETAINED_STATE_DRIFT = 'retained_state'
PROTECTED_TASK_LANE_DRIFT = 'protected_task_lane'
PROTECTED_SOURCE_DRIFT = 'protected_source'
# Drift the guard removed itself this run: paths the drift policy classifies as
# generated residue, which need no operator judgement.
RECONCILED_DRIFT = 'reconciled'
# Unresolved conflicts, source deletions/type changes and unclassified paths
# require review. Ordinary source edits are work, not an integrity failure.
OPERATOR_DECISION_DRIFT = 'operator_decision'
SAFE_REMOVE_CLASS = 'safe-remove'
RETAINED_STATE_CLASS = 'retained-state'
DRIFT_DISPOSITIONS = (
    ACTIONABLE_DRIFT,
    RETAINED_STATE_DRIFT,
    PROTECTED_TASK_LANE_DRIFT,
    PROTECTED_SOURCE_DRIFT,
    RECONCILED_DRIFT,
    OPERATOR_DECISION_DRIFT,
)


def name_a_few(paths: tuple[str, ...], limit: int = 5) -> str:
    """Name the first few paths and count the rest, for a chat-sized line."""

    unique = sorted(set(paths))
    if not unique:
        return 'nothing'
    if len(unique) <= limit:
        return ', '.join(unique)
    remaining = len(unique) - limit
    return ', '.join(unique[:limit]) + f' and {remaining} more'


def surface_blockers(spec: SurfaceSpec) -> list[str]:
    if not spec.root.is_dir():
        return [f'{spec.slug}:root_missing={spec.root}']

    missing = [rel for rel in spec.required if not (spec.root / rel).exists()]
    if missing:
        return [f'{spec.slug}:missing={",".join(missing)}']
    return []


def relpath(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def matches_path_pattern(path: Path, root: Path, pattern: tuple[str, ...]) -> bool:
    parts = relpath(path, root).split('/')
    if len(parts) != len(pattern):
        return False
    return all(expected == '*' or actual == expected for actual, expected in zip(parts, pattern))


def should_skip_generated_scan_dir(path: Path, root: Path) -> bool:
    return any(matches_path_pattern(path, root, pattern) for pattern in SKIP_GENERATED_SCAN_DIR_PATTERNS)


def scan_attention(path: Path, root: Path, reason: str) -> str:
    return f'{relpath(path, root)}:{reason}'


def _resolved_inside_root(path: Path, resolved_root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    return True


def _stable_dir_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def find_safe_generated_residue(root: Path) -> GeneratedResidueScan:
    if not root.is_dir():
        return GeneratedResidueScan((), ())

    found: list[Path] = []
    attention: list[str] = []
    resolved_root = root.resolve(strict=True)
    found_identities: set[tuple[int, int]] = set()
    visited_dirs: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        if not _resolved_inside_root(current, resolved_root):
            attention.append(scan_attention(current, root, 'outside_root_skipped'))
            continue
        current_identity = _stable_dir_identity(current)
        if current_identity is None:
            attention.append(scan_attention(current, root, 'scan_error:OSError'))
            continue
        if current_identity in visited_dirs:
            continue
        visited_dirs.add(current_identity)
        if should_skip_generated_scan_dir(current, root):
            attention.append(scan_attention(current, root, 'generated_artifact_tree_skipped'))
            continue
        try:
            children = list(current.iterdir())
        except OSError as exc:
            attention.append(scan_attention(current, root, f'scan_error:{exc.__class__.__name__}'))
            continue

        for child in children:
            try:
                child.lstat()
            except OSError as exc:
                attention.append(scan_attention(child, root, f'scan_error:{exc.__class__.__name__}'))
                continue
            if os.path.islink(child):
                continue
            try:
                child_is_dir = os.path.isdir(child)
            except OSError as exc:
                attention.append(scan_attention(child, root, f'scan_error:{exc.__class__.__name__}'))
                continue
            if not child_is_dir:
                continue
            if not _resolved_inside_root(child, resolved_root):
                attention.append(scan_attention(child, root, 'outside_root_skipped'))
                continue
            name = child.name
            if name in SAFE_GENERATED_DIR_NAMES:
                identity = _stable_dir_identity(child)
                if identity is None:
                    attention.append(scan_attention(child, root, 'scan_error:OSError'))
                    continue
                if identity not in found_identities:
                    found.append(child)
                    found_identities.add(identity)
                continue
            if name in SKIP_GENERATED_SCAN_DIR_NAMES:
                continue
            stack.append(child)
    return GeneratedResidueScan(tuple(sorted(found)), tuple(sorted(set(attention))))


def cleanup_safe_generated_residue(root: Path) -> CleanupResult:
    removed: list[str] = []
    errors: list[str] = []
    scan = find_safe_generated_residue(root)
    for path in scan.paths:
        path_label = relpath(path, root)
        try:
            shutil.rmtree(path)
            removed.append(path_label)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f'{path_label}:{exc.__class__.__name__}')
    return CleanupResult(tuple(removed), tuple(errors), scan.attention)


def git_status_entries(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ['/usr/bin/git', '-C', str(root), 'status', '--porcelain=v1', '--untracked-files=all'],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip().replace('\n', ' ')
        return (f'!! git_status_failed={stderr or result.returncode}',)
    return tuple(line for line in result.stdout.splitlines() if line.strip())


def git_worktree_paths(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ['/usr/bin/git', '-C', str(root), 'worktree', 'list', '--porcelain'],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return ()

    paths: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith('worktree '):
            paths.append(Path(line.removeprefix('worktree ')))
    return tuple(paths)


def is_git_worktree(root: Path) -> bool:
    result = subprocess.run(
        ['/usr/bin/git', '-C', str(root), 'rev-parse', '--is-inside-work-tree'],
        check=False,
        text=True,
        capture_output=True,
    )
    return result.returncode == 0 and result.stdout.strip() == 'true'


def remove_generated_drift_path(root: Path, relative: str) -> str | None:
    """Delete one policy-classified generated-residue path. Returns an error label."""

    target = root / relative
    try:
        # Never follow a link out of the checkout, and never walk a resolved
        # path that escapes it.
        if target.is_symlink():
            target.unlink()
            return None
        resolved = target.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return f'{relative}:{exc.__class__.__name__}'
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f'{relative}:{exc.__class__.__name__}'
    return None


def classify_root_drift(
    root: Path,
    slug: str,
    entries: tuple[str, ...],
    *,
    reconcile: bool = True,
) -> tuple[tuple[GitDrift, ...], tuple[str, ...]]:
    """Split root drift by disposition, removing generated residue as it goes.

    Returns the classified drift plus any errors hit while removing residue.
    Retained state, protected lanes, and root-authoritative source are never
    touched: only paths the drift policy calls generated residue are removed,
    and only when they are untracked.
    """

    policy = load_policy()
    grouped: dict[str, list[str]] = {disposition: [] for disposition in DRIFT_DISPOSITIONS}
    reasons: dict[str, list[str]] = {disposition: [] for disposition in DRIFT_DISPOSITIONS}
    errors: list[str] = []
    for entry in entries:
        item = classify_item(parse_porcelain_line(entry), root, policy)
        classification = item['classification']
        path = item['path']
        if item['status'] in {'DD', 'AU', 'UD', 'UA', 'DU', 'AA', 'UU'}:
            disposition = OPERATOR_DECISION_DRIFT
            reason = f'{path}:unmerged'
        elif classification == RETAINED_STATE_CLASS:
            disposition = RETAINED_STATE_DRIFT
            reason = f'{path}:{classification}'
        elif classification in {'root-local', 'normalize-to-root'} and not {'D', 'T'}.intersection(item['status']):
            disposition = PROTECTED_SOURCE_DRIFT
            reason = f'{path}:{classification}:preserved'
        elif classification == SAFE_REMOVE_CLASS and item['status'] == '??' and reconcile:
            error = remove_generated_drift_path(root, path)
            if error:
                errors.append(error)
                disposition = OPERATOR_DECISION_DRIFT
                reason = f'{path}:{classification}:remove_failed'
            else:
                disposition = RECONCILED_DRIFT
                reason = f'{path}:{classification}:removed'
        else:
            # Unknown or tracked generated paths cannot be removed safely.
            disposition = OPERATOR_DECISION_DRIFT
            reason = f'{path}:{classification}'
        grouped[disposition].append(entry)
        reasons[disposition].append(reason)
    drift = tuple(
        GitDrift(slug, root, tuple(grouped[disposition]), disposition, tuple(reasons[disposition]))
        for disposition in DRIFT_DISPOSITIONS
        if grouped[disposition]
    )
    return drift, tuple(errors)


def git_drift(
    root: Path,
    slug: str = 'control-plane',
    *,
    reconcile: bool = True,
) -> tuple[tuple[GitDrift, ...], tuple[str, ...]]:
    if not is_git_worktree(root):
        return (), ()

    canonical_root = root.resolve()
    drift: list[GitDrift] = []
    errors: list[str] = []
    for path in git_worktree_paths(root) or (root,):
        if path.resolve() == canonical_root:
            entries = git_status_entries(path)
            if not entries:
                continue
            if any(entry.startswith('!! git_status_failed=') for entry in entries):
                errors.extend(entries)
                continue
            classified, classify_errors = classify_root_drift(
                root,
                slug,
                entries,
                reconcile=reconcile,
            )
            drift.extend(classified)
            errors.extend(classify_errors)
            continue
        lane_slug = f'{slug}:worktree:{path.name}'
        drift.append(
            GitDrift(
                lane_slug,
                path,
                (),
                PROTECTED_TASK_LANE_DRIFT,
                ('registered_nonstanding_worktree_not_scanned',),
            )
        )
    return tuple(drift), tuple(errors)


def format_drift(drift: tuple[GitDrift, ...], root: Path) -> str:
    parts: list[str] = []
    for item in drift:
        sample = ','.join(item.entries[:8])
        if len(item.entries) > 8:
            sample += f',...(+{len(item.entries) - 8})'
        parts.append(f'{item.slug}[{relpath(item.root, root)}][{item.disposition}]:{sample}')
    return '; '.join(parts)


def write_artifacts(
    *,
    marker: str,
    result: str,
    roots: str,
    blockers: list[str],
    cleanup: CleanupResult,
    drift: tuple[GitDrift, ...],
    next_step: str,
    artifact_root: Path | None = None,
    control_root: Path | None = None,
    drift_errors: list[str] | None = None,
) -> ArtifactResult:
    artifact_root = artifact_root or ARTIFACT_ROOT
    control_root = control_root or CONTROL_PLANE_ROOT
    now = datetime.now(timezone.utc)
    artifact_id = f'workspace-integrity-{now.strftime("%Y%m%dT%H%M%S%fZ")}'
    artifact_root.mkdir(parents=True, exist_ok=True)
    json_path = artifact_root / f'{artifact_id}.json'
    text_path = artifact_root / f'{artifact_id}.txt'

    payload = {
        'schema': 'openclaw.workspace_integrity.v1',
        'artifact_id': artifact_id,
        'created_at_utc': now.isoformat().replace('+00:00', 'Z'),
        'marker': marker,
        'result': result,
        'control_root': str(control_root),
        'roots': roots.split(',') if roots else [],
        'root_blockers': blockers,
        'cleanup': {
            'safe_generated_residue_removed': list(cleanup.removed),
            'safe_generated_residue_cleanup_errors': list(cleanup.errors),
            'safe_generated_residue_attention': list(cleanup.attention),
            'drift_reconcile_errors': list(drift_errors or ()),
        },
        'git_drift': [
            {
                'slug': item.slug,
                'root': str(item.root),
                'root_relpath': relpath(item.root, control_root),
                'entries': list(item.entries),
                'disposition': item.disposition,
                'reasons': list(item.reasons),
            }
            for item in drift
        ],
        'drift_summary': {
            disposition: sum(len(item.entries) for item in drift if item.disposition == disposition)
            for disposition in DRIFT_DISPOSITIONS
        },
        'next': next_step,
    }
    with os.fdopen(os.open(json_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + '\n')

    text_lines = [
        marker,
        f'artifact_id: {artifact_id}',
        f'created_at_utc: {payload["created_at_utc"]}',
        f'result: {result}',
        f'control_root: {control_root}',
        f'roots: {roots or "none"}',
        '',
        'root_blockers:',
        *(f'- {item}' for item in blockers),
        *(('- none',) if not blockers else ()),
        '',
        'cleanup:',
        f'- safe_generated_residue_removed: {", ".join(cleanup.removed) if cleanup.removed else "none"}',
        f'- safe_generated_residue_cleanup_errors: {", ".join(cleanup.errors) if cleanup.errors else "none"}',
        f'- safe_generated_residue_attention: {", ".join(cleanup.attention) if cleanup.attention else "none"}',
        '',
        'git_drift:',
    ]
    if drift:
        for item in drift:
            text_lines.append(
                f'- {item.slug} [{relpath(item.root, control_root)}] disposition={item.disposition}'
            )
            text_lines.extend(f'  - {entry}' for entry in item.entries)
            text_lines.extend(f'  - reason: {reason}' for reason in item.reasons)
    else:
        text_lines.append('- none')
    text_lines.extend(('', f'next: {next_step}', ''))
    with os.fdopen(os.open(text_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(text_lines))

    return ArtifactResult(
        artifact_id=artifact_id,
        json_path=json_path,
        text_path=text_path,
        json_relpath=relpath(json_path, control_root),
        text_relpath=relpath(text_path, control_root),
    )


def main() -> int:
    blockers: list[str] = []
    for spec in SURFACE_SPECS:
        blockers.extend(surface_blockers(spec))

    cleanup = cleanup_safe_generated_residue(CONTROL_PLANE_ROOT)
    drift, drift_errors = git_drift(CONTROL_PLANE_ROOT)
    roots = ','.join(spec.slug for spec in SURFACE_SPECS)

    failed = bool(blockers or cleanup.errors or drift_errors)
    reconciled_paths = tuple(
        reason.split(':', 1)[0]
        for item in drift
        if item.disposition == RECONCILED_DRIFT
        for reason in item.reasons
    )
    operator_paths = tuple(
        reason.split(':', 1)[0]
        for item in drift
        if item.disposition == OPERATOR_DECISION_DRIFT
        for reason in item.reasons
    )
    attention = bool(operator_paths or cleanup.attention)
    result = (
        'workspace_integrity_failed'
        if failed
        else 'workspace_integrity_attention_required'
        if attention
        else 'success'
    )
    waiting_on_you = name_a_few(operator_paths)
    if failed:
        next_step = (
            'restore missing canonical root paths or fix cleanup errors, then rerun workspace integrity guard'
        )
    elif operator_paths:
        next_step = f'review unclassified or conflicted paths: {waiting_on_you}; preserve source edits'
    elif attention:
        next_step = 'review the scan attention entries recorded in the artifact'
    else:
        next_step = 'none'
    marker = 'WORKSPACE_INTEGRITY_FAIL' if failed else 'WORKSPACE_INTEGRITY_ATTENTION' if attention else 'WORKSPACE_INTEGRITY_OK'
    try:
        write_artifacts(
            marker=marker,
            result=result,
            roots=roots,
            blockers=blockers,
            cleanup=cleanup,
            drift=drift,
            next_step=next_step,
            drift_errors=list(drift_errors),
        )
    except OSError:
        print('Workspace check failed: its local diagnostic report could not be saved. Source edits and task work were preserved.')
        return 1

    if failed:
        problems: list[str] = []
        if blockers:
            affected_roots = ', '.join(sorted({item.split(':', 1)[0] for item in blockers}))
            problems.append(f'required paths are missing in {affected_roots}')
        if cleanup.errors:
            problems.append('automatic cache cleanup could not complete')
        if drift_errors:
            problems.append('source-state checking or generated-file cleanup failed')
        print(f'Workspace check failed: {"; ".join(problems)}. Source edits and task work were preserved; details were saved locally.')
        return 1
    if operator_paths:
        print(f'Workspace check preserved unclassified or conflicted files for review: {waiting_on_you}. Nothing was reverted.')
    if cleanup.attention:
        location_word = 'location' if len(cleanup.attention) == 1 else 'locations'
        print(f'Workspace check could not inspect {len(cleanup.attention)} {location_word}; those paths were left untouched.')
    if cleanup.removed or reconciled_paths:
        removed = []
        if cleanup.removed:
            folder_word = 'folder' if len(cleanup.removed) == 1 else 'folders'
            removed.append(f'{len(cleanup.removed)} disposable cache {folder_word}')
        if reconciled_paths:
            file_word = 'file' if len(reconciled_paths) == 1 else 'files'
            removed.append(f'{len(reconciled_paths)} generated {file_word}')
        print(f'Workspace cleanup removed {" and ".join(removed)}. Source edits, saved memory and task work were preserved.')
    elif not attention:
        print('OpenClaw workspace cleanup passed. No disposable residue needed removal; source edits, saved memory and task work were preserved.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
