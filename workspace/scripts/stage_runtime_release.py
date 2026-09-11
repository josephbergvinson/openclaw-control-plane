#!/usr/bin/env python3
"""Copy one completed clean build into a new read-only runtime candidate.

This stages files and a private receipt only. It does not select, seal or execute
the candidate, install dependencies, or change configuration or services.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

EXCLUDED_NAMES = {'.git', '.artifacts', '.cache', '.pytest_cache', '__pycache__'}
REQUIRED_FILES = (
    'openclaw.mjs', 'package.json', 'dist/build-info.json',
    'dist/control-ui/index.html',
    'dist/extensions/browser/chrome-extension/manifest.json',
    'dist/extensions/browser/chrome-extension/background.js',
    'dist/extensions/discord/index.js', 'dist/extensions/discord/openclaw.plugin.json',
    'dist/extensions/discord/package.json', 'dist/extensions/lane-contract/index.js',
    'dist/extensions/lane-contract/openclaw.plugin.json',
    'dist/extensions/lane-contract/package.json', 'dist/infra/runtime-state-migration.js',
)
REQUIRED_DIRECTORIES = ('dist-runtime', 'packages/ai/dist', 'node_modules')
PROTECTED_ROOTS = {'src', 'dist', 'dist-runtime', 'packages', 'node_modules', 'extensions',
                   'openclaw.mjs', 'package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml'}


class StagingError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def physical_path(path: Path, *, new: bool = False) -> None:
    if not path.is_absolute() or '..' in path.parts:
        raise StagingError('paths must be absolute without parent traversal')
    if new:
        if os.path.lexists(path):
            raise StagingError(f'destination already exists: {path}')
        if not path.parent.is_dir() or path.parent.resolve() != path.parent:
            raise StagingError('destination parent must be an existing physical directory')
    elif not path.is_dir() or path.resolve() != path:
        raise StagingError('source must be an existing physical directory')


def git_identity(source: Path, commit: str, exclude_roots: tuple[str, ...] = ()) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    git = shutil.which('git')
    if not git:
        raise StagingError('Git is required to verify the completed source checkout')

    def read(*arguments: str) -> str:
        result = subprocess.run([git, '-c', 'core.fsmonitor=false', '-C', str(source), *arguments],
                                env=env, capture_output=True, text=True, check=False)
        if result.returncode:
            raise StagingError('source Git identity could not be read')
        return result.stdout

    head = read('rev-parse', 'HEAD').strip()
    if head != commit:
        raise StagingError('source HEAD does not match the expected commit')
    if read('status', '--porcelain=v1', '--untracked-files=normal', '-z'):
        raise StagingError('source checkout must be clean before and after staging')
    for name in exclude_roots:
        if read('ls-files', '-z', '--', name):
            raise StagingError(f'cannot exclude a root containing tracked source: {name}')
    try:
        build = json.loads((source / 'dist/build-info.json').read_text())
    except (OSError, ValueError) as exc:
        raise StagingError('completed dist/build-info.json is unavailable') from exc
    if not isinstance(build, dict) or build.get('commit') != commit:
        raise StagingError('build-info commit does not match the expected source commit')
    return {'head': head, 'tree': read('rev-parse', 'HEAD^{tree}').strip(),
            'buildInfoSha256': sha256((source / 'dist/build-info.json').read_bytes())}


def snapshot(root: Path, exclude_roots: tuple[str, ...] = ()) -> list[dict]:
    """Read the included tree without following symlink directories."""
    rows = []
    def inaccessible(error: OSError) -> None:
        raise StagingError('included source or candidate subtree could not be enumerated') from error

    for directory, directories, files in os.walk(root, followlinks=False, onerror=inaccessible):
        excluded = EXCLUDED_NAMES | (set(exclude_roots) if Path(directory) == root else set())
        directories[:] = sorted(name for name in directories if name not in excluded)
        for name in sorted(directories + files):
            if name in excluded or name.endswith('.tsbuildinfo'):
                continue
            path = Path(directory) / name
            info = path.lstat()
            row = {'path': path.relative_to(root).as_posix(), 'mode': stat.S_IMODE(info.st_mode)}
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                if Path(target).is_absolute():
                    raise StagingError(f'absolute symlink is not a relocatable internal link: {row["path"]}')
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise StagingError(f'dangling or cyclic symlink: {row["path"]}') from exc
                if not resolved.is_relative_to(root):
                    raise StagingError(f'symlink escapes the release tree: {row["path"]}')
                row.update(type='symlink', target=target)
            elif stat.S_ISDIR(info.st_mode):
                row.update(type='directory')
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                after = path.lstat()
                fields = ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
                if any(getattr(info, key) != getattr(after, key) for key in fields):
                    raise StagingError(f'file changed while reading: {row["path"]}')
                row.update(type='file', bytes=info.st_size, sha256=digest.hexdigest(),
                           device=info.st_dev, inode=info.st_ino, links=info.st_nlink,
                           mtimeNs=info.st_mtime_ns, ctimeNs=info.st_ctime_ns)
            else:
                raise StagingError(f'unsupported filesystem member: {row["path"]}')
            rows.append(row)
    return sorted(rows, key=lambda row: row['path'])


def require_artifacts(root: Path) -> None:
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            raise StagingError(f'required build artifact is absent: {name}')
    for name in REQUIRED_DIRECTORIES:
        if not (root / name).is_dir():
            raise StagingError(f'required build directory is absent: {name}')


def write_receipt(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix='.release-staging-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Exclusive publication; never replaces a receipt.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def stage(source: Path, candidate: Path, commit: str, receipt: Path,
          *, exclude_roots: tuple[str, ...] = ()) -> dict:
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise StagingError('expected source commit must be 40 lowercase hexadecimal characters')
    if any(not isinstance(name, str) for name in exclude_roots):
        raise StagingError('excluded root must be a simple string basename')
    exclude_roots = tuple(sorted(set(exclude_roots)))
    for name in exclude_roots:
        if (not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9._-]+', name) is None
                or name in {'.', '..'} or name in PROTECTED_ROOTS):
            raise StagingError('excluded root must be a simple basename outside protected build/source roots')
    physical_path(source)
    physical_path(candidate, new=True)
    physical_path(receipt, new=True)
    if candidate.is_relative_to(source) or receipt.is_relative_to(source) or receipt.is_relative_to(candidate):
        raise StagingError('candidate and receipt must be outside source; receipt must be outside candidate')
    if receipt == candidate:
        raise StagingError('candidate and receipt must be distinct')
    started = now()
    identity = git_identity(source, commit, exclude_roots)
    require_artifacts(source)
    before = snapshot(source, exclude_roots)
    candidate.mkdir(mode=0o700)
    relocated = []
    try:
        # copy2 creates independent regular files even when source dependencies are hardlinked.
        default_ignore = shutil.ignore_patterns(*sorted(EXCLUDED_NAMES), '*.tsbuildinfo')

        def ignored(directory, names):
            return default_ignore(directory, names) | (set(exclude_roots) if Path(directory) == source else set())

        shutil.copytree(source, candidate, symlinks=True, dirs_exist_ok=True, ignore=ignored)
        for row in before:
            relative = Path(row['path'])
            if row['type'] != 'file' or relative.parent.name != '.bin' or relative.parent.parent.name != 'node_modules':
                continue
            target = candidate / relative
            data = target.read_bytes()
            source_prefix = str(source).encode() + os.sep.encode()
            if source_prefix not in data:
                continue
            # pnpm's generated shell shims are text executables. A matching byte
            # sequence in an arbitrary binary is not a relocatable shim.
            if not data.startswith(b'#!') or b'\0' in data or not row['mode'] & 0o111:
                raise StagingError(f'source path occurs in an unsupported .bin member: {relative}')
            changed = data.replace(source_prefix, str(candidate).encode() + os.sep.encode())
            target.chmod(stat.S_IMODE(target.stat().st_mode) | 0o200)
            target.write_bytes(changed)
            target.chmod(row['mode'])
            relocated.append({'path': row['path'], 'beforeSha256': sha256(data), 'afterSha256': sha256(changed)})
        require_artifacts(candidate)
        copied = snapshot(candidate)
        indexed = {row['path']: row for row in before}
        changed_hashes = {row['path']: row['afterSha256'] for row in relocated}
        if {row['path'] for row in copied} != set(indexed):
            raise StagingError('copied tree membership differs from the included source tree')
        for row in copied:
            old = indexed[row['path']]
            if row['type'] != old['type'] or row['mode'] != old['mode']:
                raise StagingError(f'copied member type or mode changed: {row["path"]}')
            if row['type'] == 'file':
                if row['links'] != 1 or (row['device'], row['inode']) == (old['device'], old['inode']):
                    raise StagingError(f'candidate reuses a source/shared file identity: {row["path"]}')
                if row['sha256'] != changed_hashes.get(row['path'], old['sha256']):
                    raise StagingError(f'copied file differs from its source: {row["path"]}')
            elif row['type'] == 'symlink' and row['target'] != old['target']:
                raise StagingError(f'copied symlink changed: {row["path"]}')
        if snapshot(source, exclude_roots) != before or git_identity(source, commit, exclude_roots) != identity:
            raise StagingError('source changed during staging')
        for row in reversed(copied):
            if row['type'] != 'symlink':
                (candidate / row['path']).chmod(row['mode'] & ~0o222)
        candidate.chmod(0o555)
        final = snapshot(candidate)
        copied_index = {row['path']: row for row in copied}
        if {row['path'] for row in final} != set(copied_index):
            raise StagingError('candidate membership changed while making it read-only')
        for row in final:
            previous = copied_index[row['path']]
            if row['type'] != previous['type']:
                raise StagingError('candidate member type changed while making it read-only')
            if row['type'] == 'symlink':
                if row['target'] != previous['target']:
                    raise StagingError('candidate symlink changed while making it read-only')
            elif row['mode'] != previous['mode'] & ~0o222:
                raise StagingError('candidate read-only modes do not match the copied files')
            if row['type'] == 'file' and (row['links'] != 1 or any(
                    row[key] != previous[key] for key in ('device', 'inode', 'bytes', 'sha256'))):
                raise StagingError('candidate file identity/content changed while making it read-only')
        value = {'schemaVersion': 1, 'status': 'staged-readonly', 'startedAt': started,
                 'completedAt': now(), 'source': str(source), 'candidate': str(candidate),
                 'commit': commit, 'sourceIdentity': identity,
                 'exclusions': sorted(EXCLUDED_NAMES) + ['*.tsbuildinfo'],
                 'excludedTopLevelRoots': list(exclude_roots),
                 'sourceInventorySha256': sha256(json.dumps(before, sort_keys=True).encode()),
                 'requiredArtifacts': list(REQUIRED_FILES), 'relocatedShims': relocated,
                 'regularFileCount': sum(row['type'] == 'file' for row in final), 'files': final,
                 'sourceUnchanged': True, 'sourceHardlinksReused': False,
                 'candidateExecuted': False, 'candidateSealed': False, 'liveStateChanged': False}
        write_receipt(receipt, value)
        return value
    except Exception as exc:
        # Keep this exclusively created candidate for diagnosis; do not remove
        # files or conceal partial staging behind an automatic retry/overwrite.
        raise StagingError(f'staging incomplete; inspect candidate {candidate}: {exc}') from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--source-commit', required=True)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--exclude-root', action='append', default=[], metavar='NAME',
                        help='Exclude one explicitly identified regenerable top-level cache; repeat as needed')
    args = parser.parse_args(argv)
    try:
        value = stage(args.source, args.candidate, args.source_commit, args.receipt,
                      exclude_roots=tuple(args.exclude_root))
        print(json.dumps({key: value[key] for key in ('status', 'candidate', 'regularFileCount', 'candidateSealed')}))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
