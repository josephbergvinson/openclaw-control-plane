#!/usr/bin/env python3
"""Check both disks and optionally run the bounded storage cleanup on pressure."""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get('WORKSPACE', (str(OPERATOR.require_path('paths.workspace'))))).expanduser()
INTERNAL_DISK_PATH = Path(os.environ.get('STORAGE_HEADROOM_INTERNAL_DISK_PATH', '/')).expanduser()
WORKSPACE_DISK_PATH = Path(os.environ.get('STORAGE_HEADROOM_WORKSPACE_DISK_PATH', (str(OPERATOR.require_path('paths.data_root'))))).expanduser()
ARTIFACT_ROOT = Path(os.environ.get('STORAGE_HEADROOM_ARTIFACT_ROOT', str(WORKSPACE / 'artifacts/storage_headroom'))).expanduser()
WARNING_GIB = float(os.environ.get('STORAGE_HEADROOM_WARNING_GIB', '30'))
CRITICAL_GIB = float(os.environ.get('STORAGE_HEADROOM_CRITICAL_GIB', '20'))
EMERGENCY_GIB = float(os.environ.get('STORAGE_HEADROOM_EMERGENCY_GIB', '10'))
OWC_WARNING_GIB = float(os.environ.get('STORAGE_HEADROOM_OWC_WARNING_GIB', '100'))
OWC_CRITICAL_GIB = float(os.environ.get('STORAGE_HEADROOM_OWC_CRITICAL_GIB', '50'))
OWC_EMERGENCY_GIB = float(os.environ.get('STORAGE_HEADROOM_OWC_EMERGENCY_GIB', '20'))
try:
    from . import openclaw_storage_prune
except ImportError:
    import openclaw_storage_prune

SEVERITY = {'ok': 0, 'warning': 1, 'critical': 2, 'emergency': 3, 'unavailable': 4}


@dataclass(frozen=True)
class Usage:
    total: int
    used: int
    free: int


def bytes_to_gib(value: int) -> float:
    return value / 1024**3


def get_usage(path: Path) -> Usage:
    usage = shutil.disk_usage(path)
    return Usage(usage.total, usage.used, usage.free)


def classify_free_gib(free_gib: float, *, external: bool = False) -> str:
    warning, critical, emergency = ((OWC_WARNING_GIB, OWC_CRITICAL_GIB, OWC_EMERGENCY_GIB)
                                     if external else (WARNING_GIB, CRITICAL_GIB, EMERGENCY_GIB))
    if free_gib < emergency:
        return 'emergency'
    if free_gib < critical:
        return 'critical'
    if free_gib < warning:
        return 'warning'
    return 'ok'


def check_disks() -> dict:
    filesystems = {}
    for key, path, external in (('internal_root', INTERNAL_DISK_PATH, False), ('workspace', WORKSPACE_DISK_PATH, True)):
        try:
            if external:
                if not path.is_mount():
                    raise OSError('OWC drive is not mounted at the configured path')
                openclaw_storage_prune.require_owc_identity()
            usage = get_usage(path)
            filesystems[key] = {'path': str(path), 'resolved_path': str(path.resolve(strict=False)),
                                'role': 'alert_authority', 'level': classify_free_gib(bytes_to_gib(usage.free), external=external),
                                'bytes': usage.__dict__, 'gib': {k: round(bytes_to_gib(v), 3) for k, v in usage.__dict__.items()}}
        except (OSError, RuntimeError, ValueError) as error:
            filesystems[key] = {'path': str(path), 'role': 'alert_authority', 'level': 'unavailable', 'error': str(error)}
    return filesystems


def get_swap_usage() -> dict:
    """Read OS-managed swap usage; never remove, relocate or truncate swap."""
    try:
        result = subprocess.run(['/usr/sbin/sysctl', '-n', 'vm.swapusage'],
                                capture_output=True, text=True, timeout=5, check=True)
        values = {}
        for name, amount, unit in re.findall(r'\b(total|used|free)\s*=\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\b', result.stdout):
            values[name + '_bytes'] = round(float(amount) * 1024 ** (' KMGT'.index(unit) if unit else 0))
        if set(values) != {'total_bytes', 'used_bytes', 'free_bytes'}:
            raise ValueError('swap usage response was incomplete')
        return {'available': True, 'managed_by': 'macOS', 'source': 'sysctl vm.swapusage', **values}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return {'available': False, 'managed_by': 'macOS', 'error': type(error).__name__}


