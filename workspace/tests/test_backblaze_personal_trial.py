from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import backblaze_health as guard
from scripts import backblaze_resource_watchdog as watchdog
from tests.test_backblaze_health import healthy, NOW, IDENTITY

resources = guard.resources
REAL_SNAPSHOT = guard.snapshot
REAL_HOST_MEMORY_STATUS = resources.host_memory_status
NEW_HGUID = '2' * 24
NEW_IDENTITY = resources.identity_fingerprint(NEW_HGUID)
EXPIRY = 'expires_' + datetime.fromtimestamp(NOW + 10 * guard.DAY, timezone.utc).strftime('%Y%m%d%H%M%S')


class PersonalTrialTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.bzdata = self.root / 'bzdata'
        self.artifacts = self.root / 'artifacts'
        self.control = self.root / 'control'
        for directory in (self.bzdata / 'bzbackup', self.bzdata / 'bzreports', self.artifacts):
            directory.mkdir(parents=True)
        for target, name, value in ((resources, 'BZDATA', self.bzdata), (guard, 'BZDATA', self.bzdata),
                                    (guard, 'ARTIFACT_ROOT', self.artifacts),
                                    (resources, 'CONTROL_DIR', self.control),
                                    (resources, 'PAUSE_STATE', self.control / 'pause.json')):
            self.stack.enter_context(patch.object(target, name, value))
        self.clock = self.stack.enter_context(patch.object(resources.time, 'time', return_value=NOW))
        self.processes = self.stack.enter_context(patch.object(resources, 'process_status', return_value={'transmitter_process_running': False}))
        self.actions = self.stack.enter_context(patch.object(resources.subprocess, 'run', side_effect=AssertionError('unexpected provider mutation')))
        self.spawns = self.stack.enter_context(patch.object(resources.subprocess, 'Popen', side_effect=AssertionError('unexpected provider spawn')))
        self.configure = self.stack.enter_context(patch.object(resources, 'configure_schedule', side_effect=AssertionError('unexpected configuration')))
        self.license = {'type': resources.TRIAL_TYPE, 'status': EXPIRY,
                        'renewal_fail': {'gmt_time': 'none'}, 'renewal_failure': 'none'}
        self.report = {'backup': {'installation': {'hguid': NEW_HGUID}, 'license': self.license,
                                  'status': {'installed': True, 'paused': 'action_pause_backup'}}}
        self.report_call = self.stack.enter_context(patch.object(guard, 'cli_report', return_value=self.report))
        self.s = healthy(backup_identity_sha256=NEW_IDENTITY, license_type=resources.TRIAL_TYPE,
                         license_status=EXPIRY, observed_epoch=NOW, personal_trial_expires_epoch=None)
        self.snapshot_call = self.stack.enter_context(patch.object(guard, 'snapshot', return_value=self.s))
        self.memory = self.stack.enter_context(patch.object(guard, 'memory_status', return_value=healthy()))
        self.stack.enter_context(patch.object(guard.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * guard.GIB)))
        (self.root / 'bzinstall.xml').write_text('<bzinstall><bzuniqueid hguid="' + NEW_HGUID + '"/></bzinstall>')
        self.config = self.bzdata / 'bzinfo.xml'
        self.config.write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now" num_backup_threads="1" net_auto_throttle="false"/></bzinfo>')
        (self.bzdata / 'overviewstatus.xml').write_text('<status><bztransmit cur_state="not_running"/></status>')
        (self.bzdata / 'bzbackup/bzfileids.dat').write_bytes(b'catalog')
        self.local_license = self.bzdata / 'bzreports/bzdc_synchostinfo.xml'
        self.write_license()
        resources.atomic_json(resources.hold_path(), {'schema': resources.HOLD_SCHEMA, 'active': True,
                              'original_identity_sha256': IDENTITY, 'prepared_epoch': NOW - 1000})
        self.hold_bytes = resources.hold_path().read_bytes()
        self.hold_hash = hashlib.sha256(self.hold_bytes).hexdigest()
        self.state_path = self.artifacts / 'state.json'
        resources.atomic_json(self.state_path, {'requested_epoch': NOW - 5000, 'backup_identity_sha256': IDENTITY,
                                               'last_attempt_epoch': NOW - 4999, 'restore_schedule': 'only_when_click_backup_now'})
        self.state_bytes = self.state_path.read_bytes()
        self.watch_receipt = dict(status='installation_hold_pause_requested', observed_epoch=NOW,
                                 source_sha256=resources.SOURCE_IDENTITIES,
                                 pause_reasserted=True, installation_state_verified_idle=False)
        self.save_watch_receipt()

    def write_license(self, **changes):
        attributes = dict(bzlicense=resources.TRIAL_TYPE, bzlicense_status=EXPIRY,
                          renewal_fail_datetime_gmt='none', renewal_fail_reason='none')
        attributes.update(changes)
        self.local_license.write_text('<content><response ' + ' '.join(key + '="' + value + '"' for key, value in attributes.items()) + '/></content>')

    def save_watch_receipt(self):
        resources.atomic_json(self.control / 'backblaze-watchdog-latest.json', self.watch_receipt)

    def transition(self, original=IDENTITY, replacement=NEW_IDENTITY, status=EXPIRY, hold_hash=None):
        return guard.transition_personal_trial(original, replacement, status, self.hold_hash if hold_hash is None else hold_hash)

    def assert_preserved(self):
        self.assertEqual(resources.hold_path().read_bytes(), self.hold_bytes)
        self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
        self.actions.assert_not_called()
        self.configure.assert_not_called()

    def admit(self):
        self.assertEqual(self.transition()[1], 0)
        return resources.trial_path().read_bytes()

    def test_transition_is_metadata_only_private_preserves_exact_prior_state_and_is_idempotent(self):
        before = self.admit()
        record = resources.read_personal_trial()
        self.assertEqual(record['prior_request_json'].encode(), self.state_bytes)
        self.assertEqual(record['prior_request_sha256'], hashlib.sha256(self.state_bytes).hexdigest())
        self.assertEqual(resources.trial_path().stat().st_mode & 0o777, 0o600)
        resources.require_no_installation_hold()
        self.assertEqual(self.transition()[1], 0)
        self.assertEqual(resources.trial_path().read_bytes(), before)
        self.assertEqual(list(self.artifacts.glob('state-personal-trial-*.json')), [])
        self.assert_preserved()

    def test_exact_arguments_and_trial_expiry_are_required_before_any_record(self):
        cases = [dict(original='bad'), dict(replacement=IDENTITY), dict(replacement='a' * 64),
                 dict(hold_hash='b' * 64), dict(status='billing_active'), dict(status='expires_20260230000000'),
                 dict(status='expires_20260926144052x')]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.transition(**arguments)[1], 1)
                self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_provider_trial_type_renewal_or_expiry_change_is_rejected(self):
        for license in ({'type': 'paid', 'status': EXPIRY}, {**self.license, 'status': 'billing_active'},
                        {**self.license, 'renewal_failure': 'declined'}):
            with self.subTest(license=license):
                self.report['backup']['license'] = license
                self.assertEqual(self.transition()[1], 1)
                self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_expired_trial_and_clock_backwards_never_admit(self):
        for now in (NOW + 10 * guard.DAY, NOW - 1500):
            with self.subTest(now=now):
                self.clock.return_value = now
                self.assertEqual(self.transition()[1], 1)
                self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_manual_drained_catalog_and_one_thread_are_required(self):
        original_config = self.config.read_bytes()
        for content in ('<bzinfo/>', '<bzinfo><do_backup backup_schedule_type="continuously" num_backup_threads="1" net_auto_throttle="false"/></bzinfo>',
                        '<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now" num_backup_threads="2"/></bzinfo>'):
            self.config.write_text(content)
            self.assertEqual(self.transition()[1], 1)
            self.assertFalse(resources.trial_path().exists())
        self.config.write_bytes(original_config)
        for change in (patch.object(resources, 'catalog_size_gib', side_effect=ValueError('missing')),
                       patch.object(resources, 'process_status', return_value={'transmitter_process_running': True})):
            with change:
                self.assertEqual(self.transition()[1], 1)
                self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_every_existing_launch_blocker_still_blocks_the_transition(self):
        for changes in ({'owc_mounted': False}, {'owc_identity_verified': False}, {'owc_selected': False},
                        {'internal_selected': False}, {'scratch_is_owc': False}, {'internal_free_gib': 29},
                        {'owc_free_gib': 19}, {'memory_available_gib': 1}, {'memory_pressure_level': 2},
                        {'heavy_build_running': True}, {'safety_freeze': 'frozen'}):
            with self.subTest(changes=changes):
                self.snapshot_call.return_value = {**self.s, **changes}
                self.assertEqual(self.transition()[1], 1)
                self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_transition_requires_fresh_exact_source_protective_pause_receipt(self):
        baseline = deepcopy(self.watch_receipt)
        for changes in ({'observed_epoch': NOW - 361}, {'source_sha256': {}}, {'status': 'idle'},
                        {'pause_reasserted': False}, {'installation_state_verified_idle': True}):
            self.watch_receipt = {**baseline, **changes}
            self.save_watch_receipt()
            self.assertEqual(self.transition()[1], 1)
            self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_expired_changed_missing_or_malformed_license_record_makes_watchdog_pause(self):
        self.admit()
        record_bytes = resources.trial_path().read_bytes()
        xml_bytes = self.local_license.read_bytes()
        scenarios = [lambda: self.write_license(bzlicense_status='billing_active'),
                     lambda: self.write_license(bzlicense='other_trial'),
                     lambda: self.write_license(renewal_fail_reason='declined'),
                     lambda: self.local_license.unlink(),
                     lambda: self.local_license.write_text('<broken'),
                     lambda: resources.trial_path().write_text('{'),
                     lambda: resources.trial_path().chmod(0o644),
                     lambda: setattr(self.clock, 'return_value', NOW + 10 * guard.DAY)]
        for mutate in scenarios:
            with self.subTest(mutate=mutate):
                mutate()
                with patch.object(resources, '_request_pause_locked') as pause:
                    self.assertEqual(watchdog.main(), 1)
                    pause.assert_called_once_with('personal_trial_unverified', reassert=True)
                self.assertEqual(json.loads((self.control / 'backblaze-watchdog-latest.json').read_text())['status'], 'personal_trial_invalid_pause_requested')
                self.assertEqual(resources.watchdog_status(self.clock.return_value), 'failed')
                resources.trial_path().write_bytes(record_bytes)
                resources.trial_path().chmod(0o600)
                self.local_license.write_bytes(xml_bytes)
                self.clock.return_value = NOW
        self.assert_preserved()

    def test_hold_or_installation_change_invalidates_transition(self):
        self.admit()
        for context in (patch.object(resources, 'installed_identity', return_value=IDENTITY),
                        patch.object(resources, 'installed_identity', side_effect=ValueError('unknown'))):
            with context:
                with self.assertRaises(resources.PersonalTrialError):
                    resources.require_no_installation_hold()
        resources.hold_path().write_bytes(self.hold_bytes + b'\n')
        with self.assertRaises(resources.PersonalTrialError):
            resources.require_no_installation_hold()

    def test_new_check_uses_separate_empty_state_and_never_adopts_old_pending_work(self):
        self.admit()
        self.watch_receipt['status'] = 'idle'
        self.save_watch_receipt()
        self.s['personal_trial_expires_epoch'] = resources.trial_expiry(EXPIRY)
        payload, code = guard.run(start=False)
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'stale')
        new_states = list(self.artifacts.glob('state-personal-trial-*.json'))
        self.assertEqual(len(new_states), 1)
        self.assertNotIn('requested_epoch', json.loads(new_states[0].read_text()))
        self.assert_preserved()

    def test_new_start_waits_for_later_normal_healthy_watchdog(self):
        self.admit()
        self.s['personal_trial_expires_epoch'] = resources.trial_expiry(EXPIRY)
        self.s['watchdog_status'] = resources.watchdog_status(NOW)
        payload, code = guard.run(start=True)
        self.assertEqual((payload['status'], code), ('blocked', 1))
        self.assert_preserved()

    def test_expired_transition_daily_path_pauses_without_report_or_request_write(self):
        self.admit()
        self.report_call.reset_mock()
        self.clock.return_value = NOW + 10 * guard.DAY
        with patch.object(resources, 'request_pause') as pause:
            payload, code = guard.run(start=True)
            self.assertEqual((payload['status'], code), ('personal_trial_blocked', 1))
            pause.assert_called_once_with('personal_trial_unverified')
        self.report_call.assert_not_called()
        self.assertEqual(list(self.artifacts.glob('state-personal-trial-*.json')), [])
        self.assert_preserved()

    def test_locks_and_write_failure_cannot_change_preserved_inputs(self):
        with resources.admin_lock():
            self.assertEqual(self.transition()[1], 1)
        with patch.object(guard, 'write_json', side_effect=OSError('disk full')):
            self.assertEqual(self.transition()[1], 1)
        self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_preexisting_new_request_is_never_adopted(self):
        new_state = self.artifacts / ('state-personal-trial-' + NEW_IDENTITY + '.json')
        new_state.write_text('{"requested_epoch": 1}')
        self.assertEqual(self.transition()[1], 1)
        self.assertFalse(resources.trial_path().exists())
        self.assertEqual(new_state.read_text(), '{"requested_epoch": 1}')
        self.assert_preserved()

    def test_resources_changing_after_snapshot_prevent_transition_write(self):
        self.memory.return_value = healthy(memory_pressure_level=2)
        self.assertEqual(self.transition()[1], 1)
        self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_expiry_during_final_resource_read_prevents_transition_publication(self):
        def expire():
            self.clock.return_value = resources.trial_expiry(EXPIRY)
            return healthy()
        self.memory.side_effect = expire
        self.assertEqual(self.transition()[1], 1)
        self.assertFalse(resources.trial_path().exists())
        self.assert_preserved()

    def test_concurrent_transition_record_is_preserved_by_exclusive_publication(self):
        real_writer = guard.write_json
        def competing_writer(path, data, **kwargs):
            path.write_text('concurrent record')
            return real_writer(path, data, **kwargs)
        with patch.object(guard, 'write_json', side_effect=competing_writer):
            self.assertEqual(self.transition()[1], 1)
        self.assertEqual(resources.trial_path().read_text(), 'concurrent record')
        self.assertEqual(list(self.control.glob('.backblaze-personal-trial-transition.json*')), [])
        self.assert_preserved()

    def test_expiry_during_schedule_change_prevents_backup_dispatch(self):
        self.admit()
        self.watch_receipt['status'] = 'idle'
        self.save_watch_receipt()
        self.s['personal_trial_expires_epoch'] = resources.trial_expiry(EXPIRY)
        def expire(mode):
            self.clock.return_value = resources.trial_expiry(EXPIRY)
        with patch.object(guard, 'configure_schedule', side_effect=expire) as configure, patch.object(resources, 'request_pause') as pause:
            payload, code = guard.run(start=True)
            self.assertEqual((payload['status'], code), ('personal_trial_blocked', 1))
            configure.assert_called_once_with('continuously')
            pause.assert_called_once_with('personal_trial_unverified')
        self.assert_preserved()

    def test_real_snapshot_never_admits_an_unrecorded_trial_and_rejects_provider_drift(self):
        class Volume:
            def __str__(self):
                return '/Volumes/test-owc'

            def is_mount(self):
                return True

            def stat(self):
                return SimpleNamespace(st_dev=Path('/').stat().st_dev + 1)

        volume = Volume()
        workspace = self.root / 'workspace'
        (workspace / 'registry').mkdir(parents=True)
        (workspace / 'registry/external_volume_guard.json').write_text(json.dumps({'mountPoint': str(volume), 'volumeUuid': 'fixture'}))
        (self.bzdata / 'bzreports/bzstat_remainingbackup.xml').write_text('<remaining/>')
        self.config.write_text('<bzinfo><scratch_drive scratch_mountpoint="' + str(volume) + '"/><do_backup backup_schedule_type="only_when_click_backup_now" num_backup_threads="1" net_auto_throttle="false"/></bzinfo>')
        report = deepcopy(self.report)
        report['backup'].update(status={'bztransmit': 'not_running', 'last_backup': {'gmt_millis': str((NOW - 10) * 1000)}, 'safety_freeze': 'not_frozen'},
                                backup={'files': {'remaining': 1}, 'bytes': {'remaining': 2}})
        report['backup']['installation']['version'] = 'fixture'
        report.update(sysinfo={'storage': [{'filepath': '/', 'selected_for_backup': True}, {'filepath': str(volume), 'selected_for_backup': True}]},
                      settings={'backup_schedule_type': 'only_when_click_backup_now'})
        with patch.object(guard, 'WORKSPACE', workspace), patch.object(guard, 'OWC', volume), patch.object(guard, 'read_volume_uuid', return_value='fixture'):
            s = REAL_SNAPSHOT(report, NOW)
            self.assertIsNone(s['personal_trial_expires_epoch'])
            self.assertIn('the Backblaze subscription is not confirmed active', guard.blockers(s))
            self.admit()
            self.watch_receipt['status'] = 'idle'
            self.save_watch_receipt()
            s = REAL_SNAPSHOT(report, NOW)
            self.assertEqual(guard.blockers(s, launch=True), [])
            report['backup']['license']['status'] = 'billing_active'
            with self.assertRaises(resources.PersonalTrialError):
                REAL_SNAPSHOT(report, NOW)
        self.assert_preserved()

    def test_cli_transition_is_exclusive_and_bad_inputs_never_fall_through_to_check(self):
        with patch('sys.argv', ['guard', '--transition-personal-trial', 'bad', NEW_IDENTITY, EXPIRY, self.hold_hash]), patch.object(guard, 'run') as run, patch('builtins.print'):
            self.assertEqual(guard.main(), 1)
            run.assert_not_called()
        with patch('sys.argv', ['guard', '--transition-personal-trial', IDENTITY, NEW_IDENTITY, EXPIRY, self.hold_hash, '--start']), patch('sys.stderr'):
            with self.assertRaises(SystemExit) as exc:
                guard.main()
            self.assertEqual(exc.exception.code, 2)
        self.assert_preserved()


