from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import backblaze_health as guard
from scripts import backblaze_resource_watchdog as watchdog
from tests.test_backblaze_health import healthy, NOW, HGUID, IDENTITY

resources = guard.resources


class InstallationHoldTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.bzdata = self.root / 'bzdata'
        self.bzdata.mkdir()
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        self.control = self.root / 'control'
        for target, name, value in ((resources, 'BZDATA', self.bzdata),
                                    (guard, 'ARTIFACT_ROOT', self.artifacts),
                                    (resources, 'CONTROL_DIR', self.control),
                                    (resources, 'PAUSE_STATE', self.control / 'pause.json')):
            self.stack.enter_context(patch.object(target, name, value))
        self.processes = self.stack.enter_context(patch.object(resources, 'process_status', return_value={'transmitter_process_running': False}))
        self.report = self.stack.enter_context(patch.object(guard, 'cli_report', return_value={
            'backup': {'installation': {'hguid': HGUID}, 'license': {'status': 'billing_active'}}}))
        self.action = self.stack.enter_context(patch.object(resources.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)))
        self.configure = self.stack.enter_context(patch.object(resources, 'configure_schedule'))
        self.stack.enter_context(patch.object(resources.time, 'time', return_value=NOW))
        self.state_path = self.artifacts / 'state.json'
        self.state_path.write_text(json.dumps({'requested_epoch': NOW - 1000, 'backup_identity_sha256': IDENTITY}))
        self.original_state = self.state_path.read_bytes()
        (self.root / 'bzinstall.xml').write_text('<bzinstall><bzuniqueid hguid="' + HGUID + '"/></bzinstall>')
        (self.bzdata / 'bzinfo.xml').write_text('<bzinfo><do_backup backup_schedule_type="only_when_click_backup_now"/></bzinfo>')
        (self.bzdata / 'overviewstatus.xml').write_text('<status><bztransmit cur_state="not_running"/></status>')
        (self.bzdata / 'bzbackup').mkdir()
        (self.bzdata / 'bzbackup/bzfileids.dat').write_bytes(b'catalog')

    def hold(self, **changes):
        data = dict(schema=resources.HOLD_SCHEMA, active=True,
                    original_identity_sha256=IDENTITY, prepared_epoch=NOW - 500)
        data.update(changes)
        resources.atomic_json(resources.hold_path(), data)
        return resources.hold_path().read_bytes()

    def assert_no_actions_or_request_change(self):
        self.action.assert_not_called()
        self.configure.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), self.original_state)

    def test_prepare_and_release_are_explicit_private_metadata_only_and_idempotent(self):
        self.assertEqual(guard.installation_hold(IDENTITY)[1], 0)
        hold = resources.hold_path()
        prepared = hold.read_bytes()
        self.assertEqual(hold.stat().st_mode & 0o777, 0o600)
        self.assertEqual(guard.installation_hold(IDENTITY)[1], 0)
        self.assertEqual(hold.read_bytes(), prepared)
        self.assertEqual(guard.installation_hold(IDENTITY, release=True)[1], 0)
        released = hold.read_bytes()
        self.assertEqual(guard.installation_hold(IDENTITY, release=True)[1], 0)
        self.assertEqual(hold.read_bytes(), released)
        data = resources.read_installation_hold()
        self.assertFalse(data['active'])
        self.assertEqual(data['original_identity_sha256'], IDENTITY)
        self.assert_no_actions_or_request_change()

    def test_prepare_never_replaces_original_identity_even_after_release(self):
        for active in (True, False):
            with self.subTest(active=active):
                changes = {} if active else dict(released_epoch=NOW, released_identity_sha256=IDENTITY)
                previous = self.hold(active=active, **changes)
                self.assertEqual(guard.installation_hold('b' * 64)[1], 1)
                self.assertEqual(resources.hold_path().read_bytes(), previous)
        self.assert_no_actions_or_request_change()

    def test_hold_has_no_automatic_expiry(self):
        self.hold(prepared_epoch=1)
        with self.assertRaises(resources.InstallationHoldError):
            resources.require_no_installation_hold()

    def test_reprepare_after_release_preserves_original_identity_and_timestamp(self):
        self.hold(active=False, released_epoch=NOW - 10, released_identity_sha256=IDENTITY)
        self.assertEqual(guard.installation_hold(IDENTITY)[1], 0)
        held = resources.read_installation_hold()
        self.assertTrue(held['active'])
        self.assertEqual(held['prepared_epoch'], NOW - 500)
        self.assertEqual(held['original_identity_sha256'], IDENTITY)
        self.assertNotIn('released_epoch', held)
        self.assert_no_actions_or_request_change()

    def test_release_missing_hold_does_not_create_one(self):
        self.assertEqual(guard.installation_hold(IDENTITY, release=True)[1], 1)
        self.assertFalse(resources.hold_path().exists())
        self.assert_no_actions_or_request_change()

    def test_prepare_requires_supplied_matching_paid_identity(self):
        for expected, license_status in (('', 'billing_active'), ('b' * 64, 'billing_active'),
                                          (IDENTITY, 'trial_active')):
            with self.subTest(expected=expected, license_status=license_status):
                self.report.return_value['backup']['license']['status'] = license_status
                self.assertEqual(guard.installation_hold(expected)[1], 1)
                self.assertFalse(resources.hold_path().exists())
        self.assert_no_actions_or_request_change()

    def test_release_rejects_trial_missing_config_catalog_unknown_status_or_live_transmitter(self):
        original = self.hold()
        scenarios = (
            patch.object(guard, 'cli_report', return_value={'backup': {'installation': {'hguid': HGUID}, 'license': {'status': 'trial_active'}}}),
            patch.object(resources, 'installed_identity', return_value='b' * 64),
            patch.object(resources, 'catalog_size_gib', side_effect=OSError('missing catalog')),
            patch.object(resources, 'process_status', return_value={'transmitter_process_running': True}),
            patch.object(resources, 'process_status', side_effect=ValueError('unknown process state')),
        )
        for context in scenarios:
            with self.subTest(context=context), context:
                self.assertEqual(guard.installation_hold(IDENTITY, release=True)[1], 1)
                self.assertEqual(resources.hold_path().read_bytes(), original)
        for name, content in (('bzinfo.xml', '<bzinfo/>'), ('bzinfo.xml', '<bzinfo><do_backup backup_schedule_type="continuously"/></bzinfo>'),
                              ('overviewstatus.xml', '<status><bztransmit cur_state="unknown"/></status>')):
            path = self.bzdata / name
            before = path.read_bytes()
            path.write_text(content)
            self.assertEqual(guard.installation_hold(IDENTITY, release=True)[1], 1)
            self.assertEqual(resources.hold_path().read_bytes(), original)
            path.write_bytes(before)
        self.assert_no_actions_or_request_change()

    def test_malformed_nonprivate_symlink_or_fifo_hold_is_fail_closed(self):
        path = resources.hold_path()
        self.hold()
        for content in ('{}', '{', '{"schema":null}'):
            path.write_text(content)
            with self.assertRaises(resources.InstallationHoldError):
                resources.read_installation_hold()
        self.hold()
        path.chmod(0o644)
        with self.assertRaises(resources.InstallationHoldError):
            resources.read_installation_hold()
        path.unlink()
        path.symlink_to(self.root / 'absent')
        with self.assertRaises(resources.InstallationHoldError):
            resources.read_installation_hold()
        path.unlink()
        os.mkfifo(path, mode=0o600)
        with self.assertRaises(resources.InstallationHoldError):
            resources.read_installation_hold()
        self.assert_no_actions_or_request_change()

    def test_malformed_timestamps_and_release_identity_never_disable_hold(self):
        for changes in (dict(prepared_epoch=True), dict(prepared_epoch=float('nan')),
                        dict(prepared_epoch=10 ** 1000), dict(released_epoch=NOW),
                        dict(active=False, released_epoch=NOW, released_identity_sha256='b' * 64)):
            with self.subTest(changes=changes):
                self.hold(**changes)
                with self.assertRaises(resources.InstallationHoldError):
                    resources.require_no_installation_hold()

    def test_identity_change_during_final_idle_check_cannot_release(self):
        original = self.hold()
        def replaced():
            (self.root / 'bzinstall.xml').write_text('<bzinstall><bzuniqueid hguid="' + '2' * 24 + '"/></bzinstall>')
            return {'transmitter_process_running': False}
        self.processes.side_effect = replaced
        self.assertEqual(guard.installation_hold(IDENTITY, release=True)[1], 1)
        self.assertEqual(resources.hold_path().read_bytes(), original)
        self.assert_no_actions_or_request_change()

    def test_active_or_invalid_hold_blocks_start_check_before_provider_read(self):
        for invalid in (False, True):
            self.hold()
            if invalid:
                resources.hold_path().write_text('{')
            for start in (False, True):
                self.report.reset_mock()
                payload, code = guard.run(start=start)
                self.assertEqual(code, 1)
                self.assertEqual(payload['status'], 'installation_hold_invalid' if invalid else 'installation_held')
                self.report.assert_not_called()
                self.assert_no_actions_or_request_change()

    def test_hold_created_after_observation_blocks_intent_schedule_completion_and_state_writes(self):
        for situation in ('request', 'resume', 'completion_restore', 'completion', 'observation'):
            with self.subTest(situation=situation):
                resources.hold_path().unlink(missing_ok=True)
                state = {'requested_epoch': NOW - guard.DAY, 'backup_identity_sha256': IDENTITY}
                snapshot = healthy()
                if situation in ('request', 'observation'):
                    snapshot['remaining_files'] = 1
                if situation in ('resume', 'completion_restore'):
                    state['restore_schedule'] = 'only_when_click_backup_now'
                if situation in ('resume', 'observation'):
                    snapshot['transmit_state'] = 'transmitting'
                if situation == 'completion_restore':
                    snapshot['schedule'] = 'continuously'
                self.state_path.write_text(json.dumps(state))
                before = self.state_path.read_bytes()
                reconcile = guard.reconcile
                def observe_hold(*args):
                    result = reconcile(*args)
                    self.hold()
                    return result
                with patch.object(guard, 'snapshot', return_value=snapshot), patch.object(guard, 'reconcile', side_effect=observe_hold):
                    payload, code = guard.run(start=False)
                self.assertEqual(payload['status'], 'installation_held')
                self.assertEqual(code, 1)
                self.assertEqual(self.state_path.read_bytes(), before)
                self.action.assert_not_called()
                self.configure.assert_not_called()

    def test_watchdog_reasserts_pause_when_transmitter_reappears_despite_old_ack(self):
        self.hold()
        resources.atomic_json(resources.PAUSE_STATE, {'paused': True, 'backup_identity_sha256': IDENTITY,
                              'pause_requested_epoch': NOW - 600, 'pause_accepted_epoch': NOW - 500})
        self.processes.return_value = {'transmitter_process_running': True}
        with patch.object(watchdog, 'observe', side_effect=AssertionError('hold must run first')):
            result = watchdog.run()
        self.assertEqual(result['status'], 'installation_hold_pause_requested')
        self.assertTrue(result['pause_reasserted'])
        self.assertEqual(self.action.call_args.args[0][-1], '--pause-backup')
        self.assertEqual(self.state_path.read_bytes(), self.original_state)
        self.assertEqual(resources.read_installation_hold()['original_identity_sha256'], IDENTITY)

    def test_watchdog_missing_installer_files_still_attempts_pause_without_nested_lock(self):
        original = self.hold()
        (self.root / 'bzinstall.xml').unlink()
        (self.bzdata / 'bzinfo.xml').unlink()
        (self.bzdata / 'bzbackup/bzfileids.dat').unlink()
        resources.atomic_json(resources.PAUSE_STATE, {'paused': True, 'pause_accepted_epoch': NOW - 5})
        self.configure.side_effect = OSError('missing installer config')
        with patch('builtins.print'):
            self.assertEqual(watchdog.main(), 1)
        self.action.assert_called_once()
        self.assertEqual(self.action.call_args.args[0][-1], '--pause-backup')
        self.assertEqual(resources.hold_path().read_bytes(), original)
        self.assertEqual(self.state_path.read_bytes(), self.original_state)
        receipt = json.loads((self.control / 'backblaze-watchdog-latest.json').read_text())
        self.assertEqual(receipt['status'], 'error')
        self.assertNotEqual(receipt['error_type'], 'BlockingIOError')

    def test_corrupt_hold_and_pause_latch_still_reassert_protective_pause(self):
        self.hold()
        resources.hold_path().write_text('{')
        resources.PAUSE_STATE.write_text('{')
        self.assertEqual(watchdog.run()['status'], 'installation_hold_invalid')
        self.action.assert_called_once()
        self.assertEqual(self.action.call_args.args[0][-1], '--pause-backup')

    def test_uninstalled_client_is_reported_without_claiming_pause_or_idle(self):
        original = self.hold()
        self.action.side_effect = FileNotFoundError(2, 'No such file', str(resources.BZCLI))
        self.assertEqual(watchdog.main(), 1)
        self.action.assert_called_once()
        receipt = json.loads((self.control / 'backblaze-watchdog-latest.json').read_text())
        self.assertEqual(receipt['status'], 'installation_hold_client_unavailable')
        self.assertFalse(receipt['pause_accepted'])
        self.assertFalse(receipt['installation_state_verified_idle'])
        self.assertEqual(resources.watchdog_status(NOW), 'failed')
        self.assertEqual(resources.hold_path().read_bytes(), original)
        self.assertEqual(self.state_path.read_bytes(), self.original_state)

    def test_other_missing_paths_are_not_misreported_as_an_uninstalled_client(self):
        error = FileNotFoundError(2, 'No such file', '/unrelated/control')
        with patch.object(resources, 'protect_installation_hold', side_effect=error):
            with self.assertRaises(FileNotFoundError):
                watchdog.run()

    def test_invalid_hold_keeps_priority_when_the_client_is_also_absent(self):
        self.hold()
        resources.hold_path().write_text('{')
        self.action.side_effect = FileNotFoundError(2, 'No such file', str(resources.BZCLI))
        self.assertEqual(watchdog.main(), 1)
        self.action.assert_called_once()
        receipt = json.loads((self.control / 'backblaze-watchdog-latest.json').read_text())
        self.assertEqual(receipt['status'], 'installation_hold_invalid')
        self.assertFalse(receipt['pause_accepted'])
        self.assertFalse(receipt['installation_state_verified_idle'])
        self.assertEqual(resources.watchdog_status(NOW), 'failed')
        self.assertEqual(resources.hold_path().read_text(), '{')
        self.assertEqual(self.state_path.read_bytes(), self.original_state)

    def test_held_idle_does_not_need_heavy_report_and_cannot_start_a_request(self):
        self.hold()
        resources.atomic_json(resources.PAUSE_STATE, {'paused': True, 'backup_identity_sha256': IDENTITY,
                              'pause_requested_epoch': NOW - 600, 'pause_accepted_epoch': NOW - 500})
        self.assertEqual(watchdog.run()['status'], 'installation_held')
        self.report.assert_not_called()
        self.assert_no_actions_or_request_change()

    def test_admin_lock_contention_cannot_partially_prepare_or_release_hold(self):
        with resources.admin_lock():
            self.assertEqual(guard.installation_hold(IDENTITY)[1], 1)
        self.assertFalse(resources.hold_path().exists())
        original = self.hold()
        with resources.admin_lock():
            self.assertEqual(guard.installation_hold(IDENTITY, release=True)[1], 1)
        self.assertEqual(resources.hold_path().read_bytes(), original)
        self.assert_no_actions_or_request_change()

    def test_empty_malformed_missing_and_conflicting_cli_args_never_run_regular_check(self):
        for option in ('--prepare-installation-hold', '--release-installation-hold'):
            for value in ('', 'invalid'):
                with patch('sys.argv', ['backblaze_health.py', option, value]), patch.object(guard, 'run') as run, patch('builtins.print'):
                    self.assertEqual(guard.main(), 1)
                    run.assert_not_called()
            for arguments in ([option], [option, IDENTITY, '--start']):
                with patch('sys.argv', ['backblaze_health.py'] + arguments), patch.object(guard, 'run') as run, patch('sys.stderr'):
                    with self.assertRaises(SystemExit) as exit_result:
                        guard.main()
                    self.assertEqual(exit_result.exception.code, 2)
                    run.assert_not_called()
        self.assert_no_actions_or_request_change()


if __name__ == '__main__':
    unittest.main()
