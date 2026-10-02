from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import openclaw_health_audit_cron as cron


def security_audit_json(findings: list[tuple[str, str, str]]) -> str:
    """Build `openclaw security audit --json` output from (checkId, severity, title).

    Shape verified against the live CLI on 2026-08-13: a top-level `summary`
    with critical/warn/info counts, and `findings[]` whose entries carry
    `checkId`, `severity`, `title`, and prose detail.
    """

    summary = {'critical': 0, 'warn': 0, 'info': 0}
    for _, severity, _ in findings:
        summary[severity] = summary.get(severity, 0) + 1
    return json.dumps(
        {
            'ts': 1786579113330,
            'summary': summary,
            'findings': [
                {'checkId': check_id, 'severity': severity, 'title': title, 'detail': 'detail text'}
                for check_id, severity, title in findings
            ],
            'secretDiagnostics': [],
        }
    )


# Synthetic accepted findings exercise exact-severity matching; these are test inputs, not installed policy.
ACCEPTED_FIVE = [
    ('config.insecure_or_dangerous_flags', 'warn', 'Insecure or dangerous config flag enabled'),
    ('tools.exec.security_full_configured', 'warn', 'Exec security=full is configured'),
    ('tools.exec.auto_allow_skills_enabled', 'warn', 'autoAllowSkills is enabled for exec approvals'),
    (
        'tools.exec.allowlist_interpreter_without_strict_inline_eval',
        'warn',
        'Interpreter allowlist entries are missing strictInlineEval hardening',
    ),
    (
        'security.trust_model.multi_user_heuristic',
        'warn',
        'Potential multi-user setup detected (personal-assistant model warning)',
    ),
]

UNENCRYPTED_VOLUME = (
    'fs.unencrypted_state_volume',
    'warn',
    'Session/state data is on an unencrypted volume',
)

CROSS_AGENT_SESSIONS = (
    'security.trust_model.cross_agent_session_access_default',
    'warn',
    'Agents share Gateway-wide session access (default)',
)

DISCORD_BROAD_MEMBERS = (
    'channels.discord.allowlisted_groups.broad_members',
    'warn',
    'Discord allowlisted groups have broad member access',
)

INFO_TWO = [
    ('summary.attack_surface', 'info', 'Attack surface summary'),
    ('gateway.tailscale_serve', 'info', 'Tailscale Serve exposure enabled'),
]

SECURITY_AUDIT_ACCEPTED = security_audit_json(ACCEPTED_FIVE + INFO_TWO)

# Synthetic additional finding tests the same policy across process boundaries.
SECURITY_AUDIT_LIVE = security_audit_json([UNENCRYPTED_VOLUME] + ACCEPTED_FIVE + INFO_TWO)

# Reproduce the alert's 0 critical / 8 warn / 3 info shape without private details.
SECURITY_AUDIT_DISCORD_BROAD_MEMBERS = security_audit_json(
    ACCEPTED_FIVE + [UNENCRYPTED_VOLUME, CROSS_AGENT_SESSIONS, DISCORD_BROAD_MEMBERS]
    + INFO_TWO + [('fixture.informational', 'info', 'Additional informational finding')]
)

COMPANY_DETAIL = (
    'These allowlisted Discord targets have no effective users or roles restriction:\n'
    '- channels.discord.guilds.222222222222222222.channels.*\n'
    '- channels.discord.guilds.222222222222222222.channels.333333333333333333\n'
    'groupPolicy="allowlist" limits guilds/channels, but all members of a listed target can still trigger the agent.'
)
COMPANY_DEFAULT_DETAIL = COMPANY_DETAIL.replace(
    'channels.discord.guilds.', 'channels.discord.accounts.default.guilds.',
)


def company_alpha_audit_json(**overrides) -> str:
    payload = json.loads(SECURITY_AUDIT_DISCORD_BROAD_MEMBERS)
    finding = next(item for item in payload['findings'] if item['checkId'] == DISCORD_BROAD_MEMBERS[0])
    finding['detail'] = COMPANY_DETAIL
    finding.update(overrides)
    payload['summary'] = cron.severity_counts(cron.parse_security_findings_json(json.dumps(payload)))
    return json.dumps(payload)


def company_alpha_config() -> dict:
    """Minimal redacted config-get fixture; no tokens, prompts or provider data."""
    return {
        'channels.discord': {
            'enabled': True,
            'groupPolicy': 'allowlist',
            'accounts': {'default': {'groupPolicy': 'allowlist'}},
            'guilds': {
                '444444444444444444': {'users': ['777777777777777777'], 'channels': {'*': {}}},
                '222222222222222222': {
                    'requireMention': True, 'ignoreOtherMentions': True,
                    'channels': {
                        '*': {'enabled': True, 'requireMention': True},
                        '333333333333333333': {},
                    },
                },
            },
        },
        'bindings': [
            {'agentId': 'company-beta', 'match': {
                'channel': 'discord', 'accountId': '*', 'guildId': '555555555555555555',
                'peer': {'kind': 'channel', 'id': '666666666666666666'},
            }},
            {'agentId': 'company-alpha', 'match': {
                'channel': 'discord', 'accountId': '*', 'guildId': '222222222222222222',
            }},
            {'agentId': 'main', 'match': {'channel': 'discord', 'accountId': '*'}},
        ],
        'agents.entries.company-alpha': {
            'workspace': '/srv/company-alpha/coordination-workspace',
            'groupChat': {'mentionPatterns': []},
            'subagents': {'allowAgents': ['company-alpha']},
            'memory': {'search': {
                'enabled': True, 'rememberAcrossConversations': False,
                'sources': ['memory'], 'extraPaths': [], 'experimental': {'sessionMemory': False},
            }},
            'tools': {
                'deny': ['sessions', 'sessions_list', 'sessions_history', 'sessions_search', 'sessions_send',
                         'conversations_list', 'conversations_send', 'conversations_turn', 'session_status'],
                'toolsBySender': {
                    'channel:discord:777777777777777777': {},
                    '*': {'allow': ['read', 'ls', 'memory_search', 'memory_get', 'web_search', 'web_fetch',
                                    'company-alpha-docs-readonly__*']},
                },
                'codeMode': False, 'fs': {'workspaceOnly': True},
                'elevated': {'enabled': True, 'allowFrom': {'discord': ['777777777777777777']}},
            },
        },
    }


def company_alpha_engineering_config() -> dict:
    config = company_alpha_config()
    senders = config['agents.entries.company-alpha']['tools']['toolsBySender']
    for sender in ('888888888888888881', '888888888888888882', '888888888888888883'):
        senders[f'channel:discord:{sender}'] = {'allow': [
            'read', 'ls', 'memory_search', 'memory_get', 'web_search', 'web_fetch',
            'company-alpha-docs-readonly__*', 'edit', 'write', 'apply_patch', 'exec',
            'process', 'sessions_spawn', 'agents_wait', 'sessions_yield', 'subagents',
        ]}
    return config


SECURITY_ONLY_OUTCOME = (
    'The operational checks passed, but the audit remains failed pending security review. '
    'Message delivery was not tested.'
)

GATEWAY_STATUS_OK = '''
Runtime: running (pid 93460, state active)
Connectivity probe: ok
Capability: read-only
'''

STATUS_DEEP_OK = '''
OpenClaw status
Gateway service │ LaunchAgent installed · loaded · running
Security audit Summary: 0 critical · 5 warn · 2 info
WARN Insecure or dangerous config flag enabled
WARN Exec security=full is configured
WARN autoAllowSkills is enabled for exec approvals
WARN Interpreter allowlist entries are missing strictInlineEval hardening
WARN Potential multi-user setup detected (personal-assistant model warning)
Health
│ Gateway │ reachable │ 26ms
│ Discord │ OK │ ok
'''

TASK_MAINTENANCE_OK = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 0},
    "taskFlows": {"reconciled": 0, "pruned": 0}
  },
  "auditAfter": {
    "total": 0,
    "errors": 0,
    "warnings": 0,
    "byCode": {"stale_running": 0, "lost": 0, "delivery_failed": 0, "missing_cleanup": 0, "inconsistent_timestamps": 0},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"stale_running": 0}}
  }
}'''

TASK_MAINTENANCE_TERMINAL_HISTORY = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 3},
    "taskFlows": {"reconciled": 0, "pruned": 21}
  },
  "auditAfter": {
    "total": 7,
    "errors": 4,
    "warnings": 3,
    "byCode": {"stale_queued": 0, "stale_running": 0, "lost": 4, "delivery_failed": 1, "missing_cleanup": 0, "inconsistent_timestamps": 2},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"restore_failed": 0, "stale_running": 0, "stale_waiting": 0, "stale_blocked": 0, "cancel_stuck": 0, "missing_linked_tasks": 0, "blocked_task_missing": 0, "inconsistent_timestamps": 0}}
  }
}'''

