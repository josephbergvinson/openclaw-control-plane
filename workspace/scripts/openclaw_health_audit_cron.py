#!/usr/bin/env python3
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
import json
import re
import subprocess
import time
from dataclasses import dataclass

try:
    from openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin
except ModuleNotFoundError:
    from scripts.openclaw_cli_common import build_openclaw_env, resolve_openclaw_bin

# Accepted findings are explicit adopter policy, bound to an exact severity.
# An empty mapping accepts no findings; unknown or escalated findings remain visible.
def _accepted_security_findings() -> dict[str, tuple[str, str]]:
    value = OPERATOR.get('maintenance.accepted_security_findings', {})
    if not isinstance(value, dict):
        raise ValueError('accepted_security_findings must be an object')
    result = {}
    for key, pair in value.items():
        if (not isinstance(key, str) or not key or not isinstance(pair, list) or len(pair) != 2
                or any(not isinstance(item, str) or not item for item in pair)):
            raise ValueError('accepted_security_findings entries must bind severity and title')
        result[key] = tuple(pair)
    return result


ACCEPTED_SECURITY_FINDINGS = _accepted_security_findings()

# 'info' is the only severity it is safe to stay quiet about. Anything else —
# including a severity band the CLI grows after this script was written — is
# reported, because an unrecognised band is not evidence of harmlessness and
# silence about a new finding is the exact failure this blocker exists to
# prevent.
KNOWN_SEVERITIES = ('critical', 'warn', 'info')
QUIET_SEVERITY = 'info'
# Naming order when the line cannot hold everything: worst first, with an
# unrecognised band ranked above warn since its real weight is unknown.
SEVERITY_RANK = {'critical': 0, 'warn': 2}
UNKNOWN_SEVERITY_RANK = 1
SECURITY_BLOCKER_STEM = 'unaccepted_security_audit_findings'
# emit() trims every blocker to 120 chars, so the blocker has to fit its own
# names inside that budget rather than be cut off mid-title.
SECURITY_BLOCKER_MAX_LEN = 120


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    output: str


@dataclass(frozen=True)
class SecurityFinding:
    check_id: str
    severity: str
    title: str

    @property
    def identifier(self) -> str:
        """Short stable name for the blocker line.

        checkId is both shorter and more stable than the prose title, so it is
        what the operator gets when the 120-char budget cannot hold everything.
        """

        return self.check_id or self.title


def trim(text: str, limit: int = 220) -> str:
    clean = ' '.join((text or '').split())
    return clean[:limit] if len(clean) > limit else clean


# A timeout must not be able to kill the audit. An unguarded TimeoutExpired
# propagates out of every caller, the script dies with a traceback (exit 125),
# and cron records lastRunStatus=ok / consecutiveErrors=0 — so a run in which
# ZERO health checks executed is indistinguishable from a clean one. That is
# what happened to the weekly on 2026-08-09 and the daily on 2026-08-12.
# A timed-out command is a failed command, and it has to report as one.
COMMAND_TIMEOUT_RETURNCODE = 124


def run(cmd: list[str], *, timeout: int = 120) -> CommandResult:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            env=build_openclaw_env(),
        )
    except subprocess.TimeoutExpired as exc:
        captured = []
        for stream in (exc.stdout, exc.stderr):
            if not stream:
                continue
            text = stream.decode('utf-8', 'replace') if isinstance(stream, bytes) else stream
            if text.strip():
                captured.append(text.strip())
        captured.append(f'command timed out after {timeout}s: {" ".join(cmd)}')
        return CommandResult(COMMAND_TIMEOUT_RETURNCODE, '\n'.join(captured).strip())
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
    probe_ok = 'connectivity probe: ok' in text or 'rpc probe: ok' in text
    return probe_ok and ('runtime: running' in text or 'state active' in text)


