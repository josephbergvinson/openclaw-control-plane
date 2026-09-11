from __future__ import annotations
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from scripts import openclaw_storage_prune as prune


class CapturedLeafTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.leaf = self.root / 'selected'
        self.leaf.write_bytes(b'reviewed content')
        old = time.time() - 10 * 86400
        os.utime(self.leaf, (old, old))
        self.cutoff = time.time() - 86400
        self.device = self.leaf.stat().st_dev

    def capture(self):
        value = self.leaf.lstat()
        return prune.capture_entry(self.leaf, prune.file_identity(value), directory=False)

    def test_removes_only_selected_leaf_after_descriptor_validation(self):
        unknown = self.root / 'unclassified'
        unknown.write_bytes(b'unknown sibling stays')
        unknown_stat = prune.entry_fingerprint(unknown.stat())
        validated = []
        with self.capture() as captured:
            opened = os.fstat(captured.fd)
            def validate(current):
                validated.append(os.read(current.fd, 1024))
                self.assertFalse(self.leaf.exists())
                self.assertTrue(current.path.exists())
            self.assertTrue(prune.remove_captured_leaf(captured, opened, self.cutoff,
                                                       self.device, validator=validate))
            self.assertTrue(captured.removed)
            self.assertEqual(captured.deleted_entries, 1)
        self.assertEqual(validated, [b'reviewed content'])
        self.assertFalse(self.leaf.exists())
        self.assertEqual(unknown.read_bytes(), b'unknown sibling stays')
        self.assertEqual(prune.entry_fingerprint(unknown.stat()), unknown_stat)
        self.assertEqual(list(self.root.iterdir()), [unknown])

    def test_validator_refusal_restores_captured_content(self):
        def refuse(_):
            raise ValueError('content validation refused')
        with self.assertRaisesRegex(ValueError, 'content validation refused'):
            with self.capture() as captured:
                prune.remove_captured_leaf(captured, os.fstat(captured.fd), self.cutoff,
                                           self.device, validator=refuse)
        self.assertFalse(captured.removed)
        self.assertEqual(captured.deleted_entries, 0)
        self.assertEqual(self.leaf.read_bytes(), b'reviewed content')
        self.assertEqual(list(self.root.iterdir()), [self.leaf])

    def test_validator_same_size_content_mutation_with_restored_mtime_is_preserved(self):
        with self.assertRaisesRegex(ValueError, 'captured leaf changed before removal'):
            with self.capture() as captured:
                opened = os.fstat(captured.fd)
                def mutate(current):
                    current.path.write_bytes(b'changed! content')
                    os.utime(current.path, ns=(opened.st_atime_ns, opened.st_mtime_ns))
                    changed = os.fstat(current.fd)
                    self.assertEqual(changed.st_size, opened.st_size)
                    self.assertEqual(changed.st_mtime_ns, opened.st_mtime_ns)
                prune.remove_captured_leaf(captured, opened, self.cutoff,
                                           self.device, validator=mutate)
        self.assertFalse(captured.removed)
        self.assertEqual(captured.deleted_entries, 0)
        self.assertEqual(self.leaf.read_bytes(), b'changed! content')

    def test_validator_private_name_replacement_preserves_both_objects(self):
        saved = self.root / 'saved-selected'
        def replace(current):
            current.path.rename(saved)
            current.path.write_bytes(b'private replacement stays')
        with self.assertRaisesRegex(ValueError, 'captured leaf changed before removal'):
            with self.capture() as captured:
                prune.remove_captured_leaf(captured, os.fstat(captured.fd), self.cutoff,
                                           self.device, validator=replace)
        self.assertFalse(captured.removed)
        self.assertEqual(captured.deleted_entries, 0)
        self.assertEqual(saved.read_bytes(), b'reviewed content')
        self.assertEqual(self.leaf.read_bytes(), b'private replacement stays')

    def test_validator_ancestor_replacement_preserves_both_locations(self):
        parent = self.root / 'parent'
        parent.mkdir()
        self.leaf.rename(parent / 'selected')
        self.leaf = parent / 'selected'
        saved = self.root / 'saved-parent'
        def replace(_):
            parent.rename(saved)
            parent.mkdir()
            self.leaf.write_bytes(b'new ancestor content stays')
        with self.assertRaisesRegex(ValueError, 'ancestor identity changed'):
            with self.capture() as captured:
                prune.remove_captured_leaf(captured, os.fstat(captured.fd), self.cutoff,
                                           self.device, validator=replace)
        self.assertFalse(captured.removed)
        self.assertEqual(captured.deleted_entries, 0)
        self.assertEqual((saved / 'selected').read_bytes(), b'reviewed content')
        self.assertEqual(self.leaf.read_bytes(), b'new ancestor content stays')

    def test_public_replacement_does_not_expand_selected_deletion(self):
        def publish(_):
            self.leaf.write_bytes(b'new public content stays')
        with self.capture() as captured:
            self.assertTrue(prune.remove_captured_leaf(captured, os.fstat(captured.fd),
                                                       self.cutoff, self.device, validator=publish))
        self.assertEqual(self.leaf.read_bytes(), b'new public content stays')
        self.assertEqual(list(self.root.iterdir()), [self.leaf])

    def test_failed_exclusive_restore_keeps_private_payload_and_manifest(self):
        import json
        def collide(_):
            self.leaf.write_bytes(b'public replacement stays')
            raise ValueError('fixture refusal')
        with self.assertRaisesRegex(OSError, 'exclusive restoration unavailable'):
            with self.capture() as captured:
                prune.remove_captured_leaf(captured, os.fstat(captured.fd), self.cutoff,
                                           self.device, validator=collide)
        self.assertFalse(captured.removed)
        self.assertEqual(captured.deleted_entries, 0)
        self.assertEqual(self.leaf.read_bytes(), b'public replacement stays')
        self.assertEqual(captured.path.read_bytes(), b'reviewed content')
        manifest = captured.path.parent / 'manifest.json'
        self.assertEqual(json.loads(manifest.read_text())['original'], str(self.leaf))
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)

    def test_mutation_before_validation_rejects_without_running_callback(self):
        validator = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'captured leaf changed before validation'):
            with self.capture() as captured:
                opened = os.fstat(captured.fd)
                os.chmod(captured.path, 0o400)
                prune.remove_captured_leaf(captured, opened, self.cutoff,
                                           self.device, validator=validator)
        validator.assert_not_called()
        self.assertEqual(self.leaf.read_bytes(), b'reviewed content')

    def test_hardlinked_leaf_is_preserved_before_validation(self):
        alias = self.root / 'alias'
        os.link(self.leaf, alias)
        validator = mock.Mock()
        with self.assertRaisesRegex(ValueError, 'singly linked regular file'):
            with self.capture() as captured:
                prune.remove_captured_leaf(captured, os.fstat(captured.fd), self.cutoff,
                                           self.device, validator=validator)
        validator.assert_not_called()
        self.assertEqual(self.leaf.read_bytes(), b'reviewed content')
        self.assertEqual(alias.read_bytes(), b'reviewed content')
        self.assertEqual(self.leaf.stat().st_nlink, 2)

    def test_nonregular_capture_is_preserved(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                path = self.root / ('directory' if directory else 'link')
                if directory:
                    path.mkdir()
                else:
                    path.symlink_to(self.leaf)
                os.utime(path, (1, 1), follow_symlinks=False)
                with self.assertRaisesRegex(ValueError, 'singly linked regular file'):
                    with prune.capture_entry(path, prune.file_identity(path.lstat()),
                                             directory=directory, symlink=not directory) as captured:
                        prune.remove_captured_leaf(captured, os.fstat(captured.fd),
                                                   self.cutoff, self.device)
                self.assertTrue(os.path.lexists(path))
        self.assertEqual(self.leaf.read_bytes(), b'reviewed content')

    def test_recent_or_foreign_filesystem_leaf_is_preserved_before_validation(self):
        for cutoff, device, reason in ((0, self.device, 'recent contents'),
                                        (self.cutoff, self.device + 1, 'foreign owner or filesystem')):
            validator = mock.Mock()
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                with self.capture() as captured:
                    prune.remove_captured_leaf(captured, os.fstat(captured.fd), cutoff,
                                               device, validator=validator)
            validator.assert_not_called()
            self.assertEqual(self.leaf.read_bytes(), b'reviewed content')

    def test_fsync_failure_keeps_confirmed_effect_count(self):
        with self.capture() as captured:
            with mock.patch.object(prune.os, 'fsync', side_effect=OSError('fixture durability failure')):
                with self.assertRaisesRegex(OSError, 'fixture durability failure'):
                    prune.remove_captured_leaf(captured, os.fstat(captured.fd),
                                               self.cutoff, self.device)
            self.assertTrue(captured.removed)
            self.assertEqual(captured.deleted_entries, 1)
        self.assertFalse(self.leaf.exists())
        self.assertFalse(list(self.root.iterdir()))


class StoragePruneTests(unittest.TestCase):
    def candidate(self, path):
        old = time.time() - 10 * 86400
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                os.utime(Path(root) / name, (old, old), follow_symlinks=False)
        os.utime(path, (old, old))
        size, newest = prune.tree_facts(path, time.time() - 86400)
        s = path.stat()
        return prune.Candidate(str(path), 'xcode-generated', str(path), 1,
                               s.st_dev, s.st_ino, newest, size)

    def test_deletes_stale_generated_output_but_preserves_neighbor_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            generated = root / 'Intermediates.noindex'
            generated.mkdir()
            (generated / 'file.o').write_text('object')
            products = root / 'Products'
            products.mkdir()
            (products / 'signed-app').write_text('keep')
            candidate = self.candidate(generated)
            with mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([candidate])
            self.assertEqual(len(removed), 1)
            self.assertFalse(deferred)
            self.assertFalse(generated.exists())
            self.assertEqual((products / 'signed-app').read_text(), 'keep')

    def test_recent_child_protects_old_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve()
            (p / 'fresh').write_text('keep')
            old = time.time() - 10 * 86400
            os.utime(p, (old, old))
            with self.assertRaisesRegex(ValueError, 'recent'):
                prune.tree_facts(p, time.time() - 86400)

    def test_open_handles_protect_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / 'cache'
            p.mkdir()
            c = self.candidate(p)
            with mock.patch.object(prune, 'activity_reason', return_value='open handles'), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertEqual(deferred[0]['reason'], 'open handles')
            self.assertTrue(p.exists())

    def test_identity_swap_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'
            p.mkdir()
            c = self.candidate(p)
            p.rename(root / 'original')
            p.symlink_to(root / 'original')
            with mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertTrue(deferred)
            self.assertTrue((root / 'original').is_dir())

    def test_lsof_errors_fail_closed(self):
        for rc, stdout, stderr in [(1, '', 'permission denied'), (2, '', ''), (0, 'file', '')]:
            with mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(returncode=rc, stdout=stdout, stderr=stderr)):
                self.assertIsNotNone(prune.activity_reason(Path('/safe/cache'), ''))

    def test_lsof_requires_noninteractive_privileged_visibility(self):
        with mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr='')) as run:
            self.assertIsNone(prune.activity_reason(Path('/safe/cache'), '', ignore_current_process=True))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/sudo', '-n', '/usr/sbin/lsof', '-nP',
                         '-a', '-p', '^' + str(os.getpid()), '+D', '/safe/cache'])

    def test_privilege_failure_cannot_authorize_directory_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / 'cache'; p.mkdir(); (p / 'held').write_text('keep')
            candidate = self.candidate(p)
            denied = mock.Mock(returncode=1, stdout='', stderr='sudo: a password is required')
            with mock.patch.object(prune.subprocess, 'run', return_value=denied), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([candidate])
            self.assertFalse(removed)
            self.assertTrue(deferred[0]['error'])
            self.assertEqual((p / 'held').read_text(), 'keep')

    def test_aged_recovery_quarantines_and_descendants_are_not_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cache = root / 'Library/Caches/pnpm/dlx'; cache.mkdir(parents=True)
            quarantine = cache / (prune.QUARANTINE_PREFIX + 'preserved'); quarantine.mkdir()
            payload = quarantine / 'payload'; payload.mkdir(); (payload / 'keep').write_text('keep')
            (quarantine / 'manifest.json').write_text('{"recovery": true}')
            ordinary = cache / 'ordinary'; ordinary.mkdir()
            for path in (quarantine, payload, payload / 'keep', quarantine / 'manifest.json', ordinary):
                os.utime(path, (1, 1))
            with mock.patch.object(prune, 'USER_HOME', root), mock.patch.object(prune, 'xcode_candidates', return_value=[]), mock.patch.object(Path, 'is_mount', return_value=False):
                candidates = prune.bounded_candidates()
            self.assertIn(ordinary, [entry[0] for entry in candidates])
            self.assertNotIn(quarantine, [entry[0] for entry in candidates])
            for path in (quarantine, payload):
                with self.subTest(path=path), mock.patch.object(prune, 'activity_reason') as activity:
                    removed, deferred = prune.apply_candidates([self.candidate(path)])
                self.assertFalse(removed)
                self.assertIn('quarantine', deferred[0]['reason'])
                activity.assert_not_called()
            self.assertEqual((payload / 'keep').read_text(), 'keep')
            self.assertTrue((quarantine / 'manifest.json').is_file())

    def test_tmp_alias_process_reference_protects_directory(self):
        self.assertIn('process', prune.activity_reason(Path('/private/tmp/codex-test'), 'xcodebuild -derivedDataPath /tmp/codex-test'))

    def test_missing_external_mount_blocks_cleanup(self):
        with mock.patch.object(Path, 'is_mount', return_value=False), mock.patch.object(prune, 'discover') as discover:
            with self.assertRaisesRegex(RuntimeError, 'not mounted'):
                prune.run(apply=True)
            discover.assert_not_called()

    def test_budget_defers_remaining_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / 'cache'
            p.mkdir()
            removed, deferred = prune.apply_candidates([self.candidate(p)], deadline=0)
            self.assertFalse(removed)
            self.assertTrue(deferred[0]['error'])
            self.assertTrue(p.exists())

    def test_manifest_is_private_and_written_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cpath = root / 'cache'
            cpath.mkdir()
            candidate = self.candidate(cpath)
            def mutation(candidates, deadline):
                manifest = next((root / 'receipts').glob('storage-prune-*.json'))
                import json
                payload = json.loads(manifest.read_text())
                self.assertFalse(payload['terminal'])
                self.assertEqual(payload['candidates'][0]['path'], str(cpath))
                self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
                return [], []
            with mock.patch.object(prune, 'ARTIFACT_ROOT', root / 'receipts'), mock.patch.object(Path, 'is_mount', return_value=True), \
                 mock.patch.object(prune, 'require_owc_identity'), mock.patch.object(prune, 'discover', return_value=([candidate], [])), \
                 mock.patch.object(prune, 'disposable_simulators', return_value=([], [])), mock.patch.object(prune, 'oversized_logs', return_value=[]), \
                 mock.patch.object(prune, 'free_space', return_value={'internal': 40, 'owc': 50}), mock.patch.object(prune, 'apply_candidates', side_effect=mutation):
                result = prune.run(apply=True)
            self.assertEqual(result['status'], 'ok')
            self.assertTrue(result['terminal'])

    def test_simulator_active_test_run_prevents_native_delete(self):
        plan = {'path': '/safe/device', 'id': 'device'}
        with mock.patch.object(prune, 'process_arguments', return_value='7 /Developer/bin/xcodebuild test'), mock.patch.object(prune.subprocess, 'run') as command:
            removed, deferred = prune.delete_simulators([plan], time.monotonic() + 10)
        self.assertFalse(removed)
        self.assertIn('active Xcode', deferred[0]['reason'])
        command.assert_not_called()

    def test_named_personal_simulators_are_not_candidates(self):
        import json
        device = {'name': 'iPhone 17 Pro', 'state': 'Shutdown', 'udid': 'personal'}
        with mock.patch.object(Path, 'is_dir', return_value=True), mock.patch.object(Path, 'resolve', autospec=True, side_effect=lambda p: p), \
             mock.patch.object(Path, 'is_mount', return_value=True), mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(stdout=json.dumps({'devices': {'runtime': [device]}}))):
            plans, errors = prune.disposable_simulators(time.monotonic() + 10)
        self.assertFalse(plans)
        self.assertFalse(errors)

    def test_oversized_active_logs_are_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve() / 'gateway.log'
            p.write_text('keep')
            with mock.patch.object(Path, 'is_mount', return_value=True), mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout='writer', stderr='')):
                archived, deferred = prune.rotate_logs([{'path': str(p)}], time.monotonic() + 10)
            self.assertFalse(archived)
            self.assertTrue(deferred[0]['error'])
            self.assertEqual(p.read_text(), 'keep')

    def test_nested_symlink_is_unlinked_without_touching_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            outside = root / 'source-to-preserve'
            outside.mkdir()
            (outside / 'important').write_text('keep')
            p = root / 'cache'
            p.mkdir()
            (p / 'link').symlink_to(outside)
            c = self.candidate(p)
            with mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertEqual(len(removed), 1)
            self.assertFalse(deferred)
            self.assertEqual((outside / 'important').read_text(), 'keep')

    def test_root_replaced_during_capture_is_restored_without_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir(); (p / 'old').write_text('old')
            c = self.candidate(p)
            rename = prune.rename_exclusive
            raced = False
            def replace(src_fd, src, dst_fd, dst):
                nonlocal raced
                if not raced:
                    raced = True
                    p.rename(root / 'old-cache')
                    p.mkdir(); (p / 'new').write_text('new')
                return rename(src_fd, src, dst_fd, dst)
            with mock.patch.object(prune, 'rename_exclusive', side_effect=replace), mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertIn('captured object identity', deferred[0]['reason'])
            self.assertEqual((p / 'new').read_text(), 'new')
            self.assertEqual((root / 'old-cache/old').read_text(), 'old')
            self.assertFalse(list(root.glob('.openclaw-prune-*')))

    def test_ancestor_replaced_during_capture_preserves_both_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            parent = root / 'parent'; parent.mkdir()
            p = parent / 'cache'; p.mkdir(); (p / 'old').write_text('old')
            c = self.candidate(p)
            rename = prune.rename_exclusive
            raced = False
            def replace(src_fd, src, dst_fd, dst):
                nonlocal raced
                if not raced:
                    raced = True
                    parent.rename(root / 'old-parent')
                    parent.mkdir(); p.mkdir(); (p / 'new').write_text('new')
                return rename(src_fd, src, dst_fd, dst)
            with mock.patch.object(prune, 'rename_exclusive', side_effect=replace), mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertIn('ancestor identity changed', deferred[0]['reason'])
            self.assertEqual((p / 'new').read_text(), 'new')
            self.assertEqual((root / 'old-parent/cache/old').read_text(), 'old')

    def test_late_original_recreation_is_not_the_object_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir(); (p / 'old').write_text('old')
            c = self.candidate(p)
            facts = prune.descriptor_tree_facts
            def recreate(*args, **kwargs):
                result = facts(*args, **kwargs)
                p.mkdir(); (p / 'new').write_text('new')
                return result
            with mock.patch.object(prune, 'descriptor_tree_facts', side_effect=recreate), mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(deferred)
            self.assertEqual((p / 'new').read_text(), 'new')
            self.assertTrue(removed[0]['captured_inode_removed'])
            self.assertTrue(removed[0]['replacement_present'])
            self.assertFalse(os.path.lexists(removed[0]['path']))
            self.assertEqual(removed[0]['original_path'], str(p))

    def test_capture_restore_never_overwrites_new_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir(); (p / 'old').write_text('old')
            c = self.candidate(p)
            rename = prune.rename_exclusive
            raced = False
            def replace(src_fd, src, dst_fd, dst):
                nonlocal raced
                if not raced:
                    raced = True
                    p.rename(root / 'old-cache')
                    p.mkdir(); (p / 'wrong-capture').write_text('preserve')
                    result = rename(src_fd, src, dst_fd, dst)
                    p.mkdir(); (p / 'newest').write_text('newest')
                    return result
                return rename(src_fd, src, dst_fd, dst)
            with mock.patch.object(prune, 'rename_exclusive', side_effect=replace), mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertTrue(deferred[0]['error'])
            self.assertIn('preserved', deferred[0]['reason'])
            self.assertEqual((p / 'newest').read_text(), 'newest')
            self.assertEqual((root / 'old-cache/old').read_text(), 'old')
            q = next(root.glob('.openclaw-prune-*'))
            self.assertEqual((q / 'payload/wrong-capture').read_text(), 'preserve')
            self.assertTrue((q / 'manifest.json').exists())

    def test_descriptor_scan_refuses_a_foreign_filesystem_entry(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir(); (p / 'foreign').write_text('keep')
            c = self.candidate(p)
            original_stat = prune.os.stat
            def stat_entry(path, *args, **kwargs):
                value = original_stat(path, *args, **kwargs)
                if path == 'foreign' and kwargs.get('dir_fd') is not None:
                    return SimpleNamespace(st_dev=value.st_dev + 1, st_uid=value.st_uid,
                                           st_mode=value.st_mode, st_mtime=value.st_mtime)
                return value
            with mock.patch.object(prune.os, 'stat', side_effect=stat_entry), mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertIn('filesystem', deferred[0]['reason'])
            self.assertEqual((p / 'foreign').read_text(), 'keep')

    def test_deadline_during_removal_reports_partial_and_restores_remainder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir()
            (p / 'one').write_text('one'); (p / 'two').write_text('two')
            c = self.candidate(p)
            unlink = prune.os.unlink
            expired = False
            def remove(name, *args, **kwargs):
                nonlocal expired
                result = unlink(name, *args, **kwargs)
                if name == 'payload':
                    expired = True
                return result
            def budget(_):
                if expired:
                    raise TimeoutError('execution budget exhausted')
            with mock.patch.object(prune.os, 'unlink', side_effect=remove), mock.patch.object(prune, 'check_deadline', side_effect=budget), mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c], time.monotonic() + 60)
            self.assertFalse(removed)
            self.assertTrue(deferred[0]['error'])
            self.assertTrue(deferred[0]['partially_removed'])
            self.assertIn('execution budget', deferred[0]['reason'])
            self.assertEqual(len(list(p.iterdir())), 1)
            self.assertFalse(list(root.glob('.openclaw-prune-*')))

    def test_new_log_writer_after_capture_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'gateway.log'; p.write_bytes(b'old-log')
            value = p.stat()
            plan = {'path': str(p), 'device': value.st_dev, 'inode': value.st_ino,
                    'size': value.st_size, 'mtime_ns': value.st_mtime_ns}
            read = prune.os.read
            raced = False
            def new_writer(fd, count):
                nonlocal raced
                data = read(fd, count)
                if not raced:
                    raced = True
                    p.write_bytes(b'new-log')
                return data
            with mock.patch.object(prune, 'OWC', root), mock.patch.object(Path, 'is_mount', return_value=True), mock.patch.object(prune.os, 'read', side_effect=new_writer), mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr='')) as inspect:
                archived, deferred = prune.rotate_logs([plan], time.monotonic() + 60)
            self.assertEqual(len(inspect.call_args_list), 2)
            for call in inspect.call_args_list:
                self.assertEqual(call.args[0][:4], ['/usr/bin/sudo', '-n', '/usr/sbin/lsof', '-nP'])
            self.assertFalse(deferred)
            self.assertEqual(p.read_bytes(), b'new-log')
            self.assertEqual(Path(archived[0]['archive']).read_bytes(), b'old-log')
            self.assertTrue(archived[0]['replacement_present'])

    def test_changed_captured_log_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'gateway.log'; p.write_bytes(b'old-log')
            value = p.stat()
            plan = {'path': str(p), 'device': value.st_dev, 'inode': value.st_ino,
                    'size': value.st_size, 'mtime_ns': value.st_mtime_ns}
            read = prune.os.read
            raced = False
            def old_writer(fd, count):
                nonlocal raced
                data = read(fd, count)
                if not raced:
                    raced = True
                    q = next(root.glob('.openclaw-prune-*')) / 'payload'
                    with q.open('ab') as out:
                        out.write(b'-appended')
                return data
            with mock.patch.object(prune, 'OWC', root), mock.patch.object(Path, 'is_mount', return_value=True), mock.patch.object(prune.os, 'read', side_effect=old_writer), mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr='')):
                archived, deferred = prune.rotate_logs([plan], time.monotonic() + 60)
            self.assertFalse(archived)
            self.assertTrue(deferred[0]['error'])
            self.assertEqual(p.read_bytes(), b'old-log-appended')

    def test_privilege_failure_at_either_log_check_preserves_original(self):
        for deny_at in (0, 1):
            with self.subTest(deny_at=deny_at), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                p = root / 'gateway.log'; p.write_bytes(b'old-log')
                value = p.stat()
                plan = {'path': str(p), 'device': value.st_dev, 'inode': value.st_ino,
                        'size': value.st_size, 'mtime_ns': value.st_mtime_ns}
                results = [mock.Mock(returncode=1, stdout='', stderr='') for _ in range(2)]
                results[deny_at] = mock.Mock(returncode=1, stdout='', stderr='sudo: a password is required')
                with mock.patch.object(prune, 'OWC', root), mock.patch.object(Path, 'is_mount', return_value=True), mock.patch.object(prune.subprocess, 'run', side_effect=results):
                    archived, deferred = prune.rotate_logs([plan], time.monotonic() + 60)
                self.assertFalse(archived)
                self.assertTrue(deferred[0]['error'])
                self.assertEqual(p.read_bytes(), b'old-log')

    def test_log_append_and_close_during_last_handle_check_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'gateway.log'; p.write_bytes(b'old-log')
            value = p.stat()
            plan = {'path': str(p), 'device': value.st_dev, 'inode': value.st_ino,
                    'size': value.st_size, 'mtime_ns': value.st_mtime_ns}
            checks = 0
            def inspect(args, **kwargs):
                nonlocal checks
                checks += 1
                if checks == 2:
                    with Path(args[-1]).open('ab') as writer:
                        writer.write(b'-late-bytes')
                return mock.Mock(returncode=1, stdout='', stderr='')
            with mock.patch.object(prune, 'OWC', root), mock.patch.object(Path, 'is_mount', return_value=True), mock.patch.object(prune.subprocess, 'run', side_effect=inspect):
                archived, deferred = prune.rotate_logs([plan], time.monotonic() + 60)
            self.assertFalse(archived)
            self.assertTrue(deferred[0]['error'])
            self.assertIn('changed after archive', deferred[0]['reason'])
            self.assertEqual(p.read_bytes(), b'old-log-late-bytes')

    def test_invalid_process_snapshot_cannot_authorize_removal(self):
        for output in ('', 'bad-row', '-1 command', '123 /bin/tool\nmalformed'):
            with self.subTest(output=output), mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(stdout=output)):
                with self.assertRaisesRegex(ValueError, 'process inspection'):
                    prune.process_arguments()

    def test_guard_excludes_only_its_own_process_arguments(self):
        output = f'{os.getpid()} cleanup-script\n1 /sbin/launchd\n234 build --path /safe/cache'
        with mock.patch.object(prune.subprocess, 'run', return_value=mock.Mock(stdout=output)):
            result = prune.process_arguments()
        self.assertNotIn('cleanup-script', result)
        self.assertIn('build --path /safe/cache', result)

    def test_filesystem_change_after_final_scan_is_not_traversed(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir(); (p / 'foreign').write_text('keep')
            c = self.candidate(p)
            scan = prune.descriptor_tree_facts
            original_stat = prune.os.stat
            scanned = False
            def final_scan(*args, **kwargs):
                nonlocal scanned
                result = scan(*args, **kwargs)
                scanned = True
                return result
            def entry_stat(path, *args, **kwargs):
                value = original_stat(path, *args, **kwargs)
                if scanned and path == 'foreign' and kwargs.get('dir_fd') is not None:
                    return SimpleNamespace(st_dev=value.st_dev + 1, st_uid=value.st_uid,
                                           st_mode=value.st_mode, st_mtime=value.st_mtime)
                return value
            with mock.patch.object(prune, 'descriptor_tree_facts', side_effect=final_scan), mock.patch.object(prune.os, 'stat', side_effect=entry_stat), mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                removed, deferred = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertIn('filesystem', deferred[0]['reason'])
            self.assertFalse(deferred[0]['partially_removed'])
            self.assertEqual((p / 'foreign').read_text(), 'keep')

    def test_mid_delete_timeout_has_a_durable_terminal_partial_receipt(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir()
            (p / 'one').write_text('one'); (p / 'two').write_text('two')
            c = self.candidate(p)
            unlink = prune.os.unlink
            expired = False
            def remove(name, *args, **kwargs):
                nonlocal expired
                result = unlink(name, *args, **kwargs)
                if name == 'payload':
                    expired = True
                return result
            def budget(_):
                if expired:
                    raise TimeoutError('execution budget exhausted')
            with mock.patch.object(prune, 'ARTIFACT_ROOT', root / 'receipts'), mock.patch.object(Path, 'is_mount', return_value=True), \
                 mock.patch.object(prune, 'require_owc_identity'), mock.patch.object(prune, 'discover', return_value=([c], [])), \
                 mock.patch.object(prune, 'disposable_simulators', return_value=([], [])), mock.patch.object(prune, 'oversized_logs', return_value=[]), \
                 mock.patch.object(prune, 'free_space', return_value={'internal': 40, 'owc': 50}), \
                 mock.patch.object(prune.os, 'unlink', side_effect=remove), mock.patch.object(prune, 'check_deadline', side_effect=budget), \
                 mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                result = prune.run(apply=True)
            receipt = json.loads(Path(result['report']).read_text())
            self.assertTrue(receipt['terminal'])
            self.assertEqual(receipt['status'], 'partial')
            self.assertTrue(receipt['errors'])
            self.assertTrue(receipt['deferred'][0]['partially_removed'])
            self.assertFalse(receipt['removed'])
            self.assertEqual(len(list(p.iterdir())), 1)

    def test_mid_delete_policy_rejection_has_a_durable_terminal_partial_receipt(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            p = root / 'cache'; p.mkdir()
            (p / 'one').write_text('one'); (p / 'two').write_text('two')
            c = self.candidate(p)
            unlink = prune.os.unlink
            def remove(name, *args, **kwargs):
                result = unlink(name, *args, **kwargs)
                if name == 'payload':
                    parent_fd = os.open('..', os.O_RDONLY | os.O_DIRECTORY, dir_fd=kwargs['dir_fd'])
                    try:
                        for remaining in ('one', 'two'):
                            try:
                                os.utime(remaining, None, dir_fd=parent_fd)
                            except FileNotFoundError:
                                pass
                    finally:
                        os.close(parent_fd)
                return result
            with mock.patch.object(prune, 'ARTIFACT_ROOT', root / 'receipts'), mock.patch.object(Path, 'is_mount', return_value=True), \
                 mock.patch.object(prune, 'require_owc_identity'), mock.patch.object(prune, 'discover', return_value=([c], [])), \
                 mock.patch.object(prune, 'disposable_simulators', return_value=([], [])), mock.patch.object(prune, 'oversized_logs', return_value=[]), \
                 mock.patch.object(prune, 'free_space', return_value={'internal': 40, 'owc': 50}), \
                 mock.patch.object(prune.os, 'unlink', side_effect=remove), \
                 mock.patch.object(prune, 'activity_reason', return_value=None), mock.patch.object(prune, 'process_arguments', return_value=''):
                result = prune.run(apply=True)
            receipt = json.loads(Path(result['report']).read_text())
            self.assertTrue(receipt['terminal'])
            self.assertEqual(receipt['status'], 'partial')
            self.assertTrue(receipt['errors'])
            self.assertTrue(receipt['deferred'][0]['partially_removed'])
            self.assertEqual(receipt['deferred'][0]['reason'], 'recent contents')
            self.assertFalse(receipt['removed'])
            self.assertEqual(len(list(p.iterdir())), 1)

    def test_generic_entry_substitution_after_stat_preserves_fresh_and_stale_replacements(self):
        for kind in ('file', 'symlink', 'directory'):
            for stale in (False, True):
                with self.subTest(kind=kind, stale=stale), tempfile.TemporaryDirectory() as tmp:
                    base = Path(tmp).resolve()
                    root = base / 'cache'; root.mkdir()
                    outside = base / 'outside'; outside.write_text('target stays')
                    def populate(path, content):
                        if kind == 'directory':
                            path.mkdir(); (path / 'data').write_text(content)
                        elif kind == 'symlink':
                            path.symlink_to(outside)
                        else:
                            path.write_text(content)
                    populate(root / 'a', 'reviewed')
                    candidate = self.candidate(root)
                    stat_call, raced = prune.os.stat, False
                    with prune.capture_entry(root, (candidate.device, candidate.inode), directory=True) as captured:
                        def swap(name, *args, **kwargs):
                            nonlocal raced
                            value = stat_call(name, *args, **kwargs)
                            if name == 'a' and kwargs.get('dir_fd') == captured.fd and not raced:
                                raced = True
                                (captured.path / 'a').rename(base / 'saved-a')
                                populate(captured.path / 'a', 'UNREVIEWED')
                                if stale:
                                    old = time.time() - 10 * 86400
                                    os.utime(captured.path / 'a', (old, old), follow_symlinks=False)
                            return value
                        with mock.patch.object(prune.os, 'stat', side_effect=swap):
                            with self.assertRaises((ValueError, OSError)):
                                prune.remove_captured_tree(captured, time.time() - 86400, None)
                        self.assertFalse(captured.removed)
                        self.assertEqual(captured.deleted_entries, 0)
                    self.assertTrue(os.path.lexists(root / 'a'))
                    self.assertTrue(os.path.lexists(base / 'saved-a'))
                    if kind != 'symlink':
                        value_path = root / 'a/data' if kind == 'directory' else root / 'a'
                        self.assertEqual(value_path.read_text(), 'UNREVIEWED')
                    self.assertEqual(outside.read_text(), 'target stays')

    def test_nested_capture_manifest_records_logical_original_and_preserves_failed_rollback(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / 'cache'; root.mkdir()
            (root / 'nested').mkdir(); (root / 'nested/a').write_text('reviewed')
            candidate = self.candidate(root)
            seen = []
            with prune.capture_entry(root, (candidate.device, candidate.inode), directory=True) as captured:
                def inspect(relative, value, after_capture):
                    if relative != 'nested/a' or not after_capture:
                        return
                    # Both parent and leaf have durable captures by this point.
                    manifests = list(captured.path.rglob('manifest.json'))
                    rows = [json.loads(path.read_text()) for path in manifests]
                    leaf = next(row for row in rows if row['original'] == str(root / 'nested/a'))
                    seen.append(leaf)
                    leaf_manifest = next(path for path in manifests
                                         if json.loads(path.read_text())['original'] == str(root / 'nested/a'))
                    (leaf_manifest.parent.parent / 'a').write_text('replacement stays')
                    raise ValueError('fixture stop after capture')
                with self.assertRaises(OSError):
                    prune.remove_captured_tree(captured, time.time() - 86400, None, entry_validator=inspect)
                self.assertEqual(captured.deleted_entries, 0)
            self.assertEqual(len(seen), 1)
            self.assertEqual((root / 'nested/a').read_text(), 'replacement stays')
            manifest = next((root / 'nested').glob('.openclaw-prune-*/manifest.json'))
            self.assertEqual(json.loads(manifest.read_text())['original'], str(root / 'nested/a'))
            self.assertEqual((manifest.parent / 'payload').read_text(), 'reviewed')

    def test_progress_substitution_never_deletes_unreviewed_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve(); root = base / 'cache'; root.mkdir()
            (root / 'a').write_text('reviewed')
            candidate = self.candidate(root)
            with prune.capture_entry(root, (candidate.device, candidate.inode), directory=True) as captured:
                def replace():
                    (captured.path / 'a').rename(base / 'saved-a')
                    (captured.path / 'a').write_text('NEW UNREVIEWED DATA')
                with self.assertRaisesRegex(ValueError, 'recent'):
                    prune.remove_captured_tree(captured, time.time() - 86400, None, progress=replace)
                self.assertEqual(captured.deleted_entries, 0)
            self.assertEqual((root / 'a').read_text(), 'NEW UNREVIEWED DATA')
            self.assertEqual((base / 'saved-a').read_text(), 'reviewed')

    def test_old_nested_recovery_capture_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve(); root = base / 'cache'; root.mkdir()
            recovery = root / '.openclaw-prune-retained'; recovery.mkdir()
            (recovery / 'manifest.json').write_text('recovery evidence')
            candidate = self.candidate(root)
            with prune.capture_entry(root, (candidate.device, candidate.inode), directory=True) as captured:
                with self.assertRaisesRegex(ValueError, 'recovery quarantine'):
                    prune.remove_captured_tree(captured, time.time() - 86400, None)
            self.assertEqual((recovery / 'manifest.json').read_text(), 'recovery evidence')

    def test_symlink_capture_rejects_fifo_replacement_without_blocking(self):
        import subprocess
        import sys
        import textwrap
        with tempfile.TemporaryDirectory() as tmp:
            program = textwrap.dedent('''
                import os, stat, sys
                from pathlib import Path
                from unittest import mock
                from scripts import openclaw_storage_prune as prune
                base = Path(sys.argv[1]).resolve()
                link = base / 'link'; link.symlink_to('absent-target')
                value = link.lstat()
                rename = prune.rename_exclusive
                raced = False
                def substitute(src_fd, src, dst_fd, dst):
                    global raced
                    if src == 'link' and not raced:
                        raced = True
                        link.rename(base / 'saved-link')
                        os.mkfifo(link)
                    return rename(src_fd, src, dst_fd, dst)
                with mock.patch.object(prune, 'rename_exclusive', side_effect=substitute):
                    try:
                        with prune.capture_entry(link, (value.st_dev, value.st_ino), directory=False, symlink=True):
                            raise AssertionError('foreign FIFO was admitted')
                    except ValueError:
                        pass
                assert stat.S_ISFIFO(link.lstat().st_mode)
                assert (base / 'saved-link').is_symlink()
            ''')
            # The old Darwin flags blocked opening the substituted FIFO. Bound
            # the native child so this regression can never hang the test runner.
            result = subprocess.run([sys.executable, '-c', program, tmp], capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
