#!/usr/bin/env python3
"""Start a weekly Backblaze run and reconcile its later result without claiming restore proof.

Uses the supported bzCLI shipped with Backblaze 10.0.1+. Local XML timestamps
supplement the report because the GUI's zero-remaining count can be weeks old.
https://www.backblaze.com/computer-backup/docs/how-to-use-the-bzcli
"""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

try:
    from .external_volume_guard import read_volume_uuid
    from . import backblaze_resources as resources
except ImportError:  # Direct script execution.
    from external_volume_guard import read_volume_uuid
    import backblaze_resources as resources

WORKSPACE = OPERATOR.require_path('paths.workspace')
ARTIFACT_ROOT = WORKSPACE / 'artifacts' / 'backblaze_health'
BZCLI = OPERATOR.require_path('backup.client_executable')
BZDATA = OPERATOR.require_path('backup.client_data_root')
OWC = OPERATOR.require_path('paths.data_root')
GIB = 1024 ** 3
DAY = 86400
RETRY_SECONDS = 12 * 3600
HEALTH_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


write_json = resources.atomic_json
memory_status = resources.memory_status
configure_schedule = resources.configure_schedule


class IdentityBindingError(RuntimeError):
    pass


def valid_fingerprint(value: str) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def observed_identity(report: dict) -> str:
    try:
        installation = report['backup']['installation']
        if not isinstance(installation, dict):
            raise ValueError('installation is not an object')
        identity = resources.identity_fingerprint(installation.get('hguid'))
    except (KeyError, TypeError, ValueError):
        raise IdentityBindingError('the reported Backblaze identity is missing or invalid') from None
    require_current_identity(identity)
    return identity


def require_current_identity(expected: str) -> None:
    try:
        current = resources.installed_identity()
    except (OSError, ValueError, ET.ParseError):
        raise IdentityBindingError('the installed Backblaze identity could not be verified') from None
    if not valid_fingerprint(expected) or current != expected:
        raise IdentityBindingError('the Backblaze installation changed after observation')


def require_current_admission(expected: str) -> None:
    resources.require_no_installation_hold()
    require_current_identity(expected)


def identity_issue(s: dict, state: dict) -> str | None:
    observed = s.get('backup_identity_sha256')
    expected = state.get('backup_identity_sha256')
    if not valid_fingerprint(observed):
        return 'the installed Backblaze identity could not be verified'
    if expected is not None:
        if not valid_fingerprint(expected) or expected != observed:
            return 'the installed Backblaze identity does not match the saved backup request'
    elif any(state.get(key) for key in ('requested_epoch', 'completed_epoch', 'last_attempt_epoch', 'restore_schedule')):
        return 'the legacy backup request needs explicit binding to its previously recorded identity'
    return None


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')


def normalized(path: str | Path) -> str:
    return str(path).rstrip('/') or '/'


def cli_report() -> dict:
    result = subprocess.run([str(BZCLI), 'report', '--exit-on-error', '--format', 'minijson'],
                            capture_output=True, text=True, timeout=180)
    if result.returncode or result.stderr:
        raise RuntimeError(f'Backblaze report failed (exit {result.returncode})')
    return json.loads(result.stdout)


def require_paused_native_report(report: dict) -> None:
    status = report['backup']['status']
    if status.get('installed') is not True or status.get('paused') != 'action_pause_backup':
        raise resources.BootstrapError('native installed and protective-pause report fields are required')


def snapshot(report: dict, now: float, *, initial_trial: bool = False, bootstrap_host: dict | None = None) -> dict:
    """Only retain an explicit allowlist; bzCLI's full report includes account data."""
    backup = report['backup']
    status = backup['status']
    if status['bztransmit'] not in ('not_running', 'transmitting'):
        raise ValueError('Unknown Backblaze transmitter state')
    identity = observed_identity(report)
    trial = resources.read_personal_trial()
    if trial is not None:
        try:
            resources.require_trial_license(backup['license'], trial['license_status'], now)
        except (ValueError, TypeError, KeyError):
            raise resources.PersonalTrialError('the provider trial license changed') from None
    initializing = initial_trial or trial is not None
    missing = object()
    counts = backup.get('backup', missing) if initializing else backup['backup']
    drives = report['sysinfo']['storage']
    owc = next((d for d in drives if normalized(d['filepath']) == str(OWC)), {})
    config = ET.parse(BZDATA / 'bzinfo.xml').getroot()
    scratch = config.find('scratch_drive')
    if scratch is None:
        raise ValueError('Backblaze scratch drive configuration is unavailable')
    scan_times = {}
    for mount in ('/', str(OWC)):
        volume = next((v for v in config.iter('bzvolume')
                       if normalized(v.get('mountPointPath', '')) == mount), None)
        scans = list((BZDATA / 'bzfilelists').glob(volume.attrib['bzVolumeGuid'] + '_*filelist.dat')) if volume is not None else []
        scan_times[mount] = max((p.stat().st_mtime for p in scans), default=None if initializing else 0)
    remaining_path = BZDATA / 'bzreports' / 'bzstat_remainingbackup.xml'
    def optional_integer(value, label):
        if value is missing and initializing:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int)) or re.fullmatch(r'[0-9]+', str(value)) is None:
            raise ValueError('Backblaze ' + label + ' is invalid')
        return int(value)

    def remaining(kind):
        if counts is missing and initializing:
            return None
        if not isinstance(counts, dict):
            raise ValueError('Backblaze counts are malformed')
        section = counts.get(kind, missing)
        if section is missing and initializing:
            return None
        if not isinstance(section, dict):
            raise ValueError('Backblaze counts are malformed')
        return optional_integer(section.get('remaining', missing), 'remaining count')

    last = status.get('last_backup', missing) if initializing else status['last_backup']
    if last is not missing and not isinstance(last, dict):
        raise ValueError('Backblaze last-backup metadata is malformed')
    last_millis = optional_integer(last.get('gmt_millis', missing) if last is not missing else missing, 'last-backup time')
    last_backup = last_millis / 1000 if last_millis is not None else None
    remaining_files, remaining_bytes = remaining('files'), remaining('bytes')
    try:
        remaining_epoch = remaining_path.stat().st_mtime
    except FileNotFoundError:
        if not initializing:
            raise
        remaining_epoch = None
    mounted = OWC.is_mount() and OWC.stat().st_dev != Path('/').stat().st_dev
    contract = json.loads((WORKSPACE / 'registry' / 'external_volume_guard.json').read_text())
    correct_volume = (mounted and normalized(contract['mountPoint']) == str(OWC)
                      and read_volume_uuid(OWC).upper() == contract['volumeUuid'].upper())
    host = memory_status() if bootstrap_host is None else bootstrap_host
    return {
        'backup_identity_sha256': identity,
        'observed_at': utc(now), 'observed_epoch': now, **host,
        'watchdog_status': resources.watchdog_status(now),
        'owc_mounted': mounted, 'owc_identity_verified': correct_volume, 'owc_selected': owc.get('selected_for_backup') is True,
        'internal_selected': any(normalized(d['filepath']) == '/' and d.get('selected_for_backup') is True for d in drives),
        'scratch_is_owc': normalized(scratch.get('scratch_mountpoint', '')) == str(OWC),
        'schedule': report['settings']['backup_schedule_type'],
        'license_status': backup['license']['status'], 'license_type': backup['license'].get('type'),
        'personal_trial_expires_epoch': trial['expires_epoch'] if trial is not None else None,
        'safety_freeze': status['safety_freeze'],
        'client_version': backup['installation'].get('version'),
        'completion_unknown': any(value is None for value in (remaining_files, remaining_bytes, last_backup, remaining_epoch, *scan_times.values())),
        # Vendor overview can retain 'transmitting' after a crashed/stopped PID.
        # A successfully read OS snapshot is the authority for process liveness.
        'transmit_report_state': status['bztransmit'],
        'transmit_state': 'transmitting' if host['transmitter_process_running'] else 'not_running',
        'remaining_files': remaining_files, 'remaining_bytes': remaining_bytes,
        'last_backup_epoch': last_backup, 'last_backup_at': utc(last_backup) if last_backup is not None else None,
        'owc_scan_epoch': scan_times[str(OWC)], 'internal_scan_epoch': scan_times['/'],
        'remaining_report_epoch': remaining_epoch,
        'internal_free_gib': round(shutil.disk_usage('/').free / GIB, 2),
        'owc_free_gib': round(shutil.disk_usage(OWC).free / GIB, 2) if mounted else 0,
        # This guard never authorizes destructive cleanup. A fresh upload count is
        # still not a verified restore or proof that mandatory exclusions are covered.
        'restore_verified': False, 'local_backup_removal_authorized': False,
    }


