"""Portability contract tests execute real snapshot/restore I/O in fixtures."""
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from scripts.operator_contract import ContractError, OperatorContract
from test_openclaw_runtime_activate import (
    CANDIDATE_COMMIT, FakeBackend, StoppedRestoreBackend, activate_module,
    fixture, restore, run,
)


def configure(monkeypatch, **runtime):
    values = {name: activate_module.OPERATOR.get(name, {}) for name in ('paths', 'runtime', 'identifiers')}
    values['runtime'].update(runtime)
    monkeypatch.setattr(activate_module, 'OPERATOR', OperatorContract(values))


def snapshot_absent(fixture, monkeypatch):
    path = activate_module.OPERATOR.require_path('paths.session_reservations')
    path.unlink()
    configure(monkeypatch, session_reservations_mode='absent')
    output_root = fixture.root / 'absent-snapshot'
    manifest = output_root / 'stopped-snapshot.json'
    receipt = activate_module.create_stopped_snapshot(
        fixture.paths, output_root, manifest, fixture.candidate, CANDIDATE_COMMIT,
        fixture.seal, FakeBackend(fixture),
    )
    assert receipt['recoveredInvariants']['sessionReservations']['exists'] is False
    return replace(fixture, snapshot=manifest), path


def test_explicit_absence_survives_snapshot_activation_and_recheck(fixture, monkeypatch):
    fixture, path = snapshot_absent(fixture, monkeypatch)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result['outcome'] == 'activated'
    assert result['verification']['recoveredInvariants']['sessionReservations'] == {
        'mode': 'absent', 'path': str(path), 'storePath': str(path.parent / 'sessions.json'), 'exists': False,
    }
    assert backend.gateway_boots == backend.node_boots == 1
    assert not os.path.lexists(path)
    recheck = FakeBackend(fixture)
    assert run(fixture, recheck)['outcome'] == 'activated'
    assert recheck.gateway_boots == recheck.node_boots == 0
    assert not os.path.lexists(path)


def test_explicit_absence_survives_real_restore(fixture, monkeypatch):
    fixture, path = snapshot_absent(fixture, monkeypatch)
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))['outcome'] == 'snapshot_restore_required'
    restored = restore(fixture, StoppedRestoreBackend(fixture))
    assert restored['outcome'] == 'restored'
    assert restored['restoreApplied'] is True
    assert not os.path.lexists(path)
    assert fixture.paths.current_link.resolve() == fixture.predecessor


@pytest.mark.parametrize('entry', ['file', 'directory', 'dangling-symlink'])
def test_absence_refuses_any_new_entry_before_consuming_start(fixture, monkeypatch, entry):
    fixture, path = snapshot_absent(fixture, monkeypatch)
    if entry == 'file':
        path.write_text('{}')
    elif entry == 'directory':
        path.mkdir()
    else:
        path.symlink_to(path.parent / 'missing')
    backend = FakeBackend(fixture)
    assert run(fixture, backend)['outcome'] == 'failed_before_apply'
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.path.lexists(path)
    assert not fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME).exists()


def test_absence_change_after_boot_requires_restore(fixture, monkeypatch):
    fixture, path = snapshot_absent(fixture, monkeypatch)
    class ChangedAfterBoot(FakeBackend):
        def verify_node(self, release, device, inode):
            result = super().verify_node(release, device, inode)
            path.write_text('{}')
            return result
    result = run(fixture, ChangedAfterBoot(fixture))
    assert result['outcome'] == 'snapshot_restore_required'
    assert result['restoreRequired'] is True
    assert path.read_text() == '{}'


def test_required_mode_never_turns_missing_history_into_absence(fixture):
    path = activate_module.OPERATOR.require_path('paths.session_reservations')
    path.unlink()
    with pytest.raises(activate_module.ActivationError):
        activate_module.validate_session_reservations(fixture.paths.candidate_state_dir)
    assert not os.path.lexists(path)


