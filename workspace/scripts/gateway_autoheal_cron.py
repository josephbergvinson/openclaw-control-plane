#!/usr/bin/env python3
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

try:
    from openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin
except ModuleNotFoundError:
    from scripts.openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin

WHAT = 'check gateway health and auto-restart only when unhealthy'
RESTART_TAIL = ('gateway', 'restart', '--json', '--safe')
READYZ_URL = OPERATOR.require_string('runtime.readyz_url')
READYZ_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 60
# Upstream may defer a safe restart for five minutes; retain two minutes for the new listener.
RESTART_OBSERVE_TIMEOUT_SECONDS = 420
RESTART_OBSERVE_POLL_SECONDS = 1
SAFE_RESTART_RESULTS = frozenset({'scheduled', 'deferred', 'coalesced'})
SAFE_RESTART_COUNT_FIELDS = (
    'queueSize',
    'pendingReplies',
    'embeddedRuns',
    'cronRuns',
    'backgroundExecSessions',
    'rootRequests',
    'activeTasks',
)
SAFE_RESTART_BLOCKER_KINDS = frozenset(
    {
        'queue',
        'reply',
        'embedded-run',
        'cron-run',
        'background-exec',
        'root-request',
        'task',
    }
)


def trim(text: str, limit: int = 220) -> str:
    text = ' '.join((text or '').split())
    return text[:limit] if len(text) > limit else text