def active(s: dict) -> bool:
    return s['transmit_state'] == 'transmitting'


def blockers(s: dict, *, launch: bool = False) -> list[str]:
    issues = []
    if s['watchdog_status'] != 'healthy':
        issues.append('the backup resource supervisor is unavailable or has failed')
    if not s['owc_mounted']:
        issues.append('OWC is not mounted')
    if not s['owc_identity_verified']:
        issues.append('the mounted OWC volume does not match the registered disk identity')
    if not s['owc_selected']:
        issues.append('OWC is not selected for backup')
    if not s['internal_selected']:
        issues.append('the Mac is not selected for backup')
    if not s['scratch_is_owc']:
        issues.append('temporary backup storage is not on OWC')
    minimum_internal = 30 if launch else 10
    if s['internal_free_gib'] < minimum_internal:
        issues.append(f'the Mac needs at least {minimum_internal} GiB free ' +
                      ('to start rebuilding its backup catalog' if launch else 'while backing up'))
    if s['owc_free_gib'] < 20:
        issues.append('OWC needs at least 20 GiB free')
    if launch:
        issues.extend(issue for issue in resources.resource_blockers(s) if issue not in issues)
    admitted_trial = (s.get('license_type') == resources.TRIAL_TYPE
                      and s.get('personal_trial_expires_epoch') is not None
                      and s['personal_trial_expires_epoch'] > s['observed_epoch'])
    if s['license_status'] != 'billing_active' and not admitted_trial:
        issues.append('the Backblaze subscription is not confirmed active')
    if s['safety_freeze'] != 'not_frozen':
        issues.append('Backblaze reports a safety freeze')
    return issues


def completed(s: dict, state: dict, now: float) -> bool:
    requested = state.get('requested_epoch', 0)
    evidence = ('remaining_files', 'remaining_bytes', 'last_backup_epoch', 'owc_scan_epoch',
                'internal_scan_epoch', 'remaining_report_epoch')
    known = all(type(s.get(key)) in (int, float) and math.isfinite(s[key]) for key in evidence)
    return bool(known and not identity_issue(s, state) and state.get('backup_identity_sha256')
                and requested and s['remaining_files'] == 0 and s['remaining_bytes'] == 0
                and requested <= s['last_backup_epoch'] <= now + 60
                and requested <= s['owc_scan_epoch'] <= now + 60
                and requested <= s['internal_scan_epoch'] <= now + 60
                and requested <= s['remaining_report_epoch'] <= now + 60
                and now - s['last_backup_epoch'] <= 8 * DAY
                and not active(s))


