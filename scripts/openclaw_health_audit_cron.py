#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    from openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin
except ModuleNotFoundError:
    from scripts.openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin

WORKSPACE = Path('<OPENCLAW_WORKSPACE>')
WHAT_DAILY = 'run daily OpenClaw health audit'
WHAT_WEEKLY = 'run weekly OpenClaw deep health audit'
ACCEPTED_SECURITY_FINDINGS = {
    'Insecure or dangerous config flags enabled',
    'Exec security=full is configured',
    'autoAllowSkills is enabled for exec approvals',
    'Interpreter allowlist entries are missing strictInlineEval hardening',
    'Potential multi-user setup detected (personal-assistant model warning)',
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


def trim(text: str, limit: int = 220) -> str:
    clean = ' '.join((text or '').split())
    return clean[:limit] if len(clean) > limit else clean


def run(cmd: list[str], *, timeout: int = 120) -> CommandResult:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=build_openclaw_env(),
    )
    output = '\n'.join(part for part in [(proc.stdout or '').strip(), (proc.stderr or '').strip()] if part).strip()
    return CommandResult(proc.returncode, output)


def status_deep_healthy(output: str) -> bool:
    text = output.lower()
    return (
        'gateway service' in text
        and 'running' in text
        and 'health' in text
        and 'gateway' in text
        and 'reachable' in text
        and 'discord' in text
        and 'ok' in text
    )


def gateway_status_healthy(output: str) -> bool:
    text = output.lower()
    return 'rpc probe: ok' in text and ('runtime: running' in text or 'state active' in text)


def parse_security_summary(output: str) -> tuple[int | None, int | None, int | None]:
    match = re.search(r'Security audit\s+Summary:\s+(\d+)\s+critical\s+[·•]\s+(\d+)\s+warn\s+[·•]\s+(\d+)\s+info', output, re.I)
    if not match:
        return None, None, None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def accepted_security_count(output: str) -> int:
    return sum(1 for finding in ACCEPTED_SECURITY_FINDINGS if finding.lower() in output.lower())


def classify_status_deep_security(output: str) -> tuple[str, str | None]:
    critical, warn, info = parse_security_summary(output)
    if critical is None:
        return 'not_reported', None
    accepted_count = accepted_security_count(output)
    if critical == 0 and accepted_count >= warn:
        return f'accepted_warnings_ignored={warn} info={info}', None
    return f'critical={critical} warn={warn} info={info}', f'unaccepted_security_audit_findings critical={critical} warn={warn} info={info}'


def disk_status() -> str:
    usage = shutil.disk_usage(WORKSPACE)
    free_gib = usage.free / (1024 ** 3)
    return f'free={free_gib:.1f}GiB'


def emit(result_code: str, status_parts: list[tuple[str, str]]) -> int:
    print(result_code)
    print('STATUS | ' + ' | '.join(f'{key}: {value}' for key, value in status_parts))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Compact OpenClaw health audit cron output.')
    parser.add_argument('--weekly', action='store_true', help='Use weekly/deep audit labels and run read-only security audit deep probe when available.')
    args = parser.parse_args(argv)

    what = WHAT_WEEKLY if args.weekly else WHAT_DAILY
    openclaw_bin = resolve_openclaw_bin()
    if not openclaw_bin:
        return emit(
            'OPENCLAW_HEALTH_AUDIT_ALERT',
            [
                ('what', what),
                ('result', 'runtime_prereq_missing'),
                ('blockers', 'openclaw binary missing from OPENCLAW_BIN, canonical candidates, and PATH'),
                ('next', 'restore OPENCLAW_BIN or <HOME>/.openclaw-cli/bin/openclaw before retrying'),
            ],
        )

    blockers: list[str] = []
    notes: list[str] = []

    gateway = run([openclaw_bin, 'gateway', 'status'], timeout=90)
    gateway_ok = gateway.returncode == 0 and gateway_status_healthy(gateway.output)
    notes.append('gateway=healthy' if gateway_ok else f'gateway_probe={trim(gateway.output or str(gateway.returncode))}')

    status = run([openclaw_bin, 'status', '--deep'], timeout=180)
    status_ok = status.returncode == 0 and status_deep_healthy(status.output)
    if not status_ok:
        blockers.append(f'status_deep={trim(status.output or str(status.returncode))}')

    security_note, security_blocker = classify_status_deep_security(status.output)
    if security_blocker:
        blockers.append(security_blocker)

    if args.weekly:
        deep_security = run([openclaw_bin, 'security', 'audit', '--deep'], timeout=180)
        if deep_security.returncode != 0:
            blockers.append(f'security_audit_deep_rc={deep_security.returncode}')
        else:
            deep_note, deep_blocker = classify_status_deep_security(deep_security.output)
            security_note = f'{security_note}; deep={deep_note}'
            if deep_blocker:
                blockers.append(f'deep_{deep_blocker}')

    if blockers:
        return emit(
            'OPENCLAW_HEALTH_AUDIT_ALERT',
            [
                ('what', what),
                ('result', 'attention_needed'),
                ('blockers', '; '.join(trim(item, 180) for item in blockers[:4])),
                ('security_audit', security_note),
                ('disk', disk_status()),
                ('next', 'inspect the named blocker; accepted security-posture findings are intentionally ignored'),
            ],
        )

    return emit(
        'OPENCLAW_HEALTH_AUDIT_OK',
        [
            ('what', what),
            ('result', 'healthy'),
            ('runtime', 'gateway/status_deep reachable'),
            ('security_audit', security_note),
            ('disk', disk_status()),
            ('next', 'none'),
        ],
    )


if __name__ == '__main__':
    raise SystemExit(main())
