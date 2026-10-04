from __future__ import annotations
import errno
import os
import subprocess
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

    def test_budget_deferral_certifies_every_unstarted_generated_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            candidates = []
            for index in range(26):
                path = root / f'output-{index}'
                path.mkdir()
                candidates.append(self.candidate(path))
            removed, deferred = prune.apply_candidates(candidates, deadline=0)
            self.assertEqual(removed, [])
            self.assertEqual(len(deferred), 26)
            self.assertEqual({row['path'] for row in deferred}, {candidate.path for candidate in candidates})
            self.assertTrue(all(row['state'] == 'not_started' and row['effects'] == 'none'
                                and row['target_count'] == 1 for row in deferred))
            self.assertTrue(all(Path(candidate.path).exists() for candidate in candidates))

    def test_budget_after_capture_without_deletion_has_positive_restoration_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'cache'
            path.mkdir()
            (path / 'keep').write_text('intact')
            candidate = self.candidate(path)
            with mock.patch.object(prune, 'activity_reason', return_value=None), \
                 mock.patch.object(prune, 'process_arguments', return_value=''), \
                 mock.patch.object(prune, 'remove_captured_tree', side_effect=TimeoutError('execution budget exhausted')):
                removed, deferred = prune.apply_candidates([candidate])
            self.assertEqual(removed, [])
            self.assertEqual(deferred[0]['state'], 'restored')
            self.assertEqual(deferred[0]['effects'], 'restored_zero')
            self.assertEqual(deferred[0]['restored_identity'], {'device':candidate.device, 'inode':candidate.inode})
            self.assertEqual((path / 'keep').read_text(), 'intact')
            self.assertFalse(list(path.parent.glob(prune.QUARANTINE_PREFIX + '*')))

    def test_preinventory_simulator_deadline_is_readonly_unknown_count_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / 'Library/Developer/CoreSimulator/Devices').mkdir(parents=True)
            with mock.patch.object(prune, 'USER_HOME', home), \
                 mock.patch.object(prune, 'OWC', home / 'unmounted'), \
                 mock.patch.object(prune.subprocess, 'run', side_effect=AssertionError('no native inventory')):
                plans, errors = prune.disposable_simulators(0)
        self.assertEqual(plans, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]['cause'], 'execution_budget')
        self.assertEqual(errors[0]['stage'], 'simulator_discovery')
        self.assertEqual(errors[0]['target_count'], 0)
        self.assertEqual(errors[0]['effects'], 'none')

    def test_nonbudget_timeouts_after_capture_are_blocking_not_deferrals(self):
        errors = (TimeoutError(errno.ETIMEDOUT, 'guarded read timed out'),
                  TimeoutError(errno.ETIMEDOUT, 'execution budget exhausted'),
                  TimeoutError('provider read timed out'),
                  subprocess.TimeoutExpired('fixture inventory', 1))
        for error in errors:
            with self.subTest(error=repr(error)), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp).resolve() / 'cache'
                path.mkdir()
                (path / 'keep').write_text('intact')
                candidate = self.candidate(path)
                with mock.patch.object(prune, 'activity_reason', return_value=None), \
                     mock.patch.object(prune, 'process_arguments', return_value=''), \
                     mock.patch.object(prune, 'remove_captured_tree', side_effect=error):
                    removed, deferred = prune.apply_candidates([candidate])
                self.assertEqual(removed, [])
                self.assertEqual(len(deferred), 1)
                self.assertTrue(deferred[0]['error'])
                self.assertNotEqual(deferred[0].get('cause'), 'execution_budget')
                self.assertNotIn('state', deferred[0])
                self.assertEqual((path / 'keep').read_text(), 'intact')
                self.assertFalse(list(path.parent.glob(prune.QUARANTINE_PREFIX + '*')))

    def test_nonbudget_simulator_inventory_timeouts_remain_errors(self):
        for error in (TimeoutError(errno.ETIMEDOUT, 'native inventory timed out'),
                      subprocess.TimeoutExpired('fixture inventory', 1)):
            with self.subTest(error=repr(error)), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                (home / 'Library/Developer/CoreSimulator/Devices').mkdir(parents=True)
                with mock.patch.object(prune, 'USER_HOME', home), \
                     mock.patch.object(prune, 'OWC', home / 'unmounted'), \
                     mock.patch.object(prune.subprocess, 'run', side_effect=error):
                    plans, errors = prune.disposable_simulators(time.monotonic() + 60)
                self.assertEqual(plans, [])
                self.assertEqual(len(errors), 1)
                self.assertTrue(errors[0]['error'])
                self.assertNotEqual(errors[0].get('cause'), 'execution_budget')
                self.assertIn('simulator inventory failed', errors[0]['reason'])

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