def reconcile(s: dict, state: dict, now: float, start: bool) -> tuple[str, str, bool]:
    """Return status, human report, and whether one supported resume is appropriate."""
    identity_error = identity_issue(s, state)
    issues = [identity_error] if identity_error else blockers(s)
    if issues:
        return 'blocked', 'Backblaze needs attention: ' + '; '.join(issues) + '. Local backups remain protected.', False
    if completed(s, state, now):
        state['completed_epoch'] = s['last_backup_epoch']
        if not start:
            return ('completed', 'Backblaze finished uploading the selected files on the Mac and OWC. '
                    'A restore check is still required before removing local backups.', False)
    pending = bool(state.get('requested_epoch') and not state.get('completed_epoch'))
    if state.get('paused_for_resources') and pending:
        if active(s):
            if now - state.get('pause_requested_epoch', now) > 900:
                return 'pause_stalled', 'Backblaze has not stopped after its pause request. Its large catalog needs attention; local backups remain protected.', False
            return 'paused', 'Backblaze is finishing its pause to protect Mac memory and disk space. Local backups remain protected.', False
        issues = blockers(s, launch=True)
        if issues:
            return 'paused', 'Backblaze is paused to protect the Mac: ' + '; '.join(issues) + '. The daily check will resume it when resources recover.', False
        return 'request', '', True
    if active(s):
        if now - state.get('last_progress_epoch', now) > 2 * DAY:
            return 'stalled', 'Backblaze is running but has shown no measurable scan or upload progress for two days. Local backups remain protected.', False
        if not state.get('requested_epoch'):
            # Existing manual/continuous uploads can be observed, but cannot be
            # claimed as this guard's completed weekly backup without a later scan.
            state['requested_epoch'] = now
            state['backup_identity_sha256'] = s['backup_identity_sha256']
        return 'running', 'Backblaze is still backing up the Mac and OWC; the cloud backup is not yet complete.', False
    if start or pending:
        issues = blockers(s, launch=True)
        if issues:
            return 'blocked', 'Backblaze needs attention: ' + '; '.join(issues) + '. Local backups remain protected.', False
        if now - state.get('last_attempt_epoch', 0) < RETRY_SECONDS:
            return ('waiting', 'Backblaze has not finished the requested backup. The daily check will retry '
                    'after the current retry interval; local backups remain protected.', False)
        return 'request', '', True
    if state.get('completed_epoch') and s['last_backup_epoch'] is not None and now - s['last_backup_epoch'] <= 8 * DAY:
        return 'waiting', 'Backblaze is waiting for its next weekly run; the last completed upload remains recorded. Local backups await restore verification.', False
    return 'stale', 'Backblaze has no verified recent backup run for OWC. Local backups remain protected.', False


