#!/usr/bin/env python3
"""Prune unreferenced OpenClaw runtime release directories.

The helper is intentionally dry-run by default. Apply mode requires either
``--apply`` or ``OPENCLAW_RUNTIME_RELEASE_PRUNE_APPLY=1``. It never restarts the
Gateway and only considers direct children named ``openclaw-*`` under the
configured releases root.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable

try:
    from .operator_contract import load_operator_contract
except ImportError:  # direct script execution
    from operator_contract import load_operator_contract

OPERATOR = load_operator_contract()


try:
    from .openclaw_runtime_retention_metadata import (
        PROMOTION_OPERATION_LOCK_KIND,
        PROMOTION_TERMINAL_RECEIPT_KIND,
        PROMOTION_TERMINAL_STATES,
        SAFE_TO_PRUNE_DISPOSITION,
        classify_operation_lock as classify_shared_operation_lock,
        dependency_requires_retention,
        iter_json_objects,
    )
except ImportError:  # direct script execution
    from openclaw_runtime_retention_metadata import (
        PROMOTION_OPERATION_LOCK_KIND,
        PROMOTION_TERMINAL_RECEIPT_KIND,
        PROMOTION_TERMINAL_STATES,
        SAFE_TO_PRUNE_DISPOSITION,
        classify_operation_lock as classify_shared_operation_lock,
        dependency_requires_retention,
        iter_json_objects,
    )

def launchagent_label_markers() -> tuple[str, ...]:
    values = OPERATOR.require_list("runtime.launchagent_label_markers")
    if not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("runtime.launchagent_label_markers must be nonempty strings")
    return tuple(values)


APPLY_ENV = 'OPENCLAW_RUNTIME_RELEASE_PRUNE_APPLY'
ANNOUNCE_NOOP_ENV = 'OPENCLAW_RUNTIME_RELEASE_RETENTION_ANNOUNCE_NOOP'
PROCESS_ARGV_SCAN_TIMEOUT_SECONDS = 20
# Do not use recursive lsof +D against the release tree: complete
# self-contained releases make that path tree-size dependent. Use one bounded
# global lsof field scan, then filter exact lexical/physical release-root paths.
OPEN_FILE_SCAN_METHOD = 'global_lsof_field_scan_exact_release_root_filter'
OPEN_FILE_SCAN_TIMEOUT_SECONDS = 60
SIZE_PROBE_TIMEOUT_SECONDS = 5
SYMLINK_SCAN_TIMEOUT_SECONDS = 180
MEASURE_RELEASE_SIZE = os.environ.get(
    'OPENCLAW_RUNTIME_RELEASE_RETENTION_MEASURE_SIZE',
    '0',
) == '1'
LAST_PROCESS_SCAN_REPORT: dict[str, Any] = {}
PROMOTION_OPERATION_DEPENDENCIES = {
    'previousReleasePath': 'previousReleaseDisposition',
    'rollbackReleasePath': 'rollbackReleaseDisposition',
    'releasePath': 'releaseDisposition',
    'stableReleasePath': 'stableReleaseDisposition',
    'finalReleasePath': 'finalReleaseDisposition',
}
DEFAULT_KEEP_LATEST = int(os.environ.get('OPENCLAW_RUNTIME_RELEASE_KEEP_LATEST', '2'))
DEFAULT_MIN_AGE_DAYS = int(os.environ.get('OPENCLAW_RUNTIME_RELEASE_MIN_AGE_DAYS', '3'))
PHYSICAL_DIRECTORY_ENTRY = 'physical_directory'
SYMLINK_ENTRY = 'symlink'
SYMLINK_PROTECTION_REASON = 'release-symlink-fail-closed'
RMTREE_AVOIDS_SYMLINK_ATTACKS = bool(getattr(shutil.rmtree, 'avoids_symlink_attacks', False))


def _positive_int_env(name: str, default: int) -> int:
    """Read a non-negative integer setting; 0 means "no limit"."""

    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


# Every deletion is independently revalidated immediately before rmtree, so a
# larger per-run budget repeats the same verified mutation rather than
# weakening it. A budget of 1 could never drain a real backlog: it left the
# nightly job reporting "ok" while more candidates accumulated than it removed.
# 0 means drain the whole verified candidate list in one run.
MAX_MUTATION_ATTEMPTS_PER_RUN = _positive_int_env(
    'OPENCLAW_RUNTIME_RELEASE_MAX_MUTATIONS_PER_RUN',
    0,
)
# Wall-clock bound so a large backlog stops cleanly and still writes a terminal
# report, instead of being killed by the cron child timeout with no report at
# all. 0 disables the bound.
MUTATION_DEADLINE_SECONDS = _positive_float_env(
    'OPENCLAW_RUNTIME_RELEASE_MUTATION_DEADLINE_SECONDS',
    600.0,
)
DEFERRED_BUDGET_STATE = 'deferred_run_budget'
LAST_PROMOTION_OPERATION_CLASSIFICATIONS: list[dict[str, Any]] = []
LAST_TERMINAL_ARCHIVE_NOTES: list[dict[str, str]] = []
ORPHAN_MIN_AGE_DAYS = 2


def mutation_deadline(seconds: float | None = None) -> float | None:
    """Return a monotonic deadline for this run's mutations, or None."""

    budget = MUTATION_DEADLINE_SECONDS if seconds is None else seconds
    if budget <= 0:
        return None
    return time.monotonic() + budget


def timeout_capped_by_deadline(
    maximum_seconds: float,
    *,
    deadline: float | None,
) -> float:
    """Return a subprocess timeout that cannot outlive the absolute run deadline."""

    if deadline is None:
        return maximum_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('runtime retention run deadline exhausted before scan')
    return min(maximum_seconds, remaining)


def budget_exhausted(*, attempts: int, max_attempts: int, deadline: float | None) -> bool:
    if max_attempts > 0 and attempts >= max_attempts:
        return True
    return deadline is not None and time.monotonic() >= deadline


@dataclass
class ReleaseRecord:
    path: Path
    realpath: Path
    size_bytes: int | None
    device: int | None = None
    inode: int | None = None
    directory_mtime_ns: int | None = None
    directory_ctime_ns: int | None = None
    entry_type: str = PHYSICAL_DIRECTORY_ENTRY
    link_path: Path | None = None
    raw_target: str | None = None
    target_exists: bool | None = None
    protected_reasons: list[str] = field(default_factory=list)
    action_state: str = 'pending'
    removal_started_at_utc: str | None = None
    removal_finished_at_utc: str | None = None
    after_exists: bool | None = None
    removed: bool = False
    remove_error: str | None = None

    def __post_init__(self) -> None:
        if self.entry_type == SYMLINK_ENTRY:
            if self.link_path != self.path:
                raise ValueError(f'symlink release record must identify its lexical link path: {self.path}')
            if not isinstance(self.raw_target, str) or not self.raw_target:
                raise ValueError(f'symlink release record must include its raw target: {self.path}')
            if not isinstance(self.target_exists, bool):
                raise ValueError(f'symlink release record must include target existence: {self.path}')
            if SYMLINK_PROTECTION_REASON not in self.protected_reasons:
                self.protected_reasons.append(SYMLINK_PROTECTION_REASON)
        elif self.entry_type == PHYSICAL_DIRECTORY_ENTRY:
            if self.link_path is not None or self.raw_target is not None or self.target_exists is not None:
                raise ValueError(f'physical release directory cannot include symlink metadata: {self.path}')
            if self.device is None or self.inode is None:
                raise ValueError(f'physical release directory must include device and inode identity: {self.path}')
            if self.directory_mtime_ns is None or self.directory_ctime_ns is None:
                raise ValueError(f'physical release directory must include directory timestamps: {self.path}')
        else:
            raise ValueError(f'unexpected release record entry type: {self.entry_type!r}')

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def protected(self) -> bool:
        return bool(self.protected_reasons)

    @property
    def prunable_candidate(self) -> bool:
        return self.entry_type == PHYSICAL_DIRECTORY_ENTRY and not self.protected


@dataclass(frozen=True)
class Reference:
    source: str
    path: Path


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace('+00:00', 'Z')


def realpath(path: Path) -> Path:
    return Path(os.path.realpath(path))


def bytes_to_gib(value: int) -> float:
    return value / (1024 ** 3)


def format_gib(value: int | None) -> str:
    return 'unmeasured' if value is None else f'{bytes_to_gib(value):.2f}GiB'


def total_size_bytes(records: list[ReleaseRecord]) -> int | None:
    total = 0
    for record in records:
        if record.size_bytes is None:
            return None
        total += record.size_bytes
    return total