TASK_MAINTENANCE_BLOCKED_FLOWS = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 3},
    "taskFlows": {"reconciled": 0, "pruned": 21}
  },
  "auditAfter": {
    "total": 16,
    "errors": 4,
    "warnings": 12,
    "byCode": {"stale_queued": 0, "stale_running": 0, "lost": 4, "delivery_failed": 1, "missing_cleanup": 0, "inconsistent_timestamps": 2},
    "taskFlows": {"total": 9, "errors": 0, "warnings": 9, "byCode": {"restore_failed": 0, "stale_running": 0, "stale_waiting": 0, "stale_blocked": 9, "cancel_stuck": 0, "missing_linked_tasks": 0, "blocked_task_missing": 0, "inconsistent_timestamps": 0}}
  }
}'''

TASK_MAINTENANCE_UNKNOWN_CODE = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 0},
    "taskFlows": {"reconciled": 0, "pruned": 0}
  },
  "auditAfter": {
    "total": 2,
    "errors": 2,
    "warnings": 0,
    "byCode": {"stale_queued": 0, "stale_running": 0, "lost": 0, "delivery_failed": 0, "missing_cleanup": 0, "inconsistent_timestamps": 0, "orphaned_admission": 2},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"stale_running": 0}}
  }
}'''

TASK_MAINTENANCE_RESIDUE = '''{
  "mode": "apply",
  "maintenance": {
    "tasks": {"reconciled": 0, "recovered": 0, "cleanupStamped": 0, "pruned": 0},
    "taskFlows": {"reconciled": 0, "pruned": 0}
  },
  "auditAfter": {
    "total": 436,
    "errors": 9,
    "warnings": 427,
    "byCode": {"stale_running": 1, "lost": 8, "delivery_failed": 1, "missing_cleanup": 0, "inconsistent_timestamps": 426},
    "taskFlows": {"total": 0, "errors": 0, "warnings": 0, "byCode": {"stale_running": 0}}
  }
}'''