def run_locked(start: bool = False) -> tuple[dict, int]:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    # Serialize this administrative action and its receipt. The CLI independently
    # promises --backup-now does nothing when a backup is already in progress.
    with (ARTIFACT_ROOT / 'guard.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'busy', 'message': 'A Backblaze check is already running.'}, 0
        state_path = ARTIFACT_ROOT / 'state.json'
        s = None
        try:
            resources.require_no_installation_hold()
            trial = resources.read_personal_trial()
            if trial is not None:
                # Preserve the old request in place; its intent and completion
                # can never be transferred to the new installation.
                state_path = ARTIFACT_ROOT / ('state-personal-trial-' + trial['new_identity_sha256'] + '.json')
            if trial is not None:
                try:
                    state = json.loads(resources.private_bytes(state_path, 32768))
                except FileNotFoundError:
                    state = {}
            else:
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if not isinstance(state, dict):
                raise ValueError('Backblaze maintenance state is not an object')
            if state.get('restore_schedule') not in (None, 'only_when_click_backup_now'):
                raise ValueError('Backblaze saved schedule is invalid')
            control = resources.read_control()
            now = time.time()
            s = snapshot(cli_report(), now)
            now = time.time()
            issue = identity_issue(s, state)
            if issue:
                raise IdentityBindingError(issue)
            if (control.get('paused') and state.get('requested_epoch') and not state.get('completed_epoch')
                    and state.get('backup_identity_sha256') == s['backup_identity_sha256']
                    and control.get('backup_identity_sha256') == s['backup_identity_sha256']):
                # A pause latch protects resources; it is not authority to create
                # a new request or transfer a previous installation's intent.
                state['paused_for_resources'] = True
                state.setdefault('pause_requested_epoch', control.get('pause_requested_epoch', time.time()))
            for key, direction in (('remaining_bytes', -1), ('last_backup_epoch', 1), ('owc_scan_epoch', 1)):
                value, previous = s.get(key), state.get(key)
                if type(value) in (int, float) and math.isfinite(value):
                    if type(previous) in (int, float) and direction * value > direction * previous:
                        state['last_progress_epoch'] = now
                    state[key] = value
            status, message, request = reconcile(s, state, now, start)
            dangerous = (s['watchdog_status'] != 'healthy' or s['memory_pressure_level'] != 1
                         or s['internal_free_gib'] < 10 or s['memory_free_percent'] < 15)
            if dangerous and (active(s) or s['schedule'] == 'continuously'):
                state.setdefault('backup_identity_sha256', s['backup_identity_sha256'])
                state.setdefault('requested_epoch', now)
                state.setdefault('restore_schedule', 'only_when_click_backup_now')
                state.update(paused_for_resources=True, pause_reason='memory_or_disk_pressure')
                state.setdefault('pause_requested_epoch', now)
                with resources.admin_lock():
                    resources.require_no_installation_hold()
                    write_json(state_path, state)
                resources.request_pause('memory_or_disk_pressure')
                status, message, request = reconcile(s, state, now, start)
                if status not in ('blocked', 'pause_stalled'):
                    status, message, request = ('paused', 'Backblaze was asked to pause to protect Mac memory and disk space. '
                                               'The daily check will resume it when resources recover.', False)
            if (not request and status in ('running', 'waiting')
                    and state.get('restore_schedule') and s['schedule'] == state['restore_schedule']
                    and not state.get('completed_epoch')):
                # Recover an accepted intent if configure succeeded ambiguously
                # or the process stopped between intent and schedule mutation.
                with resources.admin_lock():
                    resources.require_no_installation_hold()
                    require_current_admission(state['backup_identity_sha256'])
                    fresh = memory_status()
                    fresh['internal_free_gib'] = shutil.disk_usage('/').free / GIB
                    if (resources.read_control().get('paused') or resources.resource_blockers(fresh)
                            or resources.watchdog_status(time.time()) != 'healthy'):
                        raise RuntimeError('Host resources prevent restoring continuous indexing')
                    require_current_admission(state['backup_identity_sha256'])
                    configure_schedule('continuously')
            if request:
                # Persist intent before dispatch: a timeout or crash after dispatch
                # must not trigger an immediate duplicate request on the next check.
                if not state.get('requested_epoch') or state.get('completed_epoch'):
                    state = {'requested_epoch': now, 'last_progress_epoch': now,
                             'backup_identity_sha256': s['backup_identity_sha256']}
                state['last_attempt_epoch'] = now
                state['swap_at_start_gib'] = s['swap_used_gib']
                state['attempt_count'] = state.get('attempt_count', 0) + 1
                if s['schedule'] == 'only_when_click_backup_now':
                    state['restore_schedule'] = s['schedule']
                with resources.admin_lock():
                    resources.require_no_installation_hold()
                    require_current_admission(state['backup_identity_sha256'])
                    write_json(state_path, state)
                    fresh = memory_status()
                    fresh['internal_free_gib'] = shutil.disk_usage('/').free / GIB
                    if resources.resource_blockers(fresh) or resources.watchdog_status(time.time()) != 'healthy':
                        raise RuntimeError('Host resources changed before backup start')
                    if state.get('restore_schedule'):
                        # CLI backup-now alone reuses old catalogs in manual mode.
                        # Continuous mode supplies the vendor-supported fresh scans.
                        require_current_admission(state['backup_identity_sha256'])
                        configure_schedule('continuously')
                    try:
                        require_current_admission(state['backup_identity_sha256'])
                        response = subprocess.run([str(BZCLI), 'action', '--backup-now'],
                                                  capture_output=True, text=True, timeout=60)
                        if response.returncode:
                            status, message = 'failed', 'Backblaze rejected the backup request. Local backups remain protected; the daily check will retry.'
                        else:
                            state.pop('paused_for_resources', None)
                            resources.atomic_json(resources.PAUSE_STATE, {'paused': False, 'started_epoch': time.time()})
                            status, message = 'requested', 'Backblaze accepted the request to back up the Mac and OWC. Upload completion will be checked daily.'
                    except subprocess.TimeoutExpired:
                        status, message = 'uncertain', 'The Backblaze start request timed out. The daily check will reconcile its status before retrying.'
            if status == 'completed' and state.get('restore_schedule'):
                if s['schedule'] not in ('continuously', state['restore_schedule']):
                    raise RuntimeError('Backblaze schedule changed outside this maintenance run')
                with resources.admin_lock():
                    resources.require_no_installation_hold()
                    require_current_admission(state['backup_identity_sha256'])
                    configure_schedule(state['restore_schedule'])
                state.pop('restore_schedule')
                state['schedule_restored_epoch'] = time.time()
            with resources.admin_lock():
                resources.require_no_installation_hold()
                if status == 'completed':
                    require_current_admission(state['backup_identity_sha256'])
                write_json(state_path, state)
            payload = {'schema': 'openclaw.backblaze_health.v1', 'status': status,
                       'message': message, 'snapshot': s, 'run': state}
        except resources.InstallationHoldError as exc:
            payload = {'schema': 'openclaw.backblaze_health.v1',
                       'status': 'installation_hold_invalid' if exc.invalid else 'installation_held',
                       'observed_at': utc(time.time()),
                       'message': ('Backblaze needs attention: its installation hold could not be verified.' if exc.invalid else
                                   'Backblaze scheduled backups are blocked by an explicit installation hold; release is required before they resume.') +
                                  ' The pending backup request and local backups remain protected.'}
        except (IdentityBindingError, resources.PersonalTrialError) as exc:
            try:
                resources.request_pause('personal_trial_unverified' if isinstance(exc, resources.PersonalTrialError)
                                        else 'backup_identity_unverified')
                pause_note = ' A protective pause was requested.'
            except (OSError, RuntimeError, ValueError, ET.ParseError, subprocess.SubprocessError):
                pause_note = ' A protective pause could not be confirmed.'
            payload = {'schema': 'openclaw.backblaze_health.v1',
                       'status': 'personal_trial_blocked' if isinstance(exc, resources.PersonalTrialError) else 'identity_blocked',
                       'observed_at': utc(time.time()),
                       'message': 'Backblaze needs attention: ' + str(exc) + '.' + pause_note +
                                  ' Local backups remain protected.'}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError, ET.ParseError,
                subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
            # Do not persist provider stdout/stderr, which can contain account data.
            payload = {'schema': 'openclaw.backblaze_health.v1', 'status': 'error',
                       'observed_at': utc(time.time()), 'error_type': type(exc).__name__,
                       'message': 'Backblaze status could not be verified. Local backups remain protected; the next scheduled check will retry.'}
        write_json(ARTIFACT_ROOT / 'latest.json', payload)
        (ARTIFACT_ROOT / 'latest.txt').write_text(payload['message'] + '\n')
        return payload, 0 if payload['status'] in ('completed', 'running', 'requested', 'waiting') else 1


def run(start: bool = False) -> tuple[dict, int]:
    try:
        return run_locked(start)
    except OSError as exc:
        # Disk exhaustion must remain a concise scheduler-visible failure even
        # when writing the normal receipt is itself impossible.
        return {'status': 'error', 'error_type': type(exc).__name__,
                'message': 'Backblaze maintenance could not save its status. Check free disk space; local backups remain protected.'}, 1


def adopt_legacy_identity(expected: str) -> tuple[dict, int]:
    """Bind legacy maintenance metadata to a previously recorded fingerprint.

    Provider reads only. This does not start, pause, configure or license a backup.
    The caller must supply the old identity, never infer it from a replacement.
    """
    try:
        if not valid_fingerprint(expected):
            raise ValueError('invalid expected fingerprint')
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        with (ARTIFACT_ROOT / 'guard.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state_path = ARTIFACT_ROOT / 'state.json'
            state = json.loads(state_path.read_text())
            if (not isinstance(state, dict) or not state.get('requested_epoch')
                    or state.get('backup_identity_sha256') is not None):
                raise ValueError('not an unbound legacy request')
            with resources.admin_lock():
                resources.require_no_installation_hold()
                report = cli_report()
                if report['backup']['license']['status'] != 'billing_active':
                    raise ValueError('paid license is not confirmed')
                if observed_identity(report) != expected:
                    raise IdentityBindingError('the known old identity is not currently installed')
                require_current_identity(expected)
                state.update(backup_identity_sha256=expected, identity_adopted_epoch=time.time())
                write_json(state_path, state)
        return {'status': 'identity_adopted', 'backup_identity_sha256': expected,
                'message': 'The legacy request is bound to its verified existing Backblaze identity. No backup action was performed.'}, 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError):
        return {'status': 'identity_adoption_failed',
                'message': 'The legacy request could not be bound to the supplied old Backblaze identity. No backup action was performed.'}, 1


def installation_hold(expected: str, *, release: bool = False) -> tuple[dict, int]:
    """Explicit local metadata only; hold identity and pending request never migrate."""
    try:
        if not valid_fingerprint(expected):
            raise ValueError('invalid expected fingerprint')
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        # Same lock order as normal checks. Holding guard.lock keeps a pending
        # request unchanged while the separate installation hold is prepared.
        with (ARTIFACT_ROOT / 'guard.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with resources.admin_lock():
                hold = resources.read_installation_hold()
                if hold is not None and hold['original_identity_sha256'] != expected:
                    raise IdentityBindingError('the original held identity cannot change')
                if release and hold is None:
                    raise ValueError('there is no installation hold to release')
                report = cli_report()
                if report['backup']['license']['status'] != 'billing_active':
                    raise ValueError('paid license is not confirmed')
                if observed_identity(report) != expected:
                    raise IdentityBindingError('the supplied identity is not currently installed')
                if release:
                    resources.require_manual_and_drained()
                require_current_identity(expected)
                if hold is None:
                    hold = {'schema': resources.HOLD_SCHEMA, 'active': True,
                            'original_identity_sha256': expected, 'prepared_epoch': time.time()}
                elif hold['active'] == (not release):
                    # Retrying explicit prepare/release cannot refresh its age
                    # or silently replace the original identity.
                    return {'status': 'installation_hold_released' if release else 'installation_hold_prepared',
                            'message': 'The installation hold already has the requested state. No backup action was performed.'}, 0
                if release:
                    released = time.time()
                    if released < hold['prepared_epoch']:
                        raise ValueError('the hold clock moved backwards')
                    hold.update(active=False, released_epoch=released, released_identity_sha256=expected)
                else:
                    hold['active'] = True
                    hold.pop('released_epoch', None)
                    hold.pop('released_identity_sha256', None)
                write_json(resources.hold_path(), hold)
        return {'status': 'installation_hold_released' if release else 'installation_hold_prepared',
                'message': ('The installation hold was released for its verified, paid, idle original installation.' if release else
                            'The installation hold was prepared. Scheduled backups remain blocked until explicit release.') +
                           ' No backup action was performed.'}, 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError):
        return {'status': 'installation_hold_change_failed',
                'message': 'The installation hold could not be changed for the supplied Backblaze identity. No backup action was performed.'}, 1


def transition_personal_trial(original: str, replacement: str, status: str, hold_sha256: str) -> tuple[dict, int]:
    """Admit one observed personal trial by local metadata only, preserving prior intent."""
    try:
        if not all(valid_fingerprint(value) for value in (original, replacement, hold_sha256)) or original == replacement:
            raise ValueError('invalid distinct transition identities')
        expires = resources.trial_expiry(status)
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        with (ARTIFACT_ROOT / 'guard.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with resources.admin_lock():
                prior_transition = resources.read_personal_trial()
                hold_raw = resources.private_bytes(resources.hold_path(), 8192)
                hold = resources.read_installation_hold()
                if (hashlib.sha256(hold_raw).hexdigest() != hold_sha256 or not hold['active']
                        or hold['original_identity_sha256'] != original):
                    raise ValueError('the supplied original hold does not match')
                state_path = ARTIFACT_ROOT / 'state.json'
                prior_raw = resources.private_bytes(state_path, 32768)
                prior = json.loads(prior_raw)
                if not isinstance(prior, dict) or prior.get('backup_identity_sha256') != original:
                    raise ValueError('the prior request is not bound to the original identity')
                report = cli_report()
                require_paused_native_report(report)
                now = time.time()
                resources.require_trial_license(report['backup']['license'], status, now)
                resources.require_trial_license(resources.local_trial_license(), status, now)
                if observed_identity(report) != replacement or not hold['prepared_epoch'] <= now < expires <= now + 15 * DAY:
                    raise ValueError('the exact new trial identity and expiry are not verified')
                resources.require_manual_and_drained()
                config = ET.parse(BZDATA / 'bzinfo.xml').getroot().find('do_backup')
                if config is None or config.get('num_backup_threads') != '1' or config.get('net_auto_throttle') != 'false':
                    raise ValueError('one upload thread is not verified')
                s = snapshot(report, now, initial_trial=True)
                s['personal_trial_expires_epoch'] = expires  # Exact requested trial validated above.
                if prior_transition is None:
                    resources.require_transition_watchdog(now)
                    # This bypass is confined to the metadata-only transition:
                    # normal starts still require a later healthy watchdog run.
                    s['watchdog_status'] = 'healthy'
                if s['schedule'] != 'only_when_click_backup_now' or active(s) or blockers(s, launch=True):
                    raise ValueError('the protected transition prerequisites are not satisfied')
                if prior_transition is not None:
                    if (prior_transition['original_identity_sha256'] != original
                            or prior_transition['new_identity_sha256'] != replacement
                            or prior_transition['license_status'] != status
                            or prior_transition['original_hold_sha256'] != hold_sha256
                            or prior_transition['prior_request_sha256'] != hashlib.sha256(prior_raw).hexdigest()):
                        raise ValueError('an existing transition cannot be replaced')
                else:
                    new_state_path = ARTIFACT_ROOT / ('state-personal-trial-' + replacement + '.json')
                    if os.path.lexists(new_state_path):
                        raise ValueError('a new-identity request already exists before transition')
                    record = {'schema': resources.TRIAL_SCHEMA, 'original_identity_sha256': original,
                              'new_identity_sha256': replacement, 'original_hold_sha256': hold_sha256,
                              'license_type': resources.TRIAL_TYPE, 'license_status': status,
                              'expires_epoch': expires, 'transitioned_epoch': now,
                              'prior_request_json': prior_raw.decode('utf-8'),
                              'prior_request_sha256': hashlib.sha256(prior_raw).hexdigest()}
                    # The shared locks exclude maintenance writers. Recheck the
                    # bound local inputs immediately before the sole record write.
                    require_current_identity(replacement)
                    resources.require_trial_license(resources.local_trial_license(), status, time.time())
                    resources.require_manual_and_drained()
                    resources.require_upload_thread_bound()
                    resources.require_transition_watchdog(time.time())
                    fresh = memory_status()
                    fresh['internal_free_gib'] = shutil.disk_usage('/').free / GIB
                    if resources.resource_blockers(fresh):
                        raise ValueError('host resources changed before the transition')
                    if (resources.private_bytes(resources.hold_path(), 8192) != hold_raw
                            or resources.private_bytes(state_path, 32768) != prior_raw
                            or os.path.lexists(new_state_path)):
                        raise ValueError('preserved transition inputs changed')
                    require_current_identity(replacement)
                    resources.require_trial_license(resources.local_trial_license(), status, time.time())
                    write_json(resources.trial_path(), record, exclusive=True)
        return {'status': 'personal_trial_transitioned',
                'message': 'The verified new Backblaze trial is admitted until its recorded expiry. The old request and hold remain preserved. No backup action was performed.'}, 0
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, OverflowError, ET.ParseError, subprocess.SubprocessError):
        return {'status': 'personal_trial_transition_failed',
                'message': 'The new Backblaze trial could not be admitted with the supplied identities, expiry and preserved hold. No backup action was performed.'}, 1


def bootstrap_admission_receipt(path: Path, expected_sha256: str, replacement: str, *, freshness_epoch: float | None = None) -> dict:
    raw = resources.private_bytes(path, 8192)
    receipt = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != expected_sha256
            or set(receipt) != {'schema', 'observed_epoch', 'new_identity_sha256', 'full_disk_access', 'enrollment_guard'}
            or receipt['schema'] != 'openclaw.backblaze_bootstrap_admission.v1'
            or receipt['new_identity_sha256'] != replacement
            or type(receipt['observed_epoch']) not in (int, float)
            or not 0 <= (time.time() if freshness_epoch is None else freshness_epoch) - receipt['observed_epoch'] <= 120
            or receipt['full_disk_access'] != {'com.backblaze.Backblaze': True, 'com.backblaze.bzbmenu': True}
            or set(receipt['enrollment_guard']) != {'pid', 'start_command_sha256'}
            or not valid_fingerprint(receipt['enrollment_guard']['start_command_sha256'])):
        raise resources.BootstrapError('fresh bound FDA and enrollment-guard handoff receipt required')
    if resources.process_identity(receipt['enrollment_guard']['pid']) == receipt['enrollment_guard']:
        raise resources.BootstrapError('the enrollment pause guard has not drained')
    return receipt