def sample_free_space(path: Path) -> dict[str, Any]:
    # Constant-time filesystem counters, not a release-tree size walk. This is
    # reporting-only: unavailable counters must not change deletion authority.
    try:
        value = os.statvfs(path)
        available = int(value.f_bavail * value.f_frsize)
        if available < 0:
            raise ValueError('negative filesystem available space')
    except (OSError, ValueError) as exc:
        return {'status': 'failed', 'free_bytes': None, 'error': f'{type(exc).__name__}: {exc}'}
    return {'status': 'measured', 'free_bytes': available, 'error': None}


def observed_free_space_delta(filesystem_space: dict[str, Any] | None) -> int | None:
    if filesystem_space is None:
        return None
    before = filesystem_space['before']
    after = filesystem_space['after']
    if before['status'] != 'measured' or after['status'] != 'measured':
        return None
    # Keep the sign: concurrent filesystem users can outweigh this run's
    # removals. This is an observed filesystem delta, not attributed savings.
    return after['free_bytes'] - before['free_bytes']


def path_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def safe_release_realpath(path: Path, releases_root: Path) -> Path:
    root_real = realpath(releases_root)
    candidate_real = realpath(path)
    if candidate_real.parent != root_real:
        raise ValueError(f'unexpected release path outside direct releases root: {path} -> {candidate_real}')
    if not candidate_real.name.startswith('openclaw-'):
        raise ValueError(f'unexpected release directory name: {path}')
    return candidate_real