class OpenClawHealthAuditCronTests(unittest.TestCase):
    def assert_public_prose(self, output: str) -> None:
        self.assertTrue(output.strip())
        self.assertEqual(len(output.strip().splitlines()), 1)
        for forbidden in (
            'STATUS |',
            'OPENCLAW_HEALTH_AUDIT_',
            'Gateway Exec',
            'NO_REPLY',
            '/Users/',
            '/Volumes/',
            '⚠️',
            '🧩',
        ):
            self.assertNotIn(forbidden, output)

    def test_daily_compacts_accepted_security_findings(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertEqual(
            output,
            "OpenClaw's daily health audit passed. The gateway and Discord are reachable, "
            'the security audit found no new unapproved issues, and task-ledger maintenance completed cleanly.',
        )
        self.assert_public_prose(output)
        self.assertNotIn('WARN Insecure', output)

    def test_gateway_failure_is_a_real_reader_facing_blocker(self):
        run_results = [
            cron.CommandResult(1, 'private gateway diagnostic'),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('gateway RPC check failed', output)
        self.assertNotIn('private gateway diagnostic', output)
        self.assert_public_prose(output)

    def test_weekly_success_is_natural_prose(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main(['--weekly'])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertIn("OpenClaw's weekly deep health audit passed", output)
        self.assert_public_prose(output)

    def test_unaccepted_security_findings_alert(self):
        audit = security_audit_json(
            [('gateway.public_bind', 'critical', 'Gateway is bound to a public interface')]
            + ACCEPTED_FIVE
            + INFO_TWO
        )
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, audit),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
            cron.CommandResult(0, TASK_MAINTENANCE_OK),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn("OpenClaw's daily health audit needs attention", output)
        self.assertIn('security audit found issues requiring review', output)
        self.assertNotIn('gateway.public_bind', output)
        self.assert_public_prose(output)

    def test_discord_access_warning_keeps_daily_and_weekly_audits_failed(self):
        """Healthy operations/receipts cannot make an unaccepted warning green."""
        responses = {
            ('gateway', 'status'): GATEWAY_STATUS_OK,
            ('status', '--deep'): STATUS_DEEP_OK,
            ('security', 'audit', '--json'): SECURITY_AUDIT_DISCORD_BROAD_MEMBERS,
            ('security', 'audit', '--deep', '--json'): SECURITY_AUDIT_DISCORD_BROAD_MEMBERS,
            ('tasks', 'maintenance', '--json'): TASK_MAINTENANCE_OK,
            ('tasks', 'maintenance', '--apply', '--json'): TASK_MAINTENANCE_OK,
            ('cron', 'status', '--json'): json.dumps({'enabled': True, 'triggersEnabled': True}),
            ('cron', 'list', '--all', '--json'): json.dumps({'jobs': [
                dict(id=job_id, enabled=True, state=dict(
                    lastRunAtMs=int(cron.time.time() * 1000),
                    lastRunStatus='ok', lastDeliveryStatus='delivered',
                ))
                for job_id in cron.MAINTENANCE_JOB_IDS
            ]}),
        }

        def fake_run(command, **kwargs):
            return cron.CommandResult(0, responses[tuple(command[1:])])

        for weekly in (False, True):
            for maintenance in (False, True):
                with self.subTest(weekly=weekly, maintenance=maintenance), \
                     mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
                     mock.patch.object(cron, 'run', side_effect=fake_run) as runner:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = cron.main(
                            (['--weekly'] if weekly else []) + (['--maintenance'] if maintenance else [])
                        )
                    output = buf.getvalue().strip()
                    audit_name = 'weekly deep health audit' if weekly else 'daily health audit'
                    self.assertEqual(rc, 1)
                    self.assertEqual(
                        output,
                        f"OpenClaw's {audit_name} needs attention: the security audit found issues requiring review: "
                        'broad member access in allowlisted Discord groups (warning). ' + SECURITY_ONLY_OUTCOME,
                    )
                    self.assert_public_prose(output)
                    self.assertEqual(runner.call_count, 5 + int(weekly) + 2 * int(maintenance))

    def test_terminal_history_inside_retention_does_not_alert(self):
        """Finished rows waiting out their cleanup window are not the operator's problem."""
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_TERMINAL_HISTORY),
            cron.CommandResult(0, TASK_MAINTENANCE_TERMINAL_HISTORY),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertIn("OpenClaw's daily health audit passed", output)
        self.assertNotIn('flow_pruned', output)
        self.assertNotIn('retained_history', output)

    def test_weekly_reports_distinct_normal_and_deep_security_issues(self):
        normal = security_audit_json([
            ('models.weak_tier', 'warn', 'private model configuration'),
        ])
        deep = security_audit_json([
            (CROSS_AGENT_SESSIONS[0], 'critical', 'private account configuration'),
        ])
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=[
                 cron.CommandResult(0, GATEWAY_STATUS_OK),
                 cron.CommandResult(0, STATUS_DEEP_OK),
                 cron.CommandResult(0, normal),
                 cron.CommandResult(0, deep),
                 cron.CommandResult(0, TASK_MAINTENANCE_OK),
                 cron.CommandResult(0, TASK_MAINTENANCE_OK),
             ]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main(['--weekly'])
        self.assertEqual(rc, 1)
        self.assertIn('configured model tier (warning)', buf.getvalue())
        self.assertIn('shared access to conversations between agents (critical)', buf.getvalue())
        self.assertNotIn('private', buf.getvalue())
        self.assert_public_prose(buf.getvalue())

    def test_blocked_task_flows_are_reported_because_nothing_prunes_them(self):
        """A blocked TaskFlow is not history: it is residue that only grows.

        Maintenance prunes a flow only once its status is succeeded, failed,
        cancelled, or lost. 'blocked' is none of those, so a blocked flow is
        never pruned and its finding never ages out. Counting it as terminal
        history would hide a set that can only get larger.
        """
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_BLOCKED_FLOWS),
            cron.CommandResult(0, TASK_MAINTENANCE_BLOCKED_FLOWS),
            cron.CommandResult(0, TASK_MAINTENANCE_BLOCKED_FLOWS),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('task-ledger maintenance left active residue', output)
        # The finished task rows alongside them are still not named.
        self.assertNotIn('lost=', output)
        self.assert_public_prose(output)

    def test_an_unrecognised_finding_code_is_reported_not_dropped(self):
        """A code this script has never heard of must not pass as history."""
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_UNKNOWN_CODE),
            cron.CommandResult(0, TASK_MAINTENANCE_UNKNOWN_CODE),
            cron.CommandResult(0, TASK_MAINTENANCE_UNKNOWN_CODE),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('task-ledger maintenance left active residue', output)
        self.assertNotIn('orphaned_admission', output)

    def test_repairable_residue_after_two_apply_passes_still_alerts(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('task-ledger maintenance left active residue', output)
        self.assertNotIn('stale_running', output)
        self.assertNotIn('lost=', output)
        self.assertNotIn('inconsistent_timestamps', output)

    def test_second_apply_pass_runs_when_the_first_leaves_repairable_residue(self):
        run_results = [
            cron.CommandResult(0, GATEWAY_STATUS_OK),
            cron.CommandResult(0, STATUS_DEEP_OK),
            cron.CommandResult(0, SECURITY_AUDIT_ACCEPTED),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_RESIDUE),
            cron.CommandResult(0, TASK_MAINTENANCE_TERMINAL_HISTORY),
        ]
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
             mock.patch.object(cron, 'run', side_effect=run_results) as runner:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 0)
        self.assertIn("OpenClaw's daily health audit passed", output)
        apply_calls = [
            call for call in runner.call_args_list if '--apply' in call.args[0]
        ]
        self.assertEqual(len(apply_calls), 2)
        self.assertNotIn('flow_pruned', output)

    def test_missing_openclaw_binary_reports_prereq_missing(self):
        with mock.patch.object(cron, 'resolve_openclaw_bin', return_value=None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cron.main([])

        output = buf.getvalue().strip()
        self.assertEqual(rc, 1)
        self.assertIn('could not run because the local OpenClaw command was unavailable', output)
        self.assertNotIn('/Users/', output)
        self.assert_public_prose(output)

    def test_real_entrypoint_preserves_audit_status_and_public_output(self):
        """The scheduler's real child path must not turn a failed audit green."""
        root = Path(__file__).resolve().parents[1]
        baseline = {
            'gateway status': (0, GATEWAY_STATUS_OK),
            'status --deep': (0, STATUS_DEEP_OK),
            'security audit --json': (0, SECURITY_AUDIT_LIVE),
            'security audit --deep --json': (0, SECURITY_AUDIT_LIVE),
            'tasks maintenance --json': (0, TASK_MAINTENANCE_OK),
            'tasks maintenance --apply --json': (0, TASK_MAINTENANCE_TERMINAL_HISTORY),
        }
        cases = [
            ('accepted warnings and terminal history', None, None, 0, 'health audit passed'),
            ('accepted rendered fallback', 'security audit --json', (1, 'private diagnostic'), 0, 'health audit passed'),
            ('gateway failure', 'gateway status', (7, 'private diagnostic'), 1, 'gateway RPC check failed'),
            ('runtime timeout', 'status --deep', (124, 'private diagnostic'), 1, 'full runtime and Discord check did not complete'),
            ('maintenance failure', 'tasks maintenance --apply --json', (7, 'private diagnostic'), 1, 'task-ledger maintenance did not complete'),
            ('deep audit failure', 'security audit --deep --json', (7, 'private diagnostic'), 1, 'deep security check did not complete'),
            ('intentional shared sessions', 'security audit --json',
             (0, security_audit_json([CROSS_AGENT_SESSIONS] + ACCEPTED_FIVE)),
             0, 'health audit passed'),
            ('model tier warning', 'security audit --json',
             (0, security_audit_json([
                 ('models.weak_tier', 'warn', 'private diagnostic /Users/private-model'),
                 CROSS_AGENT_SESSIONS,
             ])), 1, 'configured model tier (warning)'),
            ('shared sessions escalation', 'security audit --json',
             (0, security_audit_json([
                 (CROSS_AGENT_SESSIONS[0], 'critical', 'private diagnostic /Users/private-agent'),
             ])), 1, 'shared access to conversations between agents (critical)'),
            ('deep model tier warning', 'security audit --deep --json',
             (0, security_audit_json([
                 ('models.weak_tier', 'warn', 'private diagnostic /Users/private-model'),
             ])), 1, 'configured model tier (warning)'),
            ('Discord member access warning', 'security audit --json',
             (0, SECURITY_AUDIT_DISCORD_BROAD_MEMBERS),
             1, 'broad member access in allowlisted Discord groups (warning)'),
            ('deep Discord member access warning', 'security audit --deep --json',
             (0, SECURITY_AUDIT_DISCORD_BROAD_MEMBERS),
             1, 'broad member access in allowlisted Discord groups (warning)'),
            ('Discord member access critical', 'security audit --json',
             (0, security_audit_json([
                 (DISCORD_BROAD_MEMBERS[0], 'critical', 'private diagnostic /Users/private-discord'),
             ])), 1, 'broad member access in allowlisted Discord groups (critical)'),
            ('unknown private finding', 'security audit --json',
             (0, security_audit_json([
                 ('private diagnostic /Users/private-account', 'critical',
                  'private diagnostic /Volumes/private-config'),
             ])), 1, 'an unrecognized security finding (critical)'),
        ]
        for weekly in (False, True):
            for label, command, replacement, expected_code, expected_text in cases:
                if command == 'security audit --deep --json' and not weekly:
                    continue
                with self.subTest(weekly=weekly, case=label), tempfile.TemporaryDirectory() as raw:
                    tmp = Path(raw)
                    responses = dict(baseline)
                    if command:
                        responses[command] = replacement
                    fake_cli = tmp / 'openclaw-fixture'
                    fake_cli.write_text(
                        '#!/usr/bin/env python3\nimport sys\n'
                        f'responses = {responses!r}\n'
                        'code, output = responses[" ".join(sys.argv[1:])]\n'
                        'if "--json" in sys.argv:\n'
                        '    print(\'healthy diagnostic phaseDurationsMs={"validation":23694}\', file=sys.stderr)\n'
                        'print(output)\nraise SystemExit(code)\n',
                        encoding='utf-8',
                    )
                    fake_cli.chmod(0o700)
                    receipts = tmp / 'receipts'
                    fixture_operator = json.loads(Path(os.environ['OPENCLAW_OPERATOR_CONFIG']).read_text())
                    fixture_operator['paths']['openclaw_cli'] = str(fake_cli)
                    fixture_operator_path = tmp / 'operator.json'
                    fixture_operator_path.write_text(json.dumps(fixture_operator))
                    proc = subprocess.run(
                        [
                            sys.executable, str(root / 'scripts/cron_python_entrypoint.py'),
                            '--script', str(root / 'scripts/openclaw_health_audit_cron.py'),
                            '--receipt-dir', str(receipts), '--cwd', str(tmp),
                            *(['--weekly'] if weekly else []),
                        ],
                        env={**os.environ, 'OPENCLAW_BIN': str(fake_cli), 'OPENCLAW_OPERATOR_CONFIG': str(fixture_operator_path)},
                        capture_output=True, text=True, timeout=20, check=False,
                    )
                    self.assertEqual(proc.returncode, expected_code, proc.stdout + proc.stderr)
                    self.assertIn(expected_text, proc.stdout)
                    self.assert_public_prose(proc.stdout)
                    self.assertNotIn('private diagnostic', proc.stdout)
                    self.assertNotIn('models.weak_tier', proc.stdout)
                    self.assertNotIn(CROSS_AGENT_SESSIONS[0], proc.stdout)
                    self.assertNotIn(DISCORD_BROAD_MEMBERS[0], proc.stdout)
                    self.assertNotIn('new or changed', proc.stdout)
                    if expected_code == 1:
                        security_only = command.startswith('security audit') and replacement[0] == 0
                        if security_only:
                            self.assertIn(SECURITY_ONLY_OUTCOME, proc.stdout)
                            self.assertNotIn('before relying on scheduled delivery', proc.stdout)
                        else:
                            self.assertNotIn(SECURITY_ONLY_OUTCOME, proc.stdout)
                            self.assertIn('No healthy result was recorded', proc.stdout)
                    self.assertEqual(proc.stderr, '')
                    receipt_paths = list(receipts.glob('*.json'))
                    self.assertEqual(len(receipt_paths), 1)
                    receipt = json.loads(receipt_paths[0].read_text(encoding='utf-8'))
                    self.assertEqual(receipt['child']['exit_code'], expected_code)
                    self.assertEqual(receipt['status'], 'completed' if expected_code == 0 else 'failed')


class ScopedCompanyAlphaAcceptanceTests(unittest.TestCase):
    def setUp(self):
        from scripts.operator_contract import OperatorContract
        policy = {
            'enabled': True,
            'owner_id': '777777777777777777',
            'guild_id': '222222222222222222',
            'channel_ids': ['*', '333333333333333333'],
            'agent_id': 'company-alpha',
            'workspace': '/srv/company-alpha/coordination-workspace',
            'engineering_sender_ids': ['888888888888888881', '888888888888888882', '888888888888888883'],
            'readonly_tool_namespace': 'company-alpha-docs-readonly__*',
        }
        self.policy = policy
        patch = mock.patch.object(cron, 'OPERATOR', OperatorContract({
            'maintenance': {'accepted_company_posture': policy},
        }))
        patch.start()
        self.addCleanup(patch.stop)

    def test_absent_or_disabled_contract_keeps_the_company_warning_blocking(self):
        from scripts.operator_contract import OperatorContract
        for policy in (None, {'enabled': False}):
            with self.subTest(policy=policy), mock.patch.object(cron, 'OPERATOR', OperatorContract({
                'maintenance': {'accepted_company_posture': policy},
            })), mock.patch.object(cron, 'run') as runner:
                self.assertFalse(cron.verify_company_posture('/bin/openclaw', company_alpha_audit_json()))
                self.assertIsNotNone(cron.classify_security(
                    company_alpha_audit_json(), '', company_posture_verified=True,
                )[1])
                runner.assert_not_called()

    def test_enabled_contract_rejects_missing_or_widened_bindings(self):
        from scripts.operator_contract import OperatorContract
        changes = [
            ('enabled', 'yes'), ('owner_id', '*'), ('guild_id', '*'),
            ('channel_ids', ['*']), ('channel_ids', ['*', '*']),
            ('agent_id', '../main'), ('workspace', '<absolute-company-workspace>'),
            ('workspace', '/srv/company-alpha/../private'),
            ('engineering_sender_ids', ['888888888888888881'] * 3),
            ('engineering_sender_ids', ['777777777777777777', '888888888888888882', '888888888888888883']),
            ('readonly_tool_namespace', '*'), ('unexpected_scope', True),
        ]
        for field, value in changes:
            policy = {**self.policy, field: value}
            with self.subTest(field=field, value=value), mock.patch.object(cron, 'OPERATOR', OperatorContract({
                'maintenance': {'accepted_company_posture': policy},
            })), mock.patch.object(cron, 'run') as runner:
                with self.assertRaises(ValueError):
                    cron.verify_company_posture('/bin/openclaw', company_alpha_audit_json())
                runner.assert_not_called()
        for field in self.policy:
            policy = dict(self.policy)
            del policy[field]
            with self.subTest(missing=field), mock.patch.object(cron, 'OPERATOR', OperatorContract({
                'maintenance': {'accepted_company_posture': policy},
            })), self.assertRaises(ValueError):
                cron.verify_company_posture('/bin/openclaw', company_alpha_audit_json())

    def test_unconditional_broad_mapping_cannot_bypass_scoped_proof(self):
        with mock.patch.dict(cron.ACCEPTED_SECURITY_FINDINGS, {
            DISCORD_BROAD_MEMBERS[0]: ('warn', DISCORD_BROAD_MEMBERS[2]),
        }):
            for output, rendered in ((company_alpha_audit_json(detail='changed target'), ''),
                                     (None, f'Security audit\nWARN {DISCORD_BROAD_MEMBERS[2]}\n')):
                self.assertIsNotNone(cron.classify_security(
                    output, rendered, company_posture_verified=True,
                )[1])

    def test_changed_broad_mapping_title_is_not_fallback_acceptance_authority(self):
        with mock.patch.dict(cron.ACCEPTED_SECURITY_FINDINGS, {
            DISCORD_BROAD_MEMBERS[0]: ('warn', 'Changed broad-member title'),
        }):
            self.assertIsNotNone(cron.classify_security(
                None, 'Security audit\nWARN Changed broad-member title\n',
                company_posture_verified=True,
            )[1])

    def test_daily_and_deep_paths_each_collect_their_own_scoped_proof(self):
        for weekly, changed_deep in ((False, False), (True, False), (True, True)):
            with self.subTest(weekly=weekly, changed_deep=changed_deep):
                config = company_alpha_engineering_config()
                reads = []
                deep_started = False
                def fake_run(command, **kwargs):
                    nonlocal deep_started
                    args = tuple(command[1:])
                    if args[:2] == ('config', 'get'):
                        self.assertEqual(kwargs, {'timeout': 15})
                        reads.append(args[2])
                        value = json.loads(json.dumps(config[args[2]]))
                        if deep_started and changed_deep and args[2] == 'channels.discord':
                            value['guilds']['222222222222222222']['ignoreOtherMentions'] = False
                        return cron.CommandResult(0, json.dumps(value))
                    if args == ('security', 'audit', '--deep', '--json'):
                        deep_started = True
                    response = {
                        ('gateway', 'status'): GATEWAY_STATUS_OK,
                        ('status', '--deep'): STATUS_DEEP_OK,
                        ('security', 'audit', '--json'): company_alpha_audit_json(),
                        ('security', 'audit', '--deep', '--json'): company_alpha_audit_json(),
                        ('tasks', 'maintenance', '--json'): TASK_MAINTENANCE_OK,
                        ('tasks', 'maintenance', '--apply', '--json'): TASK_MAINTENANCE_OK,
                    }[args]
                    return cron.CommandResult(0, response)
                with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/openclaw'), \
                     mock.patch.object(cron, 'run', side_effect=fake_run), redirect_stdout(io.StringIO()) as output:
                    rc = cron.main(['--weekly'] if weekly else [])
                self.assertEqual(rc, 1 if changed_deep else 0)
                self.assertEqual(reads, list(config) * (4 if weekly else 2))
                self.assertNotIn(COMPANY_DETAIL, output.getvalue())
                self.assertIn('needs attention' if changed_deep else 'health audit passed', output.getvalue())

    def verify(self, config=None, audit=None):
        config = company_alpha_config() if config is None else config
        audit = company_alpha_audit_json() if audit is None else audit

        def fake_run(command, **kwargs):
            self.assertEqual(command[:3], ['/bin/openclaw', 'config', 'get'])
            self.assertEqual(command[4:], ['--json'])
            self.assertEqual(kwargs, {'timeout': 15})
            return cron.CommandResult(0, json.dumps(config[command[3]]))

        with mock.patch.object(cron, 'run', side_effect=fake_run):
            return cron.verify_company_posture('/bin/openclaw', audit)

    def assert_blocked(self, config, audit=None):
        audit = company_alpha_audit_json() if audit is None else audit
        verified = self.verify(config, audit)
        self.assertFalse(verified)
        for prefix in ('', 'deep_'):
            _, blocker = cron.classify_security(audit, '', prefix=prefix, company_posture_verified=verified)
            self.assertIsNotNone(blocker)

    def test_approved_setup_is_accepted_only_with_fresh_scope_proof(self):
        audit = company_alpha_audit_json()
        self.assertTrue(self.verify())
        self.assertNotIn(DISCORD_BROAD_MEMBERS[0], cron.ACCEPTED_SECURITY_FINDINGS)
        for prefix in ('', 'deep_'):
            self.assertIsNotNone(cron.classify_security(audit, '', prefix=prefix)[1])
            self.assertEqual(
                cron.classify_security(audit, '', prefix=prefix, company_posture_verified=self.verify()),
                ('accepted_warnings_ignored=8 info=3', None),
            )

    def test_named_engineering_successor_requires_the_same_fresh_scoped_proof(self):
        config = company_alpha_engineering_config()
        for detail in (COMPANY_DETAIL, COMPANY_DEFAULT_DETAIL):
            audit = company_alpha_audit_json(detail=detail)
            verified = self.verify(config, audit)
            self.assertTrue(verified)
            for prefix in ('', 'deep_'):
                self.assertIsNotNone(cron.classify_security(audit, '', prefix=prefix)[1])
                self.assertEqual(
                    cron.classify_security(audit, '', prefix=prefix, company_posture_verified=verified),
                    ('accepted_warnings_ignored=8 info=3', None),
                )
        for changes in ({'severity': 'critical'}, {'severity': 'high'},
                        {'detail': COMPANY_DETAIL.replace('222222222222222222', '111111111111111111')}):
            self.assert_blocked(config, company_alpha_audit_json(**changes))

    def test_engineering_sender_expansion_or_owner_elevation_is_not_accepted(self):
        agent = 'agents.entries.company-alpha'
        for sender in ('888888888888888881', '888888888888888882', '888888888888888883'):
            for extra in ('browser', 'message', 'gateway', 'secrets', 'sessions_history'):
                with self.subTest(sender=sender, extra=extra):
                    config = company_alpha_engineering_config()
                    config[agent]['tools']['toolsBySender'][f'channel:discord:{sender}']['allow'].append(extra)
                    self.assert_blocked(config)
            config = company_alpha_engineering_config()
            config[agent]['tools']['toolsBySender'][f'channel:discord:{sender}'] = {}
            self.assert_blocked(config)
            config = company_alpha_engineering_config()
            config[agent]['tools']['elevated']['allowFrom']['discord'].append(sender)
            self.assert_blocked(config)
        config = company_alpha_engineering_config()
        senders = config[agent]['tools']['toolsBySender']
        senders['channel:discord:111111111111111111'] = senders.pop('channel:discord:888888888888888883')
        self.assert_blocked(config)

    def test_engineering_successor_preserves_private_and_mention_boundaries(self):
        agent = ('agents.entries.company-alpha',)
        tools = (*agent, 'tools')
        guild = ('channels.discord', 'guilds', '222222222222222222')
        for path, value in (
            ((*tools, 'toolsBySender', '*'), {'allow': ['exec', 'write']}),
            ((*tools, 'deny'), []), ((*tools, 'fs', 'workspaceOnly'), False),
            ((*agent, 'memory', 'search', 'sources'), ['memory', 'sessions']),
            ((*agent, 'subagents', 'allowAgents'), ['company-alpha', 'main']),
            ((*guild, 'requireMention'), False), ((*guild, 'ignoreOtherMentions'), False),
            ((*guild, 'channels', '*', 'autoThread'), True),
            (('channels.discord', 'accounts', 'other'), {'groupPolicy': 'allowlist'}),
            (('bindings', 1, 'agentId'), 'main'),
        ):
            with self.subTest(path=path):
                config = company_alpha_engineering_config()
                target = config
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assert_blocked(config)

    def test_same_inherited_targets_in_the_sole_default_account_are_accepted(self):
        audit = company_alpha_audit_json(detail=COMPANY_DEFAULT_DETAIL)
        self.assertTrue(self.verify(audit=audit))
        self.assertEqual(
            cron.classify_security(audit, '', company_posture_verified=self.verify(audit=audit)),
            ('accepted_warnings_ignored=8 info=3', None),
        )
        config = company_alpha_config()
        config['channels.discord']['accounts']['other'] = {'groupPolicy': 'allowlist'}
        self.assert_blocked(config, audit)

    def test_detail_is_private_and_preserved_without_trimming(self):
        finding = next(f for f in cron.parse_security_findings_json(company_alpha_audit_json())
                       if f.check_id == DISCORD_BROAD_MEMBERS[0])
        self.assertEqual(finding.detail, COMPANY_DETAIL)
        self.assertNotIn(COMPANY_DETAIL, repr(finding))
        for detail in (None, [], {}, 42):
            finding = next(f for f in cron.parse_security_findings_json(company_alpha_audit_json(detail=detail))
                           if f.check_id == DISCORD_BROAD_MEMBERS[0])
            self.assertEqual(finding.detail, '')

    def test_missing_unrecognized_or_expanded_finding_scope_never_qualifies(self):
        details = [
            '', None, [], 'detail text', COMPANY_DETAIL + '\nprivate diagnostic',
            COMPANY_DETAIL.replace('222222222222222222', '111111111111111111'),
            COMPANY_DETAIL.replace('333333333333333333', '111111111111111111'),
            COMPANY_DETAIL.replace('channels.discord.guilds', 'channels.discord.accounts.other.guilds'),
            COMPANY_DETAIL.replace('\ngroupPolicy=', '\n- channels.discord.guilds.1.channels.*\ngroupPolicy='),
            COMPANY_DETAIL.replace('- channels.discord.guilds.222222222222222222.channels.*\n', ''),
            COMPANY_DETAIL.replace('restriction:', 'restrictions:'),
        ]
        for detail in details:
            with self.subTest(detail=detail):
                audit = company_alpha_audit_json(detail=detail)
                with mock.patch.object(cron, 'run') as runner:
                    self.assertFalse(cron.verify_company_posture('/bin/openclaw', audit))
                runner.assert_not_called()
                self.assertIsNotNone(cron.classify_security(audit, '', company_posture_verified=True)[1])

    def test_higher_unknown_severity_and_impostor_id_stay_blocking(self):
        for changes in ({'severity': 'critical'}, {'severity': 'high'}, {'severity': ''},
                        {'severity': None}, {'severity': 42}, {'checkId': 'unknown.check'}):
            with self.subTest(changes=changes):
                audit = company_alpha_audit_json(**changes)
                self.assert_blocked(company_alpha_config(), audit)
                self.assertIsNotNone(cron.classify_security(audit, '', company_posture_verified=True)[1])

    def test_title_is_never_acceptance_authority_and_fallback_stays_blocking(self):
        for title in ('private changed title', None):
            self.assertIsNone(cron.classify_security(
                company_alpha_audit_json(title=title), '', company_posture_verified=self.verify(),
            )[1])
        text = f'Security audit\nSummary: 0 critical · 1 warn · 0 info\nWARN {DISCORD_BROAD_MEMBERS[2]}\n{COMPANY_DETAIL}'
        for output in (None, 'bad JSON'):
            self.assertIsNotNone(cron.classify_security(output, text, company_posture_verified=True)[1])

    def test_another_finding_is_not_absorbed_or_misdescribed(self):
        payload = json.loads(company_alpha_audit_json())
        payload['findings'].append({'checkId': 'new.check', 'severity': 'warn', 'title': 'private diagnostic'})
        payload['summary']['warn'] += 1
        audit = json.dumps(payload)
        verified = self.verify(audit=audit)
        _, blocker = cron.classify_security(audit, '', company_posture_verified=verified)
        self.assertIn('new.check', blocker)
        self.assertNotIn(DISCORD_BROAD_MEMBERS[0], blocker)
        message = cron.public_security_problem(audit, '', blocker, company_posture_verified=verified)
        self.assertIn('an unrecognized security finding', message)
        self.assertNotIn('broad member access', message)
        self.assertNotIn('private', message)

    def test_an_additional_same_check_with_different_scope_stays_blocking(self):
        payload = json.loads(company_alpha_audit_json())
        payload['findings'].append({
            'checkId': DISCORD_BROAD_MEMBERS[0], 'severity': 'warn', 'title': DISCORD_BROAD_MEMBERS[2],
            'detail': COMPANY_DETAIL.replace('222222222222222222', '111111111111111111'),
        })
        payload['summary']['warn'] += 1
        self.assertIsNotNone(cron.classify_security(json.dumps(payload), '', company_posture_verified=self.verify())[1])

    def test_relaxed_or_missing_protective_fields_fail_closed(self):
        discord = ('channels.discord',)
        guild = (*discord, 'guilds', '222222222222222222')
        agent = ('agents.entries.company-alpha',)
        search = (*agent, 'memory', 'search')
        tools = (*agent, 'tools')
        changes = [
            (discord, None), ((*discord, 'groupPolicy'), 'open'), ((*discord, 'allowBots'), True),
            ((*discord, 'accounts'), {'other': {'groupPolicy': 'allowlist'}}),
            ((*discord, 'accounts'), {'default': {'groupPolicy': 'open'}}),
            ((*discord, 'accounts'), {'default': {'groupPolicy': 'allowlist', 'guilds': {}}}),
            (guild, None), ((*guild, 'requireMention'), False), ((*guild, 'ignoreOtherMentions'), False),
            ((*guild, 'channels', '*', 'requireMention'), False),
            ((*guild, 'channels', '333333333333333333', 'requireMention'), False),
            ((*guild, 'channels', '333333333333333333', 'ignoreOtherMentions'), False),
            ((*guild, 'channels', '*', 'autoThread'), True),
            ((*guild, 'channels', '333333333333333333', 'autoThread'), True),
            ((*guild, 'channels', '111111111111111111'), {}),
            ((*discord, 'guilds', '111111111111111111'), {'channels': {'*': {}}}),
            ((*discord, 'guilds', '444444444444444444', 'channels', '*', 'users'), []),
            ((*discord, 'guilds', '444444444444444444', 'channels', '*', 'roles'), ['*']),
            (agent, None), ((*agent, 'workspace'), '/Users/private'),
            ((*agent, 'groupChat'), {'mentionPatterns': ['.*']}),
            ((*agent, 'subagents', 'allowAgents'), ['company-alpha', 'main']),
            ((*search, 'rememberAcrossConversations'), True), ((*search, 'sources'), ['memory', 'sessions']),
            ((*search, 'extraPaths'), ['/Users/private']), ((*search, 'experimental', 'sessionMemory'), True),
            (tools, None), ((*tools, 'deny'), []), ((*tools, 'codeMode'), True),
            ((*tools, 'fs', 'workspaceOnly'), False),
            ((*tools, 'toolsBySender', '*'), {}),
            ((*tools, 'toolsBySender', '*', 'allow'), ['*']),
            ((*tools, 'toolsBySender', 'id:teammate'), {}),
            ((*tools, 'elevated', 'allowFrom', 'discord'), ['*']),
            (('bindings',), []), (('bindings', 1, 'agentId'), 'main'),
            (('bindings', 1, 'match', 'accountId'), 'other'),
            (('bindings', 1, 'match', 'channel'), 'Discord'),
            (('bindings', 1, 'session'), {'groupScope': 'main'}),
        ]
        for path, value in changes:
            with self.subTest(path=path, value=value):
                config = company_alpha_config()
                target = config
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assert_blocked(config)
        for path in (guild + ('requireMention',), guild + ('ignoreOtherMentions',),
                     search + ('extraPaths',), search + ('experimental',), tools + ('toolsBySender',),
                     tools + ('codeMode',), tools + ('elevated',), tools + ('fs',), agent + ('workspace',)):
            with self.subTest(missing=path):
                config = company_alpha_config()
                target = config
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
                self.assert_blocked(config)

    def test_competing_and_duplicate_company_bindings_are_ambiguous(self):
        for match in (
            {'channel': 'discord', 'accountId': '*', 'peer': {'kind': 'channel', 'id': '*'}},
            {'channel': 'discord', 'accountId': '*', 'guildId': '222222222222222222', 'roles': ['123']},
            {'channel': 'discord', 'accountId': 'other'},
        ):
            config = company_alpha_config()
            config['bindings'].insert(0, {'agentId': 'main', 'match': match})
            self.assert_blocked(config)
        config = company_alpha_config()
        config['bindings'].append(config['bindings'][1])
        self.assert_blocked(config)

    def test_failed_malformed_or_changing_config_reads_never_accept(self):
        values = list(company_alpha_config().values())
        for position in range(6):
            for bad in (cron.CommandResult(1, 'private diagnostic'),
                        cron.CommandResult(124, 'private timeout'),
                        cron.CommandResult(0, 'not JSON'), cron.CommandResult(0, 'null')):
                with self.subTest(position=position, bad=bad):
                    results = [cron.CommandResult(0, json.dumps(value)) for value in values * 2]
                    results[position] = bad
                    with mock.patch.object(cron, 'run', side_effect=results):
                        self.assertFalse(cron.verify_company_posture('/bin/openclaw', company_alpha_audit_json()))
        # Even an individually acceptable change during collection is ambiguous.
        changed = company_alpha_config()
        changed['bindings'][1]['comment'] = 'changed during read'
        results = [cron.CommandResult(0, json.dumps(value)) for value in values + list(changed.values())]
        with mock.patch.object(cron, 'run', side_effect=results):
            self.assertFalse(cron.verify_company_posture('/bin/openclaw', company_alpha_audit_json()))


class GatewayStatusParsingTests(unittest.TestCase):
    def test_current_connectivity_probe_label_is_healthy(self) -> None:
        self.assertTrue(cron.gateway_status_healthy(GATEWAY_STATUS_OK))

    def test_rollback_rpc_probe_label_remains_healthy(self) -> None:
        self.assertTrue(
            cron.gateway_status_healthy('RPC probe: OK\nRuntime: running (pid 123, state active)')
        )

    def test_probe_without_running_runtime_is_not_healthy(self) -> None:
        self.assertFalse(cron.gateway_status_healthy('Connectivity probe: OK\nRuntime: stopped'))



class SecurityFailurePresentationTests(unittest.TestCase):
    def test_discord_warning_description_uses_only_the_known_check_id(self):
        data = json.loads(security_audit_json([
            (DISCORD_BROAD_MEMBERS[0], 'warn', 'private title /Users/private-discord'),
        ]))
        data['findings'][0].update(
            detail='private detail /Volumes/private-config',
            remediation='private remediation',
            unknownField='private unknown field',
        )
        output = json.dumps(data)
        _, blocker = cron.classify_security(output, '')
        self.assertEqual(
            cron.public_security_problem(output, '', blocker),
            'the security audit found issues requiring review: '
            'broad member access in allowlisted Discord groups (warning)',
        )

    def test_discord_rendered_title_is_recognized_but_not_accepted(self):
        for severity, label in (('warn', 'warning'), ('critical', 'critical')):
            with self.subTest(severity=severity):
                text = f'Security audit\n{severity.upper()} {DISCORD_BROAD_MEMBERS[2]}\n'
                _, blocker = cron.classify_security(None, text)
                self.assertIsNotNone(blocker)
                self.assertEqual(
                    cron.public_security_problem(None, text, blocker),
                    'the security audit found issues requiring review: '
                    f'broad member access in allowlisted Discord groups ({label})',
                )

    def test_unknown_id_or_rendered_title_does_not_borrow_the_discord_description(self):
        for json_output, text_output in (
            (security_audit_json([('private unknown id', 'warn', DISCORD_BROAD_MEMBERS[2])]), ''),
            (None, f'Security audit\nWARN {DISCORD_BROAD_MEMBERS[2]} /Users/private-account\n'),
        ):
            with self.subTest(json_output=json_output):
                _, blocker = cron.classify_security(json_output, text_output)
                self.assertEqual(
                    cron.public_security_problem(json_output, text_output, blocker),
                    'the security audit found issues requiring review: an unrecognized security finding (warning)',
                )

    def test_operational_failures_never_claim_the_operational_checks_passed(self):
        arguments = dict(
            weekly=True, gateway_ok=True, status_ok=True,
            security_problems=['the security audit found issues requiring review'],
            deep_security_ok=True, task_maintenance_blocker=None,
        )
        cases = [
            ({'gateway_ok': False}, 'gateway RPC check failed'),
            ({'status_ok': False}, 'full runtime and Discord check did not complete'),
            ({'deep_security_ok': False}, 'deep security check did not complete'),
            ({'task_maintenance_blocker': 'task_maintenance_apply=private diagnostic'},
             'task-ledger maintenance did not complete'),
            ({'task_maintenance_blocker': 'task_ledger_repair_incomplete private diagnostic'},
             'task-ledger maintenance left active residue'),
            ({'security_problems': []}, 'one or more required checks did not complete'),
        ]
        for changes, problem in cases:
            with self.subTest(changes=changes):
                message = cron.public_failure_message(**(arguments | changes))
                self.assertIn(problem, message)
                self.assertIn('No healthy result was recorded', message)
                self.assertNotIn('operational checks passed', message)
                self.assertNotIn('private diagnostic', message)

    def test_summary_mismatch_does_not_present_an_untrusted_subset(self):
        data = json.loads(security_audit_json([
            ('models.weak_tier', 'warn', 'private model configuration'),
        ]))
        data['summary']['warn'] = 2
        output = json.dumps(data)
        _, blocker = cron.classify_security(output, '')
        self.assertEqual(
            cron.public_security_problem(output, '', blocker),
            'the security audit result was incomplete',
        )

    def test_unknown_findings_remain_visible_but_never_copy_private_fields(self):
        data = json.loads(security_audit_json([
            (f'private diagnostic {index}', 'warn', '/Users/private-account')
            for index in range(6)
        ]))
        data['findings'][0]['severity'] = '/Volumes/private-severity'
        data['summary']['warn'] = 5
        output = json.dumps(data)
        _, blocker = cron.classify_security(output, '')
        message = cron.public_security_problem(output, '', blocker)
        self.assertIn('an unrecognized security finding (unrecognized severity)', message)
        self.assertIn('3 additional findings', message)
        self.assertNotIn('private', message)
        self.assertLess(len(message), 300)


class SecurityFindingsAreNamedAndAcceptedPerSeverity(unittest.TestCase):
    """
    2026-08-08 and 2026-08-10: the operator's only signal was
    "unaccepted_security_audit_findings critical=0 warn=6 info=2". The one fact
    he needed — that the volume holding session transcripts and credentials is
    unencrypted — was in the audit and not in the alert.
    """

    def classify(self, findings, **kwargs):
        return cron.classify_security(security_audit_json(findings), '', **kwargs)

    def test_a_new_unaccepted_warning_is_named(self):
        new = ('gateway.auth_token_missing', 'warn', 'Gateway is reachable without an auth token')
        note, blocker = self.classify([new] + ACCEPTED_FIVE + [UNENCRYPTED_VOLUME] + INFO_TWO)
        self.assertIsNotNone(blocker)
        self.assertIn('gateway.auth_token_missing', blocker)
        self.assertIn('warn=7', blocker)
        self.assertIn('critical=0 warn=7 info=2', note)

    def test_discord_warning_remains_unaccepted_in_ordinary_and_deep_audits(self):
        findings = cron.parse_security_findings_json(SECURITY_AUDIT_DISCORD_BROAD_MEMBERS)
        self.assertEqual(len(findings), 11)
        self.assertEqual(
            cron.unaccepted_security_findings(findings),
            [cron.SecurityFinding(*DISCORD_BROAD_MEMBERS, detail='detail text')],
        )
        self.assertNotIn(DISCORD_BROAD_MEMBERS[0], cron.ACCEPTED_SECURITY_FINDINGS)
        for prefix in ('', 'deep_'):
            with self.subTest(prefix=prefix):
                note, blocker = cron.classify_security(SECURITY_AUDIT_DISCORD_BROAD_MEMBERS, '', prefix=prefix)
                self.assertEqual(note, 'critical=0 warn=8 info=3 source=json')
                self.assertEqual(
                    blocker,
                    f'{prefix}unaccepted_security_audit_findings critical=0 warn=8 {DISCORD_BROAD_MEMBERS[0]}',
                )

    def test_explicit_fixture_volume_warning_acceptance_is_severity_bound(self):
        """Authorised 2026-07-23 in the OWC primary-storage amendment."""
        note, blocker = self.classify([UNENCRYPTED_VOLUME] + ACCEPTED_FIVE + INFO_TWO)
        self.assertIsNone(blocker)
        self.assertEqual(note, 'accepted_warnings_ignored=6 info=2')

    def test_fixture_shared_access_warning_is_accepted_only_at_warn(self):
        note, blocker = self.classify([CROSS_AGENT_SESSIONS] + ACCEPTED_FIVE)
        self.assertIsNone(blocker)
        self.assertEqual(note, 'accepted_warnings_ignored=6 info=0')
        for severity in ('critical', 'high'):
            with self.subTest(severity=severity):
                findings = [cron.SecurityFinding(
                    CROSS_AGENT_SESSIONS[0], severity, CROSS_AGENT_SESSIONS[2],
                )]
                self.assertEqual(cron.unaccepted_security_findings(findings), findings)

    def test_cross_agent_acceptance_does_not_absorb_a_model_tier_warning(self):
        _, blocker = self.classify([
            CROSS_AGENT_SESSIONS,
            ('models.weak_tier', 'warn', 'Some configured models are below recommended tiers'),
        ])
        self.assertIsNotNone(blocker)
        self.assertIn('models.weak_tier', blocker)
        self.assertNotIn(CROSS_AGENT_SESSIONS[0], blocker)

    def test_cross_agent_rendered_acceptance_keeps_severity_boundary(self):
        for severity, accepted in (('WARN', True), ('CRITICAL', False)):
            with self.subTest(severity=severity):
                _, blocker = cron.classify_security(
                    None, f'Security audit\n{severity} {CROSS_AGENT_SESSIONS[2]}\n',
                )
                self.assertEqual(blocker is None, accepted)

    def test_an_accepted_title_demoted_to_info_cannot_absorb_a_new_warning(self):
        """The defect that made a new warning invisible.

        Five accepted titles are present in the output, one of them at info.
        The old code counted accepted titles anywhere at any severity (5) and
        compared that against the warn total (5), concluded everything was
        ruled on, and reported nothing — while a genuinely new warning sat in
        that warn total.
        """
        demoted = ('tools.exec.security_full_configured', 'info', 'Exec security=full is configured')
        new = ('fs.world_readable_config', 'warn', 'Config file is world-readable')
        findings = [demoted, new] + [f for f in ACCEPTED_FIVE if f[0] != demoted[0]]

        self.assertEqual(sum(1 for f in findings if f[1] == 'warn'), 5)
        self.assertEqual(
            sum(1 for f in findings if f[0] in cron.ACCEPTED_SECURITY_FINDINGS),
            5,
            'the old accepted-title count must equal the warn total, or this test does not reproduce the bug',
        )

        note, blocker = self.classify(findings)
        self.assertIsNotNone(blocker)
        self.assertIn('fs.world_readable_config', blocker)
        self.assertNotIn('tools.exec.security_full_configured', blocker)

    def test_an_accepted_finding_escalated_to_critical_is_still_reported(self):
        escalated = (
            'fs.unencrypted_state_volume',
            'critical',
            'Session/state data is on an unencrypted volume',
        )
        note, blocker = self.classify([escalated] + ACCEPTED_FIVE + INFO_TWO)
        self.assertIsNotNone(blocker)
        self.assertIn('critical=1', blocker)
        self.assertIn('fs.unencrypted_state_volume', blocker)

    def test_an_accepted_check_id_survives_rendered_title_drift(self):
        drifted = (
            'config.insecure_or_dangerous_flags',
            'warn',
            'Insecure or dangerous configuration is enabled',
        )
        note, blocker = self.classify(
            [drifted] + [finding for finding in ACCEPTED_FIVE if finding[0] != drifted[0]] + INFO_TWO
        )
        self.assertIsNone(blocker)
        self.assertEqual(note, 'accepted_warnings_ignored=5 info=2')

    def test_an_accepted_title_with_a_different_check_id_is_not_suppressed(self):
        impostor = (
            'gateway.unrelated_warning',
            'warn',
            'Exec security=full is configured',
        )
        _, blocker = self.classify([impostor] + INFO_TWO)
        self.assertIsNotNone(blocker)
        self.assertIn('gateway.unrelated_warning', blocker)

    def test_a_finding_at_a_severity_this_script_never_heard_of_is_not_dropped(self):
        """A new severity band must not read as "nothing bad".

        The severity vocabulary belongs to the CLI, not to this script. If the
        audit grows a band between "warn" and "critical", a parser that only
        knows critical/warn/info counts zero of everything it recognises and
        reports the run healthy — the same silence the 08-08 and 08-10 alerts
        were about, with the finding removed entirely instead of unnamed.
        """
        payload = {
            'summary': {'critical': 0, 'warn': 0, 'info': 0},
            'findings': [
                {
                    'checkId': 'gateway.creds_world_readable',
                    'severity': 'high',
                    'title': 'Gateway credentials are world-readable',
                }
            ],
        }
        note, blocker = cron.classify_security(json.dumps(payload), '')
        self.assertIsNotNone(blocker)
        self.assertIn('gateway.creds_world_readable', blocker)
        # Counted apart from warn so "critical=0 warn=0" cannot read as clean.
        self.assertIn('other=1', blocker)
        self.assertIn('other=1', note)

    def test_a_summary_band_with_no_parsed_findings_blocks_the_run(self):
        """The audit reporting a band this parser produced nothing for."""
        payload = {'summary': {'critical': 0, 'warn': 0, 'info': 0, 'high': 2}, 'findings': []}
        note, blocker = cron.classify_security(json.dumps(payload), '')
        self.assertIsNotNone(blocker)
        self.assertIn('security_findings_incomplete', blocker)
        self.assertIn('high:summary=2,parsed=0', blocker)

    def test_six_unaccepted_warnings_name_what_fits_and_count_the_rest(self):
        """emit() trims a blocker to 120 chars; the line has to stay self-describing."""
        findings = [UNENCRYPTED_VOLUME] + ACCEPTED_FIVE
        with mock.patch.object(cron, 'ACCEPTED_SECURITY_FINDINGS', {}):
            _, blocker = self.classify(findings + INFO_TWO)

        self.assertLessEqual(len(blocker), 120)
        self.assertEqual(blocker, cron.trim(blocker, 120), 'blocker must survive emit() untouched')
        self.assertEqual(
            blocker,
            'unaccepted_security_audit_findings critical=0 warn=6 '
            'fs.unencrypted_state_volume config.insecure_or_dangerous_flags +4',
        )

    def test_the_weekly_deep_scope_fits_the_same_budget(self):
        findings = [UNENCRYPTED_VOLUME] + ACCEPTED_FIVE
        with mock.patch.object(cron, 'ACCEPTED_SECURITY_FINDINGS', {}):
            _, blocker = self.classify(findings, prefix='deep_')

        self.assertTrue(blocker.startswith('deep_unaccepted_security_audit_findings'))
        self.assertLessEqual(len(blocker), 120)

    def test_criticals_are_named_before_warnings(self):
        critical = ('gateway.public_bind', 'critical', 'Gateway is bound to a public interface')
        with mock.patch.object(cron, 'ACCEPTED_SECURITY_FINDINGS', {}):
            _, blocker = self.classify(ACCEPTED_FIVE + [critical])

        names = blocker.split('warn=5 ', 1)[1]
        self.assertTrue(names.startswith('gateway.public_bind'), names)

    def test_falls_back_to_rendered_titles_when_json_is_unavailable(self):
        """No JSON must mean fewer details, never silence."""
        note, blocker = cron.classify_security(None, STATUS_DEEP_OK)
        self.assertIsNone(blocker)
        # The degraded source is stated rather than passed off as the JSON audit.
        self.assertEqual(note, 'accepted_warnings_ignored=5 info=2 source=status_text')

        text = STATUS_DEEP_OK.replace(
            '0 critical · 5 warn · 2 info', '0 critical · 6 warn · 2 info'
        ).replace(
            'WARN Insecure or dangerous config flag enabled',
            'WARN Insecure or dangerous config flag enabled\nWARN Gateway is reachable without an auth token',
        )
        note, blocker = cron.classify_security(None, text)
        self.assertIsNotNone(blocker)
        self.assertIn('Gateway is reachable without an auth token', blocker)

    def test_a_findings_list_that_disagrees_with_the_summary_is_not_trusted(self):
        """A subset we happen to understand must not read as the whole audit."""
        payload = json.loads(security_audit_json(ACCEPTED_FIVE + INFO_TWO))
        payload['summary']['warn'] = 6
        note, blocker = cron.classify_security(json.dumps(payload), '')
        self.assertIsNotNone(blocker)
        self.assertIn('security_findings_incomplete', blocker)

    def test_an_unusable_audit_is_reported_rather_than_passed(self):
        note, blocker = cron.classify_security(None, 'gateway is fine, nothing to see')
        self.assertEqual(note, 'not_reported')
        self.assertEqual(blocker, 'security_findings_unavailable')


class JsonCommandFramingTests(unittest.TestCase):
    # The natural 2026-09-29 audit appended this diagnostic shape to valid
    # maintenance JSON. Its nested object made the combined stream unparseable.
    diagnostic = (
        '[state/agent-db] slow OpenClaw agent database open '
        'phaseDurationsMs={"open":0,"validation":23694} '
        'integrityGateOutcome=healthy integrityGateMs=23677'
    )

    def test_maintenance_reads_json_stdout_without_structured_stderr(self):
        with tempfile.TemporaryDirectory() as raw:
            fake_cli = Path(raw) / 'openclaw-fixture'
            fake_cli.write_text(
                '#!/usr/bin/env python3\nimport sys\n'
                f'print({TASK_MAINTENANCE_OK!r})\n'
                f'print({self.diagnostic!r}, file=sys.stderr)\n',
                encoding='utf-8',
            )
            fake_cli.chmod(0o700)
            _, blocker = cron.run_task_ledger_maintenance(str(fake_cli))
        self.assertIsNone(blocker)

    def test_stderr_cannot_supply_missing_or_invalid_json_stdout(self):
        for stdout in ('', 'not JSON'):
            with self.subTest(stdout=stdout):
                result = cron.run([
                    sys.executable, '-c',
                    f'import sys; print({stdout!r}); print({TASK_MAINTENANCE_OK!r}, file=sys.stderr)',
                    '--json',
                ])
                self.assertEqual(result.returncode, 0)
                self.assertIsNone(cron.parse_json_object(result.output))

    def test_failed_json_command_keeps_diagnostics_and_failure(self):
        result = cron.run([
            sys.executable, '-c',
            f'import sys; print({TASK_MAINTENANCE_OK!r}); print("required write failed", file=sys.stderr); sys.exit(7)',
            '--json',
        ])
        self.assertEqual(result.returncode, 7)
        self.assertIn('required write failed', result.output)

    def test_text_command_keeps_both_streams(self):
        result = cron.run([
            sys.executable, '-c',
            'import sys; print("runtime status"); print("diagnostic", file=sys.stderr)',
        ])
        self.assertEqual(result.output, 'runtime status\ndiagnostic')


class RunTimeoutIsAFailureNotACrash(unittest.TestCase):
    """
    2026-08-12: an unguarded subprocess.TimeoutExpired propagated out of run(),
    killed the script with a traceback (exit 125), and cron recorded
    lastRunStatus=ok / consecutiveErrors=0. A run in which ZERO health checks
    executed was indistinguishable from a clean one. Same crash took the weekly
    on 2026-08-09.
    """

    def test_a_timed_out_command_reports_failure(self) -> None:
        result = cron.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=2,
        )
        self.assertEqual(result.returncode, cron.COMMAND_TIMEOUT_RETURNCODE)
        self.assertIn("timed out after 2s", result.output)

    def test_a_normal_command_is_unaffected(self) -> None:
        result = cron.run([sys.executable, "-c", "print('fine')"], timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.output, "fine")