def bootstrap_preflight(original: str, replacement: str, status: str, hold_sha256: str,
                        admission_path: Path, admission_sha256: str) -> dict:
    if (not all(valid_fingerprint(v) for v in (original, replacement, hold_sha256, admission_sha256))
            or original == replacement):
        raise resources.BootstrapError('explicit distinct identities and hashes required')
    if os.path.lexists(resources.trial_path()) or os.path.lexists(resources.bootstrap_stop_path()):
        raise resources.BootstrapError('existing trial or cancellation cannot be replaced')
    hold_raw = resources.private_bytes(resources.hold_path(), 8192)
    hold = resources.read_installation_hold()
    prior_path = ARTIFACT_ROOT / 'state.json'
    prior = resources.private_bytes(prior_path, 32768)
    if (not hold or not hold['active'] or hold['original_identity_sha256'] != original
            or hashlib.sha256(hold_raw).hexdigest() != hold_sha256
            or json.loads(prior).get('backup_identity_sha256') != original):
        raise resources.BootstrapError('the original hold and request are not verified')
    config = resources.bootstrap_config()
    # No elapsed time, vanished PID or old filelist timestamp can satisfy this.
    scans = resources.require_first_scan_completion(config, replacement)
    receipt_checked_epoch = time.time()
    receipt = bootstrap_admission_receipt(admission_path, admission_sha256, replacement, freshness_epoch=receipt_checked_epoch)
    native_cli = resources.require_signed_cli()
    catalog = resources.bootstrap_catalog()
    if catalog['catalog_state'] != 'not_created':
        raise resources.BootstrapError('first catalog is already present; use strict transition review')
    # This potentially 180-second read is before the active lease and outside
    # admin_lock. Optional initial completion metadata remains unknown.
    report = cli_report()
    require_paused_native_report(report)
    now = time.time()
    resources.require_trial_license(report['backup']['license'], status, now)
    resources.require_trial_license(resources.local_trial_license(), status, now)
    if observed_identity(report) != replacement or report['backup']['status'].get('installed') is not True:
        raise resources.BootstrapError('installed new native identity is not verified')
    settings = report['settings']
    if (settings.get('net_auto_throttle') is not False
            or type(settings.get('num_backup_threads')) is not int or settings['num_backup_threads'] != 1
            or settings.get('backup_schedule_type') != resources.MANUAL):
        raise resources.BootstrapError('native report does not confirm manual one-thread configuration')
    host = {**resources.host_memory_status(), **catalog}
    s = snapshot(report, now, initial_trial=True, bootstrap_host=host)
    resources.require_transition_watchdog(now)
    s.update(watchdog_status='healthy', personal_trial_expires_epoch=resources.trial_expiry(status))
    if active(s) or s['schedule'] != resources.MANUAL or blockers(s, launch=True) or resources.native_start_commands():
        raise resources.BootstrapError('bootstrap native and resource prerequisites are not satisfied')
    # Bind receipt content, completed scans and mandatory configuration facts;
    # never persist the credential-bearing native report.
    admission = {'receipt': receipt, 'first_scans': scans, 'configuration': config,
                 'identity': replacement, 'owc_uuid': resources.OWC_UUID}
    binding = {'original_identity_sha256': original, 'new_identity_sha256': replacement,
               'original_hold_sha256': hold_sha256, 'prior_request_sha256': hashlib.sha256(prior).hexdigest(),
               'prior_request_path': str(prior_path), 'license_status': status,
               'expires_epoch': resources.trial_expiry(status), 'created_epoch': now,
               'source_sha256': resources.SOURCE_IDENTITIES, 'owner_source_path': str(Path(__file__).resolve()),
               'owner_source_sha256': HEALTH_SOURCE_SHA256,
               'admission_sha256': resources.digest_json(admission), 'configuration': config, 'native_cli': native_cli,
               'owner': resources.process_identity(os.getpid()), 'argv': [str(resources.BZCLI), 'action', '--backup-now'],
               'attempt_consumed': True}
    record = {'schema': resources.BOOTSTRAP_SCHEMA, 'binding': binding,
              'binding_sha256': resources.digest_json(binding), 'phase': 'dispatching', 'heartbeat_epoch': time.time()}
    resources.require_bootstrap_binding(record)
    # The grant/handoff receipt must be fresh when preflight begins. Recheck
    # its exact bytes and current guard drain after the valid 180-second report;
    # elapsed report time is not a new lease and does not age out this admission.
    if bootstrap_admission_receipt(admission_path, admission_sha256, replacement, freshness_epoch=receipt_checked_epoch) != receipt:
        raise resources.BootstrapError('the admitted grant or handoff receipt changed')
    resources.require_first_scan_completion(resources.bootstrap_config(), replacement)
    observation = resources.observe_bootstrap(record)
    if observation['catalog_state'] != 'not_created' or observation['transmitter_process_running']:
        raise resources.BootstrapError('native bootstrap state changed during preflight')
    resources.require_transition_watchdog(time.time())
    return record


