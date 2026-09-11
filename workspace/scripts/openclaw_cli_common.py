#!/usr/bin/env python3
from __future__ import annotations
try:
    from .operator_contract import ContractError, load_operator_contract
except ImportError:
    from operator_contract import ContractError, load_operator_contract
OPERATOR = load_operator_contract()


import os
from pathlib import Path

OPENCLAW_BIN_ENV = 'OPENCLAW_BIN'
CANONICAL_OPENCLAW_CANDIDATES = (
    OPERATOR.require_path('paths.openclaw_cli'),
)
CANONICAL_NODE_BIN_CANDIDATES = (
    OPERATOR.require_path('paths.node_binary').parent,
)


def _normalize_candidate(candidate: str | Path | None) -> Path | None:
    if not candidate:
        return None
    return Path(candidate).expanduser()


def _is_executable_file(candidate: str | Path | None) -> bool:
    path = _normalize_candidate(candidate)
    return bool(path and path.is_file() and os.access(path, os.X_OK))


def resolve_openclaw_bin() -> str | None:
    env_override = os.environ.get(OPENCLAW_BIN_ENV)
    if env_override and Path(env_override) != CANONICAL_OPENCLAW_CANDIDATES[0]:
        raise ContractError('OPENCLAW_BIN disagrees with the operator contract')

    for candidate in CANONICAL_OPENCLAW_CANDIDATES:
        if _is_executable_file(candidate):
            return str(candidate)

    return None


def build_openclaw_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    expected_state = OPERATOR.require_path('paths.state_root')
    if env.get('OPENCLAW_STATE_DIR') and Path(env['OPENCLAW_STATE_DIR']) != expected_state:
        raise ContractError('OPENCLAW_STATE_DIR disagrees with the operator contract')
    expected_config = expected_state / 'openclaw.json'
    if env.get('OPENCLAW_CONFIG_PATH') and Path(env['OPENCLAW_CONFIG_PATH']) != expected_config:
        raise ContractError('OPENCLAW_CONFIG_PATH disagrees with the operator contract')
    if env.get('OPENCLAW_PROFILE'):
        raise ContractError('OPENCLAW_PROFILE conflicts with the explicit default-profile installation')
    expected_label = OPERATOR.require_string('runtime.gateway_label')
    if env.get('OPENCLAW_LAUNCHD_LABEL') and env['OPENCLAW_LAUNCHD_LABEL'] != expected_label:
        raise ContractError('OPENCLAW_LAUNCHD_LABEL disagrees with the operator contract')
    node = OPERATOR.require_path('paths.node_binary')
    if not _is_executable_file(node):
        raise ContractError('configured Node executable is unavailable')
    env['OPENCLAW_STATE_DIR'] = str(expected_state)
    env['OPENCLAW_CONFIG_PATH'] = str(expected_config)
    env['OPENCLAW_LAUNCHD_LABEL'] = expected_label
    env.pop('OPENCLAW_PROFILE', None)
    env['HOME'] = str(OPERATOR.require_path('paths.host_home'))
    prepend_dirs = [str(path) for path in CANONICAL_NODE_BIN_CANDIDATES if path.is_dir()]
    if not prepend_dirs:
        raise ContractError('configured Node directory is unavailable')

    current_parts = [part for part in env.get('PATH', '').split(os.pathsep) if part]
    merged_parts = prepend_dirs + [part for part in current_parts if part not in prepend_dirs]
    env['PATH'] = os.pathsep.join(merged_parts)
    return env
