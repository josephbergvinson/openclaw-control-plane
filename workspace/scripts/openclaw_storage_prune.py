#!/usr/bin/env python3
"""Prune only classified, inactive regenerable build/cache/scratch directories.

No arbitrary paths are accepted by the CLI. Discovery is bounded to explicit
host roots; source, databases, credentials, personal simulator devices, build products,
test receipts, releases, backups and application profile caches are excluded.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import hashlib
import fnmatch
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from . import external_volume_guard, node_compile_cache
except ImportError:
    import external_volume_guard
    import node_compile_cache

OWC = Path((str(OPERATOR.require_path('paths.data_root'))))
WORKSPACE = Path(os.environ.get('WORKSPACE', str(OPERATOR.require_path('paths.workspace'))))
ARTIFACT_ROOT = WORKSPACE / 'artifacts/storage_prune'
USER_HOME = Path((str(OPERATOR.require_path('paths.host_home'))))
XCODE_OUTPUTS = ('Build/Intermediates.noindex', 'ModuleCache.noindex', 'Index.noindex',
                 'SDKStatCaches.noindex', 'CompilationCache.noindex')
QUARANTINE_PREFIX = '.openclaw-prune-'
NPM_LOG_KIND = 'npm-debug-log'
NPM_LOG_NAME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2}_\d{3}Z-debug-\d+\.log')


def is_recovery_path(path: Path) -> bool:
    return any(part.startswith(QUARANTINE_PREFIX) for part in path.parts)


@dataclass(frozen=True)
class Candidate:
    path: str
    kind: str
    activity_root: str
    min_age_days: int
    device: int
    inode: int
    newest_mtime: float
    allocated_bytes: int
    leaf_fingerprint: tuple | None = None


def npm_log_stat(path: Path, cutoff: float) -> os.stat_result:
    """Only old npm debug logs in the exact physical user log directory."""
    parent = USER_HOME / '.npm/_logs'
    if path.parent != parent or NPM_LOG_NAME.fullmatch(path.name) is None or path.resolve() != path:
        raise ValueError('unclassified npm log path')
    stamp = datetime.strptime(path.name.split('-debug-')[0], '%Y-%m-%dT%H_%M_%S_%fZ').replace(tzinfo=timezone.utc)
    value, directory = path.lstat(), parent.lstat()
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or value.st_uid != os.getuid() or directory.st_uid != os.getuid()
            or not stat.S_ISDIR(directory.st_mode) or value.st_dev != directory.st_dev
            or getattr(value, 'st_flags', 0)):
        raise ValueError('npm log owner, type, links, flags or filesystem is invalid')
    if value.st_mtime > cutoff or stamp.timestamp() > cutoff:
        raise ValueError('recent npm log')
    return value


def tree_facts(path: Path, cutoff: float, deadline: float | None = None) -> tuple[int, float]:
    """Inspect without following links; reject recent, foreign or mounted contents."""
    root = path.lstat()
    if not stat.S_ISDIR(root.st_mode):
        raise ValueError('not a real directory')
    newest, allocated = 0.0, 0
    todo = [path]
    while todo:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('execution budget exhausted')
        current = todo.pop()
        s = current.lstat()
        if s.st_uid != os.getuid() or s.st_dev != root.st_dev:
            raise ValueError('foreign owner or filesystem')
        if s.st_mtime > cutoff:
            raise ValueError('recent contents')
        if not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode)):
            raise ValueError('special file')
        newest = max(newest, s.st_mtime)
        allocated += s.st_blocks * 512
        if stat.S_ISDIR(s.st_mode):
            todo.extend(Path(e.path) for e in os.scandir(current))
    return allocated, newest


def xcode_candidates(parent: Path, *, temporary: bool) -> list[tuple[Path, str, Path, int]]:
    result = []
    if not parent.is_dir() or parent.is_symlink():
        return result
    for root in parent.iterdir():
        if temporary and not root.name.startswith(('codex-', 'openclaw-', 'personal-data-')):
            continue
        if root.is_symlink() or not root.is_dir():
            continue
        try:
            # Xcode-generated metadata distinguishes derived data from source.
            info = plistlib.loads((root / 'info.plist').read_bytes())
            if not isinstance(info, dict) or not isinstance(info.get('WorkspacePath'), str):
                continue
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        for suffix in XCODE_OUTPUTS:
            p = root / suffix
            if p.exists() and not p.is_symlink():
                result.append((p, 'xcode-generated', root, 2 if temporary else 7))
    return [entry for entry in result if not is_recovery_path(entry[0])]


def bounded_candidates() -> list[tuple[Path, str, Path, int]]:
    result = xcode_candidates(Path('/private/tmp'), temporary=True)
    result += xcode_candidates(USER_HOME / 'Library/Developer/Xcode/DerivedData', temporary=False)
    if OWC.is_mount():
        result += xcode_candidates(OPERATOR.require_path('paths.xcode_derived_data'), temporary=False)
    temp_roots = [Path((str(OPERATOR.require_path('paths.temp_root'))))]
    if OWC.is_mount():
        temp_roots.append(OPERATOR.require_path('paths.pytest_temp_root'))
    for root in temp_roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for p in root.iterdir():
            if any(fnmatch.fnmatchcase(p.name, pattern) for pattern in
                   ('openclaw-test-home-*', 'openclaw-autoreview-trufflehog.*')):
                result.append((p, 'test-scratch', p, 7))
        pytest = root / ('pytest-of-' + OPERATOR.require_string('identifiers.host_user'))
        if pytest.is_dir() and not pytest.is_symlink():
            for p in pytest.iterdir():
                if re.fullmatch(r'pytest-\d+', p.name):
                    result.append((p, 'pytest-scratch', p, 7))
    # One audited, version-isolated Node format at this exact physical root.
    node_root = node_compile_cache.ROOT
    if OWC.is_mount() and node_root.is_dir() and node_root.resolve() == node_root:
        for p in node_root.iterdir():
            try:
                node_compile_cache.version_for(p)
            except ValueError:
                continue
            result.append((p, node_compile_cache.KIND, p, node_compile_cache.DAYS))
    # Download caches only; package stores, databases and browser/app caches stay.
    for root in (USER_HOME / 'Library/Caches/pnpm/dlx',):
        if root.is_dir() and not root.is_symlink():
            for p in root.iterdir():
                if p.is_dir() and not p.is_symlink():
                    result.append((p, 'package-download-cache', p, 14))
    logs = USER_HOME / '.npm/_logs'
    if logs.is_dir() and logs.resolve() == logs:
        result.extend((p, NPM_LOG_KIND, p, 14) for p in logs.iterdir()
                      if NPM_LOG_NAME.fullmatch(p.name))
    return [entry for entry in result if not is_recovery_path(entry[0])]


def discover(now: float | None = None, deadline: float | None = None) -> tuple[list[Candidate], list[dict]]:
    now = time.time() if now is None else now
    candidates, skipped = [], []
    node_versions, node_error = None, None
    for p, kind, activity, days in bounded_candidates():
        if deadline is not None and time.monotonic() >= deadline:
            skipped.append({'path': str(p), 'reason': 'execution budget exhausted', 'error': True})
            break
        try:
            if is_recovery_path(p):
                raise ValueError('preserved recovery quarantine')
            # Every ancestor must still resolve to the path we classified.
            if p.resolve() != p or activity.resolve() != activity:
                raise ValueError('symlink ancestor')
            if kind == NPM_LOG_KIND:
                s = npm_log_stat(p, now - days * 86400)
                allocated, newest = s.st_blocks * 512, s.st_mtime
            elif kind == node_compile_cache.KIND:
                version = node_compile_cache.version_for(p)
                s = p.lstat()
                if not stat.S_ISDIR(s.st_mode) or s.st_uid != os.getuid():
                    raise ValueError('foreign or non-directory Node cache namespace')
                if s.st_mtime > now - days * 86400:
                    raise ValueError('recent Node cache namespace')
                # Reject active/recent namespaces before their potentially large
                # file inventory. One complete process view covers discovery.
                if node_versions is None and node_error is None:
                    try:
                        node_versions = node_compile_cache.active_versions(deadline)
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        node_error = 'Node process inspection unavailable: ' + str(error)
                if node_error:
                    raise OSError(node_error)
                if version in node_versions:
                    raise ValueError('active Node version ' + version)
                with anchored_parent(p) as anchor:
                    fd = os.open(p.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=anchor.fd)
                    try:
                        if (file_identity(os.fstat(fd)) != file_identity(s)
                                or s.st_dev != os.fstat(anchor.fd).st_dev
                                or os.fstat(anchor.fd).st_uid != os.getuid()):
                            raise ValueError('Node cache root identity, owner or filesystem changed')
                        allocated, newest, _ = node_compile_cache.tree_facts(fd, now - days * 86400, deadline)
                        anchor.verify()
                    finally:
                        os.close(fd)
            else:
                allocated, newest = tree_facts(p, now - days * 86400, deadline)
                s = p.lstat()
            candidates.append(Candidate(str(p), kind, str(activity), days,
                                        s.st_dev, s.st_ino, newest, allocated,
                                        entry_fingerprint(s) if kind == NPM_LOG_KIND else None))
        except (OSError, ValueError) as error:
            skipped.append({'path': str(p), 'reason': str(error), 'error': isinstance(error, OSError)})
    return candidates, skipped


def process_arguments() -> str:
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,args='], capture_output=True,
                            text=True, timeout=15, check=True)
    # Do not persist command lines: they can include secrets. Own process only
    # names this script; all other processes remain in the activity guard.
    rows = []
    observed = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            raise ValueError('process inspection returned a malformed snapshot')
        observed += 1
        if parts[0] != str(os.getpid()):
            rows.append(line)
    if not observed:
        raise ValueError('process inspection returned an empty snapshot')
    return '\n'.join(rows)


def process_references(path: Path, commands: str) -> bool:
    aliases = [str(path)]
    if str(path).startswith('/private/tmp/'):
        aliases.append(str(path).replace('/private/tmp/', '/tmp/', 1))
    return any(alias in commands for alias in aliases)


def activity_reason(path: Path, commands: str, *, ignore_current_process: bool = False,
                    directory: bool = True) -> str | None:
    if process_references(path, commands):
        return 'active process references directory'
    try:
        # Ordinary lsof can silently omit another user's handles, including
        # root-owned build/service processes. Privilege failure must not pass.
        args = ['/usr/bin/sudo', '-n', '/usr/sbin/lsof', '-nP']
        if ignore_current_process:
            args += ['-a', '-p', '^' + str(os.getpid())]
        result = subprocess.run(args + (['+D', str(path)] if directory else [str(path)]),
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return 'open-handle inspection unavailable'
    if result.returncode == 0 or result.stdout.strip():
        return 'open handles'
    if result.returncode != 1 or result.stderr.strip():
        return 'open-handle inspection failed'
    return None


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('execution budget exhausted')


def file_identity(s: os.stat_result) -> tuple[int, int]:
    return s.st_dev, s.st_ino


def rename_exclusive(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """Public OS no-replace rename; never use a check-then-rename fallback.

    macOS's deployed Python 3.9 lacks a no-replace rename wrapper. Darwin's
    public renameatx_np (available since 10.12) supplies RENAME_EXCL=0x4,
    declared by the installed SDK's sys/stdio.h. Linux tests use renameat2.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    name, flag = ('renameatx_np', 0x4) if sys.platform == 'darwin' else ('renameat2', 1)
    call = getattr(libc, name, None)
    if call is None:
        raise OSError(errno.ENOSYS, 'exclusive descriptor rename unavailable')
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    if call(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), flag):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


