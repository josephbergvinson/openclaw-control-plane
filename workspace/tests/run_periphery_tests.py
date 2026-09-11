#!/usr/bin/env python3
"""Run exported helper fixtures with disposable operator bindings.

This creates no installed services or backups. Every default helper path is
inside a temporary directory, and provider/service executables are denied.
The tests use pytest plus Python's standard library; no dependency install runs.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


WORKSPACE = Path(__file__).resolve().parents[1]
TESTS = [
    'test_stage_runtime_release.py',
    'test_host_helper_bindings.py', 'test_openclaw_native_weekly_backup.py', 'test_operator_contract.py', 'test_backblaze_health.py',
    'test_backblaze_installation_hold.py', 'test_backblaze_personal_trial.py',
    'test_backblaze_internal_package.py', 'test_backblaze_watchdog_scheduler.py',
    'test_backblaze_portable_publication.py', 'test_trello_inspect.py', 'test_cron_python_entrypoint.py',
    'test_gateway_autoheal_cron.py', 'test_maintenance_retention_reporting.py',
    'test_node_compile_cache.py', 'test_openclaw_archive_v3_retention.py',
    'test_openclaw_backup_reporting.py', 'test_openclaw_backup_retention_cleanup.py',
    'test_openclaw_cli_common.py', 'test_openclaw_daily_backup_cron.py',
    'test_openclaw_health_audit_cron.py', 'test_openclaw_independent_backup_receipt.py',
    'test_openclaw_launchagent_integrity_guard.py', 'test_openclaw_retention_cleanup_cron.py',
    'test_openclaw_storage_prune.py', 'test_openclaw_weekly_archive_backup.py',
    'test_openclaw_weekly_backup_integration_proof.py',
    'test_openclaw_weekly_backup_reconcile.py', 'test_external_volume_guard.py',
    'test_external_volume_guard_launcher.py', 'test_root_drift_control_plane.py',
    'test_storage_headroom_guard.py', 'test_operation_effect_predicate.py',
    'test_workspace_integrity_guard.py',
    'test_openclaw_runtime_activate.py', 'test_openclaw_runtime_release_retention.py',
    'test_openclaw_runtime_promotion_retention.py', 'test_openclaw_approval_a_retention.py',
    'test_runtime_activation_portability.py',
]


def fixture_contract(root: Path) -> dict:
    home, data = root / 'home', root / 'data'
    agent = data / 'Agent'
    workspace = agent / 'Workspace'
    for path in (home, workspace, root / 'bin', agent / '.state' / 'OpenClaw',
                 data / 'ProjectInfrastructure' / 'GitRemotes'):
        path.mkdir(parents=True)
    for name in ('scripts', 'runbooks'):
        (workspace / name).symlink_to(WORKSPACE / name, target_is_directory=True)
    (workspace / 'launchd').mkdir()
    cli = root / 'bin' / 'openclaw'
    cli.write_text('#!/bin/sh\nprintf "fixture CLI refuses live operations\\n" >&2\nexit 66\n')
    cli.chmod(0o700)
    node = shutil.which('node')
    if not node:
        raise RuntimeError('Node is required only to exercise launcher fixture children')
    paths = {
        'host_home': str(home), 'workspace': str(workspace), 'data_root': str(data),
        'agent_storage_root': str(agent), 'state_root': str(agent / '.state' / 'OpenClaw'),
        'archive_root': str(agent / 'Backups' / 'weekly'),
        'git_history_root': str(data / 'ProjectInfrastructure' / 'GitRemotes'),
        'internal_control_root': str(home / 'InternalControl'),
        'node_compile_cache': str(data / 'ProjectData' / '.node-compile-cache'),
        'openclaw_cli': str(cli), 'node_binary': node, 'python_binary': sys.executable,
        'runtime_package_link': str(home / '.openclaw-cli' / 'lib' / 'node_modules' / 'openclaw'),
        'temp_root': str(root / 'scratch'), 'runtime_source': str(root / 'runtime-source'),
        'xcode_derived_data': str(data / 'Developer/Xcode/BuildData/DerivedData'),
        'pytest_temp_root': str(data / 'ProjectData/.test-tmp'),
    }
    for name in ('runtime_releases_root', 'runtime_promotions_root', 'runtime_current_link',
                 'cli_root', 'launch_agents', 'runtime_retention_lock', 'activation_lock',
                 'activation_result', 'runtime_release_retention_artifacts',
                 'runtime_promotion_retention_artifacts', 'legacy_exec_approvals',
                 'session_reservations', 'session_store', 'approval_a_execution_root'):
        paths[name] = str(root / 'activation' / name)
    for name in ('internal_operator_config', 'backblaze_watchdog_helper', 'backblaze_watchdog_stdout',
                 'backblaze_watchdog_stderr', 'volume_guard_contract', 'volume_guard_helper',
                 'volume_guard_launcher', 'volume_guard_state', 'volume_guard_incidents',
                 'volume_guard_lock', 'volume_guard_stdout', 'volume_guard_stderr', 'volume_liveness_probe'):
        paths[name] = str(home / 'InternalControl' / name)
    return {
        'schema_version': 1, 'paths': paths, 'identifiers': {'host_user': 'fixture-user', 'host_name': 'fixture-host'},
        'runtime': {'gateway_label': 'ai.openclaw.gateway', 'gateway_port': 18789,
                    'readyz_url': 'http://127.0.0.1:18789/readyz',
                    'node_label': 'ai.openclaw.node', 'required_extensions': ['discord', 'lane-contract'],
                    'launchagent_label_markers': ['openclaw'], 'expected_auth_order_count': 3,
                    'expected_auth_order_sha256': 'f' * 64, 'session_reservations_mode': 'absent'},
        'backup': {'client_executable': str(root / 'bin' / 'bzcli'),
                   'client_data_root': str(root / 'vendor-data'),
                   'frozen_clone_v2_pins': [['openclaw-backup-20200101T000000Z', 'a' * 64],
                                             ['openclaw-backup-20200102T000000Z', 'b' * 64]]},
        'services': {'backblaze': {'watchdog_label': 'com.openclaw.backblaze-resource-watchdog'}},
        'volumes': {'expected_uuid': '11111111-2222-4333-8444-555555555555', 'liveness_sha256': 'e' * 64},
        'maintenance': {
            'expected_managed_labels': ['ai.openclaw.gateway', 'ai.openclaw.node',
                'com.openclaw.backblaze-resource-watchdog'],
            'intentionally_untriggered_labels': ['com.openclaw.external-volume-guard',
                'com.openclaw.thread-context-rollover-monitor'],
            'accepted_security_findings': {},
        },
        'scheduler': {'local_backup_job_id': 'fixture-local-backup',
            'maintenance_job_ids': {'fixture-retention': 'daily retention',
                'fixture-workspace': 'workspace cleanup', 'fixture-headroom': 'storage headroom',
                'fixture-integrity': 'automation integrity', 'fixture-autoheal': 'gateway recovery'},
            'repoint_targets': ['personal_data_healthkit_api_guard.py',
                                'personal_data_location_weather_db_guard.py'],
            'ambiguous_entrypoints': ['personal_data_nightly_consolidated_checks.py']},
    }


def main() -> int:
    import pytest

    with tempfile.TemporaryDirectory(prefix='oc-ref-', dir='/private/tmp' if sys.platform == 'darwin' else None) as temporary:
        root = Path(temporary).resolve()
        # All TemporaryDirectory and pytest tmp_path fixtures stay within this
        # disposable root, including native CLI stubs generated by archive tests.
        previous_tempdir = tempfile.tempdir
        tempfile.tempdir = str(root)
        config = fixture_contract(root)
        sys.path.insert(0, str(WORKSPACE / 'tests'))
        from health_audit_fixture_policy import ACCEPTED_FINDINGS
        config['maintenance']['accepted_security_findings'] = ACCEPTED_FINDINGS
        # Every added host template receives a disposable path or synthetic label.
        for source in (WORKSPACE / 'launchd').glob('*.plist.template.json'):
            for key in re.findall(r'\$\{operator:([^}]+)\}', source.read_text()):
                value = config
                parts = key.split('.')
                for part in parts[:-1]:
                    value = value.setdefault(part, {})
                value.setdefault(parts[-1], str(root / 'home' / 'InternalControl' / parts[-1]) if parts[0] == 'paths' else 'fixture-' + parts[-1])
        config_path = root / 'operator.json'
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        # A prior interactive environment cannot redirect a fixture to live state.
        for key in list(os.environ):
            if key.startswith('OPENCLAW_') or key == 'WORKSPACE':
                os.environ.pop(key)
        os.environ.update({
            'OPENCLAW_OPERATOR_CONFIG': str(config_path),
            'OPENCLAW_STATE_DIR': config['paths']['state_root'],
            'OPENCLAW_BIN': config['paths']['openclaw_cli'],
            'OPENCLAW_INTERNAL_PYTHON': sys.executable,
            'HOME': config['paths']['host_home'], 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONPATH': str(WORKSPACE),
        })
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(WORKSPACE))
        sys.path.insert(0, str(WORKSPACE / 'tests'))
        sys.path.insert(0, str(WORKSPACE / 'scripts'))
        from scripts.operator_contract import OperatorContract
        fixture = OperatorContract(config)
        for source in (WORKSPACE / 'launchd').glob('*.plist.template.json'):
            target = Path(config['paths']['workspace']) / 'launchd' / ('com.openclaw.' + source.name.replace('.template.json', ''))
            target.write_bytes(plistlib.dumps(fixture.render(json.loads(source.read_text()))))
        real_popen = subprocess.Popen

        def fixture_popen(args, *positional, **kwargs):
            if kwargs.get('shell'):
                raise AssertionError('fixture suite must not launch shell command strings')
            command = args[0] if isinstance(args, (list, tuple)) and args else args
            name = Path(str(command)).name
            if name in {'launchctl', 'bzcli', 'bztransmit', 'docker', 'osascript', 'openclaw'}:
                candidate = Path(str(command)).resolve()
                if not candidate.is_relative_to(root):
                    raise AssertionError(f'fixture attempted live service executable: {name}')
            return real_popen(args, *positional, **kwargs)

        subprocess.Popen = fixture_popen
        try:
            targets = sys.argv[1:] or [str(WORKSPACE / 'tests' / name) for name in TESTS]
            return pytest.main(['-q', '-p', 'no:cacheprovider', '--basetemp', str(root / 'pytest'), *targets])
        finally:
            subprocess.Popen = real_popen
            tempfile.tempdir = previous_tempdir


if __name__ == '__main__':
    raise SystemExit(main())
