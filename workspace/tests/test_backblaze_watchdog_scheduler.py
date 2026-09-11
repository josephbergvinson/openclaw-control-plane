"""The staged service is inert and its internal package is dependency-complete."""
from pathlib import Path
import hashlib
import json

from scripts.operator_contract import load_operator_contract

ROOT = Path(__file__).resolve().parents[1]


def test_watchdog_definition_uses_explicit_internal_bindings_and_stays_disabled():
    operator = load_operator_contract()
    template = ROOT / 'launchd/backblaze-resource-watchdog.plist.template.json'
    data = operator.render(json.loads(template.read_text()))
    assert data['ProgramArguments'] == [str(operator.require_path('paths.python_binary')),
                                        str(operator.require_path('paths.backblaze_watchdog_helper'))]
    assert data['WorkingDirectory'] == str(operator.require_path('paths.internal_control_root'))
    assert data['StartInterval'] == 120
    assert data['Disabled'] is True
    assert data['RunAtLoad'] is False
    assert 'KeepAlive' not in data
    assert data['EnvironmentVariables']['PYTHONDONTWRITEBYTECODE'] == '1'
    assert data['EnvironmentVariables']['OPENCLAW_OPERATOR_CONFIG'] == str(operator.require_path('paths.internal_operator_config'))
    # Startup bindings can all live internally without an external runtime import.
    home = operator.require_path('paths.host_home')
    assert operator.require_path('paths.internal_control_root').is_relative_to(home)
    assert operator.require_path('paths.backblaze_watchdog_helper').is_relative_to(home)


def test_watchdog_package_pins_every_local_dependency_and_private_receipt():
    package = json.loads((ROOT / 'dependencies/backblaze-package.json').read_text())
    assert set(package['files']) == {'backblaze_resource_watchdog.py', 'backblaze_resources.py',
                                     'external_volume_guard.py', 'operator_contract.py'}
    for name, digest in package['files'].items():
        assert hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest() == digest
    assert package['receipt'] == 'backblaze-watchdog-latest.json'
    assert package['installation_root'] == 'paths.internal_control_root'
    assert package['operator_config'] == 'paths.internal_operator_config'
    assert package['live_activation'] is False
    assert (ROOT / package['scheduler_template']).is_file()
    assert (ROOT / package['reporting_owner'].split('#')[0]).is_file()