@dataclass
class ParentAnchor:
    fds: list[int]
    names: list[str]

    @property
    def fd(self) -> int:
        return self.fds[-1]

    def verify(self) -> None:
        for index, name in enumerate(self.names):
            entry = os.stat(name, dir_fd=self.fds[index], follow_symlinks=False)
            if file_identity(entry) != file_identity(os.fstat(self.fds[index + 1])):
                raise ValueError('ancestor identity changed')


@contextmanager
def anchored_parent(path: Path):
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('capture requires an absolute classified path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fds = [os.open('/', flags)]
    names = list(path.parent.parts[1:])
    try:
        for name in names:
            fds.append(os.open(name, flags, dir_fd=fds[-1]))
        anchor = ParentAnchor(fds, names)
        anchor.verify()
        yield anchor
    finally:
        for fd in reversed(fds):
            os.close(fd)


@dataclass
class CapturedEntry:
    path: Path
    fd: int
    container_fd: int
    anchor: ParentAnchor
    original_path: Path | None = None
    deleted_entries: int = 0
    removed: bool = False


def verify_capture_container(anchor: ParentAnchor, name: str, fd: int) -> None:
    """A retained directory fd never authorizes a replacement public name."""
    named, opened = os.stat(name, dir_fd=anchor.fd, follow_symlinks=False), os.fstat(fd)
    if (file_identity(named) != file_identity(opened) or not stat.S_ISDIR(named.st_mode)
            or named.st_uid != os.getuid() or stat.S_IMODE(named.st_mode) != 0o700):
        raise ValueError('capture container identity or ownership changed; preserve recovery state')


@contextmanager
def capture_entry(path: Path, expected: tuple[int, int], *, directory: bool,
                  recovery_original: Path | None = None, symlink: bool = False):
    """Atomically capture one entry, then validate what the rename captured.

    A race can move a replacement into quarantine, but cannot authorize its
    deletion: the opened captured inode must match the durable candidate. On
    every error, restore without replacing any newly created original path.
    If restoration cannot be exclusive, keep the private recovery manifest.
    """
    recovery_original = path if recovery_original is None else recovery_original
    if directory and symlink:
        raise ValueError('capture type must be directory, file, or symlink')
    if not recovery_original.is_absolute() or '..' in recovery_original.parts:
        raise ValueError('capture requires an absolute recovery destination')
    with anchored_parent(path) as anchor:
        before = os.stat(path.name, dir_fd=anchor.fd, follow_symlinks=False)
        if file_identity(before) != expected:
            raise ValueError('directory identity changed')
        container = QUARANTINE_PREFIX + uuid.uuid4().hex
        os.mkdir(container, 0o700, dir_fd=anchor.fd)
        qfd = os.open(container, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=anchor.fd)
        captured = False
        entry = None
        try:
            verify_capture_container(anchor, container, qfd)
            manifest_fd = os.open('manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=qfd)
            with os.fdopen(manifest_fd, 'w') as manifest:
                json.dump({'schema': 'openclaw.storage_capture.v1', 'original': str(recovery_original),
                           'captured_from': str(path),
                           'expected_device': expected[0], 'expected_inode': expected[1],
                           'payload': 'payload', 'state': 'capture_intent'}, manifest)
                manifest.flush()
                os.fsync(manifest.fileno())
            os.fsync(qfd)
            os.fsync(anchor.fd)
            anchor.verify()
            rename_exclusive(anchor.fd, path.name, qfd, 'payload')
            captured = True
            os.fsync(qfd)
            os.fsync(anchor.fd)
            if symlink:
                # Darwin's public O_SYMLINK (sys/fcntl.h) opens the link itself.
                # Python 3.9 omits the constant; O_NOFOLLOW conflicts with it.
                # The parent is already opened through no-follow descriptors.
                if sys.platform == 'darwin':
                    flags = os.O_RDONLY | os.O_NONBLOCK | 0x00200000
                elif hasattr(os, 'O_PATH'):
                    flags = os.O_PATH | os.O_NOFOLLOW
                else:
                    raise OSError(errno.ENOTSUP, 'symlink descriptor capture unavailable')
            else:
                flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else os.O_NONBLOCK)
            fd = os.open('payload', flags, dir_fd=qfd)
            entry = CapturedEntry(path.parent / container / 'payload', fd, qfd, anchor,
                                  original_path=recovery_original)
            current = os.fstat(fd)
            expected_type = stat.S_ISLNK if symlink else stat.S_ISDIR if directory else stat.S_ISREG
            if file_identity(current) != expected or not expected_type(current.st_mode):
                raise ValueError('captured object identity or type changed')
            anchor.verify()
            yield entry
        finally:
            if entry is not None:
                os.close(entry.fd)
            preserved = None
            try:
                # On replacement, leave both the foreign name and original
                # manifest/payload intact. The old qfd alone is not permission
                # to restore or remove entries through a substituted name.
                verify_capture_container(anchor, container, qfd)
                if captured and (entry is None or not entry.removed):
                    try:
                        rename_exclusive(qfd, 'payload', anchor.fd, path.name)
                        os.fsync(qfd)
                        os.fsync(anchor.fd)
                    except OSError as error:
                        preserved = error
                if preserved is None:
                    try:
                        os.unlink('manifest.json', dir_fd=qfd)
                    except FileNotFoundError:
                        pass
                    verify_capture_container(anchor, container, qfd)
                    os.rmdir(container, dir_fd=anchor.fd)
                    os.fsync(anchor.fd)
            finally:
                os.close(qfd)
            if preserved is not None:
                raise OSError('captured object preserved at ' + str(path.parent / container / 'payload')
                              + '; exclusive restoration unavailable') from preserved


def descriptor_tree_facts(fd: int, cutoff: float, deadline: float | None,
                          device: int | None = None) -> tuple[int, float]:
    check_deadline(deadline)
    root = os.fstat(fd)
    device = root.st_dev if device is None else device
    validate_entry(root, device, cutoff)
    allocated, newest = root.st_blocks * 512, root.st_mtime
    with os.scandir(fd) as entries:
        for entry in entries:
            check_deadline(deadline)
            value = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            validate_entry(value, device, cutoff)
            if stat.S_ISDIR(value.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if file_identity(os.fstat(child)) != file_identity(value):
                        raise ValueError('nested directory identity changed')
                    size, modified = descriptor_tree_facts(child, cutoff, deadline, device)
                finally:
                    os.close(child)
                allocated += size
                newest = max(newest, modified)
            else:
                allocated += value.st_blocks * 512
                newest = max(newest, value.st_mtime)
    return allocated, newest


def validate_entry(value: os.stat_result, device: int, cutoff: float) -> None:
    if value.st_dev != device or value.st_uid != os.getuid():
        raise ValueError('foreign owner or filesystem')
    if value.st_mtime > cutoff:
        raise ValueError('recent contents')
    if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)):
        raise ValueError('special file')