def run(
    cmd: list[str],
    *,
    timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=build_openclaw_env(),
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return 124, 'TimeoutExpired after {}s: {}'.format(
            timeout_seconds,
            ' '.join(cmd),
        )
    output = '\n'.join(part for part in [(proc.stdout or '').strip(), (proc.stderr or '').strip()] if part).strip()
    return proc.returncode, output


def timeout_within_deadline(
    deadline: float | None,
    maximum_seconds: float,
) -> float | None:
    if deadline is None:
        return maximum_seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    return min(maximum_seconds, remaining)


def parse_json_output(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    for line in reversed(value.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def request_safe_restart(
    openclaw_bin: str,
) -> tuple[int, str, dict[str, Any]]:
    argv = [openclaw_bin, *RESTART_TAIL]
    restart_rc, restart_out = run(argv)
    parsed = parse_json_output(restart_out)
    result = parsed.get('result') if isinstance(parsed, dict) else None
    message = parsed.get('message') if isinstance(parsed, dict) else None
    warnings = parsed.get('warnings') if isinstance(parsed, dict) else None
    preflight = parsed.get('preflight') if isinstance(parsed, dict) else None
    restart = parsed.get('restart') if isinstance(parsed, dict) else None
    target_pid = restart.get('pid') if isinstance(restart, dict) else None
    counts = preflight.get('counts') if isinstance(preflight, dict) else None
    blockers = preflight.get('blockers') if isinstance(preflight, dict) else None
    counts_valid = (
        isinstance(counts, dict)
        and all(
            isinstance(counts.get(field), int)
            and not isinstance(counts.get(field), bool)
            and counts.get(field) >= 0
            for field in SAFE_RESTART_COUNT_FIELDS
        )
        and isinstance(counts.get('totalActive'), int)
        and not isinstance(counts.get('totalActive'), bool)
        and counts.get('totalActive')
        == sum(counts.get(field) for field in SAFE_RESTART_COUNT_FIELDS)
    )
    blockers_valid = (
        isinstance(blockers, list)
        and all(
            isinstance(blocker, dict)
            and blocker.get('kind') in SAFE_RESTART_BLOCKER_KINDS
            and isinstance(blocker.get('count'), int)
            and not isinstance(blocker.get('count'), bool)
            and blocker.get('count') > 0
            and isinstance(blocker.get('message'), str)
            and bool(blocker.get('message').strip())
            for blocker in blockers
        )
    )
    preflight_safe = preflight.get('safe') if isinstance(preflight, dict) else None
    preflight_consistent = (
        counts_valid
        and blockers_valid
        and isinstance(preflight_safe, bool)
        and preflight_safe is (counts.get('totalActive') == 0)
        and preflight_safe is (len(blockers) == 0)
        and (result != 'scheduled' or preflight_safe is True)
        and (result != 'deferred' or preflight_safe is False)
    )
    restart_coalesced = restart.get('coalesced') if isinstance(restart, dict) else None
    restart_consistent = (
        isinstance(restart, dict)
        and restart.get('ok') is True
        and isinstance(target_pid, int)
        and not isinstance(target_pid, bool)
        and target_pid > 0
        and restart.get('signal') == 'SIGUSR1'
        and restart.get('mode') in {'emit', 'signal'}
        and isinstance(restart_coalesced, bool)
        and restart_coalesced is (result == 'coalesced')
        and isinstance(restart.get('delayMs'), int)
        and not isinstance(restart.get('delayMs'), bool)
        and restart.get('delayMs') >= 0
        and isinstance(restart.get('cooldownMsApplied'), int)
        and not isinstance(restart.get('cooldownMsApplied'), bool)
        and restart.get('cooldownMsApplied') >= 0
        and isinstance(restart.get('emitHooksQueued'), bool)
    )
    request_accepted = (
        restart_rc == 0
        and isinstance(parsed, dict)
        and parsed.get('ok') is True
        and result in SAFE_RESTART_RESULTS
        and isinstance(message, str)
        and bool(message.strip())
        and (
            warnings is None
            or (
                isinstance(warnings, list)
                and all(isinstance(warning, str) and warning.strip() for warning in warnings)
            )
        )
        and isinstance(preflight, dict)
        and isinstance(preflight.get('summary'), str)
        and bool(preflight.get('summary').strip())
        and preflight_consistent
        and restart_consistent
    )
    request = {
        'requestStatus': result,
        'targetPid': target_pid,
        'requestAccepted': request_accepted,
    }
    return restart_rc, restart_out, request


def gateway_listener_pid(status_output: str) -> int | None:
    parsed = parse_json_output(status_output)
    service = parsed.get('service') if isinstance(parsed, dict) else None
    runtime = service.get('runtime') if isinstance(service, dict) else None
    launchd_pid = runtime.get('pid') if isinstance(runtime, dict) else None
    port = parsed.get('port') if isinstance(parsed, dict) else None
    if (
        not isinstance(launchd_pid, int)
        or isinstance(launchd_pid, bool)
        or launchd_pid <= 0
        or not isinstance(port, dict)
        or port.get('port') != OPERATOR.require_int('runtime.gateway_port')
    ):
        return None
    listeners = port.get('listeners')
    if not isinstance(listeners, list) or not listeners:
        return None
    pids: set[int] = set()
    for item in listeners:
        if not isinstance(item, dict):
            return None
        pid = item.get('pid')
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        pids.add(pid)
    if len(pids) != 1 or launchd_pid not in pids:
        return None
    return launchd_pid


def observe_replacement(
    openclaw_bin: str,
    *,
    previous_pid: int,
    timeout_seconds: int = RESTART_OBSERVE_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout_seconds
    last_summary = 'replacement gateway was not observed'
    while True:
        status_timeout = timeout_within_deadline(deadline, COMMAND_TIMEOUT_SECONDS)
        if status_timeout is None:
            return False, last_summary
        status_rc, status_out = run(
            [openclaw_bin, 'gateway', 'status', '--json'],
            timeout_seconds=status_timeout,
        )
        listener_pid = gateway_listener_pid(status_out) if status_rc == 0 else None
        if listener_pid is not None and listener_pid != previous_pid:
            healthy, health_summary = probe_health(openclaw_bin, deadline=deadline)
            if healthy:
                return True, f'replacement_pid={listener_pid}; {health_summary}'
            last_summary = f'replacement_pid={listener_pid}; {health_summary}'
        elif listener_pid == previous_pid:
            last_summary = f'restart request acknowledged but gateway pid remains {previous_pid}'
        else:
            last_summary = 'gateway listener identity unavailable after restart request'
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_summary
        time.sleep(min(RESTART_OBSERVE_POLL_SECONDS, remaining))


def gateway_status_healthy(status_output: str) -> bool:
    text = status_output.lower()
    return 'rpc probe: ok' in text and ('runtime: running' in text or 'state active' in text)


def deep_status_healthy(status_output: str) -> bool:
    text = status_output.lower()
    return 'gateway service' in text and 'running' in text and 'health' in text and 'reachable' in text


def probe_readyz(
    url: str = READYZ_URL,
    *,
    timeout_seconds: float = READYZ_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            status = getattr(response, 'status', 200)
            payload = json.loads(response.read().decode('utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return False, f'readyz_failed={type(exc).__name__}:{trim(str(exc))}'
    failing = payload.get('failing') if isinstance(payload, dict) else None
    healthy = (
        status == 200
        and isinstance(payload, dict)
        and payload.get('ready') is True
        and isinstance(failing, list)
        and not failing
    )
    if healthy:
        return True, 'readyz_ok=true'
    return False, 'readyz_unhealthy=status_{} payload_{}'.format(
        status,
        trim(json.dumps(payload, sort_keys=True)),
    )


def probe_health(
    openclaw_bin: str,
    *,
    deadline: float | None = None,
) -> tuple[bool, str]:
    details: list[str] = []
    readyz_timeout = timeout_within_deadline(deadline, READYZ_TIMEOUT_SECONDS)
    if readyz_timeout is None:
        return False, 'health_probe_deadline_exhausted'
    readyz_healthy, readyz_summary = probe_readyz(timeout_seconds=readyz_timeout)
    if readyz_healthy:
        return True, readyz_summary
    details.append(readyz_summary)

    status_timeout = timeout_within_deadline(deadline, COMMAND_TIMEOUT_SECONDS)
    if status_timeout is None:
        details.append('health_probe_deadline_exhausted')
        return False, '; '.join(details)
    status_rc, status_out = run(
        [openclaw_bin, 'gateway', 'status'],
        timeout_seconds=status_timeout,
    )
    if status_rc == 0 and gateway_status_healthy(status_out):
        return True, f'gateway_status_ok={trim(status_out)}'
    if status_rc != 0:
        details.append(f'gateway_status_rc={status_rc}')
    if status_out:
        details.append(trim(status_out))

    deep_timeout = timeout_within_deadline(deadline, COMMAND_TIMEOUT_SECONDS)
    if deep_timeout is None:
        details.append('health_probe_deadline_exhausted')
        return False, '; '.join(details)
    deep_rc, deep_out = run(
        [openclaw_bin, 'status', '--deep'],
        timeout_seconds=deep_timeout,
    )
    if deep_rc == 0 and deep_status_healthy(deep_out):
        return True, f'status_deep_ok={trim(deep_out)}'
    if deep_rc != 0:
        details.append(f'status_deep_rc={deep_rc}')
    if deep_out:
        details.append(trim(deep_out))
    return False, '; '.join(details) or 'gateway_health_probe_failed'


def fail(
    summary: str,
    *,
    result: str = 'unhealthy_after_recovery',
    next_step: str = 'inspect /tmp/openclaw/openclaw-<date>.log and ~/.openclaw/logs/gateway*.log; escalate upstream if recurring',
) -> int:
    # With --receipt-dir, the existing cron supervisor retains/redacts stderr
    # privately and forwards only the natural stdout message to the operator.
    print(
        json.dumps({'result': result, 'detail': summary, 'next': next_step}, sort_keys=True),
        file=sys.stderr,
    )
    if result == 'runtime_prereq_missing':
        print('The gateway health check could not run because the OpenClaw command is unavailable.')
    elif result == 'restart_contract_unavailable':
        print('The gateway was unhealthy, but its safe restart request could not be confirmed. Recovery is not verified.')
    else:
        print('The gateway was unhealthy and recovery could not be confirmed.')
    return 1


def main() -> int:
    openclaw_bin = resolve_openclaw_bin()
    if not openclaw_bin:
        return fail(
            'configured OpenClaw executable is unavailable',
            result='runtime_prereq_missing',
            next_step=('restore configured CLI ' + str(OPERATOR.require_path('paths.openclaw_cli')) + ' before retrying'),
        )

    healthy_now, precheck_summary = probe_health(openclaw_bin)
    if healthy_now:
        print('OpenClaw gateway check passed. The gateway is responding normally; no restart was needed.')
        return 0

    try:
        restart_rc, restart_out, restart_request = request_safe_restart(openclaw_bin)
    except Exception as exc:
        return fail(
            f'safe restart request failed: {type(exc).__name__}: {exc}',
            result='restart_contract_unavailable',
            next_step='inspect the upstream gateway restart-handoff path; do not retry the restart blindly',
        )
    recovered = False
    postcheck_summary = 'safe restart request was not accepted'
    target_pid = restart_request['targetPid']
    if restart_request['requestAccepted'] is True and isinstance(target_pid, int):
        recovered, postcheck_summary = observe_replacement(
            openclaw_bin,
            previous_pid=target_pid,
        )
    if restart_rc == 0 and restart_request['requestAccepted'] is True and recovered:
        print('OpenClaw recovered after a safe gateway restart. The replacement is responding normally.')
        return 0

    return fail(
        '; '.join(
            filter(
                None,
                [
                    f'postcheck={postcheck_summary}',
                    f'precheck={precheck_summary}',
                    f'restart_rc={restart_rc}' if restart_rc != 0 else '',
                    'restart_request_accepted={}'.format(
                        str(restart_request['requestAccepted']).lower()
                    ),
                    f"restart_request_status={restart_request['requestStatus']}",
                    f"restart_target_pid={restart_request['targetPid']}",
                    trim(restart_out),
                ],
            )
        )
    )


if __name__ == '__main__':
    raise SystemExit(main())
