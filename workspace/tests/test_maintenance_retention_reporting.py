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
            with mock.patch.object(cron, 'WORKSPACE', Path(raw)), \
                 mock.patch.object(cron, 'final_free_space', return_value={
                     'internal': 40 * 1024**3, 'owc': 1500 * 1024**3}):
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
        self.assertEqual(output, 'Daily cleanup completed: no eligible items needed removal. '
                                'Mac: 40.0 GiB free; OWC: 1,500 GiB free.\n')
        self.assertEqual(receipt['status'], 'ok')

    def test_partial_success_cannot_hide_failed_child(self):
        code, output, receipt = self.emit([
            cron.ChildResult('host_storage', 0, json.dumps(self.host_payload())),
            cron.ChildResult('runtime_releases', 124, stderr='private diagnostic', timed_out=True),
        ])
        self.assertEqual(code, 1)
        self.assertIn('Daily cleanup incomplete: runtime cleanup did not finish.', output)
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

    def test_budget_partial_summary_reports_verified_removals_and_28_pending_targets(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            payload = self.host_payload()
            payload['removed'] = [{'path': str(root / f'archive-{i}.tar'), 'original_path':str(root / f'archive-{i}.tar'),
                                   'kind': 'unused-owc-backup-file',
                                   'allocated_bytes': 1024} for i in range(17)]
            def pending(path, stage):
                return {'path':str(path), 'stage':stage, 'cause':'execution_budget',
                        'state':'not_started', 'effects':'none', 'deferred':True,
                        'not_started':True, 'error':True, 'target_count':1, 'partially_removed':False}
            payload['deferred'] = [pending(root / f'pending-{i}', 'host_candidates') for i in range(26)]
            payload['deferred'].extend(pending(root / f'simulator-{i}', 'simulators') for i in range(2))
            payload['errors'] = list(payload['deferred'])
            payload['summary'] = {'removed_items':17, 'removed_cache_files':0,
                'deferred_targets':28, 'deferred_stages':2, 'budget_deferred_targets':28,
                'budget_deferred_stages':2, 'uncertain_effects':0, 'unclassified_deferred_stages':0, 'reclaimed_allocated_bytes':17 * 1024}
            payload['status'] = 'partial'
            code, output, receipt = self.emit([cron.ChildResult('host_storage', 1, json.dumps(payload))])
        self.assertEqual(code, 1)
        self.assertIn('removed 17 backup archives', output)
        self.assertIn('28 targets remain deferred across 2 stages', output)
        self.assertIn('Verified host removals reclaimed 17.0 KiB', output)
        self.assertIn('Mac: 40.0 GiB free; OWC: 1,500 GiB free.', output)
        self.assertNotIn('Inspect', output)
        self.assertNotIn(str(root), output)
        self.assertEqual(receipt['outcome_summary'], output.strip())

    def test_partial_summary_never_counts_unverified_or_partial_effect(self):
        with tempfile.TemporaryDirectory() as raw:
            present = Path(raw).resolve() / 'still-present'
            present.mkdir()
            payload = self.host_payload()
            payload.update(status='partial', errors=['custody failed'], removed=[{'path':str(present)}])
            code, output, _ = self.emit([cron.ChildResult('host_storage', 1, json.dumps(payload))])
        self.assertEqual(code, 1)
        self.assertNotIn('removed 1', output)

    def test_malformed_host_missing_allocated_bytes_becomes_typed_partial(self):
        with tempfile.TemporaryDirectory() as raw:
            payload = self.host_payload()
            payload.update(removed=[{'path':str(Path(raw).resolve() / 'missing')}], summary={})
            child = cron.ChildResult('host_storage', 0, json.dumps(payload))
            checks = cron.load_retention_effects([child], apply=True)
            self.assertFalse(all(item['satisfied'] for item in checks))
            self.assertTrue(any(item['name'] == 'host_storage_report' for item in checks))
            code, output, receipt = self.emit([child], checks)
        self.assertEqual(code, 1)
        self.assertIn('incomplete', output)
        self.assertEqual(receipt['status'], 'failed')

    def test_partial_host_summary_includes_independently_verified_runtime_progress(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            runtime = {'schema':cron.REPORT_SCHEMAS['runtime_releases'], 'mode':'apply', 'terminal':True,
                'errors':[], 'summary':{'error_count':0, 'removed_count':7},
                'removed':[f'runtime-{i}' for i in range(7)],
                'receipts':[{'state':'removed', 'path':str(root / f'runtime-{i}'), 'after_exists':False} for i in range(7)]}
            path = root / 'runtime-report.json'
            path.write_text(json.dumps(runtime))
            host = self.host_payload()
            host.update(status='partial', errors=['execution budget exhausted'])
            code, output, _ = self.emit([cron.ChildResult('host_storage', 1, json.dumps(host)),
                cron.ChildResult('runtime_releases', 0, 'STATUS | report: ' + str(path))])
        self.assertEqual(code, 1)
        self.assertIn('removed 7 runtimes', output)
        self.assertNotIn(str(path), output)

    def test_receipt_write_failure_has_honest_human_outcome(self):
        with mock.patch.object(cron.os, 'open', side_effect=OSError('disk full')), \
             mock.patch.object(cron, 'final_free_space', return_value={
                 'internal': 40 * 1024**3, 'owc': 1500 * 1024**3}):
            with mock.patch.object(cron.Path, 'mkdir'):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = cron.emit_human_result([], [], apply=True)
        self.assertEqual(code, 1)
        self.assertIn('completion is unverified', output.getvalue())


if __name__ == '__main__':
    unittest.main()
