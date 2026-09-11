from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from scripts import node_compile_cache as node
from scripts import openclaw_storage_prune as prune


class NodeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.patcher = mock.patch.object(node, 'ROOT', self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.old = time.time() - 30 * 86400
        self.path = self.root / ('v24.14.1-arm64-cf738c9d-' + str(os.getuid()))
        self.path.mkdir()
        self.write_cache(self.path / '1234abcd')
        self.age(self.path)

    def age(self, path):
        os.utime(path, (self.old, self.old), follow_symlinks=False)

    def write_cache(self, path, payload=b'generated v8 cache bytes'):
        path.write_bytes(struct.pack('<5I', 0x8ADFDBB2, 100, len(payload), 42, zlib.crc32(payload)) + payload)
        path.chmod(0o600)
        self.age(path)

    def facts(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return node.tree_facts(fd, time.time() - 14 * 86400, None)
        finally:
            os.close(fd)

    def candidate(self):
        self.age(self.path)
        allocated, newest, _ = self.facts()
        value = self.path.stat()
        return prune.Candidate(str(self.path), node.KIND, str(self.path), 14,
                               value.st_dev, value.st_ino, newest, allocated)

    def apply(self, candidate=None, versions=None):
        candidate = self.candidate() if candidate is None else candidate
        with mock.patch.object(node, 'active_versions', side_effect=versions or [set(), set()]), \
             mock.patch.object(prune, 'activity_reason', return_value=None), \
             mock.patch.object(prune, 'process_arguments', return_value=''):
            return prune.apply_candidates([candidate], time.monotonic() + 20)

    def test_exact_namespace_scope(self):
        self.assertEqual(node.version_for(self.path), 'v24.14.1')
        for name in ['v24.14.1-arm64-cf738c9d-99999', 'v24.14.1-x64-cf738c9d-' + str(os.getuid()),
                     self.path.name + '.quarantine-20260911', 'openclaw',
                     self.path.name.replace('v24', 'v28'), self.path.name.replace('cf738c9d', 'CF738C9D')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                node.version_for(self.root / name)
        with self.assertRaises(ValueError):
            node.version_for(self.root / 'nested' / self.path.name)

    def test_valid_old_flat_cache_is_removed(self):
        removed, skipped = self.apply()
        self.assertEqual(len(removed), 1)
        self.assertFalse(skipped)
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.root.glob('.openclaw-prune-*')))

    def test_recent_namespace_and_descendant_are_preserved(self):
        for target in [self.path, self.path / '1234abcd']:
            self.age(self.path); self.age(self.path / '1234abcd')
            os.utime(target, None)
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'recent'):
                self.facts()

    def test_malformed_layouts_and_payloads_are_preserved(self):
        original = (self.path / '1234abcd').read_bytes()
        for data in [b'personal source file', original[:8] + b'\0' * 4 + original[12:],
                     original[:-1] + b'!', original[:19]]:
            (self.path / '1234abcd').write_bytes(data); self.age(self.path / '1234abcd')
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.facts()
        (self.path / '1234abcd').write_bytes(original); self.age(self.path / '1234abcd')
        for name in ['source.js', 'quarantine', 'abcdef12']:
            p = self.path / name; p.mkdir(); self.age(p); self.age(self.path)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'layout'):
                self.facts()
            p.rmdir()
        self.age(self.path)

    def test_hardlink_symlink_fifo_and_permissions_are_preserved(self):
        leaf = self.path / '1234abcd'
        os.link(leaf, self.root / 'linked')
        with self.assertRaisesRegex(ValueError, 'leaf'):
            self.facts()
        (self.root / 'linked').unlink()
        leaf.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'leaf'):
            self.facts()
        leaf.unlink()
        for create in [lambda: leaf.symlink_to(self.root / 'important'), lambda: os.mkfifo(leaf, 0o600)]:
            create(); self.age(self.path)
            with self.assertRaisesRegex(ValueError, 'layout'):
                self.facts()
            leaf.unlink()

    def test_foreign_leaf_owner_or_device_is_preserved(self):
        leaf = self.path / '1234abcd'
        fd = os.open(leaf, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        value = os.fstat(fd)
        attrs = {key: getattr(value, key) for key in dir(value) if key.startswith('st_')}
        for changed in [{'st_uid': os.getuid() + 1}, {'st_dev': value.st_dev + 1}]:
            foreign = SimpleNamespace(**dict(attrs, **changed))
            with self.subTest(changed=changed), mock.patch.object(node.os, 'fstat', return_value=foreign):
                with self.assertRaisesRegex(ValueError, 'foreign'):
                    node.validate_leaf(fd, leaf.name, value.st_dev, time.time(), None)
        self.assertTrue(leaf.exists())

    def test_handle_inspection_failure_before_or_after_capture_preserves_cache(self):
        c = self.candidate()
        for answers in [['open-handle inspection unavailable'], [None, 'open-handle inspection failed']]:
            with self.subTest(answers=answers), mock.patch.object(node, 'active_versions', return_value=set()), \
                 mock.patch.object(prune, 'process_arguments', return_value=''), \
                 mock.patch.object(prune, 'activity_reason', side_effect=answers):
                removed, skipped = prune.apply_candidates([c])
            self.assertFalse(removed)
            self.assertTrue(skipped[0]['error'])
            self.assertTrue((self.path / '1234abcd').exists())

    def test_leaf_open_is_nonblocking_and_nofollow(self):
        original = os.open
        with mock.patch.object(node.os, 'open', wraps=original) as opened:
            self.facts()
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_NONBLOCK)

    def test_deadline_fails_closed(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(TimeoutError):
                node.tree_facts(fd, time.time(), 0)
        finally:
            os.close(fd)

    def test_active_version_is_preserved_before_capture(self):
        with mock.patch.object(prune, 'capture_entry') as capture:
            removed, skipped = self.apply(versions=[{'v24.14.1'}])
        self.assertFalse(removed)
        self.assertIn('active Node version', skipped[0]['reason'])
        capture.assert_not_called()

    def test_newly_active_version_after_capture_rolls_back(self):
        removed, skipped = self.apply(versions=[set(), {'v24.14.1'}])
        self.assertFalse(removed)
        self.assertIn('active Node version', skipped[0]['reason'])
        self.assertTrue((self.path / '1234abcd').exists())
        self.assertFalse(list(self.root.glob('.openclaw-prune-*')))

    def test_incomplete_process_visibility_cannot_capture(self):
        c = self.candidate()
        with mock.patch.object(node, 'active_versions', side_effect=ValueError('Node process inspection incomplete')), \
             mock.patch.object(prune, 'capture_entry') as capture:
            removed, skipped = prune.apply_candidates([c])
        self.assertFalse(removed)
        self.assertTrue(skipped[0]['error'])
        capture.assert_not_called()

    def test_active_and_recent_discovery_avoid_expensive_layout_walk(self):
        bounded = [(self.path, node.KIND, self.path, 14)]
        with mock.patch.object(prune, 'bounded_candidates', return_value=bounded), \
             mock.patch.object(node, 'active_versions', return_value={'v24.14.1'}), \
             mock.patch.object(node, 'tree_facts') as facts:
            candidates, skipped = prune.discover()
        self.assertFalse(candidates); facts.assert_not_called()
        self.assertIn('active', skipped[0]['reason'])
        os.utime(self.path, None)
        with mock.patch.object(prune, 'bounded_candidates', return_value=bounded), \
             mock.patch.object(node, 'active_versions') as activity:
            candidates, skipped = prune.discover()
        self.assertFalse(candidates); activity.assert_not_called()
        self.assertIn('recent', skipped[0]['reason'])

    def test_discovery_missing_process_visibility_is_reported_as_error(self):
        with mock.patch.object(prune, 'bounded_candidates', return_value=[(self.path, node.KIND, self.path, 14)]), \
             mock.patch.object(node, 'active_versions', side_effect=subprocess.TimeoutExpired('snapshot', 1)):
            candidates, skipped = prune.discover()
        self.assertFalse(candidates)
        self.assertTrue(skipped[0]['error'])
        self.assertTrue(self.path.exists())

    def test_late_unclassified_replacement_after_scan_is_never_unlinked(self):
        c = self.candidate()
        original = prune.remove_node_cache
        def replace(captured, leaves, cutoff, deadline):
            leaf = captured.path / '1234abcd'
            leaf.rename(captured.path / 'preserved-cache')
            leaf.write_text('irreplaceable late source'); leaf.chmod(0o600)
            self.age(leaf)
            return original(captured, leaves, cutoff, deadline)
        with mock.patch.object(prune, 'remove_node_cache', side_effect=replace):
            removed, skipped = self.apply(c)
        self.assertFalse(removed)
        self.assertIn('identity changed', skipped[0]['reason'])
        self.assertEqual((self.path / '1234abcd').read_text(), 'irreplaceable late source')
        self.assertTrue((self.path / 'preserved-cache').exists())

    def test_late_old_directory_is_never_traversed(self):
        c = self.candidate()
        original = prune.remove_node_cache
        def add(captured, leaves, cutoff, deadline):
            p = captured.path / 'abcdef12'; p.mkdir(); (p / 'important').write_text('keep')
            self.age(p / 'important'); self.age(p)
            return original(captured, leaves, cutoff, deadline)
        with mock.patch.object(prune, 'remove_node_cache', side_effect=add):
            removed, skipped = self.apply(c)
        self.assertFalse(removed)
        self.assertTrue(skipped[0]['partially_removed'])
        self.assertEqual((self.path / 'abcdef12/important').read_text(), 'keep')

    def test_changed_payload_after_final_scan_is_preserved(self):
        c = self.candidate()
        original = prune.remove_node_cache
        def corrupt(captured, leaves, cutoff, deadline):
            leaf = captured.path / '1234abcd'
            leaf.write_bytes(b'important user bytes'); leaf.chmod(0o600); self.age(leaf)
            return original(captured, leaves, cutoff, deadline)
        with mock.patch.object(prune, 'remove_node_cache', side_effect=corrupt):
            removed, skipped = self.apply(c)
        self.assertFalse(removed)
        self.assertEqual((self.path / '1234abcd').read_bytes(), b'important user bytes')
        self.assertFalse(skipped[0]['partially_removed'])

    def test_new_original_namespace_survives_captured_removal(self):
        c = self.candidate()
        original = prune.remove_node_cache
        def recreate(*args):
            self.path.mkdir(); (self.path / 'new').write_text('keep')
            return original(*args)
        with mock.patch.object(prune, 'remove_node_cache', side_effect=recreate):
            removed, skipped = self.apply(c)
        self.assertFalse(skipped)
        self.assertTrue(removed[0]['replacement_present'])
        self.assertEqual((self.path / 'new').read_text(), 'keep')

    def test_failed_leaf_rollback_keeps_logical_mapping_after_outer_restore(self):
        c = self.candidate()
        original_bytes = (self.path / '1234abcd').read_bytes()
        rename, validate = prune.rename_exclusive, node.validate_leaf
        raced = False
        def replace(src_fd, src, dst_fd, dst):
            nonlocal raced
            result = rename(src_fd, src, dst_fd, dst)
            if src == '1234abcd':
                raced = True
                replacement = os.open(src, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=src_fd)
                with os.fdopen(replacement, 'w') as stream:
                    stream.write('new leaf to preserve')
            return result
        def reject_after_capture(*args):
            if raced:
                raise ValueError('fixture late validation failure')
            return validate(*args)
        with mock.patch.object(prune, 'rename_exclusive', side_effect=replace), \
             mock.patch.object(node, 'validate_leaf', side_effect=reject_after_capture):
            removed, skipped = self.apply(c)
        self.assertFalse(removed)
        self.assertTrue(skipped[0]['error'])
        self.assertEqual((self.path / '1234abcd').read_text(), 'new leaf to preserve')
        self.assertFalse(list(self.root.glob('.openclaw-prune-*')))
        manifest_path = next(self.path.glob('.openclaw-prune-*/manifest.json'))
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest['original'], str(self.path / '1234abcd'))
        self.assertEqual((manifest_path.parent / 'payload').read_bytes(), original_bytes)

    def test_leaf_capture_writes_recovery_mapping_before_rename(self):
        c = self.candidate()
        original = prune.rename_exclusive
        mappings = []
        def inspect(src_fd, src, dst_fd, dst):
            manifest_fd = os.open('manifest.json', os.O_RDONLY, dir_fd=dst_fd)
            with os.fdopen(manifest_fd) as stream:
                mappings.append(json.load(stream))
            return original(src_fd, src, dst_fd, dst)
        with mock.patch.object(prune, 'rename_exclusive', side_effect=inspect):
            removed, skipped = self.apply(c)
        self.assertFalse(skipped)
        self.assertEqual(len(mappings), 2)
        self.assertEqual(mappings[0]['original'], str(self.path))
        self.assertEqual(mappings[1]['original'], str(self.path / '1234abcd'))
        self.assertTrue(mappings[1]['captured_from'].endswith('/payload/1234abcd'))
        self.assertEqual(mappings[1]['payload'], 'payload')