if __name__ == '__main__':
    unittest.main()


def test_maintenance_receipts_reject_silent_success_and_stale_runs():
    now = 100_000_000
    jobs = [dict(id=job_id, enabled=True, state=dict(lastRunAtMs=now, lastRunStatus="ok", lastDeliveryStatus="delivered")) for job_id in cron.MAINTENANCE_JOB_IDS]
    jobs[0]["state"]["lastDeliveryStatus"] = "not-delivered"
    jobs[1]["state"]["lastRunAtMs"] = 1
    with mock.patch.object(cron, "run", side_effect=[cron.CommandResult(0, '{"enabled": true}'), cron.CommandResult(0, json.dumps({"jobs": jobs}))]):
        problems = cron.maintenance_problems("fixture", now_ms=now)
    assert problems == ["daily retention has no confirmed report delivery", "workspace cleanup has no recent run"]


def test_maintenance_scheduler_disabled_is_not_healthy():
    with mock.patch.object(cron, "run", return_value=cron.CommandResult(0, '{"enabled": false}')) as runner:
        assert cron.maintenance_problems("fixture") == ["the scheduler is unavailable or disabled"]
    assert runner.call_count == 1


def test_maintenance_receipts_reject_future_timestamp_and_malformed_state():
    now = 100_000_000
    jobs = [dict(id=job_id, enabled=True, state=dict(lastRunAtMs=now, lastRunStatus="ok", lastDeliveryStatus="delivered")) for job_id in cron.MAINTENANCE_JOB_IDS]
    jobs[0]["state"]["lastRunAtMs"] = now + 301_000
    jobs[1]["state"] = ["broken"]
    with mock.patch.object(cron, "run", side_effect=[cron.CommandResult(0, '{"enabled": true}'), cron.CommandResult(0, json.dumps({"jobs": jobs}))]):
        assert cron.maintenance_problems("fixture", now_ms=now) == ["daily retention has no recent run", "workspace cleanup has an unreadable run record"]