def run_cleanup() -> dict:
    script = Path(__file__).with_name('openclaw_storage_prune.py')
    result = subprocess.run([sys.executable, str(script), '--apply', '--json', '--budget-seconds', '300'],
                            capture_output=True, text=True, timeout=340)
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError('cleanup returned no valid terminal receipt') from error
    if (not isinstance(receipt, dict) or not isinstance(receipt.get('errors'), list)
            or receipt.get('schema') != 'openclaw.storage_prune.v1' or receipt.get('terminal') is not True
            or receipt.get('mode') != 'apply' or receipt.get('status') not in ('ok', 'partial', 'failed')):
        raise RuntimeError('cleanup terminal receipt failed validation')
    if result.returncode and receipt['status'] == 'ok':
        raise RuntimeError('cleanup exit code disagrees with its receipt')
    return {k: receipt.get(k) for k in ('status', 'report', 'errors', 'reclaimed_allocated_bytes')}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='run safe cleanup if either disk is under pressure')
    mode.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    before = check_disks()
    cleanup = None
    action = 'checked'
    if args.apply and any(f['level'] not in ('ok', 'unavailable') for f in before.values()):
        if before['workspace']['level'] != 'unavailable':
            try:
                cleanup = run_cleanup()
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                cleanup = {'status': 'failed', 'errors': [str(error)], 'report': None}
            action = 'guarded_cleanup_attempted'
    after = check_disks() if cleanup is not None else before
    level = max((f['level'] for f in after.values()), key=SEVERITY.__getitem__)
    status = 'ok' if level == 'ok' and (cleanup is None or cleanup['status'] == 'ok') else 'attention'
    swap = get_swap_usage()
    now = datetime.now(timezone.utc)
    payload = {'schema': 'openclaw.storage_headroom.v4', 'created_at_utc': now.isoformat(),
               'status': status, 'terminal': True, 'level': level, 'alert_authority': 'both_disks',
               'marker': 'STORAGE_HEADROOM_OK' if status == 'ok' else 'STORAGE_HEADROOM_FAIL',
               'action': action, 'filesystems': after, 'before_filesystems': before, 'cleanup': cleanup,
               'system_swap': swap}
    parts = []
    for key, name in (('internal_root', 'Mac mini'), ('workspace', 'OWC')):
        disk = after[key]
        parts.append(f'{name} {disk["gib"]["free"]:.1f} GiB free ({disk["level"]})'
                     if disk['level'] != 'unavailable' else f'{name} unavailable')
    message = f'Storage {"healthy" if status == "ok" else "needs attention"}: ' + '; '.join(parts) + '.'
    if cleanup:
        message += (' Safe cleanup completed.' if cleanup['status'] == 'ok' else ' Safe cleanup needs attention.')
    if status != 'ok':
        message += ' More space is needed.' if level not in ('ok', 'unavailable') else ' Check the disk or cleanup failure.'
    if (after['internal_root']['level'] not in ('ok', 'unavailable')
            and swap['available'] and swap['used_bytes'] >= 4 * 1024**3):
        message += f' Memory spillover uses {bytes_to_gib(swap["used_bytes"]):.1f} GiB of disk; reduce concurrent heavy work.'
    payload['summary'] = message
    # If the external drive is missing, avoid writing into a shadow mount.
    if after['workspace']['level'] != 'unavailable':
        try:
            ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
            stamp = now.strftime('%Y%m%dT%H%M%S%fZ')
            openclaw_storage_prune.write_receipt(ARTIFACT_ROOT / f'storage-headroom-{stamp}.json', payload)
            openclaw_storage_prune.write_receipt(ARTIFACT_ROOT / 'latest.json', payload)
            openclaw_storage_prune.write_text_receipt(ARTIFACT_ROOT / 'latest.txt', message + '\n')
        except OSError:
            status = 'attention'
            message += ' The storage check could not save its receipt.'
    print(message)
    return 0 if status == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