class NodeProcessTests(unittest.TestCase):
    def test_rewritten_process_title_does_not_hide_node(self):
        # Only pid/state comes from ps; identification uses the kernel path.
        with mock.patch.object(node, 'process_table', return_value={1: 'S', 2: 'S', 3: 'Z'}), \
             mock.patch.object(node, 'executable_path', side_effect=['/bin/launchd', '/renamed-title/bin/node']) as path:
            result = node.kernel_snapshot(None)
        self.assertEqual(result[2], '/renamed-title/bin/node')
        self.assertEqual(path.call_count, 2)

    def test_unreadable_live_pid_fails_closed_but_exited_esrch_is_allowed(self):
        for failure, after, expected in [(PermissionError(errno.EPERM, 'denied'), {}, True),
                                         (ProcessLookupError(errno.ESRCH, 'gone'), {1: 'S'}, True),
                                         (ProcessLookupError(errno.ESRCH, 'gone'), {}, False),
                                         (ProcessLookupError(errno.ESRCH, 'gone'), {1: 'Z'}, False)]:
            with self.subTest(failure=failure, after=after), \
                 mock.patch.object(node, 'process_table', side_effect=[{1: 'S'}, after]), \
                 mock.patch.object(node, 'executable_path', side_effect=failure):
                if expected:
                    with self.assertRaisesRegex(ValueError, 'live executable'):
                        node.kernel_snapshot(None)
                else:
                    self.assertEqual(node.kernel_snapshot(None), {})

    def test_malformed_empty_or_incomplete_ps_is_rejected(self):
        for output in ['', 'no pid', '12 S\n12 S\n', '12 S\n']:
            with self.subTest(output=output), \
                 mock.patch.object(node.subprocess, 'run', return_value=mock.Mock(stdout=output, stderr='')):
                with self.assertRaises(ValueError):
                    node.process_table(None)

    def test_lsof_nul_parser_preserves_spaces_and_rejects_missing_fields(self):
        parsed = node.text_images('p42\0\nftxt\0D0x1000018\0i99\0n/path with spaces/node\0\n')
        self.assertEqual(parsed, {42: [('/path with spaces/node', 16777240, 99)]})
        for output in ['p42\0ftxt\0n/path/node\0', 'p42\0ftxt\0Dnope\0i99\0n/node\0', 'pwat\0']:
            with self.subTest(output=output), self.assertRaises(ValueError):
                node.text_images(output)

    def test_version_probe_uses_exact_image_and_sanitized_environment(self):
        executable = Path('/bin/sh')
        snapshot = {'schema': 'openclaw.node_processes.v1', 'complete': True, 'inspected': 20,
                    'executables': [{'path': str(executable), 'identity': list(node.fingerprint(executable.stat())), 'pids': [5]}]}
        replies = [mock.Mock(stdout=json.dumps(snapshot), stderr=''), mock.Mock(stdout='v24.15.0\n', stderr='')]
        with mock.patch.dict(os.environ, {'NODE_OPTIONS': '--require /do/not/run.js', 'NODE_COMPILE_CACHE': '/do/not/write'}), \
             mock.patch.object(node.subprocess, 'run', side_effect=replies) as run:
            self.assertEqual(node.active_versions(), {'v24.15.0'})
        self.assertEqual(run.call_args.args[0], ['/bin/sh', '--version'])
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/usr/bin:/bin', 'NODE_DISABLE_COMPILE_CACHE': '1'})
        self.assertEqual(run.call_args_list[0].args[0][:2], ['/usr/bin/sudo', '-n'])

    def test_replaced_executable_or_incomplete_snapshot_blocks_version_probe(self):
        executable = Path('/bin/sh')
        for complete, identity in [(False, list(node.fingerprint(executable.stat()))), (True, [0] * 8)]:
            snapshot = {'schema': 'openclaw.node_processes.v1', 'complete': complete, 'inspected': 20,
                        'executables': [{'path': str(executable), 'identity': identity, 'pids': [5]}]}
            with self.subTest(complete=complete), \
                 mock.patch.object(node.subprocess, 'run', return_value=mock.Mock(stdout=json.dumps(snapshot), stderr='')) as run:
                with self.assertRaisesRegex(ValueError, 'inspection failed'):
                    node.active_versions()
            self.assertEqual(run.call_count, 1)

    def test_lsof_diagnostics_fail_closed(self):
        for snapshots, result in [([{1: '/bin/node'}] * 2, mock.Mock(returncode=1, stdout='', stderr='denied')),
                                  ([{1: '/bin/node'}] * 2, mock.Mock(returncode=2, stdout='', stderr=''))]:
            with self.subTest(snapshots=snapshots), mock.patch.object(node.sys, 'platform', 'darwin'), \
                 mock.patch.object(node.os, 'geteuid', return_value=0), \
                 mock.patch.object(node, 'kernel_snapshot', side_effect=snapshots), \
                 mock.patch.object(node.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(ValueError, 'inspection'):
                    node.native_snapshot(None)

    @staticmethod
    def bound_images(nodes, deadline):
        return [{'path': path, 'identity': [1, len(path)], 'pids': [pid]}
                for pid, path in nodes.items()]

    def test_non_node_spawn_and_exec_churn_does_not_invalidate_complete_view(self):
        before = {11: '/tools/v24.15/bin/node', 21: '/bin/bash', 22: '/bin/sleep'}
        after = {11: '/tools/v24.15/bin/node', 21: '/bin/ps', 31: '/usr/bin/python3'}
        with mock.patch.object(node.sys, 'platform', 'darwin'), mock.patch.object(node.os, 'geteuid', return_value=0), \
             mock.patch.object(node, 'kernel_snapshot', side_effect=[before, after]) as snapshot, \
             mock.patch.object(node, 'bind_node_images', side_effect=self.bound_images) as bind:
            result = node.native_snapshot(None)
        self.assertTrue(result['complete'])
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(bind.call_count, 2)
        self.assertEqual([entry['path'] for entry in result['executables']], ['/tools/v24.15/bin/node'])

    def test_new_or_changed_node_requires_resampling_and_retains_earlier_image(self):
        for new_pid in [11, 22]:
            snapshots = [{11: '/v24.14/bin/node', 30: '/bin/bash'},
                         {new_pid: '/v24.15/bin/node', 31: '/bin/sleep'},
                         {new_pid: '/v24.15/bin/node', 32: '/bin/ps'}]
            with self.subTest(new_pid=new_pid), mock.patch.object(node.sys, 'platform', 'darwin'), \
                 mock.patch.object(node.os, 'geteuid', return_value=0), \
                 mock.patch.object(node, 'kernel_snapshot', side_effect=snapshots) as snapshot, \
                 mock.patch.object(node, 'bind_node_images', side_effect=self.bound_images) as bind:
                result = node.native_snapshot(None)
            self.assertEqual(snapshot.call_count, 3)
            self.assertEqual(bind.call_count, 3)
            self.assertEqual({entry['path'] for entry in result['executables']},
                             {'/v24.14/bin/node', '/v24.15/bin/node'})

    def test_actual_node_churn_still_fails_closed(self):
        snapshots = [{1: '/v24.' + str(i) + '/bin/node'} for i in range(4)]
        with mock.patch.object(node.sys, 'platform', 'darwin'), mock.patch.object(node.os, 'geteuid', return_value=0), \
             mock.patch.object(node, 'kernel_snapshot', side_effect=snapshots), \
             mock.patch.object(node, 'bind_node_images', side_effect=self.bound_images):
            with self.assertRaisesRegex(ValueError, 'unstable snapshot'):
                node.native_snapshot(None)

    def test_same_path_replaced_image_is_not_merged_with_older_observation(self):
        bindings = [[{'path': '/bin/node', 'identity': [1, inode], 'pids': [11]}] for inode in [10, 20]]
        with mock.patch.object(node.sys, 'platform', 'darwin'), mock.patch.object(node.os, 'geteuid', return_value=0), \
             mock.patch.object(node, 'kernel_snapshot', return_value={11: '/bin/node'}), \
             mock.patch.object(node, 'bind_node_images', side_effect=bindings):
            with self.assertRaisesRegex(ValueError, 'executable identity changed'):
                node.native_snapshot(None)

    def test_unreadable_live_pid_is_not_ignored_by_node_only_stability(self):
        with mock.patch.object(node.sys, 'platform', 'darwin'), mock.patch.object(node.os, 'geteuid', return_value=0), \
             mock.patch.object(node, 'kernel_snapshot', side_effect=ValueError('Node process inspection cannot resolve live executable')), \
             mock.patch.object(node, 'bind_node_images') as bind:
            with self.assertRaisesRegex(ValueError, 'cannot resolve live executable'):
                node.native_snapshot(None)
        bind.assert_not_called()

    def test_observed_node_cannot_disappear_before_identity_binding(self):
        with mock.patch.object(node, 'process_table', return_value={}), \
             mock.patch.object(node.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr='')):
            with self.assertRaisesRegex(ValueError, 'observed image exited before binding'):
                node.bind_node_images({11: '/bin/node'}, None)

    def test_no_budget_left_does_not_launch_privileged_helper(self):
        with mock.patch.object(node.subprocess, 'run') as run:
            with self.assertRaises(TimeoutError):
                node.active_versions(time.monotonic() + 1)
        run.assert_not_called()

    def test_failed_helper_guard_reason_survives_parent_and_host_receipt(self):
        error = subprocess.CalledProcessError(1, ['helper'], stderr=node.HELPER_ERROR_PREFIX +
                                             'Node process inspection cannot bind running image\n')
        with mock.patch.object(node.subprocess, 'run', side_effect=error):
            with self.assertRaisesRegex(ValueError, 'cannot bind running image'):
                node.active_versions()
        path = node.ROOT / ('v24.14.1-arm64-cf738c9d-' + str(os.getuid()))
        candidate = prune.Candidate(str(path), node.KIND, str(path), 14, 1, 1, 1, 0)
        with mock.patch.object(node.subprocess, 'run', side_effect=error), \
             mock.patch.object(prune, 'capture_entry') as capture:
            removed, skipped = prune.apply_candidates([candidate])
        self.assertFalse(removed)
        self.assertTrue(skipped[0]['error'])
        self.assertFalse(skipped[0]['partially_removed'])
        self.assertIn('cannot bind running image', skipped[0]['reason'])
        capture.assert_not_called()

    def test_helper_errno_timeout_and_child_exit_are_normalized(self):
        cases = [(OSError(errno.ESRCH, 'private detail /secret/path'), 'operating-system error (errno 3)'),
                 (subprocess.TimeoutExpired(['child', '--token=secret'], 1, stderr='secret'), 'timed out'),
                 (subprocess.CalledProcessError(7, ['child', '--token=secret'], stderr='secret'), 'child failed (exit 7)')]
        for error, expected in cases:
            with self.subTest(error=type(error)):
                text = node.safe_failure_text(error)
                self.assertIn(expected, text)
                self.assertNotIn('secret', text)
                self.assertEqual(node.safe_helper_stderr(node.HELPER_ERROR_PREFIX + text + '\n'), text)

    def test_unknown_or_oversized_stderr_cannot_leak_into_receipt(self):
        for stderr in ['private argv --token=secret', node.HELPER_ERROR_PREFIX + 'Node process inspection unstable snapshot\nsecret',
                       node.HELPER_ERROR_PREFIX + 'x' * 1000, node.HELPER_ERROR_PREFIX + 'Node process inspection unstable snapshot secret']:
            error = subprocess.CalledProcessError(1, ['helper', '--token=secret'], stderr=stderr)
            with self.subTest(stderr=stderr[:60]), mock.patch.object(node.subprocess, 'run', side_effect=error):
                with self.assertRaisesRegex(ValueError, 'unrecognized diagnostic withheld') as caught:
                    node.active_versions()
            self.assertNotIn('secret', str(caught.exception))
            self.assertLess(len(str(caught.exception)), 160)
        self.assertEqual(node.safe_failure_text(ValueError('secret')), 'Node process inspection unclassified failure')

    def test_caller_timeout_reports_stage_without_command_or_stderr(self):
        error = subprocess.TimeoutExpired(['helper', '--token=secret'], 1, stderr='private environment')
        with mock.patch.object(node.subprocess, 'run', side_effect=error):
            with self.assertRaisesRegex(ValueError, '^Node process inspection helper timed out$'):
                node.active_versions()

    def test_native_missing_lsof_mapping_fails_closed(self):
        with mock.patch.object(node.sys, 'platform', 'darwin'), mock.patch.object(node.os, 'geteuid', return_value=0), \
             mock.patch.object(node, 'kernel_snapshot', return_value={42: '/bin/node'}), \
             mock.patch.object(node, 'process_table', return_value={42: 'S'}), \
             mock.patch.object(Path, 'stat', return_value=Path('/bin/sh').stat()), \
             mock.patch.object(node.subprocess, 'run', return_value=mock.Mock(returncode=1, stdout='', stderr='')):
            with self.assertRaisesRegex(ValueError, 'bind running image'):
                node.native_snapshot(None)


if __name__ == '__main__':
    unittest.main()
