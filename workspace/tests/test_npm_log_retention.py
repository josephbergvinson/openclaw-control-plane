from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from scripts import openclaw_storage_prune as prune


class NpmLogRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.logs = self.home / '.npm/_logs'
        self.logs.mkdir(parents=True)
        self.old = time.time() - 20 * 86400
        self.leaf = self.logs / '2000-01-02T03_04_05_006Z-debug-0.log'
        self.leaf.write_bytes(b'old npm diagnostic')
        os.utime(self.leaf, (self.old, self.old))
        patch = mock.patch.object(prune, 'USER_HOME', self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def discover(self):
        with mock.patch.object(prune, 'bounded_candidates',
                               return_value=[(self.leaf, prune.NPM_LOG_KIND, self.leaf, 14)]):
            return prune.discover()

    def apply(self, candidate):
        with mock.patch.object(prune, 'process_arguments', return_value=''), \
             mock.patch.object(prune, 'activity_reason', return_value=None):
            return prune.apply_candidates([candidate])

    def test_real_discovery_selects_regular_debug_log_and_preserves_unknowns(self):
        unknown = self.logs / 'notes.log'
        unknown.write_bytes(b'unknown stays')
        directory = self.logs / 'old-diagnostics'
        directory.mkdir()
        original_is_dir = Path.is_dir
        def isolated_is_dir(path):
            return original_is_dir(path) if self.home in path.parents or path == self.home else False
        with mock.patch.object(Path, 'is_dir', isolated_is_dir), \
             mock.patch.object(Path, 'is_mount', return_value=False), \
             mock.patch.object(prune, 'xcode_candidates', return_value=[]):
            candidates, skipped = prune.discover()
        self.assertEqual(skipped, [])
        self.assertEqual([c.path for c in candidates], [str(self.leaf)])
        removed, deferred = self.apply(candidates[0])
        self.assertEqual(deferred, [])
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0]['captured_inode_removed'])
        self.assertFalse(self.leaf.exists())
        self.assertEqual(unknown.read_bytes(), b'unknown stays')
        self.assertTrue(directory.is_dir())

    def test_hardlink_symlink_flagged_and_recent_files_are_rejected(self):
        other = self.logs / 'alias'
        os.link(self.leaf, other)
        self.assertEqual(self.discover()[0], [])
        other.unlink()
        self.leaf.unlink()
        self.leaf.symlink_to(other)
        self.assertEqual(self.discover()[0], [])
        self.leaf.unlink()
        self.leaf.write_bytes(b'recent')
        self.assertEqual(self.discover()[0], [])
        if hasattr(os, 'chflags'):
            os.utime(self.leaf, (self.old, self.old))
            os.chflags(self.leaf, 0x8000)
            try:
                self.assertEqual(self.discover()[0], [])
            finally:
                os.chflags(self.leaf, 0)

    def test_backdated_same_inode_change_after_discovery_is_preserved(self):
        candidate = self.discover()[0][0]
        self.leaf.write_bytes(b'new same inode diagnostic')
        os.utime(self.leaf, (self.old, self.old))
        removed, deferred = self.apply(candidate)
        self.assertEqual(removed, [])
        self.assertIn('changed after discovery', deferred[0]['reason'])
        self.assertEqual(self.leaf.read_bytes(), b'new same inode diagnostic')

    def test_new_file_at_original_name_is_preserved_after_capture(self):
        candidate = self.discover()[0][0]
        original_capture = prune.capture_entry
        @contextmanager
        def replace_name(*args, **kwargs):
            self.assertFalse(kwargs['directory'])
            with original_capture(*args, **kwargs) as captured:
                self.leaf.write_bytes(b'replacement belongs to another writer')
                yield captured
        with mock.patch.object(prune, 'capture_entry', replace_name):
            removed, deferred = self.apply(candidate)
        self.assertEqual(deferred, [])
        self.assertTrue(removed[0]['replacement_present'])
        self.assertEqual(self.leaf.read_bytes(), b'replacement belongs to another writer')

    def test_change_during_last_activity_check_prevents_unlink(self):
        candidate = self.discover()[0][0]
        def activity(path, *args, **kwargs):
            if path.name == 'payload':
                path.write_bytes(b'changed during activity inspection')
                os.utime(path, (self.old, self.old))
            return None
        with mock.patch.object(prune, 'process_arguments', return_value=''), \
             mock.patch.object(prune, 'activity_reason', side_effect=activity):
            removed, deferred = prune.apply_candidates([candidate])
        self.assertEqual(removed, [])
        self.assertIn('changed before validation', deferred[0]['reason'])
        self.assertEqual(self.leaf.read_bytes(), b'changed during activity inspection')

    def test_open_file_and_unavailable_inspection_are_preserved(self):
        candidate = self.discover()[0][0]
        for reason in ('open handles', 'open-handle inspection unavailable'):
            with mock.patch.object(prune, 'process_arguments', return_value=''), \
                 mock.patch.object(prune, 'activity_reason', return_value=reason):
                removed, deferred = prune.apply_candidates([candidate])
            self.assertEqual(removed, [])
            self.assertEqual(deferred[0]['reason'], reason)
            self.assertTrue(self.leaf.exists())

    def test_regular_file_inspection_uses_privileged_exact_path(self):
        result = subprocess.CompletedProcess([], 1, '', '')
        with mock.patch.object(prune.subprocess, 'run', return_value=result) as run:
            self.assertIsNone(prune.activity_reason(self.leaf, '', directory=False,
                                                    ignore_current_process=True))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['/usr/bin/sudo', '-n', '/usr/sbin/lsof', '-nP'])
        self.assertEqual(command[-1], str(self.leaf))
        self.assertNotIn('+D', command)
        self.assertIn('^' + str(os.getpid()), command)


if __name__ == '__main__':
    unittest.main()
