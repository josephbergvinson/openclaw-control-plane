#!/usr/bin/env python3
"""Internal 120-second resource supervisor; no heavy bzCLI report and no messaging."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET

try:
    from . import backblaze_resources as resources
except ImportError:
    import backblaze_resources as resources


def observe() -> dict:
    overview = ET.parse(resources.BZDATA / 'overviewstatus.xml').getroot().find('bztransmit')
    config = ET.parse(resources.BZDATA / 'bzinfo.xml').getroot().find('do_backup')
    if overview is None or config is None:
        raise ValueError('Backblaze status is missing')
    if overview.get('cur_state') not in ('not_running', 'transmitting'):
        raise ValueError('Backblaze transmitter state is unknown')
    schedule = config.get('backup_schedule_type')
    if schedule not in ('continuously', 'when_files_change', 'once_per_day', 'only_when_click_backup_now'):
        raise ValueError('Backblaze schedule is missing or unknown')
    resources.installed_identity()
    resources.catalog_size_gib()  # Missing/empty metadata is never healthy idle.
    processes = resources.process_status()
    running = processes['transmitter_process_running']
    scanning = schedule != 'only_when_click_backup_now'
    if not running and not scanning:
        return {'status': 'idle', 'observed_epoch': time.time()}
    snapshot = resources.memory_status(processes)
    snapshot['internal_free_gib'] = round(shutil.disk_usage('/').free / resources.GIB, 2)
    dangerous = (snapshot['internal_free_gib'] < 10
                 or snapshot['memory_free_percent'] < 15 or snapshot['memory_pressure_level'] != 1)
    return {'status': 'pause_requested' if dangerous else 'observing',
            'observed_epoch': time.time(), 'snapshot': snapshot}


def run() -> dict:
    bootstrap = resources.protect_bootstrap()
    if bootstrap is not None:
        return bootstrap
    try:
        held = resources.protect_installation_hold()
    except FileNotFoundError as error:
        if error.filename != str(resources.BZCLI):
            raise
        # A local uninstall removes the control executable. Keep this a blocked
        # state without claiming that a pause was accepted or the backup is idle.
        status = 'installation_hold_client_unavailable'
        try:
            resources.read_installation_hold()
        except resources.InstallationHoldError:
            status = 'installation_hold_invalid'
        return {'status': status,
                'observed_epoch': time.time(), 'pause_accepted': False,
                'installation_state_verified_idle': False}
    if held is not None:
        return held
    try:
        result = observe()
    except (OSError, ValueError, RuntimeError, ET.ParseError, subprocess.SubprocessError) as error:
        resources.request_pause('backup_state_unavailable')
        # This remains a failed supervisor state even when pause was accepted.
        return {'status': 'state_unavailable_pause_requested', 'observed_epoch': time.time(),
                'error_type': type(error).__name__}
    if result['status'] == 'pause_requested':
        resources.request_pause('memory_or_disk_pressure')
    return result


def main() -> int:
    try:
        result = run()
        result['source_sha256'] = resources.SOURCE_IDENTITIES
        resources.atomic_json(resources.CONTROL_DIR / 'backblaze-watchdog-latest.json', result)
        return 1 if result['status'] in ('state_unavailable_pause_requested',
                                        'installation_hold_invalid', 'installation_hold_pause_requested',
                                        'installation_hold_client_unavailable', 'personal_trial_invalid_pause_requested',
                                        'bootstrap_pause_pending') else 0
    except Exception as exc:
        # No provider stderr or account data in launchd logs.
        try:
            resources.atomic_json(resources.CONTROL_DIR / 'backblaze-watchdog-latest.json',
                                  {'status': 'error', 'observed_epoch': time.time(),
                                   'source_sha256': resources.SOURCE_IDENTITIES,
                                   'error_type': type(exc).__name__})
        except OSError:
            pass  # An old/missing receipt also blocks the next daily start.
        print('Backblaze resource watchdog failed: ' + type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