def test_maintenance_headroom_nonzero_result_does_not_claim_noncompletion():
    """A completed capacity warning and execution failure both remain alerts."""
    now = 100_000_000
    for diagnostic in (
        'Storage needs attention: internal disk 27.8 GiB free (warning). Safe cleanup completed.',
        'private execution diagnostic token=fixture-secret',
    ):
        jobs = [dict(id=job_id, enabled=True, state=dict(
            lastRunAtMs=now, lastRunStatus='ok', lastDeliveryStatus='delivered',
        )) for job_id in cron.MAINTENANCE_JOB_IDS]
        jobs[0]['state']['lastRunStatus'] = 'error'
        headroom = next(job for job in jobs if cron.MAINTENANCE_JOB_IDS[job['id']] == 'storage headroom')
        headroom['state'].update(lastRunStatus='error', lastDiagnosticSummary=diagnostic)
        results = [cron.CommandResult(0, '{"enabled": true}'),
                   cron.CommandResult(0, json.dumps({'jobs': jobs}))]
        with mock.patch.object(cron, 'run', side_effect=results) as runner:
            problems = cron.maintenance_problems('fixture', now_ms=now)
        assert problems == ['daily retention did not finish successfully', 'storage headroom needs attention']
        assert runner.call_count == 2
        assert diagnostic not in '; '.join(problems)


