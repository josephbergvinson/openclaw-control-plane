#!/usr/bin/env python3
"""Recognize the audited Node compile-cache layout and inspect native Node images.

The privileged CLI is read-only. It never executes the inspected binaries.
Unknown layouts and incomplete process visibility are preservation conditions.
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import time
import zlib

ROOT = Path((str(OPERATOR.require_path('paths.node_compile_cache'))))
KIND = 'node-compile-cache'
DAYS = 14
VERSION = r'v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
# Only the major/architecture families actually inspected on this host. A new
# Node format requires another classification review, not a broader name glob.
NAMESPACE = re.compile(r'(v(?:22|24|26)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-arm64-[0-9a-f]{8}-([0-9]+)')
LEAF = re.compile(r'[0-9a-f]{8}')
MAX_FILE_BYTES = 64 * 1024 * 1024


def version_for(path: Path) -> str:
    match = NAMESPACE.fullmatch(path.name)
    if path.parent != ROOT or not match or match[2] != str(os.getuid()):
        raise ValueError('unclassified Node cache namespace')
    return match[1]


def deadline_timeout(deadline: float | None, maximum: float) -> float:
    remaining = maximum if deadline is None else min(maximum, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('execution budget exhausted')
    return remaining


def fingerprint(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def validate_leaf(fd: int, name: str, device: int, cutoff: float,
                  deadline: float | None) -> os.stat_result:
    """Read the Node header and payload CRC through an already confined descriptor."""
    before = os.fstat(fd)
    if (not LEAF.fullmatch(name) or not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
        raise ValueError('unclassified Node cache leaf')
    if before.st_dev != device or before.st_uid != os.getuid():
        raise ValueError('foreign Node cache owner or filesystem')
    if before.st_mtime > cutoff:
        raise ValueError('recent Node cache contents')
    if before.st_size <= 20 or before.st_size > MAX_FILE_BYTES:
        raise ValueError('unclassified Node cache size')
    deadline_timeout(deadline, 1)
    os.lseek(fd, 0, os.SEEK_SET)
    header = os.read(fd, 20)
    if len(header) != 20:
        raise ValueError('incomplete Node cache header')
    magic, source_size, size, _, checksum = struct.unpack('<5I', header)
    if magic != 0x8ADFDBB2 or source_size == 0 or size != before.st_size - 20:
        raise ValueError('unclassified Node cache header')
    remaining, actual = size, 0
    while remaining:
        deadline_timeout(deadline, 1)
        data = os.read(fd, min(remaining, 1024 * 1024))
        if not data:
            raise ValueError('incomplete Node cache payload')
        actual = zlib.crc32(data, actual)
        remaining -= len(data)
    if actual != checksum:
        raise ValueError('unclassified Node cache checksum')
    if fingerprint(os.fstat(fd)) != fingerprint(before):
        raise ValueError('Node cache leaf changed during inspection')
    return before


def tree_facts(fd: int, cutoff: float, deadline: float | None) -> tuple[int, float, list[tuple[str, tuple[int, int]]]]:
    """Flat namespace only; never recurse into a directory with an unknown layout."""
    root = os.fstat(fd)
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.getuid():
        raise ValueError('foreign or non-directory Node cache namespace')
    if root.st_mtime > cutoff:
        raise ValueError('recent Node cache namespace')
    allocated, newest, leaves = root.st_blocks * 512, root.st_mtime, []
    with os.scandir(fd) as entries:
        for entry in entries:
            deadline_timeout(deadline, 1)
            before = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            if not LEAF.fullmatch(entry.name) or not stat.S_ISREG(before.st_mode):
                raise ValueError('unclassified Node cache layout')
            child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                value = validate_leaf(child, entry.name, root.st_dev, cutoff, deadline)
                if fingerprint(value) != fingerprint(before):
                    raise ValueError('Node cache leaf identity changed')
            finally:
                os.close(child)
            allocated += value.st_blocks * 512
            newest = max(newest, value.st_mtime)
            leaves.append((entry.name, (value.st_dev, value.st_ino)))
    if not leaves:
        raise ValueError('unclassified empty Node cache namespace')
    if fingerprint(os.fstat(fd)) != fingerprint(root):
        raise ValueError('Node cache namespace changed during inspection')
    return allocated, newest, leaves


def process_table(deadline: float | None) -> dict[int, str]:
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,stat='], capture_output=True,
                            text=True, timeout=deadline_timeout(deadline, 10), check=True)
    if result.stderr.strip():
        raise ValueError('Node process inspection emitted diagnostics')
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) in rows:
            raise ValueError('Node process inspection malformed snapshot')
        rows[int(fields[0])] = fields[1]
    if not rows or os.getpid() not in rows:
        raise ValueError('Node process inspection incomplete snapshot')
    return rows


def executable_path(pid: int) -> str:
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    call = library.proc_pidpath
    call.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    call.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    if not call(pid, buffer, len(buffer)):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    path = os.fsdecode(buffer.value)
    if not path.startswith('/'):
        raise ValueError('Node process inspection invalid executable path')
    return path


def kernel_snapshot(deadline: float | None) -> dict[int, str]:
    """Inspect every live PID, including Node processes with overwritten titles."""
    rows = process_table(deadline)
    paths, unresolved = {}, {}
    for pid, state in rows.items():
        deadline_timeout(deadline, 1)
        if 'Z' in state:
            continue  # A zombie has no executable or future cache writes.
        try:
            paths[pid] = executable_path(pid)
        except OSError as error:
            unresolved[pid] = error
    if unresolved:
        after = process_table(deadline)
        for pid, error in unresolved.items():
            # ESRCH from ps itself/another exited child is expected. An unreadable
            # live PID or any other error is incomplete visibility, never empty.
            if error.errno != errno.ESRCH or (pid in after and 'Z' not in after[pid]):
                raise ValueError('Node process inspection cannot resolve live executable')
    return paths


def text_images(output: str) -> dict[int, list[tuple[str, int, int]]]:
    """Parse lsof's NUL fields rather than ambiguous whitespace in paths."""
    result, pid, record = {}, None, {}
    def finish():
        if record:
            if pid is None or record.get('f') != 'txt' or not all(k in record for k in ('D', 'i', 'n')):
                raise ValueError('Node process inspection incomplete text mapping')
            try:
                result.setdefault(pid, []).append((record['n'], int(record['D'], 16), int(record['i'])))
            except ValueError as error:
                raise ValueError('Node process inspection malformed text mapping') from error
    for field in output.split('\0'):
        field = field.lstrip('\n')
        if not field:
            continue
        key, value = field[0], field[1:]
        if key == 'p':
            finish(); record = {}
            if not value.isdigit():
                raise ValueError('Node process inspection malformed PID')
            pid = int(value)
        elif key == 'f':
            finish(); record = {'f': value}
        elif key in ('D', 'i', 'n'):
            if key in record:
                raise ValueError('Node process inspection duplicate text field')
            record[key] = value
        else:
            raise ValueError('Node process inspection unknown text field')
    finish()
    return result