def entry_fingerprint(value: os.stat_result, *, after_rename: bool = False) -> tuple:
    """An exclusive rename changes ctime; every other entry property stays bound."""
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
            value.st_nlink, value.st_size, value.st_mtime_ns,
            None if after_rename else value.st_ctime_ns,
            getattr(value, 'st_birthtime', None), getattr(value, 'st_flags', 0))


def _unlink_captured_leaf(captured: CapturedEntry, expected_stat: os.stat_result,
                          deadline: float | None, deletion_owner: CapturedEntry,
                          *, link_target: str | None = None) -> bool:
    """Final shared check and effect; callers finish every callback beforehand."""
    check_deadline(deadline)
    captured.anchor.verify()
    verify_capture_container(captured.anchor, captured.path.parent.name, captured.container_fd)
    if (entry_fingerprint(os.fstat(captured.fd)) != entry_fingerprint(expected_stat)
            or file_identity(os.stat('payload', dir_fd=captured.container_fd, follow_symlinks=False))
            != file_identity(expected_stat)
            or (link_target is not None and os.readlink('payload', dir_fd=captured.container_fd) != link_target)):
        raise ValueError('captured leaf changed before removal')
    # No callback or public child-name reopen between this validation and
    # unlink of the exclusively captured name. Record the effect before fsync
    # so a durability error cannot erase the confirmed deletion count.
    os.unlink('payload', dir_fd=captured.container_fd)
    captured.removed = True
    deletion_owner.deleted_entries += 1
    os.fsync(captured.container_fd)
    return True