def test_headroom_attention_keeps_daily_and_weekly_health_audits_failed():
    """The wording correction keeps the failing health result and success noise."""
    for weekly in (False, True):
        for headroom_status in ('error', 'ok'):
            jobs = [dict(id=job_id, enabled=True, state=dict(
                lastRunAtMs=int(cron.time.time() * 1000),
                lastRunStatus='ok', lastDeliveryStatus='delivered',
            )) for job_id in cron.MAINTENANCE_JOB_IDS]
            headroom = next(job for job in jobs if cron.MAINTENANCE_JOB_IDS[job['id']] == 'storage headroom')
            headroom['state'].update(
                lastRunStatus=headroom_status,
                lastDiagnosticSummary='private diagnostic token=fixture-secret',
            )
            responses = {
                ('gateway', 'status'): GATEWAY_STATUS_OK,
                ('status', '--deep'): STATUS_DEEP_OK,
                ('security', 'audit', '--json'): SECURITY_AUDIT_ACCEPTED,
                ('security', 'audit', '--deep', '--json'): SECURITY_AUDIT_ACCEPTED,
                ('tasks', 'maintenance', '--json'): TASK_MAINTENANCE_OK,
                ('tasks', 'maintenance', '--apply', '--json'): TASK_MAINTENANCE_OK,
                ('cron', 'status', '--json'): json.dumps({'enabled': True, 'triggersEnabled': True}),
                ('cron', 'list', '--all', '--json'): json.dumps({'jobs': jobs}),
            }

            def fake_run(command, **kwargs):
                return cron.CommandResult(0, responses[tuple(command[1:])])

            with mock.patch.object(cron, 'resolve_openclaw_bin', return_value='/bin/fixture-openclaw'), \
                 mock.patch.object(cron, 'run', side_effect=fake_run) as runner:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cron.main((['--weekly'] if weekly else []) + ['--maintenance'])
            output = buf.getvalue().strip()
            audit_name = 'weekly deep health audit' if weekly else 'daily health audit'
            if headroom_status == 'error':
                assert rc == 1
                assert output == (
                    f"OpenClaw's {audit_name} needs attention: storage headroom needs attention. "
                    'Runtime checks and task-ledger maintenance passed.'
                )
            else:
                assert rc == 0
                assert output.startswith(f"OpenClaw's {audit_name} passed.")
                assert 'needs attention' not in output
            assert 'private diagnostic' not in output
            assert 'fixture-secret' not in output
            assert runner.call_count == 7 + int(weekly)
