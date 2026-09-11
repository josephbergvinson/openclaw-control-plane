from __future__ import annotations
import json
from pathlib import Path
import pytest
from scripts.operator_contract import ContractError, OperatorContract, load_operator_contract


def test_contract_requires_explicit_operational_paths(tmp_path: Path) -> None:
    contract = OperatorContract(workspace=tmp_path)
    assert contract.require_path('paths.workspace') == tmp_path
    with pytest.raises(ContractError, match='missing'):
        contract.require_path('paths.state_root')


@pytest.mark.parametrize('value', ['relative', '/tmp/../state', '~/state', '/tmp/${state}', '/tmp/<operator>', '/tmp/state\n'])
def test_unresolved_or_ambiguous_paths_are_rejected(value: str) -> None:
    with pytest.raises(ContractError):
        OperatorContract({'paths': {'state_root': value}}).require_path('paths.state_root')


def test_integer_binding_does_not_accept_boolean() -> None:
    with pytest.raises(ContractError):
        OperatorContract({'runtime': {'gateway_port': True}}).require_int('runtime.gateway_port')


def test_explicit_missing_or_malformed_configuration_never_falls_back(tmp_path: Path) -> None:
    selected = tmp_path / 'operator.json'
    with pytest.raises(ContractError, match='does not exist'):
        load_operator_contract(selected)
    selected.write_text('[]')
    with pytest.raises(ContractError, match='JSON object'):
        load_operator_contract(selected)


def test_json_references_preserve_types_and_reject_partial_interpolation(tmp_path: Path) -> None:
    contract = OperatorContract({'paths': {'workspace': str(tmp_path)}, 'runtime': {'gateway_port': 18789}})
    assert contract.render({'argv': ['${operator:paths.workspace}'], 'port': '${operator:runtime.gateway_port}'}) == {'argv': [str(tmp_path)], 'port': 18789}
    with pytest.raises(ContractError, match='whole JSON value'):
        contract.render('prefix ${operator:paths.workspace}')
    with pytest.raises(ContractError, match='missing'):
        contract.render('${operator:runtime.missing}')


def test_reads_do_not_alias_mutable_configuration(tmp_path: Path) -> None:
    values = {'paths': {'workspace': str(tmp_path)}, 'runtime': {'extensions': ['discord']}}
    contract = OperatorContract(values)
    values['runtime']['extensions'].append('changed')
    result = contract.get('runtime.extensions')
    result.append('changed-again')
    assert contract.require_list('runtime.extensions') == ['discord']


def test_backup_generation_pins_are_explicit_and_validated() -> None:
    from unittest import mock
    from scripts import openclaw_backup_retention_cleanup as retention
    for rows in (None, [['../escape', 'a' * 64]], [['openclaw-backup-20200101T000000Z', 'bad']],
                 [['openclaw-backup-20200101T000000Z', 'a' * 64]] * 2):
        values = {'backup': {'frozen_clone_v2_pins': rows}} if rows is not None else {}
        with mock.patch.object(retention, 'OPERATOR', OperatorContract(values)):
            with pytest.raises(ValueError):
                retention.frozen_clone_v2_pins()
    with mock.patch.object(retention, 'OPERATOR', OperatorContract({'backup': {'frozen_clone_v2_pins': []}})):
        assert retention.frozen_clone_v2_pins() == ()


@pytest.mark.parametrize('runtime_root,success', [(None, True), ('relative/runtime', False)])
def test_unused_personal_data_route_does_not_block_cron_help(tmp_path: Path, runtime_root, success: bool) -> None:
    import os
    import subprocess
    import sys
    selected = tmp_path / 'operator.json'
    selected.write_text(json.dumps({'paths': {'workspace': str(tmp_path), 'personal_data_runtime': runtime_root}}))
    child = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / 'scripts/cron_python_entrypoint.py'), '--help'],
        env={**os.environ, 'OPENCLAW_OPERATOR_CONFIG': str(selected)}, capture_output=True, text=True,
    )
    assert (child.returncode == 0) is success
    if success:
        assert '--script' in child.stdout
    else:
        assert 'absolute' in child.stderr
