from __future__ import annotations

import base64
from contextlib import contextmanager, ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from scripts import developer_cache_formats as formats
from scripts import openclaw_storage_prune as prune


NOW = 1790726400  # 2026-09-30 UTC
OLD = NOW - 48 * 3600
MODULE = 'a' * 40


def pnpm_bytes(package='fixture', native=False):
    headers = json.dumps({'etag': 'fixture', 'modified': '2026-09-15T00:00:00Z'}).encode()
    version = {'name': package, 'version': '1.0.0'}
    if not native:
        return headers + b'\n' + json.dumps({'name': package, 'dist-tags': {'latest': '1.0.0'},
                                            'versions': {'1.0.0': version}}).encode()
    record = json.dumps(version).encode()
    summary = json.dumps({'name': package, 'distTags': {'latest': '1.0.0'},
                          'versions': [['1.0.0', 0, len(record)]]}).encode()
    return (f'pacquet-meta-v1 {len(headers)} {len(summary)}\n'.encode()
            + headers + summary + record)


def vitest_bytes(static_mocks=False):
    pool = [{'file': '1', 'id': '1', 'url': '2', 'importedUrls': '3', 'mappings': False,
             'deps': '3', 'dynamicDeps': '3', 'staticMocks': None}, '/fixture.ts', '/@fs/fixture.ts', []]
    if static_mocks:
        pool[0]['staticMocks'] = '4'
        pool += [['5'], {'method': '6', 'specifier': '7', 'hasFactory': True,
                        'factoryLoadsOriginal': False}, 'mock', './dependency']
    return b'export const fixture = true;' + formats.TRAILER + base64.b64encode(json.dumps(pool).encode())


class DeveloperCacheRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def cache(self, kind=formats.VITEST_KIND, *, layout='metadata', project=False, native=False,
              registry='registry.npmjs.org'):
        if kind == formats.PNPM_KIND:
            root = self.home / 'Library/Caches/pnpm/v11' / layout / registry
            leaf = root / ('@scope/fixture.jsonl' if native else 'fixture.jsonl')
            leaf.parent.mkdir(parents=True)
            leaf.write_bytes(pnpm_bytes('@scope/fixture' if native else 'fixture', native))
        else:
            root = self.home / '.cache/openclaw-vitest-fixture-20260915'
            root.mkdir(parents=True)
            (root / '_metadata.json').write_text('{"lockfileHash":"892c7ca7"}')
            target = root / '0-test-vitest-vitest.agents-core.config.ts' if project else root
            if project:
                target.mkdir()
                (target / '_metadata.json').write_text('{"lockfileHash":"892c7ca7"}')
            leaf = target / MODULE
            leaf.write_bytes(vitest_bytes(static_mocks=True))
        self.age(root)
        return root, leaf

    def age(self, root):
        for path in [*root.rglob('*'), root]:
            if not path.is_symlink():
                os.utime(path, (OLD, OLD))

    @contextmanager
    def owner(self, commands='1 /sbin/launchd', activity=None):
        original_is_dir = Path.is_dir
        def isolated_is_dir(path):
            return original_is_dir(path) if path == self.home or self.home in path.parents else False
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(prune, 'USER_HOME', self.home))
            stack.enter_context(mock.patch.object(prune.time, 'time', return_value=NOW))
            stack.enter_context(mock.patch.object(Path, 'is_dir', isolated_is_dir))
            stack.enter_context(mock.patch.object(Path, 'is_mount', return_value=False))
            stack.enter_context(mock.patch.object(prune, 'xcode_candidates', return_value=[]))
            stack.enter_context(mock.patch.object(prune, 'process_arguments', return_value=commands))
            guard = stack.enter_context(mock.patch.object(prune, 'activity_reason', side_effect=activity,
                                                         return_value=None))
            yield guard

    def discovered(self):
        with self.owner():
            candidates, skipped = prune.discover()
        self.assertEqual(skipped, [])
        self.assertEqual(len(candidates), 1)
        return candidates

    def test_existing_owner_discovers_and_removes_all_audited_layouts(self):
        for index, (kind, layout, project, native) in enumerate([
            (formats.PNPM_KIND, 'metadata', False, False),
            (formats.PNPM_KIND, 'metadata-full', False, True),
            (formats.PNPM_KIND, 'metadata-full-filtered', False, False),
            (formats.VITEST_KIND, '', False, False),
            (formats.VITEST_KIND, '', True, False),
        ]):
            with self.subTest(kind=kind, layout=layout, project=project):
                self.home = self.home / str(index)
                root, leaf = self.cache(kind, layout=layout, project=project, native=native)
                with self.owner() as activity:
                    candidates, skipped = prune.discover()
                    self.assertEqual(skipped, [])
                    self.assertEqual(len(candidates), 1)
                    self.assertEqual(candidates[0].kind, kind)
                    self.assertEqual(candidates[0].min_age_days, 2)
                    removed, deferred = prune.apply_candidates(candidates)
                self.assertEqual(deferred, [])
                self.assertEqual(len(removed), 1)
                self.assertTrue(removed[0]['captured_inode_removed'])
                self.assertFalse(root.exists())
                self.assertFalse(leaf.exists())
                self.assertEqual(activity.call_count, 2)
                self.assertEqual(activity.call_args_list[0].args[0], root)
                self.assertTrue(activity.call_args_list[1].kwargs['ignore_current_process'])

    def test_encoded_public_pnpm_is_discovered_and_removed_by_existing_owner(self):
        for layout in formats.PNPM_LAYOUTS:
            with self.subTest(layout=layout):
                root, leaf = self.cache(formats.PNPM_KIND, layout=layout, native=True,
                                        registry='https%3A+registry.npmjs.org')
                with self.owner() as activity:
                    candidates, skipped = prune.discover()
                    self.assertEqual(skipped, [])
                    self.assertEqual(len(candidates), 1)
                    self.assertEqual(candidates[0].path, str(root))
                    self.assertEqual(candidates[0].kind, formats.PNPM_KIND)
                    self.assertEqual(candidates[0].min_age_days, 2)
                    removed, deferred = prune.apply_candidates(candidates)
                self.assertEqual(deferred, [])
                self.assertEqual(len(removed), 1)
                self.assertTrue(removed[0]['captured_inode_removed'])
                self.assertFalse(root.exists())
                self.assertFalse(leaf.exists())
                self.assertEqual(activity.call_count, 2)
                self.assertEqual(activity.call_args_list[0].args[0], root)
                self.assertTrue(activity.call_args_list[1].kwargs['ignore_current_process'])

    def test_root_and_each_leaf_must_be_at_least_48_hours_old(self):
        for kind, registry, native in ((formats.VITEST_KIND, 'registry.npmjs.org', False),
                                      (formats.PNPM_KIND, 'https%3A+registry.npmjs.org', True)):
            with self.subTest(kind=kind):
                root, leaf = self.cache(kind, registry=registry, native=native)
                for recent in (root, leaf):
                    self.age(root)
                    os.utime(recent, (OLD + 1, OLD + 1))
                    with self.owner():
                        candidates, skipped = prune.discover()
                    self.assertEqual(candidates, [])
                    self.assertIn('recent', skipped[0]['reason'])
                    self.assertTrue(leaf.exists())
                self.age(root)
                self.assertEqual(len(self.discovered()), 1)
                # Keep the next subcase isolated from this accepted cache.
                self.home = self.home / kind

    def test_other_encoded_registries_are_not_discovered_or_classified(self):
        for registry in ('https%3A+private.example', 'https%3A+registry.npmjs.org.evil',
                         'http%3A+registry.npmjs.org', 'https%3A+registry.npmjs.org+443'):
            with self.subTest(registry=registry):
                root, leaf = self.cache(formats.PNPM_KIND, registry=registry, native=True)
                self.assertNotIn((root, formats.PNPM_KIND), formats.candidates(self.home))
                with self.assertRaisesRegex(ValueError, 'unclassified public pnpm metadata path'):
                    formats.classify(root, formats.PNPM_KIND, self.home, OLD)
                with self.owner():
                    candidates, _ = prune.discover()
                self.assertEqual(candidates, [])
                self.assertEqual(leaf.read_bytes(), pnpm_bytes('@scope/fixture', native=True))

    def test_exact_public_namespace_excludes_stores_private_registry_and_unknown_generations(self):
        for relative in ('Library/Caches/pnpm/v11/metadata/private.example',
                         'Library/pnpm/store/v11/metadata/registry.npmjs.org',
                         '.cache/vitest', '.cache/openclaw-vitest-undated'):
            root = self.home / relative
            root.mkdir(parents=True)
            (root / 'user-work').write_text('keep')
        with self.owner():
            candidates, _ = prune.discover()
        self.assertEqual(candidates, [])

    def test_rejects_recent_or_invalid_generation_date(self):
        root, leaf = self.cache()
        for name in ('openclaw-vitest-fixture-20260930', 'openclaw-vitest-fixture-20260230'):
            replacement = root.with_name(name)
            root.rename(replacement)
            root, leaf = replacement, replacement / MODULE
            with self.owner():
                candidates, skipped = prune.discover()
            self.assertEqual(candidates, [])
            self.assertTrue(skipped)
            self.assertTrue(leaf.exists())

    def test_unknown_layout_links_and_missing_metadata_preserve_entire_tree(self):
        root, leaf = self.cache()
        for kind in ('unknown', 'directory', 'symlink', 'hardlink', 'metadata'):
            with self.subTest(kind=kind):
                unknown = root / ('b' * 40 if kind in ('symlink', 'hardlink') else 'user-work')
                if kind == 'unknown':
                    unknown.write_text('keep')
                elif kind == 'directory':
                    unknown.mkdir()
                elif kind == 'symlink':
                    unknown.symlink_to(leaf)
                elif kind == 'hardlink':
                    os.link(leaf, unknown)
                else:
                    (root / '_metadata.json').rename(self.home / 'saved-metadata')
                self.age(root)
                with self.owner():
                    candidates, skipped = prune.discover()
                self.assertEqual(candidates, [])
                self.assertTrue(skipped)
                self.assertTrue(leaf.exists())
                if kind == 'directory':
                    unknown.rmdir()
                elif kind == 'metadata':
                    (self.home / 'saved-metadata').rename(root / '_metadata.json')
                else:
                    unknown.unlink()

    def test_unknown_pnpm_headers_shapes_names_and_trailing_native_data_are_preserved(self):
        root, leaf = self.cache(formats.PNPM_KIND)
        for data in (b'{"unknown":"header"}\n{}', pnpm_bytes('other'),
                     b'{}\n{"name":"fixture","versions":{},"dist-tags":{}}',
                     pnpm_bytes(native=True) + b'unclassified',
                     b'{"etag":"one","etag":"two"}\n' + pnpm_bytes().partition(b'\n')[2]):
            leaf.write_bytes(data)
            self.age(root)
            with self.owner():
                candidates, skipped = prune.discover()
            self.assertEqual(candidates, [])
            self.assertTrue(skipped)
            self.assertEqual(leaf.read_bytes(), data)

    def test_invalid_vitest_base64_pool_and_metadata_are_preserved(self):
        root, leaf = self.cache()
        for data in (b'generated without trailer', b'code' + formats.TRAILER + b'not-base64',
                     b'code' + formats.TRAILER + base64.b64encode(b'{"id":"fixture"}'),
                     b'code' + formats.TRAILER + base64.b64encode(b'[{"id":"9","url":"9","importedUrls":[],"mappings":false}]')):
            leaf.write_bytes(data)
            self.age(root)
            with self.owner():
                candidates, skipped = prune.discover()
            self.assertEqual(candidates, [])
            self.assertTrue(skipped)
        leaf.write_bytes(vitest_bytes())
        (root / '_metadata.json').write_text('{"lockfileHash":"892c7ca7","unknown":true}')
        self.age(root)
        with self.owner():
            candidates, skipped = prune.discover()
        self.assertEqual(candidates, [])
        self.assertTrue(skipped)

    def test_tree_size_and_deadline_fail_closed(self):
        root, leaf = self.cache()
        with self.owner(), mock.patch.object(formats, 'MAX_ENTRIES', 2):
            candidates, skipped = prune.discover()
        self.assertEqual(candidates, [])
        self.assertIn('budget', skipped[0]['reason'])
        with self.owner():
            candidates, skipped = prune.discover(deadline=time.monotonic() - 1)
        self.assertEqual(candidates, [])
        self.assertTrue(skipped[0]['error'])
        self.assertTrue(leaf.exists())

    def test_producers_guard_discovery_before_inventory(self):
        for kind, command in ((formats.PNPM_KIND, '42 /cache/pnpm-native install'),
                              (formats.VITEST_KIND, '42 node /checkout/node_modules/vitest/vitest.mjs run'),
                              (formats.VITEST_KIND, '42 node (vitest 1)')):
            self.home = self.home / kind
            root, leaf = self.cache(kind)
            with self.owner(command), mock.patch.object(formats, 'tree_facts') as inventory:
                candidates, skipped = prune.discover()
            self.assertEqual(candidates, [])
            self.assertIn('producer', skipped[0]['reason'])
            inventory.assert_not_called()
            self.assertTrue(leaf.exists())
        self.assertIsNone(formats.producer_reason(formats.PNPM_KIND,
                          '42 node /checkout/node_modules/.pnpm/package/index.js'))

    def test_producer_starting_after_capture_restores_without_removal(self):
        root, leaf = self.cache()
        candidates = self.discovered()
        with self.owner(), mock.patch.object(prune, 'process_arguments', side_effect=[
            '1 /sbin/launchd', '42 node /checkout/node_modules/vitest/vitest.mjs run']):
            removed, skipped = prune.apply_candidates(candidates)
        self.assertEqual(removed, [])
        self.assertIn('producer', skipped[0]['reason'])
        self.assertFalse(skipped[0]['partially_removed'])
        self.assertTrue(leaf.exists())

    def test_privileged_open_handle_refusal_preserves_cache(self):
        root, leaf = self.cache()
        candidates = self.discovered()
        for reason in ('open handles', 'open-handle inspection unavailable'):
            with self.owner(activity=lambda *a, **k: reason):
                removed, skipped = prune.apply_candidates(candidates)
            self.assertEqual(removed, [])
            self.assertEqual(skipped[0]['reason'], reason)
            self.assertTrue(leaf.exists())

    def test_content_change_with_restored_mtime_is_bound_across_discovery(self):
        root, leaf = self.cache()
        candidates = self.discovered()
        leaf.write_bytes(vitest_bytes().replace(b'true;', b'null;'))
        os.utime(leaf, (OLD, OLD))
        with self.owner():
            removed, skipped = prune.apply_candidates(candidates)
        self.assertEqual(removed, [])
        self.assertIn('changed after discovery', skipped[0]['reason'])
        self.assertFalse(skipped[0]['partially_removed'])
        self.assertIn(b'null;', leaf.read_bytes())

    def test_late_unclassified_child_is_restored_and_never_deleted(self):
        root, leaf = self.cache()
        candidates = self.discovered()
        def publish(path, *args, **kwargs):
            if kwargs.get('ignore_current_process'):
                (path / 'user-work').write_text('keep')
                os.utime(path / 'user-work', (OLD, OLD))
                os.utime(path, (OLD, OLD))
        with self.owner(activity=publish):
            removed, skipped = prune.apply_candidates(candidates)
        self.assertEqual(removed, [])
        self.assertTrue(skipped)
        self.assertEqual((root / 'user-work').read_text(), 'keep')

    def test_leaf_change_inside_final_format_validator_is_preserved(self):
        root, leaf = self.cache()
        candidates = self.discovered()
        original = formats.validate_leaf
        inspected, captured_roots = [], []
        def remember_capture(path, *args, **kwargs):
            if kwargs.get('ignore_current_process'):
                captured_roots.append(path)
        def mutate(fd, kind, relative, deadline):
            original(fd, kind, relative, deadline)
            inspected.append(relative)
            if relative == '_metadata.json' and inspected.count(relative) == 2:
                # First read is the root inventory; second is the captured leaf
                # immediately before unlink. Same-size valid JSON still changed.
                current = next(captured_roots[0].glob(prune.QUARANTINE_PREFIX + '*/payload'))
                current.write_bytes(current.read_bytes().replace(b'892c7ca7', b'892c7ca8'))
                os.utime(current, (OLD, OLD))
        with self.owner(activity=remember_capture), mock.patch.object(formats, 'validate_leaf', side_effect=mutate):
            removed, skipped = prune.apply_candidates(candidates)
        self.assertEqual(removed, [])
        self.assertIn('changed before removal', skipped[0]['reason'])
        self.assertEqual((root / '_metadata.json').read_text(), '{"lockfileHash":"892c7ca8"}')

    def test_public_replacement_is_outside_selected_capture(self):
        root, leaf = self.cache()
        candidates = self.discovered()
        def publish(path, *args, **kwargs):
            if kwargs.get('ignore_current_process'):
                root.mkdir()
                (root / 'user-work').write_text('keep')
        with self.owner(activity=publish):
            removed, skipped = prune.apply_candidates(candidates)
        self.assertEqual(skipped, [])
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0]['replacement_present'])
        self.assertEqual((root / 'user-work').read_text(), 'keep')

    def test_forged_kind_or_retention_cannot_expand_deletion_authority(self):
        root, leaf = self.cache()
        candidate = self.discovered()[0]
        for changed in (replace(candidate, min_age_days=0),
                        replace(candidate, activity_root=str(self.home)),
                        replace(candidate, kind=formats.PNPM_KIND)):
            with self.owner():
                removed, skipped = prune.apply_candidates([changed])
            self.assertEqual(removed, [])
            self.assertTrue(skipped)
            self.assertTrue(leaf.exists())


if __name__ == '__main__':
    unittest.main()