def remove_captured_leaf(captured: CapturedEntry, expected_stat: os.stat_result,
                         cutoff: float, device: int, deadline: float | None = None,
                         *, validator: Callable[[CapturedEntry], None] | None = None) -> bool:
    """Remove one captured, singly linked regular file after caller validation.

    The caller binds its reviewed pre-capture stat to the captured inode while
    allowing only rename's ctime change, then supplies the post-capture stat as
    expected_stat. This full fingerprint stays fixed across the validator,
    which can inspect content and native metadata through the captured fd.
    Refusal or mutation raises and leaves capture_entry to restore exclusively
    or retain its recovery manifest. No sibling names are traversed.
    """
    check_deadline(deadline)
    if captured.original_path is None or captured.removed:
        raise ValueError('leaf capture has no recoverable live payload')
    current = os.fstat(captured.fd)
    validate_entry(current, device, cutoff)
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise ValueError('captured leaf must be a singly linked regular file')
    if entry_fingerprint(current) != entry_fingerprint(expected_stat):
        raise ValueError('captured leaf changed before validation')
    if validator is not None:
        validator(captured)
    return _unlink_captured_leaf(captured, expected_stat, deadline, captured)


def remove_captured_tree(captured: CapturedEntry, cutoff: float, deadline: float | None,
                         *, entry_validator: Callable[[str, os.stat_result, bool], None] | None = None,
                         captured_leaf_validator: Callable[[CapturedEntry, str], None] | None = None,
                         progress: Callable[[], None] | None = None) -> None:
    """Exclusively capture every entry before removing its private bound name.

    A concurrent publisher can replace a public child name after stat. Capturing
    that name and checking the opened inode makes such a replacement ineligible
    for deletion. Each nested capture records the original logical destination;
    failed exclusive rollback leaves a private recovery manifest. New names are
    never added to the captured directory's initial child list.
    """
    device = os.fstat(captured.fd).st_dev
    if captured.original_path is None:
        raise ValueError('tree capture has no original recovery destination')

    def validate(relative: str, value: os.stat_result, after_capture: bool) -> None:
        validate_entry(value, device, cutoff)
        if entry_validator is not None:
            entry_validator(relative, value, after_capture)

    def finish_directory(current: CapturedEntry) -> None:
        check_deadline(deadline)
        current.anchor.verify()
        value = os.fstat(current.fd)
        if (value.st_dev != device or value.st_uid != os.getuid()
                or not stat.S_ISDIR(value.st_mode)
                or file_identity(os.stat('payload', dir_fd=current.container_fd, follow_symlinks=False))
                != file_identity(value)):
            raise ValueError('captured directory identity changed')
        # Own child captures changed this directory's size and timestamps. A
        # late foreign child makes rmdir fail; it is never traversed or removed.
        os.rmdir('payload', dir_fd=current.container_fd)
        current.removed = True
        if current is not captured:
            captured.deleted_entries += 1
        os.fsync(current.container_fd)

    def walk(current: CapturedEntry, relative: str) -> None:
        validate(relative, os.fstat(current.fd), True)
        with os.scandir(current.fd) as entries:
            names = [entry.name for entry in entries]
        for name in names:
            if is_recovery_path(Path(name)):
                raise ValueError('preserved recovery quarantine inside candidate')
            if progress is not None:
                progress()
            check_deadline(deadline)
            current.anchor.verify()
            child_relative = name if relative == '.' else relative + '/' + name
            value = os.stat(name, dir_fd=current.fd, follow_symlinks=False)
            validate(child_relative, value, False)
            link_target = os.readlink(name, dir_fd=current.fd) if stat.S_ISLNK(value.st_mode) else None
            with capture_entry(current.path / name, file_identity(value),
                               directory=stat.S_ISDIR(value.st_mode),
                               symlink=stat.S_ISLNK(value.st_mode),
                               recovery_original=captured.original_path / child_relative) as child:
                opened = os.fstat(child.fd)
                if entry_fingerprint(opened, after_rename=True) != entry_fingerprint(value, after_rename=True):
                    raise ValueError('captured child changed before removal')
                validate(child_relative, opened, True)
                if stat.S_ISDIR(opened.st_mode):
                    walk(child, child_relative)
                    finish_directory(child)
                else:
                    if captured_leaf_validator is not None:
                        captured_leaf_validator(child, child_relative)
                    _unlink_captured_leaf(child, opened, deadline, captured, link_target=link_target)
    walk(captured, '.')
    finish_directory(captured)