def bootstrap_heartbeat(record: dict) -> dict:
    return resources.update_bootstrap(record, heartbeat_epoch=time.time())


def protective_bootstrap_heartbeat(record: dict) -> None:
    try:
        bootstrap_heartbeat(record)
    except (OSError, RuntimeError, ValueError):
        pass  # Contention cannot interrupt an already requested protective pause.


def supervise_bootstrap(record: dict, child) -> dict:
    """One owned start child; slow local probes run separately from heartbeat."""
    started, heartbeat = time.monotonic(), -10.0
    probe = None
    capture = None
    probe_started = -10.0
    try:
        while True:
            now = time.monotonic()
            current = resources.read_bootstrap()
            if current is None or current['binding_sha256'] != record['binding_sha256']:
                raise resources.BootstrapError('bootstrap binding changed')
            record = current
            if resources.bootstrap_cancelled(record) or record['phase'] not in ('dispatching', 'observing'):
                raise resources.BootstrapError('bootstrap cancellation is permanent')
            if now - heartbeat >= 5:
                record = bootstrap_heartbeat(record)
                heartbeat = now
            resources.require_trial_license(resources.local_trial_license(), record['binding']['license_status'], time.time())
            if resources.installed_identity() != record['binding']['new_identity_sha256']:
                raise resources.BootstrapError('native bootstrap identity changed')
            catalog = resources.bootstrap_catalog(strict=record.get('first_catalog_seen') is not None)
            if catalog['catalog_state'] == 'established':
                record = resources.update_bootstrap(record, first_catalog_seen=catalog['catalog_observation'])
                resources.cancel_bootstrap(record, 'first_catalog_seen')
                break
            # Cheap file-backed identity/license/config checks repeat even while
            # either native action or expensive resource probe is outstanding.
            resources.require_trial_license(resources.local_trial_license(), record['binding']['license_status'], time.time())
            if (resources.installed_identity() != record['binding']['new_identity_sha256']
                    or resources.bootstrap_config() != record['binding']['configuration']):
                raise resources.BootstrapError('native bootstrap configuration changed')
            if child is not None:
                result = child.poll()
                if result is None and now - started >= 60:
                    raise subprocess.TimeoutExpired(record['binding']['argv'], 60)
                if result is not None:
                    # A late successful response cannot override the stop latch.
                    record = resources.update_bootstrap(record, native_exit=result, phase='observing')
                    child = None
                    if result != 0:
                        raise resources.BootstrapError('native bootstrap action failed')
            if probe is None and now - probe_started >= 5:
                capture = tempfile.TemporaryFile()
                probe = subprocess.Popen([sys.executable, '-B', str(Path(resources.__file__).resolve()), '--bootstrap-probe'],
                                         stdout=capture, stderr=subprocess.DEVNULL)
                probe_started = now
            if probe is not None and probe.poll() is not None:
                capture.seek(0)
                raw = capture.read(16385)
                if probe.returncode or len(raw) > 16384:
                    raise resources.BootstrapError('bootstrap resource observation failed')
                observation = json.loads(raw)
                capture.close()
                capture, probe = None, None
                if observation['catalog_state'] == 'established':
                    record = resources.update_bootstrap(record, first_catalog_seen=observation['catalog_observation'])
                    resources.cancel_bootstrap(record, 'first_catalog_seen')
                    break
                record = resources.update_bootstrap(record, last_observation=observation)
            elif probe is not None and now - probe_started >= 60:
                raise resources.BootstrapError('bootstrap resource observation timed out')
            # No total deadline. Unknown vendor progress and an absent initial
            # catalog do not establish a stall or a completion claim.
            time.sleep(1)
    finally:
        for owned in (child, probe):
            if owned is not None and owned.poll() is None:
                owned.terminate()
                try:
                    owned.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    owned.kill()
                    owned.wait(timeout=2)
        if capture is not None:
            capture.close()
    return record