def du_bytes(path: Path, *, deadline: float | None = None) -> int | None:
    # Size is reporting-only. Do not let a slow deep release tree make the
    # retention classifier tree-size dependent; deletion safety is enforced by
    # identity/protection rescans, not by byte counts.
    try:
        timeout_seconds = timeout_capped_by_deadline(
            SIZE_PROBE_TIMEOUT_SECONDS,
            deadline=deadline,
        )
        proc = subprocess.run(
            ['/usr/bin/du', '-sk', str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        lines = proc.stdout.strip().splitlines()
        if proc.returncode == 0 and len(lines) == 1:
            blocks = int(lines[0].split()[0])
            if blocks >= 0:
                return blocks * 1024
    except Exception:
        return None
    return None


def list_release_records(
    releases_root: Path,
    *,
    deadline: float | None = None,
) -> list[ReleaseRecord]:
    try:
        root_info = releases_root.stat()
    except FileNotFoundError as exc:
        if os.path.lexists(releases_root):
            raise ValueError(f'releases root is a dangling symlink: {releases_root}') from exc
        return []
    except OSError as exc:
        raise ValueError(f'cannot stat releases root {releases_root}: {type(exc).__name__}: {exc}') from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f'releases root must resolve to a directory: {releases_root}')

    try:
        children = sorted(releases_root.iterdir(), key=lambda candidate: candidate.name)
    except OSError as exc:
        raise ValueError(f'cannot inventory releases root {releases_root}: {type(exc).__name__}: {exc}') from exc

    records: list[ReleaseRecord] = []
    for child in children:
        if not child.name.startswith('openclaw-'):
            continue
        try:
            child_info = child.lstat()
        except OSError as exc:
            raise ValueError(f'cannot lstat release entry {child}: {type(exc).__name__}: {exc}') from exc
        if stat.S_ISDIR(child_info.st_mode):
            child_real = safe_release_realpath(child, releases_root)
            records.append(
                ReleaseRecord(
                    path=child,
                    realpath=child_real,
                    size_bytes=(
                        du_bytes(child, deadline=deadline)
                        if MEASURE_RELEASE_SIZE
                        else None
                    ),
                    device=child_info.st_dev,
                    inode=child_info.st_ino,
                    directory_mtime_ns=child_info.st_mtime_ns,
                    directory_ctime_ns=child_info.st_ctime_ns,
                    entry_type=PHYSICAL_DIRECTORY_ENTRY,
                )
            )
            continue
        if stat.S_ISLNK(child_info.st_mode):
            try:
                raw_target = os.readlink(child)
            except OSError as exc:
                raise ValueError(f'cannot read release symlink {child}: {type(exc).__name__}: {exc}') from exc
            records.append(
                ReleaseRecord(
                    path=child,
                    realpath=realpath(child),
                    size_bytes=child_info.st_size,
                    entry_type=SYMLINK_ENTRY,
                    link_path=child,
                    raw_target=raw_target,
                    target_exists=child.exists(),
                )
            )
            continue
        raise ValueError(
            f'unexpected release entry type under {releases_root}: '
            f'{child} mode={oct(stat.S_IFMT(child_info.st_mode))}'
        )
    return records


def collect_current_symlink_ref(current_symlink: Path) -> list[Reference]:
    if not os.path.lexists(current_symlink):
        raise ValueError(f'authoritative current runtime symlink is absent: {current_symlink}')
    if not current_symlink.is_symlink():
        raise ValueError(f'authoritative current runtime pointer is not a symlink: {current_symlink}')
    current_real = realpath(current_symlink)
    root_real = realpath(OPERATOR.require_path("paths.runtime_releases_root"))
    if current_real.parent != root_real or not current_real.name.startswith('openclaw-'):
        raise ValueError(
            f'authoritative current runtime symlink escapes the direct release root: '
            f'{current_symlink} -> {current_real}'
        )
    if not current_real.is_dir():
        raise ValueError(f'authoritative current runtime symlink is dangling or not a directory: {current_symlink}')
    return [Reference('current-runtime-symlink', current_real)]


def iter_plist_program_paths(plist_paths: Iterable[Path]) -> Iterable[Reference]:
    for plist_path in plist_paths:
        with plist_path.open('rb') as handle:
            data = plistlib.load(handle)
        label = str(data.get('Label') or '')
        args = data.get('ProgramArguments') or []
        program = data.get('Program')
        if program:
            args = [program, *args]
        haystack = ' '.join(str(item) for item in [label, *args])
        if not any(marker in haystack for marker in launchagent_label_markers()):
            continue
        for item in args:
            token = str(item)
            if token.startswith('/'):
                yield Reference(f'launchagent:{plist_path}', realpath(Path(token)))


def collect_launchagent_refs(home: Path | None = None) -> list[Reference]:
    home = home if home is not None else OPERATOR.require_path("paths.host_home")
    roots = [home / 'Library' / 'LaunchAgents', Path('/Library/LaunchAgents')]
    refs: list[Reference] = []
    for root in roots:
        if not root.exists():
            continue
        if not root.is_dir():
            raise ValueError(f'LaunchAgent root is not a directory: {root}')
        for plist_path in sorted(root.glob('*.plist')):
            strict = 'openclaw' in plist_path.name.lower()
            try:
                parsed = list(iter_plist_program_paths([plist_path]))
            except Exception as exc:
                if strict:
                    raise ValueError(
                        f'cannot inspect authoritative OpenClaw LaunchAgent {plist_path}: '
                        f'{type(exc).__name__}: {exc}'
                    ) from exc
                continue
            refs.extend(parsed)
    return refs


def collect_symlink_refs(
    roots: Iterable[Path],
    *,
    required_roots: Iterable[Path] = (),
    deadline: float | None = None,
) -> list[Reference]:
    refs: list[Reference] = []
    required = {Path(os.path.abspath(path)) for path in required_roots}
    for root in roots:
        root_absolute = Path(os.path.abspath(root))
        if not os.path.lexists(root):
            if root_absolute in required:
                raise ValueError(f'authoritative symlink reference root is absent: {root}')
            continue
        if not root.is_dir():
            raise ValueError(f'symlink reference root is not a directory: {root}')
        try:
            timeout_seconds = timeout_capped_by_deadline(
                SYMLINK_SCAN_TIMEOUT_SECONDS,
                deadline=deadline,
            )
            completed = subprocess.run(
                ['/usr/bin/find', str(root), '-type', 'l', '-print0'],
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except Exception as exc:
            raise ValueError(
                f'cannot traverse authoritative symlink reference root {root}: '
                f'{type(exc).__name__}: {exc}'
            ) from exc
        if completed.returncode != 0:
            stderr = os.fsdecode(completed.stderr).strip()
            raise ValueError(
                f'cannot traverse authoritative symlink reference root {root}: '
                f'find exit {completed.returncode}: {stderr}'
            )
        if deadline is not None and time.monotonic() >= deadline:
            raise ValueError(
                f'cannot traverse authoritative symlink reference root {root}: '
                'runtime retention run deadline exhausted after find'
            )
        for raw in completed.stdout.split(b'\0'):
            if not raw:
                continue
            if deadline is not None and time.monotonic() >= deadline:
                raise ValueError(
                    f'cannot traverse authoritative symlink reference root {root}: '
                    'runtime retention run deadline exhausted while resolving symlinks'
                )
            path = Path(os.fsdecode(raw))
            try:
                refs.append(Reference(f'symlink:{path}', realpath(path)))
            except OSError as exc:
                raise ValueError(
                    f'cannot inspect authoritative symlink reference {path}: '
                    f'{type(exc).__name__}: {exc}'
                ) from exc
    return refs


def normalize_lsof_name(value: str) -> Path | None:
    """Return a filesystem path from an lsof name field, or None for non-files."""

    text = value.strip()
    if not text:
        return None
    for suffix in (' (deleted)', ' (deleted inode)', ' [deleted]'):
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip()
            break
    if not text.startswith('/'):
        return None
    return Path(text)


def path_matches_release_root(path: Path, *, root_lexical: Path, root_real: Path) -> bool:
    if not path.is_absolute():
        return False
    path_lexical = Path(os.path.abspath(path))
    if path_lexical == root_lexical or path_under(path_lexical, root_lexical):
        return True
    try:
        path_physical = realpath(path)
    except OSError:
        path_physical = Path(os.path.realpath(path))
    return path_physical == root_real or path_under(path_physical, root_real)


def reference_names_a_real_path(ref_path: Path) -> bool:
    """Probe existence without letting an unnameable reference abort the scan.

    A process-argv reference is a whole command line, not a path. When that
    line is longer than the filesystem's name limit, the probe itself raises
    ENAMETOOLONG. One such process anywhere on the machine used to abort the
    entire retention run: classification raised, every candidate was recorded
    as remove_failed, and nothing was ever pruned. A string the OS cannot name
    is by definition not an existing path, so answering "no" is both safe and
    correct, and string containment below still protects the release.

    Only unnameable strings are answered that way. Any other probe failure --
    a permission error, an I/O error on the volume holding a real reference --
    means the reference might name a live release we simply could not read,
    and the run must fail closed on it exactly as it did before, rather than
    quietly treat an unreadable dependency as absent and delete what it
    protects.
    """

    try:
        return ref_path.exists() or ref_path.is_symlink()
    except ValueError:
        # An embedded NUL or similar: not a name any filesystem can hold.
        return False
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            return False
        raise


def collect_process_refs(*, deadline: float | None = None) -> list[Reference]:
    global LAST_PROCESS_SCAN_REPORT

    started = time.monotonic()
    refs: list[Reference] = []
    root_lexical = Path(os.path.abspath(OPERATOR.require_path("paths.runtime_releases_root")))
    root_real = realpath(OPERATOR.require_path("paths.runtime_releases_root"))
    lsof_stdout_lines = 0
    lsof_matched_names = 0
    argv_matched_lines = 0
    try:
        argv_timeout_seconds = timeout_capped_by_deadline(
            PROCESS_ARGV_SCAN_TIMEOUT_SECONDS,
            deadline=deadline,
        )
        proc = subprocess.run(
            ['/bin/ps', '-axo', 'pid=,command='],
            check=False,
            capture_output=True,
            text=True,
            timeout=argv_timeout_seconds,
        )
    except Exception as exc:
        raise ValueError(
            f'cannot inspect authoritative process argv references: '
            f'{type(exc).__name__}: {exc}'
        ) from exc
    if proc.returncode != 0:
        raise ValueError(
            f'authoritative process argv scan failed with exit {proc.returncode}: '
            f'{proc.stderr.strip()[:240]}'
        )
    for line in proc.stdout.splitlines():
        if (
            str(root_lexical) in line
            or str(root_real) in line
            or 'openclaw-runtime/releases/openclaw-' in line
        ):
            refs.append(Reference(f'process-argv:{line.strip()[:180]}', Path(line)))
            argv_matched_lines += 1
    lsof_timeout_seconds = OPEN_FILE_SCAN_TIMEOUT_SECONDS
    try:
        lsof_timeout_seconds = timeout_capped_by_deadline(
            OPEN_FILE_SCAN_TIMEOUT_SECONDS,
            deadline=deadline,
        )
        proc = subprocess.run(
            ['/usr/sbin/lsof', '-n', '-F', 'pcn'],
            check=False,
            capture_output=True,
            text=True,
            timeout=lsof_timeout_seconds,
        )
    except Exception as exc:
        LAST_PROCESS_SCAN_REPORT = {
            'backend': 'lsof',
            'method': OPEN_FILE_SCAN_METHOD,
            'releases_root': str(OPERATOR.require_path("paths.runtime_releases_root")),
            'releases_root_realpath': str(root_real),
            'duration_seconds': round(time.monotonic() - started, 3),
            'timeout_seconds': lsof_timeout_seconds,
            'status': 'error',
            'error': f'{type(exc).__name__}: {exc}',
        }
        raise ValueError(
            f'cannot inspect authoritative open-file references via {OPEN_FILE_SCAN_METHOD}: '
            f'{type(exc).__name__}: {exc}'
        ) from exc
    if proc.returncode not in {0, 1} or proc.stderr.strip():
        LAST_PROCESS_SCAN_REPORT = {
            'backend': 'lsof',
            'method': OPEN_FILE_SCAN_METHOD,
            'releases_root': str(OPERATOR.require_path("paths.runtime_releases_root")),
            'releases_root_realpath': str(root_real),
            'duration_seconds': round(time.monotonic() - started, 3),
            'timeout_seconds': lsof_timeout_seconds,
            'status': 'error',
            'exit': proc.returncode,
            'stderr': proc.stderr.strip()[:240],
        }
        raise ValueError(
            f'authoritative open-file scan failed with exit {proc.returncode}: '
            f'{proc.stderr.strip()[:240]}'
        )
    pid = '?'
    command = '?'
    for line in proc.stdout.splitlines():
        if not line:
            continue
        lsof_stdout_lines += 1
        field, value = line[0], line[1:]
        if field == 'p':
            pid = value or '?'
        elif field == 'c':
            command = value or '?'
        elif field == 'n':
            path = normalize_lsof_name(value)
            if path is not None and path_matches_release_root(
                path,
                root_lexical=root_lexical,
                root_real=root_real,
            ):
                refs.append(Reference(f'lsof:{command}:{pid}', path))
                lsof_matched_names += 1
        elif field != 'f':
            LAST_PROCESS_SCAN_REPORT = {
                'backend': 'lsof',
                'method': OPEN_FILE_SCAN_METHOD,
                'releases_root': str(OPERATOR.require_path("paths.runtime_releases_root")),
                'releases_root_realpath': str(root_real),
                'duration_seconds': round(time.monotonic() - started, 3),
                'timeout_seconds': lsof_timeout_seconds,
                'status': 'error',
                'malformed_field': line[:160],
            }
            raise ValueError(
                f'authoritative open-file scan returned malformed field data: '
                f'{line[:160]}'
            )
    LAST_PROCESS_SCAN_REPORT = {
        'backend': 'lsof',
        'method': OPEN_FILE_SCAN_METHOD,
        'releases_root': str(OPERATOR.require_path("paths.runtime_releases_root")),
        'releases_root_realpath': str(root_real),
        'duration_seconds': round(time.monotonic() - started, 3),
        'timeout_seconds': lsof_timeout_seconds,
        'status': 'ok',
        'argv_matched_lines': argv_matched_lines,
        'lsof_stdout_field_count': lsof_stdout_lines,
        'lsof_matched_name_count': lsof_matched_names,
        'reference_count': len(refs),
        'parsed_references': [
            {'source': ref.source, 'path': str(ref.path)}
            for ref in refs
            if ref.source.startswith('lsof:')
        ],
    }
    return refs


def load_json_object(path: Path) -> dict[str, Any]:
    open_flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = -1
    try:
        descriptor = os.open(path, open_flags)
    except OSError as exc:
        raise ValueError(
            f'promotion protection metadata must be a physical regular file '
            f'opened without following symlinks: {path} ({type(exc).__name__}: {exc})'
        ) from exc
    try:
        opened_info = os.fstat(descriptor)
        try:
            path_info = path.lstat()
        except OSError as exc:
            raise ValueError(
                f'cannot revalidate opened promotion protection metadata {path}: '
                f'{type(exc).__name__}: {exc}'
            ) from exc
        if (
            not stat.S_ISREG(opened_info.st_mode)
            or not stat.S_ISREG(path_info.st_mode)
            or (opened_info.st_dev, opened_info.st_ino) != (path_info.st_dev, path_info.st_ino)
        ):
            raise ValueError(
                f'promotion protection metadata must be a physical regular file '
                f'opened without following symlinks: {path}'
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_read_info = os.fstat(descriptor)
        if (
            (opened_info.st_dev, opened_info.st_ino) != (after_read_info.st_dev, after_read_info.st_ino)
            or opened_info.st_size != after_read_info.st_size
            or opened_info.st_mtime_ns != after_read_info.st_mtime_ns
            or opened_info.st_ctime_ns != after_read_info.st_ctime_ns
        ):
            raise ValueError(f'promotion protection metadata changed while being read: {path}')
        raw_payload = b''.join(chunks).decode('utf-8')
    except Exception as exc:
        if isinstance(exc, ValueError) and str(exc).startswith('promotion protection metadata'):
            raise
        if isinstance(exc, ValueError) and str(exc).startswith('cannot revalidate opened promotion protection metadata'):
            raise
        raise ValueError(f'cannot read promotion protection metadata {path}: {type(exc).__name__}: {exc}') from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        value = json.loads(raw_payload)
    except Exception as exc:
        raise ValueError(f'cannot read promotion protection metadata {path}: {type(exc).__name__}: {exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'promotion protection metadata must be a JSON object: {path}')
    return value


def normalize_release_dependency_path(value: Any, *, source: str, releases_root: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{source} must be a non-empty absolute release path')
    raw_path = Path(value).expanduser()
    if not raw_path.is_absolute():
        raise ValueError(f'{source} must be an absolute release path: {value!r}')
    dependency_lexical = Path(os.path.abspath(raw_path))
    root_real = realpath(releases_root)
    dependency_parent_real = realpath(dependency_lexical.parent)
    dependency_real = realpath(dependency_lexical)
    if (
        dependency_parent_real != root_real
        or dependency_real.parent != root_real
        or not dependency_real.name.startswith('openclaw-')
    ):
        raise ValueError(
            f'{source} must resolve to a direct openclaw-* child of {root_real}: '
            f'{dependency_lexical} -> {dependency_real}'
        )
    return dependency_real


def collect_activation_result_refs(
    result_path: Path | None = None,
    releases_root: Path | None = None,
) -> list[Reference]:
    """Protect the reduced activator's exact candidate and rollback targets.

    The activation lock is held shared for the whole retention run, so this
    canonical result cannot be replaced by a concurrent activation after it is
    validated. Any present-but-invalid result blocks retention rather than
    silently dropping either rollback reference.
    """

    path = result_path if result_path is not None else effective_activation_result_path()
    root = releases_root if releases_root is not None else OPERATOR.require_path("paths.runtime_releases_root")
    if not os.path.lexists(path):
        return []
    path_info = path.lstat()
    if not stat.S_ISREG(path_info.st_mode) or path_info.st_nlink != 1:
        raise ValueError(
            f'activation result must be one physical regular file: {path}'
        )
    record = load_json_object(path)
    try:
        from . import openclaw_runtime_activate as activation
    except ImportError:
        import openclaw_runtime_activate as activation
    if (
        record.get('outcome') != 'activated'
        or record.get('statesVisited') != ['preflight', 'apply', 'verify', 'terminal']
        or record.get('candidateAttemptCount') != 1
        or record.get('rollbackAttemptCount') != 0
        or record.get('error') is not None
    ):
        raise ValueError(f'activation result is not one terminal activated result: {path}')

    bindings: dict[str, Path] = {}
    for role in ('candidate', 'rollback'):
        value = record.get(role)
        if not isinstance(value, dict):
            raise ValueError(f'activation result {role} binding is absent: {path}')
        commit = value.get('commit')
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ValueError(f'activation result {role} commit is invalid: {path}')
        release = normalize_release_dependency_path(
            value.get('path'),
            source=f'{path}:{role}.path',
            releases_root=root,
        )
        try:
            release_info = release.lstat()
        except OSError as exc:
            raise ValueError(
                f'activation result {role} release cannot be inspected: {release}'
            ) from exc
        if (
            not stat.S_ISDIR(release_info.st_mode)
            or type(value.get('device')) is not int
            or type(value.get('inode')) is not int
            # This reader already resolves supported host aliases within the
            # releases root. Preserve that contract without rewriting history.
            or not activation.historical_identity_matches({**value, 'path': str(release)}, release, release_info)
        ):
            raise ValueError(
                f'activation result {role} release identity drifted: {release}'
            )
        bindings[role] = release
    if bindings['candidate'] == bindings['rollback']:
        raise ValueError(f'activation result candidate and rollback must differ: {path}')

    rollback = record['rollback']
    if rollback.get('source') not in {
        'healthy_loaded_process',
        'cold_recovery_binding',
    }:
        raise ValueError(f'activation result rollback source is invalid: {path}')
    verification = record.get('verification')
    loaded = verification.get('loaded') if isinstance(verification, dict) else None
    health = verification.get('health') if isinstance(verification, dict) else None
    candidate = record['candidate']
    loaded_release = loaded.get('release') if isinstance(loaded, dict) else None
    if (
        not isinstance(loaded, dict)
        or not isinstance(loaded_release, str)
        or not Path(loaded_release).is_absolute()
        or realpath(Path(loaded_release)) != bindings['candidate']
        or loaded.get('releaseDevice') != candidate.get('device')
        or loaded.get('releaseInode') != candidate.get('inode')
        or loaded.get('argumentsObservedExact') is not True
    ):
        raise ValueError(f'activation result loaded candidate identity is invalid: {path}')
    if not isinstance(health, dict) or any(
        not isinstance(health.get(endpoint), dict)
        or health[endpoint].get('accepted') is not True
        or health[endpoint].get('statusCode') != 200
        for endpoint in ('healthz', 'readyz')
    ):
        raise ValueError(f'activation result health/readiness proof is invalid: {path}')
    if not isinstance(record.get('commandEvidence'), list) or not record['commandEvidence']:
        raise ValueError(f'activation result command evidence is absent: {path}')

    return [
        Reference('activation-result:candidate', bindings['candidate']),
        Reference('activation-result:rollback', bindings['rollback']),
    ]


def classify_operation_lock(
    lock_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility wrapper around the shared retention lifecycle policy."""

    return classify_shared_operation_lock(
        lock_path,
        payload,
        load_json=load_json_object,
    )


def collect_operation_lock_dependency_refs(
    lock_path: Path,
    *,
    releases_root: Path,
    payload: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
) -> list[Reference]:
    payload = payload if payload is not None else load_json_object(lock_path)
    classification = classification or classify_operation_lock(lock_path, payload)
    lifecycle = classification.get('classification')
    if lifecycle not in {
        'legacy_unversioned_operation_lock',
        'legacy_retired_driver_lock',
        'terminalized_stale_active_lock',
        'terminalized_operation_lock',
    }:
        raise ValueError(f'{lock_path}: unsupported retention lifecycle classification: {lifecycle!r}')

    refs: list[Reference] = []
    for entry in iter_json_objects(payload):
        for path_field, disposition_field in PROMOTION_OPERATION_DEPENDENCIES.items():
            value = entry.get(path_field)
            if value is None:
                continue
            dependency = normalize_release_dependency_path(
                value,
                source=f'{lock_path}:{path_field}',
                releases_root=releases_root,
            )
            disposition = entry.get(disposition_field)
            if not dependency_requires_retention(disposition):
                continue
            ref = Reference(
                f'promotion-operation-lock:{lock_path.parent.name}:{path_field}:{disposition}',
                dependency,
            )
            if ref not in refs:
                refs.append(ref)
    return refs


def collect_retention_manifest_dependency_refs(
    manifest_path: Path,
    *,
    releases_root: Path,
) -> list[Reference]:
    payload = load_json_object(manifest_path)
    root_real = realpath(releases_root)
    refs: list[Reference] = []
    for entry in iter_json_objects(payload):
        classification = entry.get('class', entry.get('classification'))
        if 'path' not in entry:
            continue
        value = entry['path']
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{manifest_path}: retain_long_term path must be a non-empty string')
        raw_path = Path(value).expanduser()
        if not raw_path.is_absolute():
            continue
        dependency_lexical = Path(os.path.abspath(raw_path))
        dependency_real = realpath(dependency_lexical)
        if (
            realpath(dependency_lexical.parent) != root_real
            or dependency_real.parent != root_real
            or not dependency_real.name.startswith('openclaw-')
        ):
            # Retention manifests also classify worktrees, evidence roots, and
            # hidden partial build paths that are never release candidates.
            continue
        if classification == SAFE_TO_PRUNE_DISPOSITION:
            continue
        dependency = normalize_release_dependency_path(
            value,
            source=f'{manifest_path}:retain_long_term.path',
            releases_root=releases_root,
        )
        classification_text = (
            classification
            if isinstance(classification, str) and classification
            else 'unresolved'
        )
        refs.append(
            Reference(
                f'promotion-retention-manifest:{manifest_path.parent.name}:{classification_text}',
                dependency,
            )
        )
    return refs


def collect_promotion_dependency_refs(
    promotions_root: Path | None = None,
    releases_root: Path | None = None,
) -> list[Reference]:
    global LAST_PROMOTION_OPERATION_CLASSIFICATIONS
    LAST_PROMOTION_OPERATION_CLASSIFICATIONS = []
    root = promotions_root if promotions_root is not None else OPERATOR.require_path("paths.runtime_promotions_root")
    release_root = releases_root if releases_root is not None else OPERATOR.require_path("paths.runtime_releases_root")
    if not os.path.lexists(root):
        return []
    try:
        root_info = root.stat()
    except OSError as exc:
        raise ValueError(
            f'cannot stat promotion dependency root {root}: '
            f'{type(exc).__name__}: {exc}'
        ) from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f'promotion dependency root must resolve to a directory: {root}')
    root_real = realpath(root)
    refs: list[Reference] = []
    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        raise ValueError(
            f'cannot inventory promotion dependency root {root}: '
            f'{type(exc).__name__}: {exc}'
        ) from exc
    for child in children:
        try:
            child_info = child.lstat()
        except OSError as exc:
            raise ValueError(
                f'cannot lstat promotion dependency artifact {child}: '
                f'{type(exc).__name__}: {exc}'
            ) from exc
        if stat.S_ISLNK(child_info.st_mode):
            raise ValueError(f'promotion dependency artifact must be a physical directory: {child}')
        if not stat.S_ISDIR(child_info.st_mode):
            continue
        child_real = realpath(child)
        if child_real.parent != root_real:
            raise ValueError(f'promotion protection directory escapes promotions root: {child} -> {child_real}')
        lock_path = child / 'operation.lock.json'
        if os.path.lexists(lock_path):
            lock_payload = load_json_object(lock_path)
            classification = classify_operation_lock(lock_path, lock_payload)
            LAST_PROMOTION_OPERATION_CLASSIFICATIONS.append(classification)
            refs.extend(
                collect_operation_lock_dependency_refs(
                    lock_path,
                    releases_root=release_root,
                    payload=lock_payload,
                    classification=classification,
                )
            )
        manifest_path = child / 'retention-manifest.json'
        if os.path.lexists(manifest_path):
            refs.extend(collect_retention_manifest_dependency_refs(manifest_path, releases_root=release_root))
    return refs


def collect_references(
    *,
    deadline: float | None = None,
) -> list[Reference]:
    refs: list[Reference] = []
    refs.extend(collect_current_symlink_ref(OPERATOR.require_path("paths.runtime_current_link")))
    refs.extend(collect_activation_result_refs())
    refs.extend(collect_launchagent_refs())
    refs.extend(
        collect_symlink_refs(
            [OPERATOR.require_path("paths.cli_root")],
            required_roots=[OPERATOR.require_path("paths.cli_root")],
            deadline=deadline,
        )
    )
    refs.extend(collect_process_refs(deadline=deadline))
    refs.extend(collect_promotion_dependency_refs())
    refs.extend(collect_unfinished_release_refs())
    return refs


def protect_records(records: list[ReleaseRecord], refs: Iterable[Reference]) -> None:
    for ref in refs:
        ref_path = ref.path
        # process argv references may not be valid paths; fall back to string containment.
        ref_text = str(ref_path)
        ref_lexical = Path(os.path.abspath(ref_path)) if ref_path.is_absolute() else None
        ref_real = realpath(ref_path) if reference_names_a_real_path(ref_path) else None
        for record in records:
            matched = False
            record_lexical = Path(os.path.abspath(record.path))
            if ref_lexical is not None and (
                ref_lexical == record_lexical or path_under(ref_lexical, record_lexical)
            ):
                matched = True
            elif ref_real is not None and (ref_real == record.realpath or path_under(ref_real, record.realpath)):
                matched = True
            elif str(record.realpath) in ref_text or str(record.path) in ref_text:
                matched = True
            if matched and ref.source not in record.protected_reasons:
                record.protected_reasons.append(ref.source)


def protect_retention_window(
    records: list[ReleaseRecord],
    *,
    now: datetime,
    keep_latest: int,
    min_age_days: int,
) -> None:
    physical = [
        record for record in records
        if record.entry_type == PHYSICAL_DIRECTORY_ENTRY
    ]
    newest = sorted(
        physical,
        key=lambda record: (
            record.directory_mtime_ns or 0,
            record.directory_ctime_ns or 0,
            record.name,
        ),
        reverse=True,
    )[:max(keep_latest, 0)]
    for record in newest:
        reason = f'latest-{max(keep_latest, 0)}'
        if reason not in record.protected_reasons:
            record.protected_reasons.append(reason)

    min_age_seconds = max(min_age_days, 0) * 24 * 60 * 60
    for record in physical:
        mtime = datetime.fromtimestamp(
            (record.directory_mtime_ns or 0) / 1_000_000_000,
            timezone.utc,
        )
        if (now - mtime).total_seconds() < min_age_seconds:
            reason = f'younger-than-{max(min_age_days, 0)}d'
            if reason not in record.protected_reasons:
                record.protected_reasons.append(reason)


def collect_unfinished_release_refs() -> list[Reference]:
    """A sealed candidate is not disposable merely because no process uses it.

    Reuse terminal promotion records and the activator's existing immutable
    retirement receipts. No new producer marker or retention database is used.
    """
    global LAST_TERMINAL_ARCHIVE_NOTES
    LAST_TERMINAL_ARCHIVE_NOTES = []
    terminal: set[Path] = set()
    for classification in LAST_PROMOTION_OPERATION_CLASSIFICATIONS:
        if classification.get('terminal_state') not in PROMOTION_TERMINAL_STATES:
            continue
        lock = Path(classification['operation_lock_path'])
        payload = load_json_object(lock)
        for entry in iter_json_objects(payload):
            for field in PROMOTION_OPERATION_DEPENDENCIES:
                if entry.get(field) is not None:
                    terminal.add(normalize_release_dependency_path(entry[field], source=str(lock), releases_root=OPERATOR.require_path("paths.runtime_releases_root")))
    archive = effective_activation_result_path().parent / 'archive'
    if os.path.lexists(archive):
        if archive.is_symlink() or not archive.is_dir():
            raise ValueError('activation retirement archive must be a physical directory')
        generations = list(archive.iterdir())
        if len(generations) > 4096:
            raise ValueError('activation retirement metadata count exceeds bound')
        try:
            from . import openclaw_runtime_activate as activation
        except ImportError:
            import openclaw_runtime_activate as activation
        for generation in generations:
            try:
                if generation.is_symlink() or not generation.is_dir():
                    raise ValueError('activation retirement generation is not physical')
                receipt_path = generation / 'retirement-receipt.json'
                if not os.path.lexists(receipt_path):
                    continue  # Interrupted preservation is not a terminal proof.
                if receipt_path.lstat().st_size > 256 * 1024:
                    raise ValueError('activation retirement receipt exceeds bound')
                receipt, _, _ = activation.read_json(receipt_path, 'retirement receipt')
                result_path = generation / 'activation-result.json'
                if (receipt.get('schemaVersion') != 1 or receipt.get('outcome') not in
                        {'activated', 'restored', 'restored_stopped', 'restored_after_late_verification'}
                        or receipt.get('result', {}).get('path') != str(result_path)):
                    raise ValueError('activation retirement receipt binding drift')
                if result_path.lstat().st_size > 256 * 1024:
                    raise ValueError('archived activation result exceeds bound')
                result, data, info = activation.read_json(result_path, 'archived activation result')
                binding = receipt['result']
                if (hashlib.sha256(data).hexdigest() != binding.get('sha256')
                        or not activation.historical_identity_matches(binding, result_path, info)):
                    raise ValueError('archived activation result identity/hash drift')
                identities = activation.retirement_release_identities(receipt)
                for role in ('candidate', 'rollback'):
                    value = result.get(role)
                    if not isinstance(value, dict) or not isinstance(value.get('path'), str):
                        continue
                    path = realpath(Path(value['path']))
                    if path.parent != realpath(OPERATOR.require_path("paths.runtime_releases_root")) or not path.name.startswith('openclaw-'):
                        continue
                    if os.path.lexists(path):
                        if activation.historical_release_identity_matches(value, identities.get(role), path):
                            terminal.add(path)
            except (OSError, ValueError, activation.ActivationError) as exc:
                # Ineligible historical evidence is not current lifecycle authority.
                # It grants no immediate disposal; the 48-hour orphan gate remains.
                LAST_TERMINAL_ARCHIVE_NOTES.append({"generation":generation.name, "reason":str(exc)})
    now = utc_now().timestamp()
    return [Reference('orphan-younger-than-2d', child)
            for child in OPERATOR.require_path("paths.runtime_releases_root").iterdir()
            if child.name.startswith('openclaw-') and not child.is_symlink()
            and child.is_dir() and realpath(child) not in terminal
            and now - max(child.stat().st_mtime, child.stat().st_ctime) < ORPHAN_MIN_AGE_DAYS * 86400]


def retention_lock_path() -> Path:
    return OPERATOR.require_path("paths.runtime_retention_lock")


def effective_activation_lock_path() -> Path:
    return OPERATOR.require_path("paths.activation_lock")


def effective_activation_result_path() -> Path:
    return OPERATOR.require_path("paths.activation_result")


def acquire_activation_read_lock() -> BinaryIO:
    """Fence retention against the activator without waiting or retrying."""

    path = effective_activation_lock_path()
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
            or opened.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(f'activation lock must be one physical regular file: {path}')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                f'activation is in progress; retention is blocked: {path}'
            ) from exc
        return os.fdopen(descriptor, 'r+b', closefd=True)
    except Exception:
        os.close(descriptor)
        raise


def acquire_shared_retention_lock() -> BinaryIO:
    """Acquire the retention-writer lock without waiting.

    Runtime promotion producers do not currently acquire this lock. The lock
    prevents overlapping retention writers; the mandatory full reference
    rescan and directory-identity check immediately before each deletion are
    the fail-closed boundary for producer activity. Current producer promotion
    directories remain non-candidates without an explicit whole-directory
    prune disposition.
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


def _restore_directory_write_access_fd(descriptor: int, display_path: Path) -> None:
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        raise ValueError(f'opened release entry is not a directory: {display_path}')
    if not opened.st_mode & stat.S_IWUSR:
        os.fchmod(descriptor, stat.S_IMODE(opened.st_mode) | stat.S_IWUSR)

    children: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                child_info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(child_info.st_mode):
                    continue
                if stat.S_ISDIR(child_info.st_mode):
                    children.append((entry.name, child_info))
    except OSError as exc:
        raise ValueError(
            f'cannot inventory verified release directory {display_path}: '
            f'{type(exc).__name__}: {exc}'
        ) from exc

    flags = (
        os.O_RDONLY
        | getattr(os, 'O_DIRECTORY', 0)
        | getattr(os, 'O_CLOEXEC', 0)
        | getattr(os, 'O_NOFOLLOW', 0)
    )
    for child_name, expected in children:
        child_path = display_path / child_name
        try:
            child_descriptor = os.open(
                child_name,
                flags,
                dir_fd=descriptor,
            )
        except OSError as exc:
            raise ValueError(
                f'cannot open verified child directory {child_path} without '
                f'following symlinks: {type(exc).__name__}: {exc}'
            ) from exc
        try:
            child_opened = os.fstat(child_descriptor)
            if (
                not stat.S_ISDIR(child_opened.st_mode)
                or (child_opened.st_dev, child_opened.st_ino)
                != (expected.st_dev, expected.st_ino)
            ):
                raise ValueError(
                    f'verified child directory identity changed before chmod: '
                    f'{child_path}'
                )
            _restore_directory_write_access_fd(child_descriptor, child_path)
        finally:
            os.close(child_descriptor)


def restore_directory_write_access(root: Path) -> None:
    """Give directories inside a verified-unprotected tree their write bit back.

    The promotion's release_sealed phase makes a release immutable: its
    directories land as dr-xr-xr-x. Unlinking a file needs write permission on
    the PARENT directory, so shutil.rmtree fails on every sealed release with
    "PermissionError: [Errno 13] Permission denied: '.npmignore'" — which is why
    release retention had never actually removed one. Observed 2026-08-13 on
    three releases in a single run.

    Only directory bits are touched, only under a path the caller has already
    revalidated as an unprotected candidate immediately beforehand. Every
    directory is opened relative to its already-open parent with O_NOFOLLOW,
    identity-checked, and changed with fchmod; no path-based chmod can follow a
    replacement symlink outside the candidate.
    """

    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    directory = getattr(os, 'O_DIRECTORY', 0)
    if not nofollow or not directory:
        raise ValueError(
            'platform lacks descriptor-relative no-follow directory opens'
        )
    flags = os.O_RDONLY | directory | getattr(os, 'O_CLOEXEC', 0) | nofollow
    try:
        descriptor = os.open(root, flags)
    except OSError as exc:
        raise ValueError(
            f'cannot open verified release directory {root} without following '
            f'symlinks: {type(exc).__name__}: {exc}'
        ) from exc
    try:
        opened = os.fstat(descriptor)
        current = root.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError(
                f'verified release directory identity changed before chmod: {root}'
            )
        _restore_directory_write_access_fd(descriptor, root)
    finally:
        os.close(descriptor)


def prune_records(
    records: list[ReleaseRecord],
    *,
    apply: bool,
    persist: Callable[[], None],
    rescan_references: Callable[[], list[Reference]] | None = None,
    max_attempts: int | None = None,
    deadline: float | None = None,
) -> None:
    if not apply:
        return
    if max_attempts is None:
        max_attempts = MAX_MUTATION_ATTEMPTS_PER_RUN
    attempts = 0
    untouched_any = False
    reference_scan_failed = False
    for record in records:
        if record.entry_type == SYMLINK_ENTRY:
            if SYMLINK_PROTECTION_REASON not in record.protected_reasons:
                record.protected_reasons.append(SYMLINK_PROTECTION_REASON)
            record.action_state = 'skipped_protected'
            record.after_exists = os.path.lexists(record.path)
            untouched_any = True
            continue
        if record.entry_type != PHYSICAL_DIRECTORY_ENTRY:
            raise ValueError(f'unexpected release record entry type before prune: {record.entry_type!r}')
        if record.protected:
            continue
        if reference_scan_failed:
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            record.remove_error = (
                'earlier authoritative reference scan failed; '
                'further release deletion blocked'
            )
            untouched_any = True
            continue
        if budget_exhausted(attempts=attempts, max_attempts=max_attempts, deadline=deadline):
            record.action_state = DEFERRED_BUDGET_STATE
            record.after_exists = os.path.lexists(record.path)
            # Deferral is not a disk mutation, so persist it once at the end.
            untouched_any = True
            continue
        attempts += 1
        if not RMTREE_AVOIDS_SYMLINK_ATTACKS:
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            record.remove_error = 'platform rmtree lacks symlink-attack resistance'
            persist()
            continue
        record.action_state = 'removal_in_progress'
        record.removal_started_at_utc = utc_text()
        persist()
        if rescan_references is not None:
            try:
                fresh_references = rescan_references()
            except Exception as exc:
                record.action_state = 'remove_failed'
                record.after_exists = os.path.lexists(record.path)
                record.remove_error = (
                    f'authoritative reference rescan failed before delete: '
                    f'{type(exc).__name__}: {exc}'
                )
                reference_scan_failed = True
                persist()
                continue
            protect_records([record], fresh_references)
            if record.protected:
                record.action_state = 'remove_failed'
                record.after_exists = os.path.lexists(record.path)
                record.remove_error = (
                    'candidate became protected during authoritative '
                    'reference rescan'
                )
                persist()
                continue
            if deadline is not None and time.monotonic() >= deadline:
                record.action_state = 'remove_failed'
                record.after_exists = os.path.lexists(record.path)
                record.remove_error = (
                    'runtime retention run deadline exhausted after '
                    'authoritative reference rescan'
                )
                reference_scan_failed = True
                persist()
                continue
        try:
            current_info = record.path.lstat()
        except FileNotFoundError:
            record.action_state = 'remove_failed'
            record.after_exists = False
            record.remove_error = 'candidate disappeared after live classification'
            persist()
            continue
        except OSError as exc:
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            record.remove_error = f'cannot lstat candidate after live classification: {type(exc).__name__}: {exc}'
            persist()
            continue
        try:
            if not stat.S_ISDIR(current_info.st_mode):
                raise ValueError(
                    f'candidate is no longer a physical release directory: '
                    f'{record.path} mode={oct(stat.S_IFMT(current_info.st_mode))}'
                )
            if (current_info.st_dev, current_info.st_ino) != (record.device, record.inode):
                raise ValueError(
                    f'candidate device/inode changed after live classification: '
                    f'{record.device}:{record.inode} -> '
                    f'{current_info.st_dev}:{current_info.st_ino}'
                )
            if (
                current_info.st_mtime_ns != record.directory_mtime_ns
                or current_info.st_ctime_ns != record.directory_ctime_ns
            ):
                raise ValueError(
                    'candidate directory metadata changed after live '
                    'classification'
                )
            current_realpath = safe_release_realpath(record.path, OPERATOR.require_path("paths.runtime_releases_root"))
            if current_realpath != record.realpath:
                raise ValueError(f'candidate realpath changed after live classification: {record.realpath} -> {current_realpath}')
        except Exception as exc:
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            record.remove_error = f'{type(exc).__name__}: {exc}'
            persist()
            continue
        try:
            restore_directory_write_access(record.path)
            shutil.rmtree(record.path)
            record.after_exists = os.path.lexists(record.path)
            if record.after_exists:
                raise ValueError('candidate path exists after rmtree returned')
            record.removed = True
            record.action_state = 'removed'
        except Exception as exc:  # pragma: no cover - defensive live safety path
            record.action_state = 'remove_failed'
            record.after_exists = os.path.lexists(record.path)
            record.remove_error = f'{type(exc).__name__}: {exc}'
        record.removal_finished_at_utc = utc_text()
        persist()
    if untouched_any:
        persist()


def relpath(path: Path) -> str:
    try:
        return path.relative_to(OPERATOR.require_path("paths.workspace")).as_posix()
    except ValueError:
        return str(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def select_report_path(artifact_root: Path | None = None) -> tuple[str, str, Path]:
    started_at = utc_now()
    operation_id = f'runtime-release-retention-{started_at.strftime("%Y%m%dT%H%M%S%fZ")}-{os.getpid()}'
    root = artifact_root or OPERATOR.require_path("paths.runtime_release_retention_artifacts")
    return operation_id, utc_text(started_at), root / f'{operation_id}.json'


def inventory_metadata(record: ReleaseRecord) -> dict[str, Any]:
    return {
        'entry_type': record.entry_type,
        'device': record.device,
        'inode': record.inode,
        'directory_mtime_ns': record.directory_mtime_ns,
        'directory_ctime_ns': record.directory_ctime_ns,
        'link_path': str(record.link_path) if record.link_path is not None else None,
        'raw_target': record.raw_target,
        'target_exists': record.target_exists,
    }


def before_snapshot(records: list[ReleaseRecord], *, observed_at_utc: str, reference_count: int) -> dict[str, Any]:
    return {
        'observed_at_utc': observed_at_utc,
        'reference_count': reference_count,
        'release_count': len(records),
        'physical_directory_count': sum(record.entry_type == PHYSICAL_DIRECTORY_ENTRY for record in records),
        'symlink_count': sum(record.entry_type == SYMLINK_ENTRY for record in records),
        'releases': [
            {
                'name': record.name,
                'path': str(record.path),
                'realpath': str(record.realpath),
                **inventory_metadata(record),
                'exists': os.path.lexists(record.path),
                'size_bytes': record.size_bytes,
                'protected': record.protected,
                'protection_reasons': list(record.protected_reasons),
            }
            for record in records
        ],
    }


def release_receipt(record: ReleaseRecord) -> dict[str, Any]:
    return {
        'name': record.name,
        'path': str(record.path),
        'realpath': str(record.realpath),
        **inventory_metadata(record),
        'before_exists': True,
        'before_size_bytes': record.size_bytes,
        'protected': record.protected,
        'protection_reasons': list(record.protected_reasons),
        'state': record.action_state,
        'removal_started_at_utc': record.removal_started_at_utc,
        'removal_finished_at_utc': record.removal_finished_at_utc,
        'after_exists': record.after_exists,
        'error': record.remove_error,
    }


def report_payload(
    records: list[ReleaseRecord],
    *,
    apply: bool,
    keep_latest: int,
    min_age_days: int,
    operation_id: str,
    started_at_utc: str,
    before: dict[str, Any],
    state: str,
    terminal: bool,
    run_errors: list[str] | None = None,
    filesystem_space: dict[str, Any] | None = None,
) -> dict[str, Any]:
    run_errors = run_errors or []
    protected = [record for record in records if record.protected]
    candidates = [record for record in records if record.prunable_candidate]
    removed = [record for record in records if record.removed]
    skipped = [record for record in records if record.protected or (not apply and record.prunable_candidate)]
    errors = [record for record in records if record.remove_error]
    return {
        # v2 readers validate removal identities/counts independently of these
        # reporting-only sizes; null means unavailable, not a measured zero.
        'schema': 'openclaw.runtime_release_retention.v2',
        'operation_id': operation_id,
        'created_at_utc': started_at_utc,
        'updated_at_utc': utc_text(),
        'mode': 'apply' if apply else 'dry-run',
        'deletion_authorized': apply,
        'state': state,
        'terminal': terminal,
        'releases_root': str(OPERATOR.require_path("paths.runtime_releases_root")),
        'releases_root_realpath': str(realpath(OPERATOR.require_path("paths.runtime_releases_root"))),
        'current_symlink': str(OPERATOR.require_path("paths.runtime_current_link")),
        'policy': {
            'keep_latest': keep_latest,
            'min_age_days': min_age_days,
            'orphan_min_age_days': ORPHAN_MIN_AGE_DAYS,
            'inactive_operation_dependencies': 'ignored unless an explicit retention disposition is present',
            'active_operation_lock': 'hard-block unless a strictly matching terminal receipt proves the lock stale',
        },
        'process_scan': dict(LAST_PROCESS_SCAN_REPORT),
        'promotion_operation_classifications': list(LAST_PROMOTION_OPERATION_CLASSIFICATIONS),
        'historical_evidence_ignored': list(LAST_TERMINAL_ARCHIVE_NOTES),
        'before': before,
        'filesystem_space': filesystem_space,
        'summary': {
            'total_releases': len(records),
            'physical_directory_count': sum(record.entry_type == PHYSICAL_DIRECTORY_ENTRY for record in records),
            'symlink_count': sum(record.entry_type == SYMLINK_ENTRY for record in records),
            'protected_count': len(protected),
            'candidate_count': len(candidates),
            'removed_count': len(removed),
            'skipped_count': len(skipped),
            'error_count': len(errors),
            'reclaimable_bytes': total_size_bytes(candidates),
            'removed_bytes': total_size_bytes(removed),
            'before_free_bytes': filesystem_space['before']['free_bytes'] if filesystem_space else None,
            'after_free_bytes': filesystem_space['after']['free_bytes'] if filesystem_space else None,
            'observed_free_space_delta_bytes': observed_free_space_delta(filesystem_space),
            'in_progress_count': sum(record.action_state == 'removal_in_progress' for record in records),
            'deferred_count': sum(record.action_state == 'deferred_run_budget' for record in records),
            # Compatibility fields remain zero while downstream readers retire
            # the legacy plugin-runtime-deps retention surface.
            'plugin_cache_total': 0,
            'plugin_cache_protected_count': 0,
            'plugin_cache_candidate_count': 0,
            'plugin_cache_removed_count': 0,
            'plugin_cache_deferred_count': 0,
            'plugin_cache_error_count': 0,
            'run_error_count': len(run_errors),
        },
        'receipts': [release_receipt(record) for record in records],
        'protected': [
            {
                'name': record.name,
                'path': str(record.path),
                'realpath': str(record.realpath),
                **inventory_metadata(record),
                'size_bytes': record.size_bytes,
                'size_gib': round(bytes_to_gib(record.size_bytes), 3) if record.size_bytes is not None else None,
                'state': record.action_state,
                'reasons': record.protected_reasons,
            }
            for record in protected
        ],
        'candidates': [
            {
                'name': record.name,
                'path': str(record.path),
                'realpath': str(record.realpath),
                **inventory_metadata(record),
                'size_bytes': record.size_bytes,
                'size_gib': round(bytes_to_gib(record.size_bytes), 3) if record.size_bytes is not None else None,
                'action': record.action_state,
                'error': record.remove_error,
            }
            for record in candidates
        ],
        'removed': [record.name for record in removed],
        'skipped': [record.name for record in skipped],
        'errors': [
            {'object': 'runtime_retention_run', 'error': error}
            for error in run_errors
        ] + [
            {'object': 'runtime_release', 'name': record.name, 'error': record.remove_error}
            for record in errors
        ],
        'gateway_restart': 'not_performed',
    }


def write_report(
    records: list[ReleaseRecord],
    *,
    apply: bool,
    keep_latest: int,
    min_age_days: int,
    operation_id: str,
    started_at_utc: str,
    before: dict[str, Any],
    state: str,
    terminal: bool,
    report_path: Path,
    run_errors: list[str] | None = None,
    filesystem_space: dict[str, Any] | None = None,
) -> Path:
    payload = report_payload(
        records,
        apply=apply,
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        operation_id=operation_id,
        started_at_utc=started_at_utc,
        before=before,
        state=state,
        terminal=terminal,
        run_errors=run_errors,
        filesystem_space=filesystem_space,
    )
    atomic_write_json(report_path, payload)
    atomic_write_json(report_path.parent / 'latest.json', payload)
    return report_path


def status_line(
    records: list[ReleaseRecord],
    *,
    apply: bool,
    report_path: Path,
    run_errors: list[str] | None = None,
    filesystem_space: dict[str, Any] | None = None,
) -> tuple[str, str]:
    run_errors = run_errors or []
    protected = [record for record in records if record.protected]
    candidates = [record for record in records if record.prunable_candidate]
    removed = [record for record in records if record.removed]
    errors = [record for record in records if record.remove_error]
    deferred = [record for record in records if record.action_state == 'deferred_run_budget']
    marker = 'RUNTIME_RELEASE_RETENTION_OK'
    result = 'no_unprotected_releases'
    if run_errors or errors:
        marker = 'RUNTIME_RELEASE_RETENTION_BLOCKED'
        result = 'remove_errors'
    elif not apply and candidates:
        marker = 'RUNTIME_RELEASE_RETENTION_DRY_RUN'
        result = 'dry_run_candidates'
    elif apply and removed:
        result = 'removed_unprotected_releases'
    report_rel = relpath(report_path)
    detail = (
        f'STATUS | result: {result} | mode: {"apply" if apply else "dry-run"} '
        f'| deletion_authorized: {str(apply).lower()} '
        f'| total: {len(records)} | protected: {len(protected)} | candidates: {len(candidates)} '
        f'| removed: {len(removed)} '
        f'| deferred: {len(deferred)} '
        '| plugin_caches: total=0,protected=0,candidates=0,removed=0 '
        '| plugin_cache_deferred: 0 '
        f'| reclaimable: {format_gib(total_size_bytes(candidates))} '
        f'| observed_free_space_delta: {format_gib(observed_free_space_delta(filesystem_space))} '
        f'| report: {report_rel} | gateway_restart: not_performed'
    )
    if run_errors or errors:
        detail += ' | blockers: ' + '; '.join(
            [f'run:{error}' for error in run_errors]
            + [f'{record.name}:{record.remove_error}' for record in errors]
        )
    return marker, detail


def _run_locked(
    *,
    apply: bool,
    keep_latest: int,
    min_age_days: int,
) -> tuple[int, str, str, Path]:
    global LAST_PROCESS_SCAN_REPORT, LAST_PROMOTION_OPERATION_CLASSIFICATIONS

    # Start the absolute wall-clock budget before any inventory or authority
    # scan. Every subprocess below receives this same deadline so the cron
    # supervisor cannot time out first and leave the run without a terminal
    # result.
    run_deadline = mutation_deadline() if apply else None

    LAST_PROCESS_SCAN_REPORT = {}
    LAST_PROMOTION_OPERATION_CLASSIFICATIONS = []
    operation_id, started_at_utc, report_path = select_report_path(OPERATOR.require_path("paths.runtime_release_retention_artifacts"))
    run_errors: list[str] = []
    records: list[ReleaseRecord] = []
    references: list[Reference] = []
    filesystem_space = {
        'path': str(realpath(OPERATOR.require_path("paths.runtime_releases_root"))),
        'before': sample_free_space(OPERATOR.require_path("paths.runtime_releases_root")),
        'after': {'status': 'pending', 'free_bytes': None, 'error': None},
    }
    before = before_snapshot(
        records,
        observed_at_utc=started_at_utc,
        reference_count=0,
    )
    write_report(
        records,
        apply=apply,
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        operation_id=operation_id,
        started_at_utc=started_at_utc,
        before=before,
        state='classification_in_progress',
        terminal=False,
        report_path=report_path,
        run_errors=run_errors,
        filesystem_space=filesystem_space,
    )

    try:
        observed_now = utc_now()
        records = list_release_records(OPERATOR.require_path("paths.runtime_releases_root"), deadline=run_deadline)
        references = collect_references(deadline=run_deadline)
        protect_records(records, references)
        protect_retention_window(
            records,
            now=observed_now,
            keep_latest=keep_latest,
            min_age_days=min_age_days,
        )
    except Exception as exc:
        run_errors.append(
            'initial authoritative classification failed: '
            f'{type(exc).__name__}: {exc}'
        )
        before = before_snapshot(
            records,
            observed_at_utc=started_at_utc,
            reference_count=len(references),
        )
        filesystem_space['after'] = sample_free_space(OPERATOR.require_path("paths.runtime_releases_root"))
        write_report(
            records,
            apply=apply,
            keep_latest=keep_latest,
            min_age_days=min_age_days,
            operation_id=operation_id,
            started_at_utc=started_at_utc,
            before=before,
            state='classification_blocked',
            terminal=True,
            report_path=report_path,
            run_errors=run_errors,
            filesystem_space=filesystem_space,
        )
        marker, detail = status_line(
            records,
            apply=apply,
            report_path=report_path,
            run_errors=run_errors,
            filesystem_space=filesystem_space,
        )
        return 1, marker, detail, report_path

    for record in records:
        record.action_state = 'skipped_protected' if record.protected else ('pending' if apply else 'would_remove')
        record.after_exists = os.path.lexists(record.path) if record.protected or not apply else None
    before = before_snapshot(records, observed_at_utc=started_at_utc, reference_count=len(references))

    def persist_in_progress() -> None:
        write_report(
            records,
            apply=apply,
            keep_latest=keep_latest,
            min_age_days=min_age_days,
            operation_id=operation_id,
            started_at_utc=started_at_utc,
            before=before,
            state='apply_in_progress',
            terminal=False,
            report_path=report_path,
            filesystem_space=filesystem_space,
        )

    if apply:
        persist_in_progress()
        try:
            fresh_references = collect_references(deadline=run_deadline)
        except Exception as exc:
            for record in records:
                if not record.protected:
                    record.action_state = 'remove_failed'
                    record.after_exists = os.path.lexists(record.path)
                    record.remove_error = (
                        'pre-delete authoritative reference scan failed; '
                        f'release deletion not attempted: {type(exc).__name__}: {exc}'
                    )
        else:
            protect_records(records, fresh_references)
            prune_records(
                records,
                apply=True,
                persist=persist_in_progress,
                rescan_references=lambda: collect_references(
                    deadline=run_deadline,
                ),
                deadline=run_deadline,
            )
        terminal_state = (
            'apply_blocked'
            if any(record.remove_error for record in records)
            else 'apply_complete'
        )
    else:
        terminal_state = 'dry_run_complete'
    filesystem_space['after'] = sample_free_space(OPERATOR.require_path("paths.runtime_releases_root"))
    write_report(
        records,
        apply=apply,
        keep_latest=keep_latest,
        min_age_days=min_age_days,
        operation_id=operation_id,
        started_at_utc=started_at_utc,
        before=before,
        state=terminal_state,
        terminal=True,
        report_path=report_path,
        filesystem_space=filesystem_space,
    )
    marker, detail = status_line(
        records,
        apply=apply,
        report_path=report_path,
        filesystem_space=filesystem_space,
    )
    rc = 1 if marker == 'RUNTIME_RELEASE_RETENTION_BLOCKED' else 0
    return rc, marker, detail, report_path


def run(
    *,
    apply: bool,
    keep_latest: int = DEFAULT_KEEP_LATEST,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
) -> tuple[int, str, str, Path]:
    activation_lock = acquire_activation_read_lock()
    try:
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
    finally:
        try:
            fcntl.flock(activation_lock.fileno(), fcntl.LOCK_UN)
        finally:
            activation_lock.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Dry-run or apply OpenClaw runtime release retention pruning.')
    parser.add_argument('--apply', action='store_true', help='Remove unprotected runtime releases. Without this flag, only writes a report.')
    parser.add_argument('--keep-latest', type=int, default=DEFAULT_KEEP_LATEST, help='Always retain this many newest physical release directories.')
    parser.add_argument('--min-age-days', type=int, default=DEFAULT_MIN_AGE_DAYS, help='Retain physical release directories younger than this many days.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    apply = bool(args.apply or os.environ.get(APPLY_ENV) == '1')
    rc, marker, detail, _ = run(
        apply=apply,
        keep_latest=args.keep_latest,
        min_age_days=args.min_age_days,
    )
    announce_noop = os.environ.get(ANNOUNCE_NOOP_ENV, '').strip().lower() in {'1', 'true', 'yes', 'on'}
    if rc == 0 and apply and 'result: no_unprotected_releases' in detail and not announce_noop:
        print('NO_REPLY')
        return 0
    print(marker)
    print(detail)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
