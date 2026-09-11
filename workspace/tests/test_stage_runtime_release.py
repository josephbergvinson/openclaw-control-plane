from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

import pytest
from scripts import stage_runtime_release as staging


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test',
         '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def built_source(tmp_path: Path):
    source = tmp_path / 'source build'
    source.mkdir()
    (source / '.gitignore').write_text('dist/\ndist-runtime/\npackages/\nnode_modules/\n.cache/\n*.tsbuildinfo\n')
    (source / 'openclaw.mjs').write_text('// Fixture entrypoint, never executed by staging.\n')
    (source / 'package.json').write_text('{"name":"fixture-runtime","version":"2026.9.3"}\n')
    git(source, 'init')
    git(source, 'add', '.')
    git(source, 'commit', '-m', 'fixture source')
    commit = git(source, 'rev-parse', 'HEAD')
    for name in staging.REQUIRED_FILES:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text('{}\n' if target.suffix == '.json' else 'fixture artifact\n')
    for name in staging.REQUIRED_DIRECTORIES:
        (source / name).mkdir(parents=True, exist_ok=True)
    (source / 'dist/build-info.json').write_text(json.dumps({'commit': commit}))
    bins = source / 'node_modules/.bin'
    bins.mkdir()
    tool = source / 'packages/ai/dist/tool.sh'
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "$0"\n')
    tool.chmod(0o755)
    shim = bins / 'fixture-tool'
    shim.write_text(f'#!/bin/sh\nexec "{tool}" "$@"\n')
    shim.chmod(0o555)
    (source / 'node_modules/fixture-ai').symlink_to('../packages/ai/dist', target_is_directory=True)
    (source / '.cache').mkdir()
    (source / '.cache/not-shipped').write_text('cache only')
    (source / 'ignored.tsbuildinfo').write_text('incremental cache')
    yield source, commit
    # Fixture-owned read-only candidates need writable parents for pytest cleanup.
    for directory, dirs, files in os.walk(tmp_path, followlinks=False):
        path = Path(directory)
        path.chmod(0o700)
        for name in files:
            item = path / name
            if not item.is_symlink():
                item.chmod(stat.S_IMODE(item.stat().st_mode) | 0o600)


def run_stage(source: Path, commit: str, tmp_path: Path):
    return staging.stage(source, tmp_path / 'candidate release', commit, tmp_path / 'staging.json')