def remove_node_cache(captured: CapturedEntry, leaves: list[tuple[str, tuple[int, int]]],
                      cutoff: float, deadline: float | None) -> None:
    """Delete only verified flat leaves, with durable exclusive capture per inode.

    A late new name is never traversed; it makes final rmdir fail closed. Each
    leaf capture writes its own recovery manifest before rename, so a crash or
    failed exclusive rollback leaves every byte with its original destination.
    """
    device = os.fstat(captured.fd).st_dev
    if captured.original_path is None:
        raise ValueError('Node cache capture has no recovery destination')
    for name, expected in leaves:
        check_deadline(deadline)
        captured.anchor.verify()
        with capture_entry(captured.path / name, expected, directory=False,
                           recovery_original=captured.original_path / name) as leaf:
            value = node_compile_cache.validate_leaf(leaf.fd, name, device, cutoff, deadline)
            check_deadline(deadline)
            leaf.anchor.verify()
            if (node_compile_cache.fingerprint(os.fstat(leaf.fd)) != node_compile_cache.fingerprint(value)
                    or file_identity(os.stat('payload', dir_fd=leaf.container_fd, follow_symlinks=False)) != expected):
                raise ValueError('Node cache leaf changed before removal')
            os.unlink('payload', dir_fd=leaf.container_fd)
            leaf.removed = True
            captured.deleted_entries += 1
            os.fsync(leaf.container_fd)
    check_deadline(deadline)
    captured.anchor.verify()
    if file_identity(os.stat('payload', dir_fd=captured.container_fd, follow_symlinks=False)) != file_identity(os.fstat(captured.fd)):
        raise ValueError('captured Node cache identity changed')
    os.rmdir('payload', dir_fd=captured.container_fd)
    captured.removed = True
    os.fsync(captured.container_fd)