def finish_bootstrap(record: dict) -> dict:
    """Post-pause 180-second report is outside the active lease and admin lock."""
    current = resources.read_bootstrap()
    stop = json.loads(resources.private_bytes(resources.bootstrap_stop_path(), 8192))
    if (current is None or current['binding_sha256'] != record['binding_sha256']
            or not current.get('first_catalog_seen') or not current.get('pause_accepted_epoch')
            or current.get('manual_verified') is not True or current.get('drained') is not True
            or stop.get('schema') != resources.BOOTSTRAP_STOP_SCHEMA
            or stop.get('binding_sha256') != current['binding_sha256']):
        raise resources.BootstrapError('durable catalog, cancellation and drain evidence required')
    resources.require_bootstrap_binding(current, owner=False)
    resources.require_manual_and_drained()
    if resources.native_start_commands():
        raise resources.BootstrapError('possible native start commands have not drained')
    report = cli_report()
    require_paused_native_report(report)
    now = time.time()
    resources.require_trial_license(report['backup']['license'], current['binding']['license_status'], now)
    if observed_identity(report) != current['binding']['new_identity_sha256']:
        raise resources.BootstrapError('post-pause native identity changed')
    settings = report['settings']
    if (settings.get('net_auto_throttle') is not False or settings.get('num_backup_threads') != 1
            or settings.get('backup_schedule_type') != resources.MANUAL):
        raise resources.BootstrapError('post-pause native configuration changed')
    s = snapshot(report, now, initial_trial=True)
    s.update(watchdog_status='healthy', personal_trial_expires_epoch=current['binding']['expires_epoch'])
    if active(s) or blockers(s, launch=True):
        raise resources.BootstrapError('post-pause mandatory resources or settings failed')
    resources.require_bootstrap_binding(current, owner=False)
    if resources.bootstrap_config() != current['binding']['configuration'] or resources.native_start_commands():
        raise resources.BootstrapError('post-pause native state changed')
    resources.require_manual_and_drained()
    return resources.update_bootstrap(current, phase='catalog_established')