def test_relocates_executable_shim_and_preserves_source_and_internal_links(built_source, tmp_path: Path) -> None:
    source, commit = built_source
    # pnpm store links may exist in the source; staging must produce new identities.
    original = source / 'node_modules/shared-content'
    original.write_text('hardlinked source dependency')
    os.link(original, source / 'node_modules/shared-alias')
    source_before = staging.snapshot(source)
    result = run_stage(source, commit, tmp_path)
    candidate = Path(result['candidate'])
    output = subprocess.run([str(candidate / 'node_modules/.bin/fixture-tool')],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert output == str(candidate / 'packages/ai/dist/tool.sh')
    assert os.readlink(candidate / 'node_modules/fixture-ai') == '../packages/ai/dist'
    assert staging.snapshot(source) == source_before
    assert original.stat().st_nlink == 2
    assert (candidate / 'node_modules/shared-content').stat().st_nlink == 1
    assert (candidate / 'node_modules/shared-alias').stat().st_nlink == 1
    assert not (candidate / '.git').exists()
    assert not (candidate / '.cache').exists()
    assert not (candidate / 'ignored.tsbuildinfo').exists()
    assert result['relocatedShims'][0]['path'] == 'node_modules/.bin/fixture-tool'
    assert result['sourceIdentity']['head'] == commit
    assert result['candidateSealed'] is False and result['candidateExecuted'] is False
    assert json.loads((tmp_path / 'staging.json').read_text()) == result
    assert not candidate.stat().st_mode & 0o222
    assert all(not row['mode'] & 0o222 for row in result['files'] if row['type'] != 'symlink')


@pytest.mark.parametrize('kind', ['escaping', 'absolute', 'dangling', 'cycle'])
def test_nonportable_links_fail_before_creating_candidate(built_source, tmp_path: Path, kind: str) -> None:
    source, commit = built_source
    outside = tmp_path / 'outside'
    outside.write_text('not part of release')
    targets = {'escaping': '../../outside', 'absolute': str(source / 'packages/ai/dist/tool.sh'),
               'dangling': 'missing', 'cycle': 'bad-link'}
    (source / 'node_modules/bad-link').symlink_to(targets[kind])
    with pytest.raises(staging.StagingError, match='symlink'):
        run_stage(source, commit, tmp_path)
    assert not (tmp_path / 'candidate release').exists()
    assert not (tmp_path / 'staging.json').exists()


@pytest.mark.parametrize('name', ['candidate release', 'staging.json'])
def test_existing_destinations_are_never_overwritten(built_source, tmp_path: Path, name: str) -> None:
    source, commit = built_source
    existing = tmp_path / name
    existing.write_text('preserve this exact content')
    with pytest.raises(staging.StagingError, match='already exists'):
        run_stage(source, commit, tmp_path)
    assert existing.read_text() == 'preserve this exact content'


@pytest.mark.parametrize('failure', ['head', 'build-info', 'dirty', 'artifact'])
def test_requires_the_exact_clean_completed_build(built_source, tmp_path: Path, failure: str) -> None:
    source, commit = built_source
    if failure == 'head':
        commit = 'a' * 40
    elif failure == 'build-info':
        (source / 'dist/build-info.json').write_text(json.dumps({'commit': 'a' * 40}))
    elif failure == 'dirty':
        (source / 'openclaw.mjs').write_text('uncommitted change')
    else:
        (source / 'dist/control-ui/index.html').unlink()
    with pytest.raises(staging.StagingError):
        run_stage(source, commit, tmp_path)
    assert not (tmp_path / 'candidate release').exists()


def test_source_change_during_copy_cannot_publish_success(built_source, tmp_path: Path, monkeypatch) -> None:
    source, commit = built_source
    real_copy = staging.shutil.copytree

    def changed_source(*args, **kwargs):
        result = real_copy(*args, **kwargs)
        (source / 'dist/build-info.json').write_text(json.dumps({'commit': commit, 'changed': True}))
        return result

    monkeypatch.setattr(staging.shutil, 'copytree', changed_source)
    with pytest.raises(staging.StagingError, match='source changed'):
        run_stage(source, commit, tmp_path)
    assert not (tmp_path / 'staging.json').exists()


def test_only_supported_generated_shims_are_rewritten(built_source, tmp_path: Path) -> None:
    source, commit = built_source
    binary = source / 'node_modules/.bin/not-a-shell-shim'
    binary.write_bytes(b'\x00binary ' + str(source).encode() + b'/bin/tool')
    binary.chmod(0o755)
    with pytest.raises(staging.StagingError, match='unsupported .bin member'):
        run_stage(source, commit, tmp_path)
    assert binary.read_bytes().startswith(b'\x00binary ')
    assert not (tmp_path / 'staging.json').exists()


@pytest.mark.parametrize('which', ['source', 'candidate', 'receipt'])
def test_symlinked_path_ancestry_cannot_redirect_staging(built_source, tmp_path: Path, which: str) -> None:
    source, commit = built_source
    alias = tmp_path / 'alias'
    alias.symlink_to(source if which == 'source' else tmp_path, target_is_directory=True)
    with pytest.raises(staging.StagingError, match='physical'):
        staging.stage(alias if which == 'source' else source,
                      alias / 'candidate' if which == 'candidate' else tmp_path / 'candidate',
                      commit, alias / 'receipt.json' if which == 'receipt' else tmp_path / 'receipt.json')
    assert not (tmp_path / 'candidate').exists()


def test_explicit_verification_cache_exclusion_is_top_level_and_recorded(built_source, tmp_path: Path) -> None:
    source, commit = built_source
    (source / '.git/info/exclude').write_text('.reference-build/\n')
    (source / '.reference-build').mkdir()
    (source / '.reference-build/private-cache').write_text('regenerable verification cache')
    (source / 'packages/ai/dist/.reference-build').mkdir()
    (source / 'packages/ai/dist/.reference-build/retained').write_text('nested name is not the selected root')
    result = staging.stage(source, tmp_path / 'candidate', commit, tmp_path / 'receipt.json',
                           exclude_roots=('.reference-build',))
    assert result['excludedTopLevelRoots'] == ['.reference-build']
    assert not (tmp_path / 'candidate/.reference-build').exists()
    assert (tmp_path / 'candidate/packages/ai/dist/.reference-build/retained').is_file()


@pytest.mark.parametrize('excluded', ['.', '..', '../outside', 'cache/nested', '/absolute',
                                    'dist', 'node_modules', 'packages', 'openclaw.mjs', '.gitignore'])
def test_exclusions_cannot_remove_protected_or_tracked_source(built_source, tmp_path: Path, excluded: str) -> None:
    source, commit = built_source
    with pytest.raises(staging.StagingError, match='exclude|excluded'):
        staging.stage(source, tmp_path / 'candidate', commit, tmp_path / 'receipt.json', exclude_roots=(excluded,))
    assert not (tmp_path / 'candidate').exists()


def test_source_prefix_does_not_rewrite_another_directory_name(built_source, tmp_path: Path) -> None:
    source, commit = built_source
    shim = source / 'node_modules/.bin/fixture-tool'
    shim.chmod(0o755)
    shim.write_text(shim.read_text() + f'# unrelated {source}-other/bin/tool\n')
    result = run_stage(source, commit, tmp_path)
    copied = (Path(result['candidate']) / 'node_modules/.bin/fixture-tool').read_text()
    assert f'{source}-other/bin/tool' in copied
    assert f'{source}/packages/ai/dist/tool.sh' not in copied


def test_unreadable_subtree_is_never_silently_omitted(built_source, tmp_path: Path, monkeypatch) -> None:
    source, commit = built_source
    real_walk = staging.os.walk

    def inaccessible(root, **kwargs):
        kwargs['onerror'](PermissionError('fixture scandir failure'))
        yield from real_walk(root, **kwargs)

    monkeypatch.setattr(staging.os, 'walk', inaccessible)
    with pytest.raises(staging.StagingError, match='could not be enumerated'):
        run_stage(source, commit, tmp_path)
    assert not (tmp_path / 'candidate release').exists()