def apply_candidates(candidates: list[Candidate], deadline: float | None = None) -> tuple[list[dict], list[dict]]:
    removed, skipped = [], []
    for index, candidate in enumerate(candidates):
        if deadline is not None and time.monotonic() >= deadline:
            skipped.extend({'path': c.path, 'reason': 'execution budget exhausted', 'error': True}
                           for c in candidates[index:])
            break
        p, activity = Path(candidate.path), Path(candidate.activity_root)
        captured = None
        try:
            if is_recovery_path(p):
                raise ValueError('preserved recovery quarantine')
            is_node = candidate.kind == node_compile_cache.KIND
            is_npm_log = candidate.kind == NPM_LOG_KIND
            if is_npm_log:
                if candidate.min_age_days != 14 or activity != p or candidate.leaf_fingerprint is None:
                    raise ValueError('unclassified npm log retention policy')
                before_log = npm_log_stat(p, time.time() - 14 * 86400)
                if entry_fingerprint(before_log) != candidate.leaf_fingerprint:
                    raise ValueError('npm log changed after discovery')
            if is_node:
                version = node_compile_cache.version_for(p)
                if candidate.min_age_days != node_compile_cache.DAYS or activity != p:
                    raise ValueError('unclassified Node cache retention policy')
                if version in node_compile_cache.active_versions(deadline):
                    raise ValueError('active Node version ' + version)
            reason = activity_reason(activity, process_arguments(), directory=not is_npm_log)
            if reason:
                raise ValueError(reason)
            if p.resolve() != p or activity.resolve() != activity:
                raise ValueError('path redirected after discovery')
            check_deadline(deadline)
            with capture_entry(p, (candidate.device, candidate.inode), directory=not is_npm_log) as captured:
                cutoff = time.time() - candidate.min_age_days * 86400
                if is_npm_log:
                    captured_log = os.fstat(captured.fd)
                    if entry_fingerprint(captured_log, after_rename=True) != entry_fingerprint(before_log, after_rename=True):
                        raise ValueError('npm log changed during capture')
                    allocated, newest = captured_log.st_blocks * 512, captured_log.st_mtime
                elif is_node:
                    if (os.fstat(captured.fd).st_dev != os.fstat(captured.anchor.fd).st_dev
                            or os.fstat(captured.anchor.fd).st_uid != os.getuid()):
                        raise ValueError('Node cache root owner or filesystem changed')
                    allocated, newest, leaves = node_compile_cache.tree_facts(captured.fd, cutoff, deadline)
                else:
                    allocated, newest = descriptor_tree_facts(captured.fd, cutoff, deadline)
                if newest != candidate.newest_mtime:
                    raise ValueError('contents changed after discovery')
                if is_node and version in node_compile_cache.active_versions(deadline):
                    raise ValueError('active Node version ' + version)
                commands = process_arguments()
                if activity == p:
                    # Capture moved this pathname; inspect open handles on its
                    # captured location while retaining original argv aliases.
                    reason = 'active process references directory' if process_references(activity, commands) else None
                else:
                    reason = activity_reason(activity, commands, ignore_current_process=True)
                reason = reason or activity_reason(captured.path, commands, ignore_current_process=True,
                                                    directory=not is_npm_log)
                if reason:
                    raise ValueError(reason)
                captured.anchor.verify()
                if is_npm_log:
                    remove_captured_leaf(captured, captured_log, cutoff, candidate.device, deadline)
                elif is_node:
                    remove_node_cache(captured, leaves, cutoff, deadline)
                else:
                    remove_captured_tree(captured, cutoff, deadline)
            # A new object at the original path belongs to another worker and
            # remains untouched. The removed captured inode is the effect proof.
            removed.append({'path': str(captured.path), 'original_path': str(p),
                            'allocated_bytes': allocated, 'kind': candidate.kind,
                            'device': candidate.device, 'inode': candidate.inode,
                            'captured_inode_removed': True, 'replacement_present': os.path.lexists(p)})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            skipped.append({'path': str(p), 'reason': str(error),
                            'partially_removed': bool(captured and captured.deleted_entries),
                            'error': isinstance(error, (OSError, subprocess.SubprocessError))
                                     or bool(captured and captured.deleted_entries)
                                     or 'inspection' in str(error)})
    return removed, skipped


def disposable_simulators(deadline: float) -> tuple[list[dict], list[dict]]:
    plans, errors = [], []
    device_sets = [(USER_HOME / 'Library/Developer/CoreSimulator/Devices', False)]
    if OWC.is_mount():
        device_sets.append((OWC / 'Developer/Xcode/BuildData/XCTestDevices', True))
    for root, test_set in device_sets:
        if not root.is_dir() or root.resolve() != root:
            continue
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('execution budget exhausted')
            command = ['xcrun', 'simctl'] + (['--set', str(root)] if test_set else [])
            result = subprocess.run(command + ['list', 'devices', '--json'], capture_output=True,
                                    text=True, timeout=20, check=True)
            devices = json.loads(result.stdout)['devices']
            for runtime, entries in devices.items():
                for device in entries:
                    name = device['name']
                    disposable = (test_set and re.match(r'^Clone \d+ of ', name)) or name.startswith('codex-')
                    if not disposable or device['state'] != 'Shutdown':
                        continue
                    p = root / device['udid']
                    if not p.is_dir() or p.resolve() != p:
                        continue
                    # Ephemeral XCTest clones age out after a week; named Codex
                    # comparison devices receive two weeks. Personal devices stay.
                    days = 7 if test_set else 14
                    try:
                        size, newest = tree_facts(p, time.time() - days * 86400, deadline)
                    except ValueError:
                        continue
                    st = p.stat()
                    plans.append({'path': str(p), 'id': device['udid'], 'name': name,
                                  'runtime': runtime, 'device_type': device.get('deviceTypeIdentifier'),
                                  'device_set': str(root) if test_set else None,
                                  'device': st.st_dev, 'inode': st.st_ino,
                                  'newest_mtime': newest, 'allocated_bytes': size, 'min_age_days': days})
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            errors.append({'path': str(root), 'reason': 'simulator inventory failed: ' + str(error), 'error': True})
    return plans, errors