def bootstrap_personal_catalog(original: str, replacement: str, status: str, hold_sha256: str,
                               admission_path: str, admission_sha256: str) -> tuple[dict, int]:
    record = None
    child = None
    try:
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        with (ARTIFACT_ROOT / 'guard.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'bootstrap_busy', 'message': 'A Backblaze maintenance owner is active. No second attempt was dispatched.'}, 1
            record = resources.read_bootstrap()
            if (record is not None and record['phase'] == 'catalog_established'
                    and resources.read_personal_trial() is not None):
                resources.catalog_size_gib()
                return {'status': 'bootstrap_already_handed_off',
                        'message': 'The recorded catalog bootstrap has already handed off to the separately admitted trial.'}, 0
            if record is None:
                record = bootstrap_preflight(original, replacement, status, hold_sha256,
                                             Path(admission_path), admission_sha256)
                # All slow probes are complete. This short nonblocking lock only
                # consumes the durable attempt and records immediate launch.
                with resources.admin_lock():
                    if os.path.lexists(resources.bootstrap_stop_path()):
                        raise resources.BootstrapError('bootstrap was cancelled before dispatch')
                    resources.require_bootstrap_binding(record, owner=False)
                    resources.require_trial_license(resources.local_trial_license(), status, time.time())
                    require_current_identity(replacement)
                    if resources.native_cli_identity() != record['binding']['native_cli']:
                        raise resources.BootstrapError('native CLI changed before dispatch')
                    if resources.bootstrap_config() != record['binding']['configuration']:
                        raise resources.BootstrapError('configuration changed before dispatch')
                    record['heartbeat_epoch'] = time.time()
                    write_json(resources.bootstrap_path(), record, exclusive=True)
                    if resources.bootstrap_cancelled(record):
                        raise resources.BootstrapError('bootstrap cancelled before dispatch')
                    child = subprocess.Popen(record['binding']['argv'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    record['native_child'] = {'pid': child.pid, 'owned_by': os.getpid()}
                    write_json(resources.bootstrap_path(), record)
                if child.poll() is None:
                    identity = resources.process_identity(child.pid)
                    record = resources.update_bootstrap(record, native_child={'pid': child.pid, 'owned_by': os.getpid(),
                                                                              'process_identity': identity})
                record = supervise_bootstrap(record, child)
                child = None
            # Replay only reconciles/pauses. There is no route to a second start.
            result = resources.pause_bootstrap(record, 'bootstrap_reconcile', lambda: protective_bootstrap_heartbeat(record))
            if result['status'] == 'bootstrap_paused_catalog':
                record = finish_bootstrap(record)
                result['status'] = 'bootstrap_catalog_established'
            return {**result, 'message': 'The single catalog attempt was reconciled and protective pause requested. Completion and restore remain unverified.'}, 0 if result['status'] == 'bootstrap_catalog_established' else 1
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError) as exc:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        # A preflight record is only a proposal until exclusive persistence.
        # Once consumed, even a crash before Popen requires pause and no retry.
        if os.path.lexists(resources.bootstrap_path()):
            try:
                persisted = resources.read_bootstrap()
                result = resources.pause_bootstrap(persisted or {'binding_sha256': 'invalid'}, 'bootstrap_failed')
            except (OSError, RuntimeError, ValueError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError):
                result = {'status': 'bootstrap_protection_failed', 'pause_accepted': False}
        else:
            result = {'status': 'bootstrap_admission_failed'}
        return {**result, 'error_type': type(exc).__name__,
                'gate': str(exc) if isinstance(exc, resources.BootstrapError) else 'bootstrap_prerequisite_unverified',
                'message': 'The first-catalog bootstrap did not pass its required gates. No retry is permitted after an attempt is consumed.'}, 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--start', action='store_true', help='Request a weekly backup; idempotent while one is pending.')
    mode.add_argument('--check', action='store_true', help='Check completion and resume an interrupted pending run after 12 hours.')
    mode.add_argument('--adopt-legacy-identity', metavar='SHA256',
                      help='Bind legacy metadata to a previously recorded matching paid-backup fingerprint; provider reads only.')
    mode.add_argument('--prepare-installation-hold', metavar='SHA256',
                      help='Prepare a persistent pause-only hold for the matching paid installation; metadata only.')
    mode.add_argument('--release-installation-hold', metavar='SHA256',
                      help='Release the hold only for its matching paid, manual, idle original installation; metadata only.')
    mode.add_argument('--transition-personal-trial', nargs=4,
                      metavar=('OLD_SHA256', 'NEW_SHA256', 'EXPIRES_STATUS', 'HOLD_SHA256'),
                      help='Admit one verified new personal trial, preserving the original hold and request; metadata only.')
    mode.add_argument('--bootstrap-personal-catalog', nargs=6,
                      metavar=('OLD_SHA256', 'NEW_SHA256', 'EXPIRES_STATUS', 'HOLD_SHA256', 'ADMISSION_PATH', 'ADMISSION_SHA256'),
                      help='One explicit first-catalog attempt; requires independently bound completed first scans and protected handoff.')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if args.bootstrap_personal_catalog is not None:
        payload, code = bootstrap_personal_catalog(*args.bootstrap_personal_catalog)
    elif args.adopt_legacy_identity is not None:
        payload, code = adopt_legacy_identity(args.adopt_legacy_identity)
    elif args.prepare_installation_hold is not None:
        payload, code = installation_hold(args.prepare_installation_hold)
    elif args.release_installation_hold is not None:
        payload, code = installation_hold(args.release_installation_hold, release=True)
    elif args.transition_personal_trial is not None:
        payload, code = transition_personal_trial(*args.transition_personal_trial)
    else:
        payload, code = run(start=args.start)
    print(json.dumps(payload, sort_keys=True) if args.json else payload['message'])
    return code


if __name__ == '__main__':
    raise SystemExit(main())
