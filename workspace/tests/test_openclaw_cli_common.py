from __future__ import annotations
import os
from pathlib import Path
from unittest import mock
import pytest
from scripts import openclaw_cli_common as cli
from scripts.operator_contract import ContractError, OperatorContract


def test_configured_cli_is_used_without_path_fallback(tmp_path: Path) -> None:
    selected = tmp_path / 'configured-cli'
    selected.write_text('#!/bin/sh\nexit 0\n')
    selected.chmod(0o700)
    with mock.patch.dict(os.environ, {'OPENCLAW_BIN': str(selected)}), mock.patch.object(cli, 'CANONICAL_OPENCLAW_CANDIDATES', (selected,)):
        assert cli.resolve_openclaw_bin() == str(selected)
        selected.unlink()
        assert cli.resolve_openclaw_bin() is None


def test_conflicting_environment_cannot_select_another_cli(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {'OPENCLAW_BIN': str(tmp_path / 'other-cli')}):
        with pytest.raises(ContractError, match='disagrees'):
            cli.resolve_openclaw_bin()


def test_cli_environment_binds_state_and_node_and_rejects_conflicts(tmp_path: Path) -> None:
    node = tmp_path / 'node'
    node.write_text('#!/bin/sh\nexit 0\n')
    node.chmod(0o700)
    contract = OperatorContract({'paths': {'node_binary': str(node), 'state_root': str(tmp_path / 'state'), 'host_home': str(tmp_path / 'home')},
                                 'runtime': {'gateway_label': 'ai.openclaw.gateway'}})
    with mock.patch.object(cli, 'OPERATOR', contract), mock.patch.object(cli, 'CANONICAL_NODE_BIN_CANDIDATES', (tmp_path,)):
        env = cli.build_openclaw_env({'PATH': '/usr/bin:/bin'})
        assert env['OPENCLAW_STATE_DIR'] == str(tmp_path / 'state')
        assert env['OPENCLAW_CONFIG_PATH'] == str(tmp_path / 'state/openclaw.json')
        assert env['OPENCLAW_LAUNCHD_LABEL'] == 'ai.openclaw.gateway'
        assert 'OPENCLAW_PROFILE' not in env
        assert env['PATH'].split(os.pathsep)[0] == str(tmp_path)
        with pytest.raises(ContractError, match='disagrees'):
            cli.build_openclaw_env({'OPENCLAW_STATE_DIR': str(tmp_path / 'other')})
        for conflict in ({'OPENCLAW_CONFIG_PATH': str(tmp_path / 'other.json')},
                         {'OPENCLAW_PROFILE': 'other-account'},
                         {'OPENCLAW_LAUNCHD_LABEL': 'ai.openclaw.other'}):
            with pytest.raises(ContractError):
                cli.build_openclaw_env(conflict)
        matching = cli.build_openclaw_env({'OPENCLAW_CONFIG_PATH': str(tmp_path / 'state/openclaw.json'),
                                          'OPENCLAW_LAUNCHD_LABEL': 'ai.openclaw.gateway', 'OPENCLAW_PROFILE': ''})
        assert matching['OPENCLAW_CONFIG_PATH'] == str(tmp_path / 'state/openclaw.json')
        assert 'OPENCLAW_PROFILE' not in matching
        node.unlink()
        with pytest.raises(ContractError, match='Node executable'):
            cli.build_openclaw_env({})
