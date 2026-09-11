from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from scripts import storage_headroom_guard as guard


class StorageHeadroomGuardTests(unittest.TestCase):
    def disks(self, internal=40, external=1500):
        return {'internal_root': {'level': guard.classify_free_gib(internal), 'gib': {'free': internal}},
                'workspace': {'level': guard.classify_free_gib(external, external=True), 'gib': {'free': external}}}

    def run_guard(self, before, after=None, cleanup=None, apply=False, swap=None):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with mock.patch.object(guard, 'ARTIFACT_ROOT', Path(tmp)), \
                 mock.patch.object(guard, 'check_disks', side_effect=[before] + ([after] if after else [])), \
                 mock.patch.object(guard, 'get_swap_usage', return_value=swap or {'available': False}), \
                 mock.patch.object(guard, 'run_cleanup', return_value=cleanup) as action, redirect_stdout(buf):
                rc = guard.main(['--apply'] if apply else ['--check-only'])
            latest = Path(tmp) / 'latest.json'
            payload = json.loads(latest.read_text()) if latest.exists() else None
            return rc, buf.getvalue().strip(), payload, action.call_count

    def test_success_is_always_human_visible(self):
        rc, text, payload, calls = self.run_guard(self.disks())
        self.assertEqual(rc, 0)
        self.assertIn('Mac mini 40.0 GiB free', text)
        self.assertIn('OWC 1500.0 GiB free', text)
        self.assertNotIn('NO_REPLY', text)
        self.assertEqual(payload['alert_authority'], 'both_disks')
        self.assertEqual(calls, 0)

    def test_external_pressure_is_an_alert_even_when_internal_healthy(self):
        rc, text, payload, _ = self.run_guard(self.disks(external=15))
        self.assertEqual(rc, 1)
        self.assertEqual(payload['level'], 'emergency')
        self.assertIn('OWC 15.0 GiB free (emergency)', text)

    def test_pressure_runs_cleanup_and_remeasures_both_disks(self):
        rc, text, payload, calls = self.run_guard(self.disks(5), self.disks(45),
                {'status': 'ok', 'errors': [], 'report': '/receipt'}, apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, 1)
        self.assertEqual(payload['action'], 'guarded_cleanup_attempted')
        self.assertIn('Mac mini 45.0 GiB free', text)
        self.assertIn('Safe cleanup completed', text)

    def test_cleanup_does_not_mask_remaining_pressure(self):
        rc, text, payload, _ = self.run_guard(self.disks(5), self.disks(9),
                {'status': 'ok', 'errors': [], 'report': '/receipt'}, apply=True)
        self.assertEqual(rc, 1)
        self.assertIn('More space is needed', text)

    def test_missing_mount_is_visible_and_does_not_write_shadow_receipt(self):
        disks = self.disks()
        disks['workspace'] = {'level': 'unavailable'}
        rc, text, payload, calls = self.run_guard(disks, apply=True)
        self.assertEqual(rc, 1)
        self.assertIn('OWC unavailable', text)
        self.assertIsNone(payload)
        self.assertEqual(calls, 0)

    def test_receipt_validation_rejects_arrays_bad_types_and_status(self):
        for payload in [[], None, {'schema': 'openclaw.storage_prune.v1', 'terminal': True, 'mode': 'apply', 'status': 'ok', 'errors': 'bad'}]:
            with mock.patch.object(guard.subprocess, 'run', return_value=mock.Mock(returncode=0, stdout=json.dumps(payload))):
                with self.assertRaises(RuntimeError):
                    guard.run_cleanup()

    def test_receipt_write_error_is_human_failure(self):
        with mock.patch.object(guard.openclaw_storage_prune, 'write_receipt', side_effect=OSError('full')):
            rc, text, _, _ = self.run_guard(self.disks())
        self.assertEqual(rc, 1)
        self.assertIn('could not save its receipt', text)

    def test_distinct_capacity_thresholds(self):
        self.assertEqual(guard.classify_free_gib(40), 'ok')
        self.assertEqual(guard.classify_free_gib(40, external=True), 'critical')
        self.assertEqual(guard.classify_free_gib(5), 'emergency')

    def test_persistent_internal_pressure_explains_swap_without_touching_it(self):
        swap = {'available': True, 'used_bytes': 20 * 1024**3, 'total_bytes': 21 * 1024**3}
        rc, text, payload, calls = self.run_guard(self.disks(19), swap=swap)
        self.assertEqual(rc, 1)
        self.assertIn('Memory spillover uses 20.0 GiB of disk; reduce concurrent heavy work.', text)
        self.assertEqual(payload['system_swap'], swap)
        self.assertEqual(calls, 0)  # Diagnostic cannot dispatch a cleanup or swap mutation.

    def test_healthy_disk_keeps_swap_diagnostics_out_of_success_message(self):
        rc, text, payload, _ = self.run_guard(self.disks(), swap={'available': True, 'used_bytes': 20 * 1024**3})
        self.assertEqual(rc, 0)
        self.assertNotIn('spillover', text)
        self.assertEqual(payload['system_swap']['used_bytes'], 20 * 1024**3)

    def test_swap_probe_is_read_only_sysctl_with_bounded_timeout(self):
        result = mock.Mock(stdout='total = 21504.00M used = 20200.25M free = 1303.75M (encrypted)')
        with mock.patch.object(guard.subprocess, 'run', return_value=result) as command:
            swap = guard.get_swap_usage()
        command.assert_called_once_with(['/usr/sbin/sysctl', '-n', 'vm.swapusage'],
                                        capture_output=True, text=True, timeout=5, check=True)
        self.assertEqual(swap['used_bytes'], round(20200.25 * 1024**2))
        self.assertEqual(swap['total_bytes'], 21 * 1024**3)
        self.assertTrue(swap['available'])

    def test_unavailable_swap_is_unknown_and_does_not_override_disk_status(self):
        with mock.patch.object(guard.subprocess, 'run', return_value=mock.Mock(stdout='not available')):
            swap = guard.get_swap_usage()
        self.assertFalse(swap['available'])
        self.assertNotIn('used_bytes', swap)
        rc, text, _, _ = self.run_guard(self.disks(), swap=swap)
        self.assertEqual(rc, 0)
        self.assertNotIn('spillover', text)


if __name__ == '__main__':
    unittest.main()
