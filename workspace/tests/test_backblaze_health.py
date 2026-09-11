from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from scripts import backblaze_health as guard
from scripts import backblaze_resource_watchdog as watchdog

NOW = 1800000000
HGUID = '1' * 24
IDENTITY = guard.resources.identity_fingerprint(HGUID)


def healthy(**changes):
    data = dict(backup_identity_sha256=IDENTITY, transmitter_process_running=False, watchdog_status='healthy', memory_pressure_level=1, heavy_build_running=False, memory_available_gib=14, memory_required_gib=12, memory_free_percent=85,
                swap_used_gib=1, owc_mounted=True, owc_identity_verified=True, owc_selected=True, internal_selected=True, scratch_is_owc=True,
                internal_free_gib=45, owc_free_gib=1500, license_status='billing_active',
                safety_freeze='not_frozen', transmit_state='not_running',
                remaining_files=0, remaining_bytes=0, last_backup_epoch=NOW - 20, schedule='only_when_click_backup_now',
                owc_scan_epoch=NOW - 30, internal_scan_epoch=NOW - 30, remaining_report_epoch=NOW - 10)
    data.update(changes)
    return data


class BackblazeHealthTests(unittest.TestCase):
    def setUp(self):
        # Never consult the host's real installation hold in fixture tests.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        control = patch.object(guard.resources, 'CONTROL_DIR', Path(temporary.name))
        control.start()
        self.addCleanup(control.stop)

    def test_stale_zero_remaining_never_means_success(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 3600}
        s = healthy(last_backup_epoch=NOW - 40 * guard.DAY)
        self.assertFalse(guard.completed(s, state, NOW))
        status, _, request = guard.reconcile(s, {}, NOW, False)
        self.assertEqual(status, 'stale')
        self.assertFalse(request)

    def test_fresh_completion_requires_requested_scan_and_remaining_report(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 3600}
        for key in ('owc_scan_epoch', 'internal_scan_epoch', 'remaining_report_epoch', 'last_backup_epoch'):
            with self.subTest(key=key):
                self.assertFalse(guard.completed(healthy(**{key: NOW - 4000}), state, NOW))
        self.assertTrue(guard.completed(healthy(), state, NOW))
        status, text, request = guard.reconcile(healthy(), state, NOW, False)
        self.assertEqual(status, 'completed')
        self.assertIn('restore check', text)
        self.assertFalse(request)

    def test_active_run_and_nonzero_queue_never_report_completion(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 3600}
        self.assertFalse(guard.completed(healthy(remaining_files=1), state, NOW))
        self.assertFalse(guard.completed(healthy(remaining_bytes=1), state, NOW))
        s = healthy(transmit_state='transmitting')
        self.assertFalse(guard.completed(s, state, NOW))
        self.assertEqual(guard.reconcile(s, state, NOW, True)[0], 'running')

    def test_guard_refuses_missing_drive_low_space_wrong_scratch_or_freeze(self):
        for changes in ({'owc_mounted': False}, {'owc_identity_verified': False}, {'owc_selected': False},
                        {'scratch_is_owc': False}, {'internal_free_gib': 29.9},
                        {'memory_available_gib': 10}, {'heavy_build_running': True},
                        {'owc_free_gib': 19.9}, {'license_status': 'trial_expired'},
                        {'safety_freeze': 'frozen'}):
            with self.subTest(changes=changes):
                status, text, request = guard.reconcile(healthy(**changes), {}, NOW, True)
                self.assertEqual(status, 'blocked')
                self.assertFalse(request)
                self.assertIn('protected', text)

    def test_running_catalog_does_not_fail_its_larger_start_threshold(self):
        s = healthy(transmit_state='transmitting', internal_free_gib=25)
        self.assertEqual(guard.reconcile(s, {}, NOW, False)[0], 'running')
        self.assertEqual(guard.reconcile(healthy(internal_free_gib=9), {}, NOW, False)[0], 'blocked')

    def test_running_without_progress_becomes_actionable(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 4 * guard.DAY, 'last_progress_epoch': NOW - 3 * guard.DAY}
        status, _, request = guard.reconcile(healthy(transmit_state='transmitting'), state, NOW, False)
        self.assertEqual(status, 'stalled')
        self.assertFalse(request)

    def test_next_weekly_start_resets_old_completion_scope(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 2 * guard.DAY, 'last_attempt_epoch': NOW - 2 * guard.DAY}
        result = subprocess.CompletedProcess([], 0, '', '')
        payload, code, stored, calls = self.run_with_snapshot(healthy(), [result], state)
        self.assertEqual(stored['requested_epoch'], NOW)
        self.assertNotIn('completed_epoch', stored)
        self.assertEqual(payload['status'], 'requested')

    def test_interrupted_pending_run_resumes_with_twelve_hour_backoff(self):
        s = healthy(remaining_files=100)
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - guard.DAY, 'last_attempt_epoch': NOW - 60}
        self.assertFalse(guard.reconcile(s, state, NOW, False)[2])
        state['last_attempt_epoch'] = NOW - guard.RETRY_SECONDS
        self.assertTrue(guard.reconcile(s, state, NOW, False)[2])

    def run_with_snapshot(self, s, response, state=None, *, start=True, config_action=None, control=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            if state:
                (root / 'state.json').write_text(json.dumps(state))
            with patch.object(guard, 'ARTIFACT_ROOT', root), \
                 patch.object(guard, 'cli_report', return_value={}), \
                 patch.object(guard, 'snapshot', return_value=s), \
                 patch.object(guard.resources, 'installed_identity', return_value=IDENTITY), \
                 patch.object(guard.time, 'time', return_value=NOW), \
                 patch.object(guard, 'configure_schedule', side_effect=config_action) as configure, \
                 patch.object(guard.resources, 'read_control', return_value=control or {}), \
                 patch.object(guard.resources, 'watchdog_status', return_value='healthy'), \
                 patch.object(guard.resources, 'CONTROL_DIR', root), \
                 patch.object(guard.resources, 'PAUSE_STATE', root / 'resource-state.json'), \
                 patch.object(guard, 'memory_status', return_value=healthy()), \
                 patch.object(guard.shutil, 'disk_usage', return_value=Mock(free=50 * guard.GIB)), \
                 patch.object(guard.resources, 'request_pause', side_effect=lambda reason: guard.subprocess.run([str(guard.BZCLI), 'action', '--pause-backup'])) as pause, \
                 patch.object(guard.subprocess, 'run', side_effect=response) as command:
                payload, code = guard.run(start=start)
                stored = json.loads((root / 'state.json').read_text()) if (root / 'state.json').exists() else {}
                return payload, code, stored, command.call_args_list

    def test_supported_start_records_intent_and_reports_request_only(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        payload, code, state, calls = self.run_with_snapshot(healthy(remaining_files=2), [result])
        self.assertEqual(code, 0)
        self.assertEqual(payload['status'], 'requested')
        self.assertNotIn('completed_epoch', state)
        self.assertEqual(state['requested_epoch'], NOW)
        self.assertEqual(state['backup_identity_sha256'], IDENTITY)
        self.assertEqual(state['restore_schedule'], 'only_when_click_backup_now')
        self.assertEqual(calls[0].args[0], [str(guard.BZCLI), 'action', '--backup-now'])

    def test_timed_out_action_is_ambiguous_and_not_immediately_retried(self):
        payload, code, state, calls = self.run_with_snapshot(
            healthy(remaining_files=2), subprocess.TimeoutExpired('bzcli', 60))
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'uncertain')
        self.assertEqual(len(calls), 1)
        self.assertFalse(guard.reconcile(healthy(remaining_files=2), state, NOW + 1, False)[2])

    def test_failed_action_does_not_become_backup_success(self):
        result = subprocess.CompletedProcess([], 1, 'private account data', 'private error')
        payload, code, state, _ = self.run_with_snapshot(healthy(remaining_files=2), [result])
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'failed')
        self.assertNotIn('private', json.dumps(payload))
        self.assertNotIn('completed_epoch', state)

    def test_failed_continuous_configuration_preserves_original_mode_and_skips_start(self):
        payload, code, state, calls = self.run_with_snapshot(
            healthy(remaining_files=5), [], config_action=RuntimeError('configuration failed'))
        self.assertEqual(code, 1)
        self.assertEqual(state['restore_schedule'], 'only_when_click_backup_now')
        self.assertEqual(calls, [])

    def test_completion_restores_original_manual_schedule(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - guard.DAY, 'restore_schedule': 'only_when_click_backup_now'}
        modes = []
        payload, code, stored, calls = self.run_with_snapshot(
            healthy(schedule='continuously'), [], state, start=False, config_action=modes.append)
        self.assertEqual(code, 0)
        self.assertEqual(payload['status'], 'completed')
        self.assertEqual(modes, ['only_when_click_backup_now'])
        self.assertNotIn('restore_schedule', stored)
        self.assertEqual(calls, [])

    def test_pending_active_run_repairs_interrupted_schedule_configuration(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 30, 'restore_schedule': 'only_when_click_backup_now'}
        modes = []
        payload, code, stored, calls = self.run_with_snapshot(
            healthy(transmit_state='transmitting'), [], state, start=False, config_action=modes.append)
        self.assertEqual(code, 0)
        self.assertEqual(modes, ['continuously'])
        self.assertEqual(calls, [])

    def test_unknown_transmitter_state_is_not_assumed_running(self):
        with self.assertRaises(ValueError):
            guard.snapshot({'backup': {'status': {'bztransmit': 'unrecognized'}}}, NOW)

    def test_dangerous_disk_pressure_actually_requests_supported_pause(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 3600, 'swap_at_start_gib': 1}
        payload, code, stored, calls = self.run_with_snapshot(
            healthy(transmit_state='transmitting', internal_free_gib=9), [result], state, start=False)
        self.assertEqual(code, 1)
        self.assertTrue(stored['paused_for_resources'])
        self.assertEqual(calls[0].args[0], [str(guard.BZCLI), 'action', '--pause-backup'])
        self.assertEqual(payload['status'], 'blocked')

    def test_swap_occupancy_and_lifetime_growth_alone_do_not_pause(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 3600, 'swap_at_start_gib': 1}
        payload, code, stored, calls = self.run_with_snapshot(
            healthy(transmit_state='transmitting', internal_free_gib=22, swap_used_gib=8),
            [result], state, start=False)
        self.assertNotIn('paused_for_resources', stored)
        self.assertEqual(payload['status'], 'running')
        self.assertEqual(calls, [])

    def test_resource_pause_never_restarts_before_memory_and_disk_recover(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 3600, 'paused_for_resources': True}
        for changes in ({'internal_free_gib': 29}, {'memory_available_gib': 8}, {'memory_pressure_level': 2}):
            with self.subTest(changes=changes):
                self.assertFalse(guard.reconcile(healthy(remaining_files=1, **changes), state, NOW, False)[2])
        self.assertTrue(guard.reconcile(healthy(remaining_files=1), state, NOW, False)[2])

    def test_watchdog_uses_current_pressure_without_heavy_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'overviewstatus.xml').write_text('<status><bztransmit cur_state="transmitting" /></status>')
            (root / 'bzinfo.xml').write_text('<bzinfo><do_backup backup_schedule_type="continuously" /></bzinfo>')
            with patch.object(watchdog.resources, 'BZDATA', root), \
                 patch.object(watchdog.resources, 'catalog_size_gib', return_value=1), \
                 patch.object(watchdog.resources, 'installed_identity', return_value=IDENTITY), \
                 patch.object(watchdog.resources, 'process_status', return_value={'transmitter_process_running': True}), \
                 patch.object(watchdog.resources, 'memory_status', return_value=healthy(swap_used_gib=9, memory_pressure_level=2)), \
                 patch.object(watchdog.shutil, 'disk_usage', return_value=Mock(free=22 * guard.GIB)), \
                 patch.object(watchdog.resources, 'request_pause') as pause, \
                 patch.object(guard, 'cli_report', side_effect=AssertionError('heavy report forbidden')):
                result = watchdog.run()
            self.assertEqual(result['status'], 'pause_requested')
            pause.assert_called_once_with('memory_or_disk_pressure')

    def test_watchdog_idle_skips_resource_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'overviewstatus.xml').write_text('<status><bztransmit cur_state="not_running" /></status>')
            (root / 'bzinfo.xml').write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now" /></bzinfo>')
            with patch.object(watchdog.resources, 'BZDATA', root), \
                 patch.object(watchdog.resources, 'catalog_size_gib', return_value=1), \
                 patch.object(watchdog.resources, 'installed_identity', return_value=IDENTITY), \
                 patch.object(watchdog.resources, 'process_status', return_value={'transmitter_process_running': False}), \
                 patch.object(watchdog.resources, 'memory_status', side_effect=AssertionError('idle must be cheap')):
                self.assertEqual(watchdog.run()['status'], 'idle')

    def test_stale_vendor_transmitting_without_process_is_idle_for_watchdog(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'overviewstatus.xml').write_text('<status><bztransmit cur_state="transmitting" /></status>')
            (root / 'bzinfo.xml').write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now" /></bzinfo>')
            with patch.object(watchdog.resources, 'BZDATA', root), \
                 patch.object(watchdog.resources, 'catalog_size_gib', return_value=1), \
                 patch.object(watchdog.resources, 'installed_identity', return_value=IDENTITY), \
                 patch.object(watchdog.resources, 'process_status', return_value={'transmitter_process_running': False}), \
                 patch.object(watchdog.resources, 'memory_status', side_effect=AssertionError('must not infer live upload')):
                self.assertEqual(watchdog.run()['status'], 'idle')

    def test_process_liveness_checks_exact_installed_executable(self):
        with patch.object(guard.resources, 'output', return_value=f'1234 /tmp/bztransmit\n3000 {guard.resources.BZDATA.parent / "bztransmit"}\n'):
            self.assertTrue(guard.resources.process_status()['transmitter_process_running'])
        with patch.object(guard.resources, 'output', return_value='1234 /tmp/bztransmit\n'):
            self.assertFalse(guard.resources.process_status()['transmitter_process_running'])
        with patch.object(guard.resources, 'output', side_effect=ValueError('unreadable')):
            with self.assertRaises(ValueError):
                guard.resources.process_status()

    def test_empty_or_malformed_process_snapshot_is_not_idle(self):
        for value in ('', '\n ', 'malformed', '-1 /Library/Backblaze.bzpkg/bztransmit',
                      'x /bin/tool', '10 /bin/tool\ninvalid'):
            with self.subTest(value=value), patch.object(guard.resources, 'output', return_value=value):
                with self.assertRaises(ValueError):
                    guard.resources.process_status()

    def test_missing_watchdog_receipt_blocks_a_start(self):
        status, message, request = guard.reconcile(healthy(watchdog_status='stale'), {}, NOW, True)
        self.assertEqual(status, 'blocked')
        self.assertIn('resource supervisor', message)
        self.assertFalse(request)

    def test_watchdog_receipt_freshness_and_failures(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(guard.resources, 'CONTROL_DIR', Path(temp)):
            path = Path(temp) / 'backblaze-watchdog-latest.json'
            self.assertEqual(guard.resources.watchdog_status(NOW), 'unavailable')
            for status, epoch, expected in [('idle', NOW, 'healthy'), ('observing', NOW-361, 'stale'),
                                             ('error', NOW, 'failed'), ('idle', NOW+61, 'stale')]:
                path.write_text(json.dumps({'status': status, 'observed_epoch': epoch, 'source_sha256': guard.resources.SOURCE_IDENTITIES}))
                self.assertEqual(guard.resources.watchdog_status(NOW), expected)

    def test_native_tsgolint_blocks_admission_without_classifying_unrelated_processes(self):
        compiler = ('/example/runtime/node_modules/.pnpm/'
                    '@oxlint-tsgolint+darwin-arm64@7.0.2001/node_modules/@oxlint-tsgolint/darwin-arm64/tsgolint')
        with patch.object(guard.resources, 'output', return_value='32768 ' + compiler + '\n'):
            processes = guard.resources.process_status()
        self.assertEqual(processes, {'heavy_build_running': True, 'transmitter_process_running': False})
        self.assertIn('an active build must finish before backup resumes',
                      guard.resources.resource_blockers(healthy(**processes)))
        for name in ('/usr/bin/ordinary-process', '/opt/tools/tsgolint-helper', '/opt/tools/esbuild'):
            with self.subTest(name=name), patch.object(guard.resources, 'output', return_value='32768 ' + name + '\n'):
                self.assertFalse(guard.resources.process_status()['heavy_build_running'])

    def test_outdated_internal_watchdog_code_blocks_admission(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(guard.resources, 'CONTROL_DIR', Path(temp)):
            (Path(temp) / 'backblaze-watchdog-latest.json').write_text(json.dumps(
                {'status': 'idle', 'observed_epoch': NOW, 'source_sha256': {'old': 'code'}}))
            self.assertEqual(guard.resources.watchdog_status(NOW), 'outdated')

    def test_watchdog_saves_private_error_receipt(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(guard.resources, 'CONTROL_DIR', Path(temp)), \
             patch.object(watchdog, 'run', side_effect=ValueError('private details')), patch('builtins.print'):
            self.assertEqual(watchdog.main(), 1)
            data = json.loads((Path(temp) / 'backblaze-watchdog-latest.json').read_text())
            self.assertEqual(data['status'], 'error')
            self.assertNotIn('private details', json.dumps(data))

    def test_accepted_pause_is_not_dispatched_repeatedly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bzinfo.xml').write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now" /></bzinfo>')
            with patch.object(guard.resources, 'CONTROL_DIR', root), \
                 patch.object(guard.resources, 'BZDATA', root), \
                 patch.object(guard.resources, 'read_control', return_value={'paused': True, 'pause_accepted_epoch': NOW-100, 'backup_identity_sha256': IDENTITY}), \
                 patch.object(guard.resources, 'installed_identity', return_value=IDENTITY), \
                 patch.object(guard.resources, 'configure_schedule') as configure, \
                 patch.object(guard.resources.subprocess, 'run') as command:
                guard.resources.request_pause('pressure')
                configure.assert_not_called()
                command.assert_not_called()

    def test_pause_that_has_not_stopped_is_actionable_after_fifteen_minutes(self):
        state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW-3600, 'paused_for_resources': True, 'pause_requested_epoch': NOW-901}
        status, message, request = guard.reconcile(healthy(transmit_state='transmitting'), state, NOW, False)
        self.assertEqual(status, 'pause_stalled')
        self.assertIn('has not stopped', message)
        self.assertFalse(request)

    def test_disk_full_receipt_failure_is_concise(self):
        with patch.object(guard, 'run_locked', side_effect=OSError('disk full private path')):
            payload, code = guard.run()
        self.assertEqual(code, 1)
        self.assertIn('free disk space', payload['message'])
        self.assertNotIn('private path', json.dumps(payload))

    def test_broken_provider_data_fails_closed_without_raw_output(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(guard, 'ARTIFACT_ROOT', Path(temp)), \
             patch.object(guard, 'cli_report', side_effect=ValueError('private account secret')):
            payload, code = guard.run()
            self.assertEqual(code, 1)
            self.assertEqual(payload['status'], 'error')
            self.assertNotIn('private account', json.dumps(payload))

    def test_identity_fingerprint_validates_and_does_not_expose_raw_identity(self):
        for value in (None, '', 'none', '0' * 24, 'not-a-guid', 'a' * 23, 'g' * 24):
            with self.subTest(value=value), self.assertRaises(ValueError):
                guard.resources.identity_fingerprint(value)
        self.assertEqual(len(IDENTITY), 64)
        self.assertNotIn(HGUID, IDENTITY)

    def test_report_identity_must_match_current_local_installation(self):
        report = {'backup': {'installation': {'hguid': HGUID}}}
        with patch.object(guard.resources, 'installed_identity', return_value=IDENTITY):
            self.assertEqual(guard.observed_identity(report), IDENTITY)
        with patch.object(guard.resources, 'installed_identity', return_value='b' * 64):
            with self.assertRaises(guard.IdentityBindingError):
                guard.observed_identity(report)
        for installation in ({}, {'hguid': None}, None, [], 'not an object', 123):
            with self.assertRaises(guard.IdentityBindingError):
                guard.observed_identity({'backup': {'installation': installation}})

    def test_malformed_installation_reports_are_concise_and_request_protective_pause(self):
        for installation in (None, [], 'invalid'):
            with self.subTest(installation=installation), tempfile.TemporaryDirectory() as temp:
                report = {'backup': {'status': {'bztransmit': 'not_running'}, 'installation': installation}}
                with patch.object(guard, 'ARTIFACT_ROOT', Path(temp)), patch.object(guard.resources, 'read_control', return_value={}), \
                     patch.object(guard, 'cli_report', return_value=report), patch.object(guard.resources, 'request_pause') as pause:
                    payload, code = guard.run()
                self.assertEqual(code, 1)
                self.assertEqual(payload['status'], 'identity_blocked')
                pause.assert_called_once_with('backup_identity_unverified')

    def test_pause_latch_alone_cannot_create_a_request_during_daily_check(self):
        for pause_identity in ('b' * 64, IDENTITY, None):
            with self.subTest(pause_identity=pause_identity):
                control = {'paused': True, 'backup_identity_sha256': pause_identity,
                           'pause_requested_epoch': NOW - guard.DAY}
                payload, code, stored, calls = self.run_with_snapshot(
                    healthy(remaining_files=1), [], start=False, control=control)
                self.assertEqual(payload['status'], 'stale')
                self.assertEqual(code, 1)
                self.assertNotIn('requested_epoch', stored)
                self.assertFalse(calls)
                # Even a subsequent, now-current pause acknowledgment still is
                # not an instruction to start a request that never existed.
                control['backup_identity_sha256'] = IDENTITY
                payload, code, stored, calls = self.run_with_snapshot(
                    healthy(remaining_files=1), [], stored, start=False, control=control)
                self.assertNotIn('requested_epoch', stored)
                self.assertFalse(calls)

    def test_orphan_resource_pause_is_not_pending_work(self):
        result = guard.reconcile(healthy(remaining_files=1), {'paused_for_resources': True}, NOW, False)
        self.assertEqual(result[0], 'stale')
        self.assertFalse(result[2])

    def test_local_identity_missing_malformed_and_replaced_are_not_equivalent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(guard.resources, 'BZDATA', root / 'bzdata'):
                with self.assertRaises(OSError):
                    guard.resources.installed_identity()
                (root / 'bzinstall.xml').write_text('<bzinstall/>')
                with self.assertRaises(ValueError):
                    guard.resources.installed_identity()
                (root / 'bzinstall.xml').write_text('<bzinstall><bzuniqueid hguid="' + HGUID + '"/></bzinstall>')
                self.assertEqual(guard.resources.installed_identity(), IDENTITY)

    def test_wrong_missing_and_legacy_identity_cannot_complete_or_resume(self):
        for state in ({'requested_epoch': NOW - guard.DAY},
                      {'requested_epoch': NOW - guard.DAY, 'backup_identity_sha256': 'b' * 64},
                      {'requested_epoch': NOW - guard.DAY, 'backup_identity_sha256': 'invalid'}):
            with self.subTest(state=state):
                self.assertFalse(guard.completed(healthy(), state, NOW))
                for start in (False, True):
                    self.assertEqual(guard.reconcile(healthy(remaining_files=1), state, NOW, start)[0], 'blocked')
                    self.assertFalse(guard.reconcile(healthy(remaining_files=1), state, NOW, start)[2])
        self.assertFalse(guard.completed(healthy(backup_identity_sha256=None),
                                        {'requested_epoch': NOW-60, 'backup_identity_sha256': IDENTITY}, NOW))

    def test_legacy_request_stays_unchanged_and_only_protective_pause_is_allowed(self):
        state = {'requested_epoch': NOW - guard.DAY, 'restore_schedule': 'only_when_click_backup_now'}
        modes = []
        payload, code, stored, calls = self.run_with_snapshot(
            healthy(remaining_files=1), [subprocess.CompletedProcess([], 0, '', '')], state,
            config_action=modes.append)
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'identity_blocked')
        self.assertEqual(stored, state)
        self.assertEqual(modes, [])
        self.assertEqual([call.args[0][-1] for call in calls], ['--pause-backup'])

    def test_identity_change_during_schedule_update_prevents_backup_now(self):
        with patch.object(guard, 'require_current_identity', side_effect=[None, None, guard.IdentityBindingError('installation changed')]):
            payload, code, stored, calls = self.run_with_snapshot(
                healthy(remaining_files=1), [subprocess.CompletedProcess([], 0, '', '')])
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'identity_blocked')
        self.assertEqual(stored['backup_identity_sha256'], IDENTITY)
        self.assertEqual([call.args[0][-1] for call in calls], ['--pause-backup'])

    def test_identity_change_during_resource_check_prevents_continuous_resume(self):
        state = {'requested_epoch': NOW - 30, 'restore_schedule': 'only_when_click_backup_now',
                 'backup_identity_sha256': IDENTITY}
        modes = []
        with patch.object(guard, 'require_current_identity', side_effect=[None, guard.IdentityBindingError('installation changed')]):
            payload, code, stored, calls = self.run_with_snapshot(
                healthy(transmit_state='transmitting'), [subprocess.CompletedProcess([], 0)],
                state, start=False, config_action=modes.append)
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'identity_blocked')
        self.assertEqual(modes, [])
        self.assertEqual(stored, state)
        self.assertEqual([call.args[0][-1] for call in calls], ['--pause-backup'])

    def test_identity_change_before_completion_restore_does_not_persist_success(self):
        state = {'requested_epoch': NOW - guard.DAY, 'restore_schedule': 'only_when_click_backup_now',
                 'backup_identity_sha256': IDENTITY}
        modes = []
        with patch.object(guard, 'require_current_identity', side_effect=guard.IdentityBindingError('installation changed')):
            payload, code, stored, calls = self.run_with_snapshot(
                healthy(schedule='continuously'), [subprocess.CompletedProcess([], 0)],
                state, start=False, config_action=modes.append)
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'identity_blocked')
        self.assertEqual(modes, [])
        self.assertEqual(stored, state)
        self.assertNotIn('completed_epoch', stored)

    def test_completion_without_schedule_restore_still_rechecks_identity_before_commit(self):
        state = {'requested_epoch': NOW - guard.DAY, 'backup_identity_sha256': IDENTITY}
        with patch.object(guard, 'require_current_identity', side_effect=guard.IdentityBindingError('installation changed')):
            payload, code, stored, calls = self.run_with_snapshot(
                healthy(), [subprocess.CompletedProcess([], 0)], state, start=False)
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'identity_blocked')
        self.assertEqual(stored, state)
        self.assertNotIn('completed_epoch', stored)

    def test_replacement_identity_cannot_reuse_an_old_accepted_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'bzinfo.xml').write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now"/></bzinfo>')
            old_pause = {'paused': True, 'pause_accepted_epoch': NOW-100,
                         'pause_requested_epoch': NOW-200, 'backup_identity_sha256': 'b' * 64}
            with patch.object(guard.resources, 'CONTROL_DIR', root), patch.object(guard.resources, 'BZDATA', root), \
                 patch.object(guard.resources, 'PAUSE_STATE', root / 'pause.json'), \
                 patch.object(guard.resources, 'read_control', return_value=old_pause), \
                 patch.object(guard.resources, 'installed_identity', return_value=IDENTITY), \
                 patch.object(guard.resources, 'configure_schedule'), \
                 patch.object(guard.resources.time, 'time', return_value=NOW), \
                 patch.object(guard.resources.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as action:
                guard.resources.request_pause('pressure')
            action.assert_called_once()
            receipt = json.loads((root / 'pause.json').read_text())
            self.assertEqual(receipt['backup_identity_sha256'], IDENTITY)
            self.assertEqual(receipt['pause_requested_epoch'], NOW)

    def test_existing_active_upload_observation_binds_new_scope(self):
        state = {}
        self.assertEqual(guard.reconcile(healthy(transmit_state='transmitting'), state, NOW, False)[0], 'running')
        self.assertEqual(state['requested_epoch'], NOW)
        self.assertEqual(state['backup_identity_sha256'], IDENTITY)

    def test_explicit_legacy_adoption_requires_known_matching_paid_identity_and_is_read_only(self):
        for expected, actual, license_status, succeeds in (
                (IDENTITY, IDENTITY, 'billing_active', True),
                ('b' * 64, IDENTITY, 'billing_active', False),
                (IDENTITY, 'b' * 64, 'billing_active', False),
                (IDENTITY, IDENTITY, 'trial_active', False)):
            with self.subTest(expected=expected, actual=actual, license_status=license_status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                state = {'requested_epoch': NOW - guard.DAY, 'restore_schedule': 'only_when_click_backup_now'}
                (root / 'state.json').write_text(json.dumps(state))
                report = {'backup': {'installation': {'hguid': HGUID}, 'license': {'status': license_status}}}
                with patch.object(guard, 'ARTIFACT_ROOT', root), patch.object(guard.resources, 'CONTROL_DIR', root), \
                     patch.object(guard, 'cli_report', return_value=report), \
                     patch.object(guard.resources, 'installed_identity', return_value=actual), \
                     patch.object(guard.resources, 'request_pause') as pause, \
                     patch.object(guard, 'configure_schedule') as configure, patch.object(guard.subprocess, 'run') as action:
                    payload, code = guard.adopt_legacy_identity(expected)
                stored = json.loads((root / 'state.json').read_text())
                self.assertEqual(code, 0 if succeeds else 1)
                self.assertEqual(stored['requested_epoch'], state['requested_epoch'])
                if succeeds:
                    self.assertEqual(stored['backup_identity_sha256'], IDENTITY)
                    self.assertIn('identity_adopted_epoch', stored)
                else:
                    self.assertEqual(stored, state)
                pause.assert_not_called(); configure.assert_not_called(); action.assert_not_called()

    def test_trial_remains_blocked_even_with_matching_identity(self):
        state = {'requested_epoch': NOW - guard.DAY, 'backup_identity_sha256': IDENTITY}
        result = guard.reconcile(healthy(license_status='trial_active', remaining_files=1), state, NOW, True)
        self.assertEqual(result[0], 'blocked')
        self.assertFalse(result[2])

    def test_empty_explicit_adoption_cannot_fall_through_to_a_resuming_check(self):
        with patch('sys.argv', ['backblaze_health.py', '--adopt-legacy-identity', '']), \
             patch.object(guard, 'run') as run, patch('builtins.print'):
            self.assertEqual(guard.main(), 1)
        run.assert_not_called()

    def test_watchdog_missing_or_malformed_state_requests_pause_and_reports_failure(self):
        for missing in ('overviewstatus.xml', 'bzinfo.xml', 'bzbackup/bzfileids.dat', 'malformed-overview', 'missing-identity'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / 'overviewstatus.xml').write_text('<status><bztransmit cur_state="not_running"/></status>')
                (root / 'bzinfo.xml').write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now"/></bzinfo>')
                (root / 'bzbackup').mkdir(); (root / 'bzbackup/bzfileids.dat').write_bytes(b'catalog')
                if missing == 'malformed-overview':
                    (root / 'overviewstatus.xml').write_text('<invalid')
                elif missing != 'missing-identity':
                    (root / missing).unlink()
                with patch.object(watchdog.resources, 'BZDATA', root), \
                     patch.object(watchdog.resources, 'installed_identity', side_effect=ValueError('missing identity') if missing == 'missing-identity' else None, return_value=IDENTITY), \
                     patch.object(watchdog.resources, 'CONTROL_DIR', root), \
                     patch.object(watchdog.resources, 'request_pause') as pause:
                    code = watchdog.main()
                self.assertEqual(code, 1)
                pause.assert_called_once_with('backup_state_unavailable')
                receipt = json.loads((root / 'backblaze-watchdog-latest.json').read_text())
                self.assertEqual(receipt['status'], 'state_unavailable_pause_requested')
                with patch.object(guard.resources, 'CONTROL_DIR', root):
                    self.assertEqual(guard.resources.watchdog_status(receipt['observed_epoch']), 'failed')

    def test_missing_configuration_still_persists_intent_and_attempts_vendor_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(guard.resources, 'CONTROL_DIR', root), patch.object(guard.resources, 'BZDATA', root), \
                 patch.object(guard.resources, 'PAUSE_STATE', root / 'pause.json'), \
                 patch.object(guard.resources, 'configure_schedule', side_effect=OSError('missing config')), \
                 patch.object(guard.resources.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as action:
                with self.assertRaisesRegex(RuntimeError, 'manual scheduling is unverified'):
                    guard.resources.request_pause('backup_state_unavailable')
            action.assert_called_once()
            self.assertEqual(action.call_args.args[0][-1], '--pause-backup')
            receipt = json.loads((root / 'pause.json').read_text())
            self.assertTrue(receipt['paused'])
            self.assertIn('pause_accepted_epoch', receipt)
            self.assertFalse(receipt['manual_schedule_confirmed'])

    def test_pause_failure_is_not_retried_twice_by_one_watchdog_run(self):
        with patch.object(watchdog, 'observe', return_value={'status': 'pause_requested'}), \
             patch.object(watchdog.resources, 'request_pause', side_effect=RuntimeError('rejected')) as pause:
            with self.assertRaises(RuntimeError):
                watchdog.run()
            pause.assert_called_once_with('memory_or_disk_pressure')


if __name__ == '__main__':
    unittest.main()