class BootstrapTests(unittest.TestCase):
    write_license = PersonalTrialTests.write_license
    save_watch_receipt = PersonalTrialTests.save_watch_receipt

    def setUp(self):
        PersonalTrialTests.setUp(self)
        self.processes.return_value = {'transmitter_process_running': False, 'heavy_build_running': False}
        self.config.write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now" num_backup_threads="1" net_auto_throttle="false"/>'
                               f'<scratch_drive scratch_mountpoint="{resources.OWC}"/>'
                               '<hard_drives_to_backup><bzvolume mountPointPath="/" bzVolumeGuid="internal"/>'
                               f'<bzvolume mountPointPath="{resources.OWC}/" bzVolumeGuid="external"/></hard_drives_to_backup></bzinfo>')
        self.catalog = self.bzdata / 'bzbackup/bzfileids.dat'
        self.catalog.unlink()
        self.owner = {'pid': os.getpid(), 'start_command_sha256': 'a' * 64}
        self.stack.enter_context(patch.object(resources, 'process_identity', side_effect=lambda pid: self.owner if pid == os.getpid() else None))
        self.stack.enter_context(patch.object(resources, 'require_signed_cli', return_value={'fixture_cli': True}))
        self.stack.enter_context(patch.object(resources, 'native_cli_identity', return_value={'fixture_cli': True}))
        self.stack.enter_context(patch.object(resources, 'native_start_commands', return_value=[]))
        self.stack.enter_context(patch.object(resources, 'host_memory_status', return_value=healthy()))
        self.report['backup']['status'] = {'installed': True, 'paused': 'action_pause_backup', 'bztransmit': 'not_running', 'safety_freeze': 'not_frozen'}
        self.report['settings'] = {'backup_schedule_type': resources.MANUAL, 'net_auto_throttle': False, 'num_backup_threads': 1}
        self.report['sysinfo'] = {'storage': [{'filepath': '/', 'selected_for_backup': True},
                                             {'filepath': str(resources.OWC), 'selected_for_backup': True}]}
        self.admission = self.root / 'admission.json'
        resources.atomic_json(self.admission, {'schema': 'openclaw.backblaze_bootstrap_admission.v1', 'observed_epoch': NOW,
                                              'new_identity_sha256': NEW_IDENTITY,
                                              'full_disk_access': {'com.backblaze.Backblaze': True, 'com.backblaze.bzbmenu': True},
                                              'enrollment_guard': {'pid': 999, 'start_command_sha256': 'b' * 64}})
        self.admission_hash = hashlib.sha256(self.admission.read_bytes()).hexdigest()
        self.arguments = (IDENTITY, NEW_IDENTITY, EXPIRY, self.hold_hash, str(self.admission), self.admission_hash)
        self.observation = {**healthy(), 'catalog_state': 'not_created', 'catalog_gib': None,
                            'memory_required_gib': 2, 'vendor_progress': 'unknown', 'observed_epoch': NOW}

    def preflight(self):
        with patch.object(resources, 'require_first_scan_completion', return_value={'fixture_only': True}), \
             patch.object(resources, 'observe_bootstrap', return_value=self.observation):
            return guard.bootstrap_preflight(*self.arguments[:4], self.admission, self.admission_hash)

    def persisted(self):
        record = self.preflight()
        resources.atomic_json(resources.bootstrap_path(), record, exclusive=True)
        return record

    def test_unobserved_scan_gate_cannot_be_opened_by_receipt_or_idle_cpu(self):
        payload, code = guard.bootstrap_personal_catalog(*self.arguments)
        self.assertEqual((code, payload['gate']), (1, 'first_scan_publication_binding_missing_or_invalid'))
        self.assertFalse(resources.bootstrap_path().exists())
        self.actions.assert_not_called()
        self.report_call.assert_not_called()

    def test_manual_one_thread_with_automatic_throttle_still_enabled_is_rejected(self):
        self.config.write_text(self.config.read_text().replace('net_auto_throttle="false"', 'net_auto_throttle="true"'))
        with self.assertRaises(resources.BootstrapError):
            self.preflight()
        self.assertFalse(resources.bootstrap_path().exists())
        self.actions.assert_not_called()

    def test_long_preflight_report_has_no_active_heartbeat_lease(self):
        def slow_report():
            self.assertFalse(resources.bootstrap_path().exists())
            with resources.admin_lock():
                pass
            self.clock.return_value = NOW + 170
            return self.report
        self.report_call.side_effect = slow_report
        record = self.preflight()
        self.assertEqual(record['binding']['created_epoch'], NOW + 170)
        self.assertEqual(record['heartbeat_epoch'], NOW + 170)
        self.assertFalse(resources.bootstrap_path().exists())

    def test_exact_preflight_bindings_and_current_handoff_are_required(self):
        for change in (patch.object(resources, 'require_signed_cli', side_effect=ValueError('signature')),
                       patch.object(resources, 'require_transition_watchdog', side_effect=ValueError('source')),
                       patch.object(resources, 'process_identity', return_value={'pid': 999, 'start_command_sha256': 'b' * 64}),
                       patch.object(resources, 'installed_identity', return_value='f' * 64)):
            with change, self.assertRaises((resources.BootstrapError, guard.IdentityBindingError, ValueError)):
                self.preflight()
        before = self.admission.read_bytes()
        self.admission.write_text(before.decode().replace('true', 'false'))
        with self.assertRaises(resources.BootstrapError):
            self.preflight()
        self.assertFalse(resources.bootstrap_path().exists())

    def test_cancel_dominates_late_success_stale_phase_and_stale_heartbeat(self):
        record = self.persisted()
        resources.cancel_bootstrap(record, 'watchdog_pressure')
        stop = resources.bootstrap_stop_path().read_bytes()
        for changes in ({'phase': 'observing', 'native_exit': 0}, {'phase': 'dispatching'}, {'phase': 'observing'}):
            with self.assertRaises(resources.BootstrapError):
                resources.update_bootstrap(record, **changes)
        updated = resources.update_bootstrap(record, heartbeat_epoch=NOW - 20)
        self.assertEqual((updated['phase'], updated['heartbeat_epoch']), ('pause_pending', NOW))
        resources.cancel_bootstrap(record, 'late_owner')
        self.assertEqual(resources.bootstrap_stop_path().read_bytes(), stop)
        self.assertEqual(updated['binding'], record['binding'])
        self.actions.assert_not_called()

    def test_first_catalog_is_sticky_empty_not_healthy_and_later_absence_strict(self):
        record = self.persisted()
        self.assertIsNone(resources.bootstrap_catalog()['catalog_gib'])
        self.catalog.touch()
        self.assertEqual(resources.bootstrap_catalog()['catalog_state'], 'initializing')
        self.catalog.write_bytes(b'catalog')
        seen = resources.bootstrap_catalog()
        self.assertEqual(seen['memory_required_gib'], 2 + 7 / resources.GIB)
        resources.update_bootstrap(record, first_catalog_seen=seen['catalog_observation'])
        resources.cancel_bootstrap(record, 'first_catalog_seen')
        latest = resources.update_bootstrap(record, first_catalog_seen=None, heartbeat_epoch=NOW)
        self.assertEqual(latest['first_catalog_seen'], seen['catalog_observation'])
        self.catalog.unlink()
        with self.assertRaises(resources.BootstrapError):
            resources.bootstrap_catalog(strict=latest['first_catalog_seen'] is not None)
        with self.assertRaises(FileNotFoundError):
            resources.memory_status()
        self.catalog.symlink_to(self.config)
        with self.assertRaises(resources.BootstrapError):
            resources.bootstrap_catalog()

    def test_cancellation_and_pause_do_not_wait_for_contended_admin_lock(self):
        record = self.persisted()
        with resources.admin_lock(), patch.object(resources, 'supervised_cli', return_value=0) as calls:
            result = resources.pause_bootstrap(record, 'owner_hung')
            self.assertTrue(resources.bootstrap_stop_path().exists())
            self.assertTrue(result['pause_accepted'])
            self.assertEqual([c.args[0][-1] for c in calls.call_args_list], ['backup_schedule_type=' + resources.MANUAL, '--pause-backup'])
            self.assertEqual(resources.read_bootstrap()['phase'], 'dispatching')
        with self.assertRaises(resources.BootstrapError):
            resources.update_bootstrap(record, phase='observing')
        self.assertEqual(resources.update_bootstrap(record, heartbeat_epoch=NOW)['phase'], 'pause_pending')

    def test_crash_after_consumed_intent_and_replay_never_spawn_again(self):
        record = self.preflight()
        with patch.object(guard, 'bootstrap_preflight', return_value=record), \
             patch.object(resources.subprocess, 'Popen', side_effect=OSError('fixture spawn failure')) as spawn, \
             patch.object(resources, 'pause_bootstrap', return_value={'status': 'bootstrap_pause_pending'}) as pause:
            self.assertEqual(guard.bootstrap_personal_catalog(*self.arguments)[1], 1)
            self.assertTrue(resources.read_bootstrap()['binding']['attempt_consumed'])
            self.assertEqual(guard.bootstrap_personal_catalog(*self.arguments)[1], 1)
            spawn.assert_called_once()
            self.assertEqual(pause.call_count, 2)
        self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
        self.assertEqual(resources.hold_path().read_bytes(), self.hold_bytes)

    def test_contended_pre_dispatch_lock_consumes_no_attempt_and_starts_nothing(self):
        record = self.preflight()
        with resources.admin_lock(), patch.object(guard, 'bootstrap_preflight', return_value=record), \
             patch.object(resources.subprocess, 'Popen') as spawn:
            self.assertEqual(guard.bootstrap_personal_catalog(*self.arguments)[1], 1)
        spawn.assert_not_called()
        self.assertFalse(resources.bootstrap_path().exists())

    def test_owner_death_pid_reuse_expiry_and_changed_hold_revoke_exception(self):
        record = self.persisted()
        for change in (patch.object(resources, 'process_identity', return_value=None),
                       patch.object(resources, 'process_identity', return_value={**self.owner, 'start_command_sha256': 'c' * 64}),
                       patch.object(resources.time, 'time', return_value=NOW + 91),
                       patch.object(resources.time, 'time', return_value=resources.trial_expiry(EXPIRY))):
            with change, self.assertRaises((resources.BootstrapError, ValueError)):
                resources.require_bootstrap_binding(record)
        resources.hold_path().write_bytes(self.hold_bytes + b' ')
        with self.assertRaises(resources.BootstrapError):
            resources.require_bootstrap_binding(record)

    def test_pause_acceptance_with_possible_start_or_transmitter_is_not_drain(self):
        record = self.persisted()
        with patch.object(resources, 'supervised_cli', return_value=0), \
             patch.object(resources, 'native_start_commands', return_value=[123]):
            result = resources.pause_bootstrap(record, 'uncertain_start')
        self.assertTrue(result['pause_accepted'])
        self.assertFalse(result['drained'])
        self.assertEqual(resources.read_bootstrap()['phase'], 'pause_pending')
        with self.assertRaises(resources.BootstrapError):
            guard.finish_bootstrap(record)

    def test_failed_cancellation_persistence_still_attempts_pause_without_success_claim(self):
        record = self.persisted()
        with patch.object(resources, 'cancel_bootstrap', side_effect=OSError('disk full')), \
             patch.object(resources, 'supervised_cli', return_value=0) as calls:
            result = resources.pause_bootstrap(record, 'disk_full')
        self.assertFalse(result['cancellation_persisted'])
        self.assertTrue(result['pause_accepted'])
        self.assertEqual(calls.call_count, 2)
        self.assertNotEqual(result['status'], 'bootstrap_catalog_established')

    def test_watchdog_bootstrap_status_is_never_ordinary_health_and_paid_missing_catalog_stays_strict(self):
        self.persisted()
        with patch.object(resources, 'observe_bootstrap', return_value=self.observation):
            result = watchdog.run()
        self.assertEqual(result['status'], 'bootstrap_observing')
        resources.atomic_json(self.control / 'backblaze-watchdog-latest.json', {**result, 'source_sha256': resources.SOURCE_IDENTITIES})
        self.assertEqual(resources.watchdog_status(NOW), 'failed')
        with self.assertRaises(resources.InstallationHoldError):
            resources.require_no_installation_hold()
        with self.assertRaises(FileNotFoundError):
            resources.catalog_size_gib()

    def test_active_action_heartbeats_unknown_progress_no_total_cutoff_and_first_catalog_pause(self):
        record = self.persisted()
        elapsed = [0]
        heartbeats = []
        original_update = resources.update_bootstrap
        class Native:
            pid = 123
            returncode = 0
            def poll(self):
                return None if elapsed[0] < 59 else 0
            def terminate(self):
                raise AssertionError('completed native CLI should not be killed')
        class Probe:
            returncode = 0
            def __init__(inner, args, stdout, stderr):
                stdout.write(json.dumps(self.observation).encode())
            def poll(inner):
                return 0
        def sleep(seconds):
            with resources.admin_lock():
                pass  # No native wait or resource read holds admin_lock.
            elapsed[0] += seconds
            self.clock.return_value = NOW + elapsed[0]
            if elapsed[0] >= 310:
                self.catalog.write_bytes(b'catalog')
        def update(expected, **changes):
            if 'heartbeat_epoch' in changes:
                heartbeats.append(elapsed[0])
            return original_update(expected, **changes)
        with patch.object(resources.time, 'monotonic', side_effect=lambda: elapsed[0]), \
             patch.object(resources.time, 'sleep', side_effect=sleep), \
             patch.object(resources.subprocess, 'Popen', side_effect=Probe), \
             patch.object(resources, 'update_bootstrap', side_effect=update):
            result = guard.supervise_bootstrap(record, Native())
        self.assertGreaterEqual(elapsed[0], 310)
        self.assertLessEqual(max(b - a for a, b in zip(heartbeats, heartbeats[1:])), 5)
        self.assertTrue(resources.bootstrap_stop_path().exists())
        self.assertIsNotNone(result['first_catalog_seen'])
        self.assertEqual(result['last_observation']['vendor_progress'], 'unknown')
        self.assertNotIn('completed_epoch', result)

    def test_cancellation_during_active_native_call_reaps_only_owned_child(self):
        record = self.persisted()
        elapsed = [0]
        class Child:
            pid = 123
            returncode = None
            terminated = False
            def poll(inner):
                return inner.returncode
            def terminate(inner):
                inner.terminated = True
                inner.returncode = -15
            def wait(inner, timeout):
                return inner.returncode
        native = Child()
        probe = Child()
        def sleep(seconds):
            elapsed[0] += seconds
            self.clock.return_value = NOW + elapsed[0]
            if elapsed[0] >= 12:
                resources.cancel_bootstrap(record, 'watchdog_cancel')
        with patch.object(resources.time, 'monotonic', side_effect=lambda: elapsed[0]), \
             patch.object(resources.time, 'sleep', side_effect=sleep), \
             patch.object(resources.subprocess, 'Popen', return_value=probe), self.assertRaises(resources.BootstrapError):
            guard.supervise_bootstrap(record, native)
        self.assertTrue(native.terminated)
        self.assertTrue(probe.terminated)
        with self.assertRaises(resources.BootstrapError):
            resources.update_bootstrap(record, phase='observing', native_exit=0)

    def test_unknown_initial_fields_remain_none_present_malformed_values_fail_and_paid_stays_strict(self):
        class Volume:
            def __str__(self):
                return str(resources.OPERATOR.require_path('paths.data_root'))
            def is_mount(self):
                return True
            def stat(self):
                return SimpleNamespace(st_dev=Path('/').stat().st_dev + 1)
        workspace = self.root / 'workspace'
        (workspace / 'registry').mkdir(parents=True)
        (workspace / 'registry/external_volume_guard.json').write_text(json.dumps({'mountPoint': str(Volume()), 'volumeUuid': 'fixture'}))
        with patch.object(guard, 'WORKSPACE', workspace), patch.object(guard, 'OWC', Volume()), \
             patch.object(guard, 'read_volume_uuid', return_value='fixture'):
            s = REAL_SNAPSHOT(self.report, NOW, initial_trial=True)
            for key in ('remaining_files', 'remaining_bytes', 'last_backup_epoch', 'last_backup_at', 'remaining_report_epoch'):
                self.assertIsNone(s[key])
            self.assertTrue(s['completion_unknown'])
            self.assertFalse(guard.completed(s, {'backup_identity_sha256': NEW_IDENTITY, 'requested_epoch': NOW - 100}, NOW))
            with self.assertRaises(KeyError):
                REAL_SNAPSHOT(self.report, NOW)
            for counts in (None, [], {'files': None}, {'files': {'remaining': -1}}, {'bytes': {'remaining': True}}, {'bytes': {'remaining': 'none'}}):
                self.report['backup']['backup'] = counts
                with self.subTest(counts=counts), self.assertRaises(ValueError):
                    REAL_SNAPSHOT(self.report, NOW, initial_trial=True)
            del self.report['backup']['backup']
            self.report['backup']['status']['last_backup'] = None
            with self.assertRaises(ValueError):
                REAL_SNAPSHOT(self.report, NOW, initial_trial=True)


    def test_real_resource_observation_preserves_every_admission_threshold_and_selected_binding(self):
        from scripts import external_volume_guard
        class Volume:
            def __str__(self):
                return str(resources.OPERATOR.require_path('paths.data_root'))
            def is_mount(self):
                return True
            def stat(self):
                return SimpleNamespace(st_dev=Path('/').stat().st_dev + 1)
        self.write_license(safety_frozen='not_frozen')
        record = self.persisted()
        with patch.object(resources, 'OWC', Volume()), patch.object(external_volume_guard, 'read_volume_uuid', return_value=resources.OWC_UUID):
            self.assertEqual(resources.observe_bootstrap(record)['catalog_state'], 'not_created')
            with patch.object(resources, 'host_memory_status', return_value=healthy(swap_used_gib=25, memory_free_percent=15)):
                self.assertEqual(resources.observe_bootstrap(record)['catalog_state'], 'not_created')
            for changes in ({'memory_available_gib': 1.99}, {'memory_pressure_level': 2}, {'heavy_build_running': True}, {'memory_free_percent': 14}):
                with patch.object(resources, 'host_memory_status', return_value=healthy(**changes)), self.assertRaises(resources.BootstrapError):
                    resources.observe_bootstrap(record)
            for mount, free in (('/', 29.99), (str(resources.OWC), 19.99)):
                with patch.object(resources.shutil, 'disk_usage', side_effect=lambda path: SimpleNamespace(free=(free if str(path) == mount else 100) * guard.GIB)), self.assertRaises(resources.BootstrapError):
                    resources.observe_bootstrap(record)
            self.config.write_text(self.config.read_text().replace('bzVolumeGuid="external"', 'bzVolumeGuid="changed"'))
            with self.assertRaises(resources.BootstrapError):
                resources.observe_bootstrap(record)

    def test_full_bootstrap_pause_acceptance_then_strict_transition_requires_later_normal_watchdog(self):
        record = self.persisted()
        self.catalog.write_bytes(b'catalog')
        record = resources.update_bootstrap(record, first_catalog_seen=resources.bootstrap_catalog()['catalog_observation'])
        with patch.object(resources, 'supervised_cli', return_value=0):
            paused = resources.pause_bootstrap(record, 'first_catalog_seen')
            self.assertEqual(paused['status'], 'bootstrap_paused_catalog')
            resources.atomic_json(self.control / 'backblaze-watchdog-latest.json', {**paused, 'source_sha256': resources.SOURCE_IDENTITIES})
            with self.assertRaises(ValueError):
                resources.require_transition_watchdog(NOW)  # Fresh post-pause report still missing.
            record = guard.finish_bootstrap(record)
            self.assertEqual(record['phase'], 'catalog_established')
            paused = watchdog.run()
        resources.atomic_json(self.control / 'backblaze-watchdog-latest.json', {**paused, 'source_sha256': resources.SOURCE_IDENTITIES})
        resources.require_transition_watchdog(NOW)
        self.assertEqual(guard.transition_personal_trial(*self.arguments[:4])[1], 0)
        self.assertEqual(resources.watchdog_status(NOW), 'failed')
        result = watchdog.run()
        self.assertEqual(result['status'], 'idle')
        resources.atomic_json(self.control / 'backblaze-watchdog-latest.json', {**result, 'source_sha256': resources.SOURCE_IDENTITIES})
        self.assertEqual(resources.watchdog_status(NOW), 'healthy')
        replay, code = guard.bootstrap_personal_catalog(*self.arguments)
        self.assertEqual((code, replay['status']), (0, 'bootstrap_already_handed_off'))
        self.spawns.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), self.state_bytes)
        self.assertEqual(resources.hold_path().read_bytes(), self.hold_bytes)
        self.actions.assert_not_called()

    def test_expiry_after_post_pause_resource_read_cannot_be_accepted(self):
        record = self.persisted()
        self.catalog.write_bytes(b'catalog')
        record = resources.update_bootstrap(record, first_catalog_seen=resources.bootstrap_catalog()['catalog_observation'])
        with patch.object(resources, 'supervised_cli', return_value=0):
            resources.pause_bootstrap(record, 'first_catalog_seen')
        def expire(*args, **kwargs):
            self.clock.return_value = resources.trial_expiry(EXPIRY)
            return self.s
        self.snapshot_call.side_effect = expire
        with self.assertRaises((resources.BootstrapError, ValueError)):
            guard.finish_bootstrap(record)
        self.assertEqual(resources.read_bootstrap()['phase'], 'pause_pending')

    def test_active_native_timeout_keeps_heartbeat_and_reaps_the_owned_child(self):
        record = self.persisted()
        elapsed = [0]
        class Child:
            pid = 123
            returncode = None
            terminated = False
            def poll(inner):
                return inner.returncode
            def terminate(inner):
                inner.terminated = True
                inner.returncode = -15
            def wait(inner, timeout):
                return inner.returncode
        native, probe = Child(), Child()
        def sleep(seconds):
            elapsed[0] += seconds
            self.clock.return_value = NOW + elapsed[0]
            with resources.admin_lock():
                pass
        with patch.object(resources.time, 'monotonic', side_effect=lambda: elapsed[0]), \
             patch.object(resources.time, 'sleep', side_effect=sleep), \
             patch.object(resources.subprocess, 'Popen', return_value=probe), self.assertRaises(subprocess.TimeoutExpired):
            guard.supervise_bootstrap(record, native)
        self.assertEqual(elapsed[0], 60)
        self.assertGreaterEqual(resources.read_bootstrap()['heartbeat_epoch'], NOW + 55)
        self.assertTrue(native.terminated)
        self.assertTrue(probe.terminated)

    def test_protective_configuration_can_heartbeat_for_30_seconds_without_admin_lock(self):
        record = self.persisted()
        elapsed, ticks = [0], []
        class Child:
            returncode = 0
            def poll(inner):
                return 0 if elapsed[0] >= 29 else None
        def sleep(seconds):
            elapsed[0] += seconds
            self.clock.return_value = NOW + elapsed[0]
        def tick():
            with resources.admin_lock():
                pass
            ticks.append(elapsed[0])
            guard.protective_bootstrap_heartbeat(record)
        with patch.object(resources.time, 'monotonic', side_effect=lambda: elapsed[0]), \
             patch.object(resources.time, 'sleep', side_effect=sleep), \
             patch.object(resources.subprocess, 'Popen', return_value=Child()):
            self.assertEqual(resources.supervised_cli([str(resources.BZCLI), 'configure', '--value', 'backup_schedule_type=' + resources.MANUAL], 30, tick), 0)
        self.assertGreaterEqual(elapsed[0], 29)
        self.assertLess(max(b - a for a, b in zip(ticks, ticks[1:])), 1)

    def test_exclusive_collision_preserves_existing_attempt_and_never_dispatches(self):
        record = self.preflight()
        other = deepcopy(record)
        other['binding']['admission_sha256'] = 'f' * 64
        other['binding_sha256'] = resources.digest_json(other['binding'])
        def introduce(*args):
            resources.atomic_json(resources.bootstrap_path(), other, exclusive=True)
            return record
        with patch.object(guard, 'bootstrap_preflight', side_effect=introduce), \
             patch.object(resources.subprocess, 'Popen') as spawn, \
             patch.object(resources, 'pause_bootstrap', return_value={'status': 'bootstrap_pause_pending'}):
            self.assertEqual(guard.bootstrap_personal_catalog(*self.arguments)[1], 1)
        self.assertEqual(resources.read_bootstrap(), other)
        spawn.assert_not_called()

    def test_protective_heartbeat_contention_does_not_interrupt_pause(self):
        record = self.persisted()
        with resources.admin_lock():
            guard.protective_bootstrap_heartbeat(record)
        self.assertEqual(resources.read_bootstrap()['heartbeat_epoch'], NOW)


    def test_trial_expiry_beyond_15_days_is_rejected_before_attempt_publication(self):
        far = 'expires_' + datetime.fromtimestamp(NOW + 16 * guard.DAY, timezone.utc).strftime('%Y%m%d%H%M%S')
        self.report['backup']['license']['status'] = far
        self.write_license(bzlicense_status=far)
        with patch.object(resources, 'require_first_scan_completion', return_value={'fixture_only': True}), \
             patch.object(resources, 'observe_bootstrap', return_value=self.observation), self.assertRaises(resources.BootstrapError):
            guard.bootstrap_preflight(IDENTITY, NEW_IDENTITY, far, self.hold_hash, self.admission, self.admission_hash)
        self.assertFalse(resources.bootstrap_path().exists())
        self.spawns.assert_not_called()

    def test_changed_signed_cli_immediately_before_dispatch_creates_no_attempt(self):
        record = self.preflight()
        with patch.object(guard, 'bootstrap_preflight', return_value=record), \
             patch.object(resources, 'native_cli_identity', return_value={'changed': True}):
            self.assertEqual(guard.bootstrap_personal_catalog(*self.arguments)[1], 1)
        self.assertFalse(resources.bootstrap_path().exists())
        self.spawns.assert_not_called()


    def test_true_stale_grant_receipt_rejected_before_full_report(self):
        self.clock.return_value = NOW + 121
        with self.assertRaises(resources.BootstrapError):
            self.preflight()
        self.report_call.assert_not_called()
        self.assertFalse(resources.bootstrap_path().exists())

    def test_high_swap_occupancy_is_telemetry_and_current_host_guards_still_apply(self):
        for occupancy in (0, 8.573, 25):
            s = healthy(swap_used_gib=occupancy, remaining_files=1)
            self.assertEqual(resources.resource_blockers(s), [])
            state = {'backup_identity_sha256': IDENTITY, 'requested_epoch': NOW - 100, 'paused_for_resources': True}
            self.assertTrue(guard.reconcile(s, state, NOW, False)[2])
            for changes in ({'memory_pressure_level': 2}, {'memory_available_gib': 1}, {'internal_free_gib': 29}, {'heavy_build_running': True}):
                self.assertTrue(resources.resource_blockers({**s, **changes}))
        record = self.persisted()
        self.assertTrue(record['binding']['attempt_consumed'])
        self.assertFalse(resources.bootstrap_cancelled(record))

    def test_high_swap_native_read_keeps_diagnostic_and_malformed_measurement_fails(self):
        outputs = ['System-wide memory free percentage: 80%', 'used = 25600M', '1',
                   'Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free: 600000.\nPages inactive: 600000.\n']
        with patch.object(resources, 'output', side_effect=outputs):
            host = REAL_HOST_MEMORY_STATUS()
        self.assertEqual(host['swap_used_gib'], 25)
        self.assertEqual(host['memory_pressure_level'], 1)
        with patch.object(resources, 'output', side_effect=[outputs[0], 'unreadable', *outputs[2:]]), self.assertRaises(ValueError):
            REAL_HOST_MEMORY_STATUS()

    def test_watchdog_occupancy_independent_pressure_freepercent_and_disk_guards(self):
        self.catalog.write_bytes(b'catalog')
        self.config.write_text(self.config.read_text().replace('only_when_click_backup_now', 'continuously'))
        for occupancy in (0, 25):
            for changes, disk, expected in (({}, 40, 'observing'), ({'memory_pressure_level': 2}, 40, 'pause_requested'),
                                            ({'memory_free_percent': 14}, 40, 'pause_requested'), ({}, 9, 'pause_requested')):
                with patch.object(resources, 'memory_status', return_value=healthy(swap_used_gib=occupancy, **changes)), \
                     patch.object(resources.shutil, 'disk_usage', return_value=SimpleNamespace(free=disk * guard.GIB)):
                    self.assertEqual(watchdog.observe()['status'], expected)
        with patch.object(resources, 'protect_installation_hold', return_value=None), \
             patch.object(resources, 'memory_status', side_effect=ValueError('unavailable')), \
             patch.object(resources, 'request_pause') as pause:
            self.assertEqual(watchdog.run()['status'], 'state_unavailable_pause_requested')
            pause.assert_called_once_with('backup_state_unavailable')


    def test_catalog_materializing_during_watchdog_resource_read_latches_before_pause(self):
        record = self.persisted()
        self.catalog.write_bytes(b'catalog')
        seen = resources.bootstrap_catalog()
        self.catalog.unlink()
        observed = {**self.observation, **seen}
        with patch.object(resources, 'observe_bootstrap', return_value=observed), \
             patch.object(resources, 'pause_bootstrap', return_value={'status': 'bootstrap_pause_pending'}) as pause:
            self.assertEqual(watchdog.run()['status'], 'bootstrap_pause_pending')
        self.assertEqual(resources.read_bootstrap()['first_catalog_seen'], seen['catalog_observation'])
        pause.assert_called_once()
        with self.assertRaises(resources.BootstrapError):
            resources.bootstrap_catalog(strict=resources.read_bootstrap()['first_catalog_seen'] is not None)


    def scan_fixture(self):
        directory = self.bzdata / 'bzfilelists'
        directory.mkdir()
        finished = datetime.fromtimestamp(NOW - 10, timezone.utc).strftime('%Y%m%d%H%M%S')
        started = datetime.fromtimestamp(NOW - 1000, timezone.utc).strftime('%Y%m%d%H%M%S')
        stats = directory / 'filestats.xml'
        stats.write_text('<filestats><progress totally_final="true" good_for_installer="true"/>'
                         '<info datetime="' + finished + '" hguid="' + NEW_HGUID + '" ahguid="' + NEW_HGUID + '"/></filestats>')
        expected = {'identity_sha256': NEW_IDENTITY, 'started_utc': started, 'finished_utc': finished,
                    'filestats_sha256': hashlib.sha256(stats.read_bytes()).hexdigest(),
                    'filestats_stat': resources.scan_stat(stats.lstat()), 'volumes': {}}
        files = []
        for mount, guid in resources.bootstrap_config()['volumes'].items():
            path = directory / (guid + '_0_filelist.dat')
            path.write_bytes(b'fixture payload must never be opened')
            os.utime(path, ns=((NOW - 10) * 10**9 + 1, (NOW - 10) * 10**9 + 1))
            expected['volumes'][mount] = {'guid_sha256': hashlib.sha256(guid.encode()).hexdigest(),
                                         'name_sha256': hashlib.sha256(path.name.encode()).hexdigest(),
                                         'stat': resources.scan_stat(path.lstat())}
            files.append(path)
        self.stack.enter_context(patch.object(resources, 'FIRST_SCAN_PUBLICATION', expected))
        return expected, stats, files

    def test_documented_publication_recipe_reads_fixture_metadata_and_never_overwrites(self):
        expected, stats, files = self.scan_fixture()
        runbook = Path(__file__).resolve().parents[1] / 'runbooks/backblaze-bootstrap.md'
        code = runbook.read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'publication.json'
            with patch('sys.argv', ['-', expected['started_utc'], str(output)]), patch('builtins.print'):
                exec(compile(code, str(runbook), 'exec'), {})
                original = output.read_bytes()
                self.assertEqual(json.loads(original), expected)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    exec(compile(code, str(runbook), 'exec'), {})
                self.assertEqual(output.read_bytes(), original)
            with patch('sys.argv', ['-', expected['started_utc'], str(self.bzdata / 'forbidden.json')]):
                with self.assertRaisesRegex(ValueError, 'outside vendor data'):
                    exec(compile(code, str(runbook), 'exec'), {})
        self.actions.assert_not_called()
        self.spawns.assert_not_called()

    def test_first_scan_binding_reads_only_small_native_evidence_and_exact_final_file_stats(self):
        expected, stats, files = self.scan_fixture()
        original_open = resources.os.open
        opened = []
        def open_native(path, *args):
            opened.append(Path(path))
            self.assertNotIn(Path(path), files)
            return original_open(path, *args)
        with patch.object(resources.os, 'open', side_effect=open_native):
            result = resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)
        self.assertEqual(result['scope'], 'native_scan_processing_publication_only')
        self.assertEqual(result['finished_epoch'], NOW - 10)
        self.assertEqual(len(opened), 3)
        self.assertNotIn(NEW_HGUID, json.dumps(result))

    def test_first_scan_flags_timestamps_and_both_published_identities_are_required(self):
        expected, stats, files = self.scan_fixture()
        original = stats.read_text()
        for text in (original.replace('totally_final="true"', 'totally_final="false"'),
                     original.replace('good_for_installer="true"', 'good_for_installer="false"'),
                     original.replace('hguid="' + NEW_HGUID + '"', 'hguid="' + '3' * 24 + '"', 1),
                     original.replace('ahguid="' + NEW_HGUID + '"', 'ahguid="' + '3' * 24 + '"'),
                     original.replace(expected['finished_utc'], expected['started_utc'])):
            stats.write_text(text)
            # Even a separately pinned content hash cannot replace final flags,
            # fresh publication time, or both native identity fields.
            expected['filestats_sha256'] = hashlib.sha256(stats.read_bytes()).hexdigest()
            expected['filestats_stat'] = resources.scan_stat(stats.lstat())
            with self.assertRaises(resources.BootstrapError):
                resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)

    def test_first_scan_future_wrong_volume_missing_final_or_replaced_inode_never_passes(self):
        expected, stats, files = self.scan_fixture()
        future = stats.with_name('filestats.xml.future')
        future.write_text('unfinished')
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)
        future.unlink()
        original_config = self.config.read_bytes()
        self.config.write_bytes(original_config.replace(b'bzVolumeGuid="external"', b'bzVolumeGuid="other"'))
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)
        self.config.write_bytes(original_config)
        saved = files[0].with_suffix('.saved')
        files[0].rename(saved)
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)
        files[0].write_bytes(saved.read_bytes())
        os.utime(files[0], ns=(expected['volumes']['/']['stat'][-2], expected['volumes']['/']['stat'][-2]))
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)
        files[0].unlink()
        files[0].symlink_to(saved)
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)

    def test_first_scan_small_file_replacement_bounds_and_recheck_reject_changes(self):
        expected, stats, files = self.scan_fixture()
        original = stats.read_bytes()
        stats.write_bytes(original + b' ')
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)
        stats.write_bytes(b'x' * 16385)
        with self.assertRaises(resources.BootstrapError):
            resources.bounded_native_bytes(stats, 16384)
        stats.unlink()
        stats.symlink_to(self.config)
        with self.assertRaises(OSError):
            resources.bounded_native_bytes(stats, 16384)
        stats.unlink()
        stats.write_bytes(original)
        expected['filestats_stat'] = resources.scan_stat(stats.lstat())
        original_read = resources.bounded_native_bytes
        def replace_after_read(path, limit):
            result = original_read(path, limit)
            if path == stats:
                self.config.write_text(self.config.read_text() + ' ')
            return result
        with patch.object(resources, 'bounded_native_bytes', side_effect=replace_after_read), self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)

    def test_first_scan_identity_guard_refuses_other_installation_and_future_publication(self):
        expected, stats, files = self.scan_fixture()
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), IDENTITY)
        install = self.root / 'bzinstall.xml'
        original = install.read_bytes()
        install.write_bytes(original.replace(NEW_HGUID.encode(), ('3' * 24).encode()))
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)
        install.write_bytes(original)
        self.clock.return_value = NOW - 11
        with self.assertRaises(resources.BootstrapError):
            resources.require_first_scan_completion(resources.bootstrap_config(), NEW_IDENTITY)


    def test_watchdog_low_free_percent_propagates_protective_pause_during_bootstrap(self):
        self.persisted()
        with patch.object(resources, 'observe_bootstrap', side_effect=resources.BootstrapError('bootstrap resource admission revoked')), \
             patch.object(resources, 'supervised_cli', return_value=0):
            result = watchdog.run()
        self.assertEqual(result['status'], 'bootstrap_pause_pending')
        self.assertTrue(result['pause_accepted'])
        self.assertTrue(resources.bootstrap_stop_path().exists())
        self.assertEqual(resources.read_bootstrap()['phase'], 'pause_pending')


    def test_missing_or_wrong_pause_report_blocks_preflight_transition_and_post_pause_acceptance(self):
        for value in (None, 'not_paused', False):
            self.report['backup']['status']['paused'] = value
            with self.assertRaises(resources.BootstrapError):
                self.preflight()
            self.catalog.write_bytes(b'catalog')
            self.assertEqual(guard.transition_personal_trial(*self.arguments[:4])[1], 1)
            self.assertFalse(resources.trial_path().exists())
            self.catalog.unlink()
        del self.report['backup']['status']['paused']
        with self.assertRaises(resources.BootstrapError):
            self.preflight()
        self.report['backup']['status']['paused'] = 'action_pause_backup'
        record = self.persisted()
        self.catalog.write_bytes(b'catalog')
        record = resources.update_bootstrap(record, first_catalog_seen=resources.bootstrap_catalog()['catalog_observation'])
        with patch.object(resources, 'supervised_cli', return_value=0):
            resources.pause_bootstrap(record, 'first_catalog_seen')
        self.report['backup']['status'].pop('paused')
        with self.assertRaises(resources.BootstrapError):
            guard.finish_bootstrap(record)
        self.assertEqual(resources.read_bootstrap()['phase'], 'pause_pending')
        self.spawns.assert_not_called()


if __name__ == '__main__':
    unittest.main()