def delete_simulators(plans: list[dict], deadline: float) -> tuple[list[dict], list[dict]]:
    removed, deferred = [], []
    for plan in plans:
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError('execution budget exhausted')
            commands = process_arguments()
            if re.search(r'(?:^|[ /])(?:xcodebuild|xctest|XCTRunner)(?:[ ]|$)', commands, re.M):
                raise ValueError('active Xcode test run')
            path = Path(plan['path'])
            reason = activity_reason(path, commands)
            if reason:
                raise ValueError(reason)
            command = ['xcrun', 'simctl'] + (['--set', plan['device_set']] if plan['device_set'] else [])
            result = subprocess.run(command + ['list', 'devices', '--json'], capture_output=True,
                                    text=True, timeout=20, check=True)
            devices = {d['udid']: d for rows in json.loads(result.stdout)['devices'].values() for d in rows}
            device = devices.get(plan['id'], {})
            if device.get('name') != plan['name'] or device.get('state') != 'Shutdown':
                raise ValueError('simulator identity or state changed')
            st = path.stat()
            if path.resolve() != path or (st.st_dev, st.st_ino) != (plan['device'], plan['inode']):
                raise ValueError('simulator directory identity changed')
            _, newest = tree_facts(path, time.time() - plan['min_age_days'] * 86400, deadline)
            if newest != plan['newest_mtime']:
                raise ValueError('simulator contents changed')
            subprocess.run(command + ['delete', plan['id']], capture_output=True, text=True, timeout=30, check=True)
            if path.exists():
                raise OSError('simulator directory remains after native deletion')
            removed.append(plan)
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
            deferred.append({'path': plan['path'], 'reason': str(error),
                             'error': isinstance(error, (OSError, subprocess.SubprocessError)) or 'inspection' in str(error)})
    return removed, deferred


def oversized_logs() -> list[dict]:
    result = []
    root = USER_HOME / '.openclaw/logs'
    for name in ('node.log', 'node.err.log', 'gateway.log', 'gateway.err.log'):
        p = root / name
        if p.is_file() and not p.is_symlink() and p.resolve() == p:
            s = p.stat()
            if s.st_uid == os.getuid() and s.st_size > 16 * 1024**2:
                result.append({'path': str(p), 'device': s.st_dev, 'inode': s.st_ino,
                               'size': s.st_size, 'mtime_ns': s.st_mtime_ns})
    return result


def rotate_logs(plans: list[dict], deadline: float) -> tuple[list[dict], list[dict]]:
    archived, deferred = [], []
    archive = OWC / 'OpenClaw/.state/OpenClaw/logs/launchd-archive'
    for plan in plans:
        p = Path(plan['path'])
        try:
            check_deadline(deadline)
            if not OWC.is_mount() or archive.resolve() != archive:
                raise OSError('log archive mount or path unavailable')
            inspection = subprocess.run(['/usr/bin/sudo', '-n', '/usr/sbin/lsof', '-nP', str(p)], capture_output=True,
                                        text=True, timeout=20)
            if inspection.returncode != 1 or inspection.stdout or inspection.stderr:
                raise OSError('oversized active log needs a supervisor-supported reopen')
            check_deadline(deadline)
            # Capture the inactive inode before copying. Later writers open the
            # original pathname, never the old inode we remove after verification.
            with capture_entry(p, (plan['device'], plan['inode']), directory=False) as captured:
                original = os.fstat(captured.fd)
                if original.st_uid != os.getuid() or (original.st_size, original.st_mtime_ns) != (plan['size'], plan['mtime_ns']):
                    raise OSError('log identity or contents changed')
                archive.mkdir(parents=True, exist_ok=True)
                target = archive / (p.name + '.' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
                with anchored_parent(target) as destination:
                    if os.fstat(destination.fd).st_dev != OWC.stat().st_dev:
                        raise OSError('log archive filesystem changed')
                    output_fd = os.open(target.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600, dir_fd=destination.fd)
                    with os.fdopen(output_fd, 'w+b') as output:
                        digest = hashlib.sha256()
                        while True:
                            check_deadline(deadline)
                            chunk = os.read(captured.fd, 1024**2)
                            if not chunk:
                                break
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                        os.fsync(destination.fd)  # Durable archive name before removing the source.
                        output.seek(0)
                        verification = hashlib.sha256()
                        while True:
                            check_deadline(deadline)
                            chunk = output.read(1024**2)
                            if not chunk:
                                break
                            verification.update(chunk)
                        current = os.fstat(captured.fd)
                        if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns) or digest.digest() != verification.digest():
                            raise OSError('log changed or archive verification failed')
                        if file_identity(os.stat(target.name, dir_fd=destination.fd, follow_symlinks=False)) != file_identity(os.fstat(output.fileno())):
                            raise OSError('log archive identity changed')
                        destination.verify()
                    inspection = subprocess.run(['/usr/bin/sudo', '-n', '/usr/sbin/lsof', '-nP', '-a', '-p', '^' + str(os.getpid()), str(captured.path)],
                                                capture_output=True, text=True, timeout=20)
                    if inspection.returncode != 1 or inspection.stdout or inspection.stderr:
                        raise OSError('captured log still has a writer or inspection failed')
                    check_deadline(deadline)
                    # A pre-capture writer may append and close between the
                    # archive comparison and handle inspection. Verify again
                    # after quiescence so those last bytes remain recoverable.
                    current = os.fstat(captured.fd)
                    if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
                        raise OSError('captured log changed after archive verification')
                    captured.anchor.verify()
                    if file_identity(os.stat('payload', dir_fd=captured.container_fd, follow_symlinks=False)) != file_identity(os.fstat(captured.fd)):
                        raise OSError('captured log identity changed')
                    os.unlink('payload', dir_fd=captured.container_fd)
                    captured.removed = True
                    os.fsync(captured.container_fd)
                archived.append({'path': str(p), 'archive': str(target), 'bytes': original.st_size,
                                 'sha256': digest.hexdigest(), 'captured_inode_removed': True,
                                 'replacement_present': os.path.lexists(p)})
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            deferred.append({'path': str(p), 'reason': str(error), 'error': True})
    return archived, deferred