def node_images(snapshot: dict[int, str]) -> dict[int, str]:
    return {pid: path for pid, path in snapshot.items() if Path(path).name in ('node', 'nodejs')}


def bind_node_images(nodes: dict[int, str], deadline: float | None) -> list[dict]:
    """Retain exact images while observed processes still permit kernel binding."""
    if not nodes:
        return []
    inspection = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', ','.join(map(str, nodes)),
                                 '-d', 'txt', '-F0pfDin'], capture_output=True, text=True,
                                timeout=deadline_timeout(deadline, 15))
    if inspection.returncode not in (0, 1) or inspection.stderr.strip():
        raise ValueError('Node process inspection lsof failed')
    mappings = text_images(inspection.stdout)
    still_live = process_table(deadline)
    executables = {}
    for pid, path in nodes.items():
        if pid not in still_live or 'Z' in still_live[pid]:
            raise ValueError('Node process inspection observed image exited before binding')
        value = Path(path).stat()
        # lsof binds the running image to the physical on-disk executable; a
        # replaced binary's --version cannot stand in for the running version.
        if (path, value.st_dev, value.st_ino) not in mappings.get(pid, []):
            raise ValueError('Node process inspection cannot bind running image')
        if executable_path(pid) != path:
            raise ValueError('Node process inspection executable changed')
        identity = fingerprint(value)
        entry = executables.setdefault(path, {'path': path, 'identity': identity, 'pids': []})
        if tuple(entry['identity']) != identity:
            raise ValueError('Node process inspection executable identity changed')
        entry['pids'].append(pid)
    return list(executables.values())


def native_snapshot(deadline: float | None) -> dict:
    if sys.platform != 'darwin' or os.geteuid() != 0:
        raise ValueError('Node process inspection requires privileged Darwin visibility')
    observed = {}

    def retain(snapshot: dict[int, str]) -> None:
        # Bind every observed Node image now, rather than discarding earlier
        # versions when a PID exits/execs between samples. The caller verifies
        # these physical executable identities again before version probes.
        for entry in bind_node_images(node_images(snapshot), deadline):
            previous = observed.get(entry['path'])
            if previous is None:
                observed[entry['path']] = entry
            else:
                if previous['identity'] != entry['identity']:
                    raise ValueError('Node process inspection executable identity changed')
                previous['pids'] = sorted(set(previous['pids']) | set(entry['pids']))

    # Every sample still resolves every live PID and rejects unreadable live
    # images. Only Node PID/image changes need stabilization: unrelated shell,
    # ps or compiler churn cannot invalidate an otherwise complete Node view.
    before = kernel_snapshot(deadline)
    retain(before)
    for _ in range(3):
        after = kernel_snapshot(deadline)
        retain(after)
        if node_images(before) == node_images(after):
            break
        before = after
    else:
        raise ValueError('Node process inspection unstable snapshot')
    return {'schema': 'openclaw.node_processes.v1', 'complete': True,
            'inspected': len(after), 'executables': list(observed.values())}