def parse_security_summary(output: str) -> tuple[int | None, int | None, int | None]:
    match = re.search(r'Security audit\s+Summary:\s+(\d+)\s+critical\s+[·•]\s+(\d+)\s+warn\s+[·•]\s+(\d+)\s+info', output, re.I)
    if not match:
        return None, None, None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


# `openclaw security audit --json` emits findings[] with checkId/severity/title
# (verified against the live CLI, 2026-08-13). The text renderer prints the same
# findings as "  WARN <title>" lines under the "Security audit" heading, which is
# the fallback when the JSON form is unavailable — losing checkIds but never the
# per-severity acceptance check.
TEXT_FINDING_RE = re.compile(r'^\s*(CRITICAL|WARN|INFO)\s+(\S.*?)\s*$')
TEXT_SECTION_START_RE = re.compile(r'^\s*Security audit\b', re.I)
TEXT_SECTION_END_RE = re.compile(r'^\s*(Full report:|Deep probe:)', re.I)


def parse_security_findings_json(output: str | None) -> list[SecurityFinding] | None:
    """Structured findings, or None if this output is not the JSON form."""

    data = parse_json_object(output or '')
    if not data:
        return None
    raw = data.get('findings')
    if not isinstance(raw, list):
        return None
    findings: list[SecurityFinding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = item.get('title')
        severity = item.get('severity')
        if not isinstance(title, str) or not isinstance(severity, str):
            continue
        check_id = item.get('checkId')
        findings.append(
            SecurityFinding(
                check_id=check_id if isinstance(check_id, str) else '',
                severity=severity.strip().lower(),
                title=title.strip(),
            )
        )
    return findings


def parse_security_findings_text(output: str) -> list[SecurityFinding] | None:
    """Findings recovered from the rendered audit section, or None if absent.

    Bounded to the security section so an unrelated line elsewhere in `status
    --deep` can never be mistaken for a finding.
    """

    findings: list[SecurityFinding] = []
    in_section = False
    for line in (output or '').splitlines():
        if TEXT_SECTION_START_RE.match(line):
            in_section = True
        if not in_section:
            continue
        if TEXT_SECTION_END_RE.match(line):
            break
        match = TEXT_FINDING_RE.match(line)
        if match:
            findings.append(
                SecurityFinding(check_id='', severity=match.group(1).lower(), title=match.group(2).strip())
            )
    return findings if in_section else None


def severity_counts(findings: list[SecurityFinding]) -> dict[str, int]:
    """Per-severity totals, including bands this script does not recognise.

    Unknown bands are counted rather than discarded so they can be compared
    against the audit's own summary and named in the blocker.
    """

    counts = {severity: 0 for severity in KNOWN_SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    return counts


def unrecognized_severity_total(counts: dict[str, int]) -> int:
    return sum(count for severity, count in counts.items() if severity not in KNOWN_SEVERITIES)


def unaccepted_security_findings(findings: list[SecurityFinding]) -> list[SecurityFinding]:
    """Reportable findings the operator has not already ruled on at that severity."""

    def is_accepted(finding: SecurityFinding) -> bool:
        if finding.check_id:
            accepted = ACCEPTED_SECURITY_FINDINGS.get(finding.check_id)
            return accepted is not None and accepted[0] == finding.severity
        return any(
            accepted_severity == finding.severity and accepted_title == finding.title
            for accepted_severity, accepted_title in ACCEPTED_SECURITY_FINDINGS.values()
        )

    unaccepted = [
        finding
        for finding in findings
        if finding.severity != QUIET_SEVERITY
        and not is_accepted(finding)
    ]
    # Criticals first: when the budget cannot name everything, name the worst.
    return sorted(
        unaccepted,
        key=lambda finding: SEVERITY_RANK.get(finding.severity, UNKNOWN_SEVERITY_RANK),
    )


def pack_identifiers(head: str, identifiers: list[str], max_len: int) -> str:
    """Name as many findings as fit, then say how many were left unnamed.

    A blocker that says only "warn=6" tells the operator nothing actionable, and
    two full titles do not fit in 120 chars. Names go in until the next one
    would overflow; the remainder is reported as a count so the operator always
    knows the line is partial.
    """

    named: list[str] = []
    for identifier in identifiers:
        remaining = len(identifiers) - len(named) - 1
        candidate = head + ''.join(f' {item}' for item in named + [identifier])
        if remaining:
            candidate += f' +{remaining}'
        if len(candidate) > max_len:
            break
        named.append(identifier)
    text = head + ''.join(f' {item}' for item in named)
    unnamed = len(identifiers) - len(named)
    if unnamed:
        text += f' +{unnamed}'
    return text


def classify_security(
    json_output: str | None,
    text_output: str,
    *,
    prefix: str = '',
    max_len: int = SECURITY_BLOCKER_MAX_LEN,
) -> tuple[str, str | None]:
    """(note, blocker) for the security audit, naming what is unaccepted.

    JSON is authoritative; the rendered section is the fallback so a CLI that
    stops emitting JSON degrades to fewer details rather than to silence.
    """

    findings = parse_security_findings_json(json_output)
    source = 'json'
    if findings is None:
        findings = parse_security_findings_text(text_output)
        source = 'status_text'
    if findings is None:
        return 'not_reported', f'{prefix}security_findings_unavailable'

    counts = severity_counts(findings)
    # The totals are only ever compared against findings from the SAME output:
    # the JSON audit and the rendered status are two separate audit runs, and
    # cross-checking one against the other would report a disagreement that
    # says nothing about whether this parser is complete.
    reported: dict[str, int] = {}
    if source == 'json':
        summary = (parse_json_object(json_output or '') or {}).get('summary')
        if isinstance(summary, dict):
            reported = {
                str(key): value
                for key, value in summary.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
    else:
        text_critical, text_warn, text_info = parse_security_summary(text_output)
        reported = {
            key: value
            for key, value in (('critical', text_critical), ('warn', text_warn), ('info', text_info))
            if value is not None
        }

    # If the audit's own totals disagree with the findings we could name, the
    # parser is behind the CLI and every name below is suspect. Say so instead
    # of quietly reporting the subset we happened to understand. Every non-info
    # total is compared, including a severity band added after this script was
    # written, so a new band cannot pass as zero findings; info is excluded
    # because the renderer routinely omits info detail.
    mismatched = {
        severity: (total, counts.get(severity, 0))
        for severity, total in reported.items()
        if severity != QUIET_SEVERITY and counts.get(severity, 0) != total
    }
    if mismatched:
        detail = ' '.join(
            f'{severity}:summary={total},parsed={parsed}' for severity, (total, parsed) in sorted(mismatched.items())
        )
        return (
            f'summary_mismatch {detail} parsed={counts}',
            f'{prefix}security_findings_incomplete {detail}',
        )

    info = reported.get(QUIET_SEVERITY, counts['info'])
    # A silent downgrade to the rendered output would hide the fact that the
    # names below are best-effort, so the fallback says so even on a clean run.
    degraded = '' if source == 'json' else f' source={source}'
    unaccepted = unaccepted_security_findings(findings)
    if not unaccepted:
        return f'accepted_warnings_ignored={counts["warn"]} info={info}{degraded}', None

    # A finding at an unrecognised severity is counted separately rather than
    # folded into warn, so "critical=0 warn=0" can never read as "nothing bad".
    other = unrecognized_severity_total(counts)
    tally = f'critical={counts["critical"]} warn={counts["warn"]}' + (f' other={other}' if other else '')
    note = f'{tally} info={info} source={source}'
    head = f'{prefix}{SECURITY_BLOCKER_STEM} {tally}'
    blocker = pack_identifiers(head, [finding.identifier for finding in unaccepted], max_len)
    return note, blocker


def parse_json_object(output: str) -> dict | None:
    """Parse CLI JSON even if a future wrapper accidentally adds banner text."""
    text = (output or '').strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find('{')
        end = text.rfind('}')
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _int_from_mapping(data: dict, key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) else None


def _counts_by_code(section: dict | None) -> dict[str, int]:
    if not isinstance(section, dict):
        return {}
    by_code = section.get('byCode')
    if not isinstance(by_code, dict):
        return {}
    return {key: value for key, value in by_code.items() if isinstance(value, int)}


# Task findings that clear themselves. Every one of these fires only on a row
# the ledger has already finished with: maintenance stamps cleanupAfter on it
# and prunes it when the window expires, so counting them daily would ask the
# operator to authorize a repair for records that need none.
#
# Nothing on the flow side belongs here. A TaskFlow is only pruned once its
# status is succeeded/failed/cancelled/lost; 'blocked' is not one of those, so
# a blocked flow is never pruned and its finding never ages out. Anything not
# named here -- including a code added to the ledger after this was written --
# is reported, because silence about a set that only grows is the failure this
# job exists to catch.
SELF_CLEARING_TASK_CODES = ('lost', 'delivery_failed', 'inconsistent_timestamps')


def repairable_residue(data: dict | None) -> dict[str, int]:
    """Findings the operator still needs to see after a repair pass.

    Everything the audit reports is surfaced except the task codes that
    provably age out on their own, so an unrecognised finding is never
    silently dropped.
    """

    if not data:
        return {}
    audit_after = data.get('auditAfter') if isinstance(data.get('auditAfter'), dict) else {}
    task_counts = _counts_by_code(audit_after)
    flow_counts = _counts_by_code(audit_after.get('taskFlows'))
    found: dict[str, int] = {}
    for code, count in task_counts.items():
        if count and code not in SELF_CLEARING_TASK_CODES:
            found[code] = count
    for code, count in flow_counts.items():
        if count:
            found[f'flow_{code}'] = count
    return found


def retained_history_total(data: dict | None) -> int:
    """Count findings that are terminal history inside their retention window."""

    if not data:
        return 0
    audit_after = data.get('auditAfter') if isinstance(data.get('auditAfter'), dict) else {}
    task_counts = _counts_by_code(audit_after)
    return sum(task_counts.get(code, 0) for code in SELF_CLEARING_TASK_CODES)


def repairs_applied(data: dict | None) -> dict[str, int]:
    """What the supported maintenance path actually changed this run."""

    if not data:
        return {}
    maintenance = data.get('maintenance')
    if not isinstance(maintenance, dict):
        return {}
    applied: dict[str, int] = {}
    for section, prefix in (('tasks', ''), ('taskFlows', 'flow_')):
        values = maintenance.get(section)
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            if isinstance(value, int) and value:
                applied[f'{prefix}{key}'] = value
    return applied


def task_ledger_counts(data: dict | None) -> dict[str, int | str] | None:
    if not data:
        return None
    audit_after = data.get('auditAfter') if isinstance(data.get('auditAfter'), dict) else {}
    total = _int_from_mapping(audit_after, 'total')
    if not total:
        return None
    by_code = audit_after.get('byCode') if isinstance(audit_after.get('byCode'), dict) else {}
    return {
        'total': total,
        'errors': audit_after.get('errors', '?'),
        'warnings': audit_after.get('warnings', '?'),
        'stale_running': by_code.get('stale_running', '?'),
        'lost': by_code.get('lost', '?'),
        'delivery_failed': by_code.get('delivery_failed', '?'),
    }


def task_ledger_summary(data: dict | None) -> str:
    counts = task_ledger_counts(data)
    if not counts:
        return 'audit_after=0 errors=0 warnings=0'
    return (
        f"audit_after={counts['total']} "
        f"errors={counts['errors']} "
        f"warnings={counts['warnings']} "
        f"stale_running={counts['stale_running']} "
        f"lost={counts['lost']} "
        f"delivery_failed={counts['delivery_failed']}"
    )


def task_ledger_residue_blocker(data: dict | None) -> str | None:
    """Block only on residue a repair pass could still have cleared.

    Everything else the audit surfaces is finished work inside its retention
    window: those rows are already terminal and carry a cleanup stamp, so the
    ledger removes them on its own schedule. Reporting them as a blocker asked
    the operator to authorize a repair for records that needed none, every day.
    """

    if not data:
        return 'task_ledger_residue_unparseable'
    repairable = repairable_residue(data)
    if not repairable:
        return None
    detail = ' '.join(f'{code}={count}' for code, count in sorted(repairable.items()))
    return f'task_ledger_repair_incomplete {detail}'


def run_task_ledger_maintenance(openclaw_bin: str) -> tuple[str, str | None]:
    """Run the supported task-ledger repair: preview, then apply, twice if needed.

    Applying maintenance can make a second pass productive (a reconciled task
    releases the flow that was waiting on it), so a run that still shows
    repairable residue gets one more apply before anything is reported.
    """
    preview = run([openclaw_bin, 'tasks', 'maintenance', '--json'], timeout=180)
    if preview.returncode != 0:
        return f'preview_failed rc={preview.returncode}', f'task_maintenance_preview={trim(preview.output or str(preview.returncode))}'
    preview_data = parse_json_object(preview.output)

    apply = run([openclaw_bin, 'tasks', 'maintenance', '--apply', '--json'], timeout=240)
    if apply.returncode != 0:
        return (
            f'preview={task_ledger_summary(preview_data)} apply_failed rc={apply.returncode}',
            f'task_maintenance_apply={trim(apply.output or str(apply.returncode))}',
        )
    apply_data = parse_json_object(apply.output)
    fixed = repairs_applied(apply_data)

    if repairable_residue(apply_data):
        # Bounded well inside the delivery window. Runs that exceed ~145s lose
        # their report to a yield race (exec hard-clamps yieldMs to 120s), and
        # this second pass only ever runs on days WITH residue — precisely the
        # days whose report matters most. A deferred second pass is reported
        # next run; a dropped report is lost.
        second = run([openclaw_bin, 'tasks', 'maintenance', '--apply', '--json'], timeout=45)
        if second.returncode == 0:
            second_data = parse_json_object(second.output)
            if second_data:
                apply_data = second_data
                for code, count in repairs_applied(second_data).items():
                    fixed[code] = fixed.get(code, 0) + count

    fixed_note = (
        ', '.join(f'{code}={count}' for code, count in sorted(fixed.items()))
        if fixed
        else 'nothing needed repair'
    )
    note = (
        f'fixed={fixed_note} '
        f'retained_history={retained_history_total(apply_data)} '
        f'after={task_ledger_summary(apply_data)}'
    )
    return note, task_ledger_residue_blocker(apply_data)


def emit(message: str, *, exit_code: int = 0) -> int:
    """Keep the process outcome consistent with the reader-facing audit result."""

    print(message)
    return exit_code


def public_security_problem(json_output: str | None, text_output: str, blocker: str) -> str:
    """Describe classified failures without copying provider titles or details."""
    if SECURITY_BLOCKER_STEM not in blocker:
        return 'the security audit result was incomplete'

    findings = parse_security_findings_json(json_output)
    if findings is None:
        findings = parse_security_findings_text(text_output)
    subjects = {
        'config.insecure_or_dangerous_flags': 'security-sensitive settings',
        'tools.exec.security_full_configured': 'unrestricted command execution',
        'tools.exec.auto_allow_skills_enabled': 'automatic permission for skill commands',
        'tools.exec.allowlist_interpreter_without_strict_inline_eval': 'interpreter command permissions',
        'security.trust_model.multi_user_heuristic': 'access by multiple users',
        'security.trust_model.cross_agent_session_access_default': 'shared access to conversations between agents',
        'fs.unencrypted_state_volume': 'unencrypted local data storage',
        'models.weak_tier': 'configured model tier',
    }
    descriptions: list[str] = []
    unaccepted = unaccepted_security_findings(findings or [])
    for finding in unaccepted[:3]:
        check_id = finding.check_id or next(
            (key for key, (_, title) in ACCEPTED_SECURITY_FINDINGS.items() if title == finding.title),
            '',
        )
        subject = subjects.get(check_id, 'an unrecognized security finding')
        severity = {'critical': 'critical', 'warn': 'warning'}.get(finding.severity, 'unrecognized severity')
        descriptions.append(f'{subject} ({severity})')
    if len(unaccepted) > 3:
        descriptions.append(f'{len(unaccepted) - 3} additional findings')
    detail = ', '.join(descriptions) or 'an unrecognized security finding'
    return f'the security audit found new or changed findings: {detail}'


def public_failure_message(
    *,
    weekly: bool,
    gateway_ok: bool,
    status_ok: bool,
    security_problems: list[str],
    deep_security_ok: bool,
    task_maintenance_blocker: str | None,
) -> str:
    audit_name = 'weekly deep health audit' if weekly else 'daily health audit'
    problems: list[str] = []
    if not gateway_ok:
        problems.append('the gateway RPC check failed')
    if not status_ok:
        problems.append('the full runtime and Discord check did not complete')
    problems.extend(dict.fromkeys(security_problems))
    if weekly and not deep_security_ok:
        problems.append('the deep security check did not complete')
    if task_maintenance_blocker:
        if task_maintenance_blocker.startswith('task_ledger_repair_incomplete'):
            problems.append('task-ledger maintenance left active residue that it could not safely settle')
        else:
            problems.append('task-ledger maintenance did not complete')

    if not problems:
        problems.append('one or more required checks did not complete')
    detail = '; '.join(problems)
    return (
        f"OpenClaw's {audit_name} needs attention: {detail}. "
        'No healthy result was recorded; inspect the affected check before relying on scheduled delivery.'
    )


MAINTENANCE_JOB_IDS = dict(OPERATOR.get('scheduler.maintenance_job_ids', {}))


def maintenance_problems(openclaw_bin: str, *, now_ms: int | None = None) -> list[str]:
    """Check scheduler RPC and daily terminal/delivery receipts, not domain effects."""
    status = run([openclaw_bin, 'cron', 'status', '--json'], timeout=45)
    payload = parse_json_object(status.output) if status.returncode == 0 else None
    if not payload or payload.get('enabled') is not True or payload.get('triggersEnabled') is False:
        return ['the scheduler is unavailable or disabled']
    listing = run([openclaw_bin, 'cron', 'list', '--all', '--json'], timeout=45)
    payload = parse_json_object(listing.output) if listing.returncode == 0 else None
    jobs = payload.get('jobs') if payload else None
    if not isinstance(jobs, list):
        return ['scheduled maintenance could not be inspected']
    indexed = {job.get('id'): job for job in jobs if isinstance(job, dict)}
    current = int(time.time() * 1000) if now_ms is None else now_ms
    problems = []
    for job_id, label in MAINTENANCE_JOB_IDS.items():
        job = indexed.get(job_id)
        if not job or job.get('enabled') is not True:
            problems.append(f'{label} is missing or disabled')
            continue
        state = job.get('state')
        if not isinstance(state, dict):
            problems.append(f'{label} has an unreadable run record')
            continue
        last = state.get('lastRunAtMs')
        if not isinstance(last, (int, float)) or isinstance(last, bool) or last > current + 5 * 60 * 1000 or current - last > 27 * 3600 * 1000:
            problems.append(f'{label} has no recent run')
        elif state.get('lastRunStatus') != 'ok':
            problems.append(f'{label} did not finish successfully')
        elif state.get('lastDeliveryStatus') != 'delivered':
            problems.append(f'{label} has no confirmed report delivery')
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Reader-facing OpenClaw health audit cron output.')
    parser.add_argument('--weekly', action='store_true', help='Use weekly/deep audit labels and run read-only security audit deep probe when available.')
    parser.add_argument('--maintenance', action='store_true', help='Also verify scheduler RPC and daily maintenance receipts.')
    args = parser.parse_args(argv)

    openclaw_bin = resolve_openclaw_bin()
    if not openclaw_bin:
        return emit(
            f"OpenClaw's {'weekly deep health audit' if args.weekly else 'daily health audit'} "
            'could not run because the local OpenClaw command was unavailable. '
            'Restore the command before the next scheduled audit.',
            exit_code=1,
        )

    blockers: list[str] = []

    gateway = run([openclaw_bin, 'gateway', 'status'], timeout=90)
    gateway_ok = gateway.returncode == 0 and gateway_status_healthy(gateway.output)
    if not gateway_ok:
        blockers.append('gateway_unreachable')

    status = run([openclaw_bin, 'status', '--deep'], timeout=180)
    status_ok = status.returncode == 0 and status_deep_healthy(status.output)
    if not status_ok:
        blockers.append('status_deep_unavailable')

    # The rendered audit inside `status --deep` carries counts and titles but no
    # checkIds, so the structured form is asked for first and the rendered one
    # is kept only as the fallback argument.
    security_audit = run([openclaw_bin, 'security', 'audit', '--json'], timeout=90)
    security_json = security_audit.output if security_audit.returncode == 0 else None
    _, security_blocker = classify_security(
        security_json,
        status.output,
    )
    security_problems: list[str] = []
    if security_blocker:
        blockers.append(security_blocker)
        security_problems.append(public_security_problem(security_json, status.output, security_blocker))

    deep_security_ok = True
    if args.weekly:
        deep_security = run([openclaw_bin, 'security', 'audit', '--deep', '--json'], timeout=180)
        if deep_security.returncode != 0:
            deep_security_ok = False
            blockers.append('security_audit_deep_unavailable')
        else:
            # The 'deep_' scope is built into the blocker rather than prefixed
            # afterwards, so the names still fit inside emit()'s 120-char trim.
            _, deep_blocker = classify_security(
                deep_security.output,
                deep_security.output,
                prefix='deep_',
            )
            if deep_blocker:
                blockers.append(deep_blocker)
                security_problems.append(public_security_problem(
                    deep_security.output, deep_security.output, deep_blocker,
                ))

    _, task_maintenance_blocker = run_task_ledger_maintenance(openclaw_bin)
    if task_maintenance_blocker:
        blockers.append(task_maintenance_blocker)

    schedule_problems = maintenance_problems(openclaw_bin) if args.maintenance else []
    if schedule_problems:
        return emit(
            f"OpenClaw's {'weekly deep health audit' if args.weekly else 'daily health audit'} needs attention: "
            + '; '.join(schedule_problems)
            + ('. Other runtime checks also need attention.' if blockers else '. Runtime checks and task-ledger maintenance passed.'),
            exit_code=1,
        )

    if blockers:
        return emit(
            public_failure_message(
                weekly=args.weekly,
                gateway_ok=gateway_ok,
                status_ok=status_ok,
                security_problems=security_problems,
                deep_security_ok=deep_security_ok,
                task_maintenance_blocker=task_maintenance_blocker,
            ),
            exit_code=1,
        )

    return emit(
        f"OpenClaw's {'weekly deep health audit' if args.weekly else 'daily health audit'} passed. "
        'The gateway and Discord are reachable, the security audit found no new unapproved issues, '
        'and task-ledger maintenance completed cleanly.'
        + (' The scheduler is active and daily maintenance runs have confirmed reports.' if args.maintenance else '')
    )


if __name__ == '__main__':
    raise SystemExit(main())