class ReadonlyTestScratchTests(unittest.TestCase):
    """Exercise only private fixtures; never discover or prune the host temp root."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.scratch = self.base / 'ProjectData/.test-tmp/pytest-of-fixture-user/pytest-15'
        self.frozen = self.scratch / 'test_failed_activation/runtime/Releases/openclaw-unrelated'
        self.frozen.mkdir(parents=True)
        (self.frozen / 'dist').mkdir()
        (self.frozen / 'openclaw.mjs').write_bytes(b'frozen fixture')
        (self.frozen / 'dist/fixture.js').write_bytes(b'frozen fixture')
        self.addCleanup(self.restore_private_fixture_modes)
        self.frozen.chmod(0o555)
        (self.frozen / 'dist').chmod(0o555)
        (self.frozen / 'openclaw.mjs').chmod(0o555)
        (self.frozen / 'dist/fixture.js').chmod(0o444)
        old = time.time() - 10 * 86400
        for root, dirs, files in os.walk(self.scratch):
            for name in dirs + files:
                os.utime(Path(root) / name, (old, old), follow_symlinks=False)
        os.utime(self.scratch, (old, old))
        size, newest = prune.tree_facts(self.scratch, time.time() - 7 * 86400)
        value = self.scratch.stat()
        self.plan = prune.Candidate(str(self.scratch), 'pytest-scratch', str(self.scratch), 7,
                                    value.st_dev, value.st_ino, newest, size)
        operator = prune.OPERATOR.__class__({
            'paths': {'temp_root': str(self.base / 'configured-scratch'),
                      'pytest_temp_root': str(self.base / 'ProjectData/.test-tmp')},
            'identifiers': {'host_user': 'fixture-user'},
        })
        for target, name, value in ((prune, 'OPERATOR', operator),
                                    (prune, 'OWC', self.base),
                                    (Path, 'is_mount', True),
                                    (prune, 'activity_reason', None),
                                    (prune, 'process_arguments', '')):
            patch = mock.patch.object(target, name, return_value=value) if name not in ('OWC', 'OPERATOR') else mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def restore_private_fixture_modes(self):
        # Fixture teardown touches only this test's newly created private tree.
        for root, dirs, _ in os.walk(self.base):
            Path(root).chmod(0o700)
            for name in dirs:
                path = Path(root) / name
                if not path.is_symlink():
                    path.chmod(0o700)

    def test_native_prune_removes_stale_frozen_fixture_and_preserves_neighbors(self):
        outside = self.base / 'outside'
        outside.write_bytes(b'preserve external target')
        (self.frozen / 'dist').chmod(0o755)
        link = self.frozen / 'dist/link'
        link.symlink_to(outside)
        os.utime(link, (self.plan.newest_mtime, self.plan.newest_mtime), follow_symlinks=False)
        os.utime(link.parent, (self.plan.newest_mtime, self.plan.newest_mtime))
        link.parent.chmod(0o555)
        sibling = self.scratch.parent / 'pytest-current'
        sibling.mkdir()
        (sibling / 'keep').write_bytes(b'active neighbor')
        removed, deferred = prune.apply_candidates([self.plan])
        self.assertEqual(deferred, [])
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0]['captured_inode_removed'])
        self.assertFalse(self.scratch.exists())
        self.assertEqual(outside.read_bytes(), b'preserve external target')
        self.assertEqual((sibling / 'keep').read_bytes(), b'active neighbor')
        self.assertFalse(list(self.base.rglob('.openclaw-prune-*')))

    def test_native_capture_also_admits_readonly_scratch_root(self):
        self.scratch.chmod(0o555)
        removed, deferred = prune.apply_candidates([self.plan])
        self.assertEqual(deferred, [])
        self.assertEqual(len(removed), 1)
        self.assertFalse(self.scratch.exists())

    def test_unclassified_generic_tree_gets_no_permission_admission(self):
        from dataclasses import replace
        with mock.patch.object(prune.os, 'fchmod', wraps=os.fchmod) as chmod:
            removed, deferred = prune.apply_candidates([replace(self.plan, kind='xcode-generated')])
        self.assertFalse(removed)
        self.assertTrue(deferred[0]['error'])
        self.assertIsInstance(deferred[0]['removed_entries'], int)
        chmod.assert_not_called()
        self.assertEqual(self.frozen.stat().st_mode & 0o777, 0o555)
        self.assertTrue((self.frozen / 'openclaw.mjs').exists())

    def test_scratch_kind_cannot_enable_permissions_for_other_policy_or_path(self):
        from dataclasses import replace
        for plan in (replace(self.plan, min_age_days=0),
                     replace(self.plan, activity_root=str(self.scratch.parent)),
                     replace(self.plan, path=str(self.frozen), activity_root=str(self.frozen))):
            with self.subTest(plan=plan), mock.patch.object(prune.os, 'fchmod', wraps=os.fchmod) as chmod:
                removed, deferred = prune.apply_candidates([plan])
                self.assertFalse(removed)
                self.assertEqual(deferred[0]['reason'], 'unclassified test scratch retention policy')
                chmod.assert_not_called()

    def test_recent_scratch_root_refuses_old_admission_without_permission_update(self):
        self.scratch.chmod(0o555)
        os.utime(self.scratch, None)
        with mock.patch.object(prune.os, 'fchmod', wraps=os.fchmod) as chmod:
            removed, deferred = prune.apply_candidates([self.plan])
        self.assertFalse(removed)
        self.assertEqual(deferred[0]['reason'], 'recent contents')
        chmod.assert_not_called()
        self.assertEqual(self.scratch.stat().st_mode & 0o777, 0o555)

    def test_permission_admission_refuses_changed_owner_filesystem_or_flags(self):
        from types import SimpleNamespace
        expected = self.frozen.lstat()
        fields = {name: getattr(expected, name) for name in
                  ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size',
                   'st_mtime', 'st_mtime_ns', 'st_ctime_ns')}
        if hasattr(expected, 'st_birthtime'):
            fields['st_birthtime'] = expected.st_birthtime
        fields['st_flags'] = 0
        real_fstat, real_stat = os.fstat, os.stat
        for changed in ({'st_uid': os.getuid() + 1}, {'st_dev': expected.st_dev + 1}, {'st_flags': 2}):
            fake = SimpleNamespace(**(fields | changed))
            def fstat(fd):
                value = real_fstat(fd)
                return fake if value.st_ino == expected.st_ino else value
            def entry_stat(*args, **kwargs):
                value = real_stat(*args, **kwargs)
                return fake if value.st_ino == expected.st_ino else value
            with self.subTest(changed=changed), mock.patch.object(prune.os, 'fstat', side_effect=fstat), \
                 mock.patch.object(prune.os, 'stat', side_effect=entry_stat), \
                 mock.patch.object(prune.os, 'fchmod', wraps=os.fchmod) as chmod:
                # A fresh opened/named fingerprint must match the admitted one;
                # neither a foreign replacement nor flags grant mode authority.
                with self.assertRaises(ValueError):
                    with prune.capture_scratch_directory(self.frozen, expected,
                                                          time.time() - 7 * 86400, None):
                        self.fail('permission admission must refuse')
                chmod.assert_not_called()
        self.assertEqual(self.frozen.stat().st_mode & 0o777, 0o555)

    def test_matching_foreign_owner_or_flagged_inode_still_cannot_grant_permissions(self):
        from types import SimpleNamespace
        expected = self.frozen.lstat()
        fields = {name: getattr(expected, name) for name in
                  ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size',
                   'st_mtime', 'st_mtime_ns', 'st_ctime_ns')}
        if hasattr(expected, 'st_birthtime'):
            fields['st_birthtime'] = expected.st_birthtime
        fields['st_flags'] = 0
        real_fstat, real_stat = os.fstat, os.stat
        for changed in ({'st_uid': os.getuid() + 1}, {'st_flags': 2}):
            fake = SimpleNamespace(**(fields | changed))
            def fstat(fd):
                value = real_fstat(fd)
                return fake if value.st_ino == expected.st_ino else value
            def entry_stat(*args, **kwargs):
                value = real_stat(*args, **kwargs)
                return fake if value.st_ino == expected.st_ino else value
            with self.subTest(changed=changed), mock.patch.object(prune.os, 'fstat', side_effect=fstat), \
                 mock.patch.object(prune.os, 'stat', side_effect=entry_stat), \
                 mock.patch.object(prune.os, 'fchmod', wraps=os.fchmod) as chmod:
                with self.assertRaises(ValueError):
                    with prune.capture_scratch_directory(self.frozen, fake,
                                                          time.time() - 7 * 86400, None):
                        self.fail('matching metadata cannot bypass owner or flags')
                chmod.assert_not_called()
        self.assertEqual(self.frozen.stat().st_mode & 0o777, 0o555)

    def test_capture_failure_restores_original_directory_mode(self):
        with mock.patch.object(prune, 'capture_entry', side_effect=PermissionError('fixture capture refused')):
            with self.assertRaisesRegex(PermissionError, 'capture refused'):
                with prune.capture_scratch_directory(self.frozen, self.frozen.lstat(),
                                                      time.time() - 7 * 86400, None):
                    self.fail('capture must fail')
        self.assertEqual(self.frozen.stat().st_mode & 0o777, 0o555)
        self.assertTrue((self.frozen / 'openclaw.mjs').exists())

    def test_replaced_public_path_is_never_chmodded_by_restoration(self):
        expected = self.frozen.lstat()
        replacement = self.frozen
        with self.assertRaisesRegex(OSError, 'exclusive restoration unavailable'):
            with prune.capture_scratch_directory(self.frozen, expected,
                                                  time.time() - 7 * 86400, None) as captured:
                replacement.mkdir(mode=0o750)
                (replacement / 'keep').write_bytes(b'replacement')
                # The exclusive rollback preserves both original and replacement.
                with mock.patch.object(prune, 'rename_exclusive', side_effect=OSError('fixture interruption')):
                    raise OSError('fixture interruption')
        self.assertEqual(replacement.stat().st_mode & 0o777, 0o750)
        self.assertEqual((replacement / 'keep').read_bytes(), b'replacement')
        payload = next(replacement.parent.glob('.openclaw-prune-*')) / 'payload'
        self.assertEqual(payload.stat().st_mode & 0o777, 0o555)
        self.assertEqual(prune.file_identity(payload.stat()), prune.file_identity(expected))
        self.assertTrue((payload / 'openclaw.mjs').exists())

    def test_partial_failure_receipt_restores_modes_and_refuses_recent_survivor(self):
        import json
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
                raise TimeoutError('fixture execution budget exhausted')
        with mock.patch.object(prune, 'ARTIFACT_ROOT', self.base / 'receipts'), \
             mock.patch.object(prune, 'require_owc_identity'), \
             mock.patch.object(prune, 'discover', return_value=([self.plan], [])), \
             mock.patch.object(prune, 'disposable_simulators', return_value=([], [])), \
             mock.patch.object(prune, 'oversized_logs', return_value=[]), \
             mock.patch.object(prune, 'free_space', return_value={'internal': 40, 'owc': 50}), \
             mock.patch.object(prune.os, 'unlink', side_effect=remove), \
             mock.patch.object(prune, 'check_deadline', side_effect=budget):
            result = prune.run(apply=True)
        receipt = json.loads(Path(result['report']).read_text())
        self.assertTrue(receipt['terminal'])
        self.assertEqual(receipt['status'], 'partial')
        self.assertEqual(len(receipt['errors']), 1)
        self.assertTrue(receipt['deferred'][0]['partially_removed'])
        self.assertEqual(receipt['deferred'][0]['removed_entries'], 1)
        self.assertEqual(receipt['removed'], [])
        self.assertEqual(receipt['reclaimed_allocated_bytes'], 0)
        self.assertEqual(len(list(self.frozen.rglob('*.mjs'))) + len(list(self.frozen.rglob('*.js'))), 1)
        self.assertEqual(self.frozen.stat().st_mode & 0o777, 0o555)
        self.assertEqual((self.frozen / 'dist').stat().st_mode & 0o777, 0o555)
        self.assertFalse(list(self.base.rglob('.openclaw-prune-*')))
        self.assertGreater(self.scratch.stat().st_mtime, time.time() - 7 * 86400)
        with mock.patch.object(prune, 'bounded_candidates', return_value=[
                (self.scratch, 'pytest-scratch', self.scratch, 7)]):
            candidates, skipped = prune.discover()
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0]['reason'], 'recent contents')
        with mock.patch.object(prune.os, 'fchmod', wraps=os.fchmod) as chmod:
            removed, deferred = prune.apply_candidates([self.plan])
        self.assertEqual(removed, [])
        self.assertEqual(deferred[0]['reason'], 'recent contents')
        self.assertFalse(deferred[0]['partially_removed'])
        chmod.assert_not_called()

    def test_configured_roots_username_and_seven_day_retention_are_preserved(self):
        self.assertEqual(prune.SCRATCH_RETENTION_DAYS, 7)
        self.assertEqual(prune.test_scratch_roots(), [self.base / 'configured-scratch',
                                                    self.base / 'ProjectData/.test-tmp'])
        self.assertEqual(prune.test_scratch_kind(self.scratch), 'pytest-scratch')
        other_user = self.scratch.parent.with_name('pytest-of-other-user') / self.scratch.name
        self.assertIsNone(prune.test_scratch_kind(other_user))
        os.utime(self.scratch, (time.time() - 6 * 86400, time.time() - 6 * 86400))
        with mock.patch.object(prune, 'bounded_candidates', return_value=[
                (self.scratch, 'pytest-scratch', self.scratch, 7)]):
            candidates, skipped = prune.discover()
        self.assertEqual(candidates, [])
        self.assertEqual(skipped[0]['reason'], 'recent contents')
        with mock.patch.object(prune.os, 'fchmod', wraps=os.fchmod) as chmod:
            removed, deferred = prune.apply_candidates([self.plan])
        self.assertEqual(removed, [])
        self.assertEqual(deferred[0]['reason'], 'recent contents')
        chmod.assert_not_called()