# These are controlled guard labels, not arbitrary subprocess output. Receipts
# must retain the reason without recording process argv, environment or stderr
# from an unexpected executable/sudo failure.
SAFE_HELPER_REASONS = frozenset({
    'Node process inspection requires privileged Darwin visibility',
    'Node process inspection emitted diagnostics',
    'Node process inspection malformed snapshot',
    'Node process inspection incomplete snapshot',
    'Node process inspection invalid executable path',
    'Node process inspection cannot resolve live executable',
    'Node process inspection incomplete text mapping',
    'Node process inspection malformed text mapping',
    'Node process inspection malformed PID',
    'Node process inspection duplicate text field',
    'Node process inspection unknown text field',
    'Node process inspection unstable snapshot',
    'Node process inspection lsof failed',
    'Node process inspection cannot bind running image',
    'Node process inspection executable changed',
    'Node process inspection executable identity changed',
    'Node process inspection observed image exited before binding',
    'Node process inspection timed out',
    'Node process inspection operating-system error',
    'Node process inspection unclassified failure',
})
HELPER_ERROR_PREFIX = 'Node process inspection failed: '


def safe_failure_text(error: BaseException) -> str:
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return 'Node process inspection timed out'
    if isinstance(error, subprocess.CalledProcessError):
        return 'Node process inspection child failed (exit ' + str(error.returncode) + ')'
    if isinstance(error, OSError):
        if isinstance(error.errno, int) and 0 <= error.errno <= 9999:
            return 'Node process inspection operating-system error (errno ' + str(error.errno) + ')'
        return 'Node process inspection operating-system error'
    message = str(error)
    return message if message in SAFE_HELPER_REASONS else 'Node process inspection unclassified failure'


def safe_helper_stderr(stderr: str | bytes | None) -> str:
    if not isinstance(stderr, str) or len(stderr) > 512:
        return 'unrecognized diagnostic withheld'
    line = stderr.strip()
    if not line.startswith(HELPER_ERROR_PREFIX):
        return 'unrecognized diagnostic withheld'
    reason = line[len(HELPER_ERROR_PREFIX):]
    if reason in SAFE_HELPER_REASONS or re.fullmatch(
            r'Node process inspection (?:operating-system error \(errno [0-9]{1,4}\)|child failed \(exit -?[0-9]{1,4}\))', reason):
        return reason
    return 'unrecognized diagnostic withheld'


def active_versions(deadline: float | None = None) -> set[str]:
    """Privileged read-only snapshot; version probes retain the caller's privileges."""
    budget = deadline_timeout(deadline, 45)
    if budget <= 2:
        raise TimeoutError('execution budget exhausted before Node process inspection')
    # Let the helper reap its bounded lsof/ps child before the caller's timeout.
    try:
        result = subprocess.run(['/usr/bin/sudo', '-n', sys.executable, '-I', str(Path(__file__).resolve()),
                                 '--snapshot', '--seconds', str(min(40, budget - 2))],
                                capture_output=True, text=True, check=True, timeout=budget)
    except subprocess.CalledProcessError as error:
        raise ValueError('Node process inspection helper failed (exit ' + str(error.returncode)
                         + '): ' + safe_helper_stderr(error.stderr)) from error
    except subprocess.TimeoutExpired as error:
        raise ValueError('Node process inspection helper timed out') from error
    if result.stderr.strip():
        raise ValueError('Node process inspection emitted diagnostics')
    try:
        snapshot = json.loads(result.stdout)
        if (snapshot['schema'] != 'openclaw.node_processes.v1' or snapshot['complete'] is not True
                or not isinstance(snapshot['inspected'], int) or snapshot['inspected'] < 1
                or not isinstance(snapshot['executables'], list)):
            raise ValueError('incomplete snapshot')
        versions = set()
        for entry in snapshot['executables']:
            path = Path(entry['path'])
            value = path.stat()
            if (not path.is_absolute() or not entry['pids'] or not stat.S_ISREG(value.st_mode)
                    or value.st_mode & 0o022 or value.st_uid not in (0, os.getuid())
                    or list(fingerprint(value)) != entry['identity']):
                raise ValueError('executable identity changed or unsafe permissions')
            version = subprocess.run([str(path), '--version'], capture_output=True, text=True, check=True,
                                     env={'PATH': '/usr/bin:/bin', 'NODE_DISABLE_COMPILE_CACHE': '1'},
                                     timeout=deadline_timeout(deadline, 5))
            if version.stderr.strip() or not re.fullmatch(VERSION, version.stdout.strip()):
                raise ValueError('unrecognized Node version')
            if fingerprint(path.stat()) != fingerprint(value):
                raise ValueError('executable changed during version inspection')
            versions.add(version.stdout.strip())
        return versions
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError('Node process inspection failed: ' + str(error)) from error


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', action='store_true', required=True)
    parser.add_argument('--seconds', type=float, default=40)
    arguments = parser.parse_args()
    if not 0 < arguments.seconds <= 40:
        parser.error('--seconds must be positive and at most 40')
    try:
        print(json.dumps(native_snapshot(time.monotonic() + arguments.seconds)))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(HELPER_ERROR_PREFIX + safe_failure_text(error), file=sys.stderr)
        raise SystemExit(1)
