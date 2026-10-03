"""Recognize disposable native cache formats for the existing storage prune owner.

No removal, retention state, store traversal, or application caches live here.
Audited producers: pnpm 11.17.0 prepareJsonForDisk/getPkgMirrorPath, pnpm 12
pacquet-meta-v1, and Vitest 5 saveCachedModule/updateMetadata.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
from datetime import datetime, timezone
from typing import Callable, NamedTuple

PNPM_KIND = 'pnpm-public-metadata-cache'
VITEST_KIND = 'vitest-generated-module-cache'
KINDS = (PNPM_KIND, VITEST_KIND)
PNPM_LAYOUTS = ('metadata', 'metadata-full', 'metadata-full-filtered')
PNPM_REGISTRY_NAMES = ('registry.npmjs.org', 'https%3A+registry.npmjs.org')
NAME = r'[a-z0-9][a-z0-9._-]{0,213}'
PACKAGE = re.compile(NAME + r'\.jsonl')
SCOPE = re.compile('@' + NAME)
VITEST_ROOT = re.compile(r'openclaw-vitest-[a-z0-9]+(?:-[a-z0-9]+)*-(\d{8})')
PROJECT = re.compile(r'\d{1,3}-test-vitest-vitest\.[a-z0-9._-]+\.config\.ts')
MODULE = re.compile(r'[0-9a-f]{40}')
TRAILER = b'\n//# vitestCache='
MAX_ENTRIES = 65536
MAX_BYTES = 64 * 1024**2
MAX_TRAILER_BYTES = 128 * 1024


def candidates(home: Path) -> list[tuple[Path, str]]:
    result = [(home / 'Library/Caches/pnpm/v11' / layout / registry, PNPM_KIND)
              for layout in PNPM_LAYOUTS for registry in PNPM_REGISTRY_NAMES]
    cache = home / '.cache'
    if cache.is_dir() and cache.resolve() == cache:
        # Bound discovery without retaining an arbitrary partial name list.
        with os.scandir(cache) as entries:
            names = []
            for entry in entries:
                if len(names) >= 4096:
                    raise ValueError('developer cache root inventory exceeds budget')
                names.append(entry.name)
        result += [(cache / name, VITEST_KIND) for name in names if VITEST_ROOT.fullmatch(name)]
    return [(p, kind) for p, kind in result if os.path.lexists(p)]


def classify(path: Path, kind: str, home: Path, cutoff: float) -> None:
    if kind == PNPM_KIND:
        if path not in {home / 'Library/Caches/pnpm/v11' / layout / registry
                        for layout in PNPM_LAYOUTS for registry in PNPM_REGISTRY_NAMES}:
            raise ValueError('unclassified public pnpm metadata path')
    elif kind == VITEST_KIND:
        match = VITEST_ROOT.fullmatch(path.name)
        if path.parent != home / '.cache' or match is None:
            raise ValueError('unclassified Vitest cache path')
        stamp = datetime.strptime(match[1], '%Y%m%d').replace(tzinfo=timezone.utc)
        if stamp.timestamp() > cutoff:
            raise ValueError('recent Vitest cache generation')
    else:
        raise ValueError('unclassified developer cache kind')


def producer_reason(kind: str, commands: str) -> str | None:
    # Match actual CLI/native producer names, not dependency paths in .pnpm.
    pattern = (r'(?:^|[/\s])(?:pnpm(?:-native|\.[cm]?js)?|pnpx|pnx|pn)(?=\s|$)'
               if kind == PNPM_KIND else r'(?:^|[/\s(])vitest(?=[/\s.)]|$)')
    return 'active ' + ('pnpm' if kind == PNPM_KIND else 'Vitest') + ' producer' if re.search(pattern, commands) else None


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError('execution budget exhausted')


def json_value(data: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate cache JSON key')
            result[key] = value
        return result
    def reject_constant(_):
        raise ValueError('nonfinite cache JSON')
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=reject_constant)
    except (UnicodeError, RecursionError) as error:
        raise ValueError('unclassified cache JSON') from error


def string_map(value) -> bool:
    return isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str)
                                          for k, v in value.items())


def pnpm_payload(data: bytes, package: str, deadline: float | None) -> None:
    header, separator, body = data.partition(b'\n')
    if not separator or len(header) > 4096:
        raise ValueError('unclassified pnpm metadata header')
    native = re.fullmatch(rb'pacquet-meta-v1 ([1-9][0-9]{0,8}) ([1-9][0-9]{0,8})', header)
    if native:
        headers_size, summary_size = map(int, native.groups())
        headers = json_value(body[:headers_size])
        metadata = json_value(body[headers_size:headers_size + summary_size])
    else:
        headers, metadata = json_value(header), json_value(body)
    if (not isinstance(headers, dict) or not set(headers) <= {'etag', 'modified'}
            or any(v is not None and not isinstance(v, str) for v in headers.values())
            or not isinstance(metadata, dict) or metadata.get('name') != package):
        raise ValueError('unclassified pnpm package metadata')
    if native:
        versions = metadata.get('versions')
        if not string_map(metadata.get('distTags')) or not isinstance(versions, list) or not versions:
            raise ValueError('unclassified pacquet metadata summary')
        records, offset, seen = body[headers_size + summary_size:], 0, set()
        for entry in versions:
            check_deadline(deadline)
            if (not isinstance(entry, list) or len(entry) != 3 or not isinstance(entry[0], str)
                    or entry[0] in seen or type(entry[1]) is not int or type(entry[2]) is not int
                    or entry[1] != offset or entry[2] <= 0 or offset + entry[2] > len(records)):
                raise ValueError('unclassified pacquet version index')
            version = json_value(records[offset:offset + entry[2]])
            if not isinstance(version, dict) or version.get('name') != package or version.get('version') != entry[0]:
                raise ValueError('unclassified pacquet version record')
            seen.add(entry[0])
            offset += entry[2]
        if offset != len(records):
            raise ValueError('unclassified trailing pacquet data')
    else:
        versions = metadata.get('versions')
        if not string_map(metadata.get('dist-tags')) or not isinstance(versions, dict) or not versions:
            raise ValueError('unclassified pnpm version metadata')
        for key, version in versions.items():
            check_deadline(deadline)
            if (not isinstance(version, dict) or version.get('version') != key
                    or version.get('name', package) != package):
                raise ValueError('unclassified pnpm version record')


def vitest_payload(data: bytes, metadata: bool) -> None:
    if metadata:
        value = json_value(data)
        if (not isinstance(value, dict) or set(value) != {'lockfileHash'}
                or not isinstance(value['lockfileHash'], str)
                or re.fullmatch(r'[0-9a-f]{8}', value['lockfileHash']) is None):
            raise ValueError('unclassified Vitest metadata')
        return
    _, separator, encoded = data.rpartition(TRAILER)
    if not separator or not encoded or len(encoded) > MAX_TRAILER_BYTES:
        raise ValueError('unclassified Vitest module trailer')
    try:
        pool = json_value(base64.b64decode(encoded, validate=True))
    except ValueError as error:
        raise ValueError('unclassified Vitest module trailer') from error
    fields = {'file', 'id', 'url', 'importedUrls', 'mappings', 'moduleType', 'deps', 'dynamicDeps', 'staticMocks'}
    if (not isinstance(pool, list) or not pool or not isinstance(pool[0], dict)
            or not {'id', 'url', 'importedUrls', 'mappings'} <= pool[0].keys()
            or not pool[0].keys() <= fields):
        raise ValueError('unclassified Vitest flatted metadata')
    used = {0}
    def reference(value):
        if isinstance(value, str):
            if len(value) > 6 or not re.fullmatch(r'0|[1-9][0-9]*', value) or int(value) >= len(pool):
                raise ValueError('invalid Vitest flatted reference')
            used.add(int(value))
            return pool[int(value)]
        return value
    for key, raw in pool[0].items():
        value = reference(raw)
        if key in ('id', 'url', 'file', 'moduleType'):
            valid = isinstance(value, str) and bool(value)
        elif key == 'mappings':
            valid = type(value) is bool or isinstance(value, str)
        elif key == 'staticMocks':
            valid = value is None or isinstance(value, list)
            if isinstance(value, list):
                for item in value:
                    call = reference(item)
                    if (not isinstance(call, dict)
                            or set(call) != {'method', 'specifier', 'hasFactory', 'factoryLoadsOriginal'}
                            or not all(isinstance(reference(call[k]), str) for k in ('method', 'specifier'))
                            or not all(type(call[k]) is bool for k in ('hasFactory', 'factoryLoadsOriginal'))):
                        valid = False
        else:
            valid = isinstance(value, list) and all(isinstance(reference(item), str) for item in value)
        if not valid:
            raise ValueError('unclassified Vitest result field')
    if used != set(range(len(pool))):
        raise ValueError('unclassified unused Vitest flatted values')


def layout(kind: str, relative: str, directory: bool) -> bool:
    if relative == '.':
        return directory
    parts = relative.split('/')
    if kind == PNPM_KIND:
        if directory:
            return len(parts) == 1 and SCOPE.fullmatch(parts[0]) is not None
        return (len(parts) == 1 and PACKAGE.fullmatch(parts[0]) is not None or
                len(parts) == 2 and SCOPE.fullmatch(parts[0]) is not None and PACKAGE.fullmatch(parts[1]) is not None)
    if directory:
        return len(parts) == 1 and PROJECT.fullmatch(parts[0]) is not None
    return (len(parts) == 1 or len(parts) == 2 and PROJECT.fullmatch(parts[0]) is not None) and (
        parts[-1] == '_metadata.json' or MODULE.fullmatch(parts[-1]) is not None)


def validate_leaf(fd: int, kind: str, relative: str, deadline: float | None) -> None:
    size = os.fstat(fd).st_size
    if size <= 0 or size > MAX_BYTES or not layout(kind, relative, False):
        raise ValueError('unclassified developer cache leaf size or name')
    metadata = kind == VITEST_KIND and relative.split('/')[-1] == '_metadata.json'
    limit = MAX_TRAILER_BYTES if kind == VITEST_KIND and not metadata else MAX_BYTES
    if metadata and size > 4096:
        raise ValueError('unclassified Vitest metadata size')
    offset = max(0, size - limit)
    data = bytearray()
    while offset < size:
        check_deadline(deadline)
        block = os.pread(fd, min(1024**2, size - offset), offset)
        if not block:
            raise ValueError('incomplete developer cache payload')
        data.extend(block)
        offset += len(block)
    if kind == PNPM_KIND:
        pnpm_payload(bytes(data), relative[:-6], deadline)
    else:
        vitest_payload(bytes(data), metadata)


class Facts(NamedTuple):
    allocated: int
    newest: float
    signature: str
    entries: dict[str, os.stat_result]


def tree_facts(fd: int, kind: str, cutoff: float, deadline: float | None,
               fingerprint: Callable) -> Facts:
    """Bound no-follow inventory; bind contents across discovery and root capture."""
    root = os.fstat(fd)
    entries, allocated, newest = {}, 0, 0.0
    def walk(current: int, relative: str):
        nonlocal allocated, newest
        check_deadline(deadline)
        before = os.fstat(current)
        directory = stat.S_ISDIR(before.st_mode)
        if (before.st_uid != os.getuid() or before.st_dev != root.st_dev or before.st_mtime > cutoff
                or getattr(before, 'st_flags', 0) or not layout(kind, relative, directory)
                or not directory and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1)):
            raise ValueError('unclassified, recent, linked or foreign developer cache entry')
        if len(entries) >= MAX_ENTRIES:
            raise ValueError('developer cache tree exceeds entry budget')
        entries[relative] = before
        allocated += before.st_blocks * 512
        newest = max(newest, before.st_mtime)
        if directory:
            count = 0
            with os.scandir(current) as children:
                for child in children:
                    count += 1
                    check_deadline(deadline)
                    value = os.stat(child.name, dir_fd=current, follow_symlinks=False)
                    child_relative = child.name if relative == '.' else relative + '/' + child.name
                    if not layout(kind, child_relative, stat.S_ISDIR(value.st_mode)):
                        raise ValueError('unclassified developer cache tree layout')
                    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                    if stat.S_ISDIR(value.st_mode):
                        flags |= os.O_DIRECTORY
                    child_fd = os.open(child.name, flags, dir_fd=current)
                    try:
                        if fingerprint(os.fstat(child_fd)) != fingerprint(value):
                            raise ValueError('developer cache entry changed while opening')
                        walk(child_fd, child_relative)
                    finally:
                        os.close(child_fd)
            if count == 0 or kind == VITEST_KIND and (('_metadata.json' if relative == '.' else relative + '/_metadata.json') not in entries):
                raise ValueError('empty or incomplete native developer cache layout')
        else:
            validate_leaf(current, kind, relative, deadline)
        if fingerprint(os.fstat(current)) != fingerprint(before):
            raise ValueError('developer cache entry changed during inspection')
    walk(fd, '.')
    signature = hashlib.sha256()
    for relative, value in sorted(entries.items()):
        signature.update(json.dumps((relative, fingerprint(value, after_rename=relative == '.')),
                                    separators=(',', ':')).encode())
    return Facts(allocated, newest, signature.hexdigest(), entries)
