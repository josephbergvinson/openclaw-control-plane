import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from scripts import openclaw_retention_cleanup_cron as cron


class MaintenanceReportingTests(unittest.TestCase):
    def host_payload(self):
        return {'schema': 'openclaw.storage_prune.v1', 'mode': 'apply',
                'status': 'ok', 'terminal': True, 'errors': [], 'removed': [],
                'after_free_bytes': {'internal': 40 * 1024**3, 'owc': 1500 * 1024**3}}

    def emit(self, children, predicates=(), apply=True):
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(cron, 'WORKSPACE', Path(raw)):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cron.emit_human_result(children, list(predicates), apply=apply)
                paths = list((Path(raw) / 'artifacts/maintenance_retention').glob('*.json'))
                self.assertEqual(len(paths), 1)
                receipt = json.loads(paths[0].read_text())
                self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        return code, out.getvalue(), receipt

    def test_reports_success_and_both_disks_without_telemetry(self):
        children = [cron.ChildResult('host_storage', 0, json.dumps(self.host_payload()))]
        code, output, receipt = self.emit(children)
        self.assertEqual(code, 0)
        self.assertEqual(output, 'Daily cleanup completed. Free space: Mac 40.0 GiB; OWC 1500 GiB.\n')
        self.assertEqual(receipt['status'], 'ok')

    def test_partial_success_cannot_hide_failed_child(self):
        code, output, receipt = self.emit([
            cron.ChildResult('host_storage', 0, json.dumps(self.host_payload())),
            cron.ChildResult('runtime_releases', 124, stderr='private diagnostic', timed_out=True),
        ])
        self.assertEqual(code, 1)
        self.assertIn('runtime cleanup did not finish successfully', output)
        self.assertNotIn('private diagnostic', output)
        self.assertEqual(receipt['children'][1]['stderr'], 'private diagnostic')

    def test_failed_effect_readback_cannot_be_reported_as_success(self):
        code, output, _ = self.emit([], [{'satisfied': False}])
        self.assertEqual(code, 1)
        self.assertIn('verification', output)

    def test_host_partial_and_preview_receipts_fail_apply_validation(self):
        for field, value in [('status', 'partial'), ('mode', 'preview'), ('terminal', False), ('errors', ['unreadable'])]:
            payload = self.host_payload()
            payload[field] = value
            self.assertFalse(all(p['satisfied'] for p in cron.validate_host_report(payload, apply=True)))

    def test_removed_path_requires_actual_absence(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'still-here'
            path.mkdir()
            payload = self.host_payload()
            payload['removed'] = [{'path': str(path)}]
            self.assertFalse(all(p['satisfied'] for p in cron.validate_host_report(payload, apply=True)))

    def test_preview_does_not_claim_cleanup_was_applied(self):
        code, output, _ = self.emit([], apply=False)
        self.assertEqual(code, 0)
        self.assertIn('no files were removed', output)

    def test_real_timeout_with_output_still_saves_concise_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            script = Path(raw) / 'slow.py'
            script.write_text("import time\nprint('private child output', flush=True)\ntime.sleep(5)\n")
            with mock.patch.object(cron, 'WORKSPACE', Path(raw)):
                result = cron.run_child('runtime_releases', script, timeout_seconds=0.2)
            self.assertTrue(result.timed_out)
            self.assertIsInstance(result.stdout, str)
            code, output, receipt = self.emit([result])
            self.assertEqual(code, 1)
            self.assertNotIn('private child output', output)
            self.assertIn('private child output', receipt['children'][0]['stdout'])

    def test_receipt_write_failure_has_honest_human_outcome(self):
        with mock.patch.object(cron.os, 'open', side_effect=OSError('disk full')):
            with mock.patch.object(cron.Path, 'mkdir'):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cron.emit_human_result([], [], apply=True)
        self.assertEqual(code, 1)
        self.assertIn('completion is unverified', output.getvalue())


if __name__ == '__main__':
    unittest.main()