@pytest.mark.parametrize('change', ['bytes', 'mode', 'storePath'])
def test_required_reservations_preserve_bytes_mode_and_store(fixture, change):
    path = activate_module.OPERATOR.require_path('paths.session_reservations')
    if change == 'mode':
        path.chmod(0o644)
    elif change == 'bytes':
        path.write_bytes(path.read_bytes() + b' ')
    else:
        path.write_text(json.dumps({'storePath': str(path.parent / 'other.json')}))
    with pytest.raises(activate_module.ActivationError, match='reservation store drift'):
        activate_module.validate_session_reservations(fixture.paths.candidate_state_dir)


def test_missing_mode_is_not_an_implicit_opt_out(fixture, monkeypatch):
    values = {name: activate_module.OPERATOR.get(name, {}) for name in ('paths', 'runtime')}
    del values['runtime']['session_reservations_mode']
    monkeypatch.setattr(activate_module, 'OPERATOR', OperatorContract(values))
    with pytest.raises(ContractError, match='session_reservations_mode'):
        activate_module.validate_session_reservations(fixture.paths.candidate_state_dir)


def test_mode_change_cannot_reinterpret_an_existing_snapshot(fixture, monkeypatch):
    path = activate_module.OPERATOR.require_path('paths.session_reservations')
    path.unlink()
    configure(monkeypatch, session_reservations_mode='absent')
    assert run(fixture, FakeBackend(fixture))['outcome'] == 'failed_before_apply'


def test_reservation_path_cannot_escape_state_root(fixture, monkeypatch):
    values = {name: activate_module.OPERATOR.get(name, {}) for name in ('paths', 'runtime')}
    values['paths']['session_reservations'] = str(fixture.root / 'outside.json')
    values['runtime']['session_reservations_mode'] = 'absent'
    monkeypatch.setattr(activate_module, 'OPERATOR', OperatorContract(values))
    with pytest.raises(activate_module.ActivationError, match='within the state root'):
        activate_module.validate_session_reservations(fixture.paths.candidate_state_dir)


def test_live_paths_use_adopter_home_node_and_cli_bindings(fixture, monkeypatch):
    from types import SimpleNamespace
    values = {name: activate_module.OPERATOR.get(name, {}) for name in ('paths', 'runtime', 'identifiers')}
    paths = fixture.paths
    values['identifiers']['host_user'] = 'fixture-account'
    values['paths'].update({
        'host_home': str(fixture.root), 'state_root': str(paths.candidate_state_dir),
        'runtime_releases_root': str(paths.releases_root), 'runtime_current_link': str(paths.current_link),
        'runtime_package_link': str(paths.package_link), 'openclaw_cli': str(paths.bin_link),
        'gateway_plist': str(paths.gateway_plist), 'node_plist': str(paths.node_plist),
        'node_binary': str(paths.node), 'runtime_node_alias': str(paths.node_alias),
        'activation_lock': str(paths.lock), 'activation_result': str(paths.result),
    })
    monkeypatch.setattr(activate_module, 'OPERATOR', OperatorContract(values))
    monkeypatch.setattr(activate_module.pwd, 'getpwnam', lambda name: SimpleNamespace(
        pw_uid=os.getuid(), pw_dir=str(fixture.root)) if name == 'fixture-account' else None)
    selected = activate_module.live_paths()
    assert selected.node == paths.node
    assert selected.bin_link == paths.bin_link
    assert activate_module.expected_gateway_working_directory(selected) == fixture.root
    assert selected.current_link == paths.current_link


def test_live_paths_reject_home_belonging_to_another_account(fixture, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(activate_module.pwd, 'getpwnam', lambda _name: SimpleNamespace(
        pw_uid=os.getuid(), pw_dir=str(fixture.root / 'other-account')))
    with pytest.raises(activate_module.ActivationError, match='home does not match'):
        activate_module.live_paths()