def require_owc_identity() -> None:
    contract = json.loads((Path(__file__).resolve().parents[1] / 'registry/external_volume_guard.json').read_text())
    metadata = external_volume_guard.volume_metadata(OWC)
    if (metadata.get('available') is not True or metadata.get('mounted') is not True
            or metadata.get('volumeUuid', '').upper() != contract['volumeUuid'].upper()
            or metadata.get('mountPoint') != contract['mountPoint']
            or metadata.get('readOnly') is not False or metadata.get('ownersEnabled') is not True):
        raise RuntimeError('OWC mounted volume identity or write permission does not match the registered drive')


def free_space() -> dict[str, int | None]:
    return {'internal': shutil.disk_usage('/').free,
            'owc': shutil.disk_usage(OWC).free if OWC.is_mount() else None}


def write_text_receipt(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        temp.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def write_receipt(path: Path, payload: dict) -> None:
    write_text_receipt(path, json.dumps(payload, indent=2, sort_keys=True) + '\n')


def run(*, apply: bool = False, budget_seconds: int = 480) -> dict:
    if not 1 <= budget_seconds <= 540:
        raise ValueError('budget must be between 1 and 540 seconds')
    deadline = time.monotonic() + budget_seconds
    # A missing OWC must never redirect artifacts or cleanup into a shadow mount.
    if not OWC.is_mount():
        raise RuntimeError('OWC drive is not mounted; storage cleanup stopped')
    require_owc_identity()
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    with (ARTIFACT_ROOT / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        now = datetime.now(timezone.utc)
        receipt = ARTIFACT_ROOT / ('storage-prune-' + now.strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        candidates, skipped = discover(deadline=deadline)
        simulators, simulator_errors = disposable_simulators(deadline)
        skipped.extend(simulator_errors)
        payload = {'schema': 'openclaw.storage_prune.v1', 'created_at': now.isoformat(),
                   'mode': 'apply' if apply else 'preview', 'status': 'planned', 'terminal': False,
                   'report': str(receipt), 'errors': [],
                   'before_free_bytes': free_space(), 'candidates': [asdict(c) for c in candidates],
                   'skipped': skipped, 'removed': [], 'simulator_plan': simulators, 'removed_simulators': [], 'log_rotation_plan': oversized_logs(), 'archived_logs': [],
                   'regeneration': 'Xcode/compiler or package manager recreates generated output; source, products and test receipts are preserved'}
        write_receipt(receipt, payload)  # Exact manifest is durable before mutation.
        if apply:
            require_owc_identity()
            removed, deferred = apply_candidates(candidates, deadline)
            payload['removed'], payload['deferred'] = removed, deferred
            deleted_simulators, simulator_errors = delete_simulators(simulators, deadline)
            payload['removed_simulators'] = deleted_simulators
            payload['deferred'].extend(simulator_errors)
            archived, log_errors = rotate_logs(payload['log_rotation_plan'], deadline)
            payload['archived_logs'] = archived
            payload['deferred'].extend(log_errors)
        payload['after_free_bytes'] = free_space()
        if payload['after_free_bytes']['owc'] is None:
            payload['skipped'].append({'reason': 'OWC drive disappeared during cleanup', 'error': True})
        payload['errors'] = [entry for entry in payload['skipped'] + payload.get('deferred', []) if entry.get('error')]
        payload['status'] = 'partial' if payload['errors'] else 'ok'
        payload['terminal'] = True
        payload['reclaimed_allocated_bytes'] = sum(r['allocated_bytes'] for r in payload['removed'])
        write_receipt(receipt, payload)
        write_receipt(ARTIFACT_ROOT / 'latest.json', payload)
        return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='remove guarded classified stale output')
    parser.add_argument('--json', action='store_true', help='emit one terminal JSON receipt')
    parser.add_argument('--budget-seconds', type=int, default=480)
    args = parser.parse_args()
    try:
        result = run(apply=args.apply, budget_seconds=args.budget_seconds)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        failure = {'schema': 'openclaw.storage_prune.v1', 'mode': 'apply' if args.apply else 'preview',
                   'status': 'failed', 'terminal': True, 'errors': [str(error)], 'report': None}
        print(json.dumps(failure) if args.json else f'Storage cleanup failed: {error}. No further cleanup was attempted.')
        return 1
    if args.json:
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'ok' else 1
    free = result['after_free_bytes']
    if free['owc'] is None:
        print('Storage cleanup needs attention: OWC is unavailable; check the drive before retrying.')
        return 1
    if args.apply:
        count = len(result['removed'])
        print(f'Storage cleanup {result["status"]}: removed {count} stale build/cache directories. '
              f'Mac mini {free["internal"] / 1024**3:.1f} GiB free; OWC {free["owc"] / 1024**3:.1f} GiB free.'
              + (f' {len(result.get("deferred", []))} candidates remain protected or need inspection.'
                 if result.get('deferred') else ''))
    else:
        print(f'Storage cleanup preview: {len(result["candidates"])} eligible directories; no files removed.')
    return 0 if result['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
