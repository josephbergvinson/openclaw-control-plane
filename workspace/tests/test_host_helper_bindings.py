from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def test_host_shell_helpers_parse_and_missing_bindings_stop_before_native_actions() -> None:
    for name in ('runtime-environment.zsh', 'claude-stale-worker-guard.zsh'):
        source = ROOT / 'scripts' / name
        syntax = subprocess.run(['/bin/zsh', '-n', str(source)], capture_output=True, text=True)
        assert syntax.returncode == 0, syntax.stderr
        # Missing required bindings stop before launchctl, credential access, ps or signals.
        result = subprocess.run(['/bin/zsh', str(source)], env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}, capture_output=True, text=True)
        assert result.returncode != 0
        assert 'required' in result.stderr
        assert result.stdout == ''


def test_stale_worker_version_and_receipt_validation_without_process_actions(tmp_path: Path) -> None:
    source = (ROOT / 'scripts/claude-stale-worker-guard.zsh').read_text()
    assert source.endswith('main "$@"\n')
    library = tmp_path / 'guard-functions.zsh'
    library.write_text(source.removesuffix('main "$@"\n') + '\nvalid_release_version 1.2.3 || exit 90\nvalid_release_version 01.2.3 && exit 91\nvalid_release_version 1.2.3-extra && exit 92\nvalid_receipt_file "$receipt_root/missing.receipt" && exit 93\nexit 0\n')
    receipts = tmp_path / 'receipts'
    receipts.mkdir()
    result = subprocess.run(['/bin/zsh', str(library)], env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'OPENCLAW_CLAUDE_CODE_ROOT': str(tmp_path / 'versions'), 'OPENCLAW_CLAUDE_WORKER_RECEIPTS': str(receipts)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(receipts.iterdir()) == []


def test_all_schedule_templates_are_disabled_and_use_command_vectors() -> None:
    jobs = json.loads((ROOT / 'scheduler/maintenance-jobs.template.json').read_text())
    assert len(jobs) == 9
    for job in jobs:
        assert job['enabled'] is False
        assert job['wakeMode'] == 'now'
        assert 'id' not in job and 'state' not in job
        payload = job['payload']
        assert payload['kind'] == 'command'
        assert isinstance(payload['argv'], list)
        assert payload['argv'][0] == '${operator:paths.python_binary}'
        assert payload['argv'][1] == '${operator:paths.cron_entrypoint}'
        assert payload['env']['OPENCLAW_OPERATOR_CONFIG'] == '${operator:paths.operator_config}'
        assert payload['env']['OPENCLAW_STATE_DIR'] == '${operator:paths.state_root}'
        assert type(payload['timeoutSeconds']) is int and payload['timeoutSeconds'] > 0
        assert job['delivery']['to'] == '${operator:scheduler.delivery_target}'


def test_all_host_definitions_are_disabled_and_retired_definitions_have_no_trigger() -> None:
    for template in (ROOT / 'launchd').glob('*.plist.template.json'):
        value = json.loads(template.read_text())
        assert value['Disabled'] is True
        assert value.get('RunAtLoad') is not True
        if template.name.startswith(('external-volume-', 'thread-context-')):
            assert not {'RunAtLoad', 'KeepAlive', 'StartInterval', 'StartCalendarInterval', 'WatchPaths', 'QueueDirectories', 'StartOnMount', 'Sockets', 'MachServices'}.intersection(value)
