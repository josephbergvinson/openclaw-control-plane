from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "cron_python_entrypoint.py"


def run_entrypoint(
    target: Path,
    *script_args: str,
    receipt_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    wrapper_args = [] if receipt_dir is None else ["--receipt-dir", str(receipt_dir)]
    return subprocess.run(
        [
            sys.executable,
            str(ENTRYPOINT),
            *wrapper_args,
            "--script",
            str(target),
            "--what",
            "test cron child",
            "--cwd",
            str(target.parent),
            *script_args,
        ],
        text=True,
        capture_output=True,
    )


def start_entrypoint(
    target: Path,
    *script_args: str,
    receipt_dir: Path | None = None,
) -> subprocess.Popen[str]:
    wrapper_args = [] if receipt_dir is None else ["--receipt-dir", str(receipt_dir)]
    return subprocess.Popen(
        [
            sys.executable,
            str(ENTRYPOINT),
            *wrapper_args,
            "--script",
            str(target),
            "--what",
            "test cron child",
            "--cwd",
            str(target.parent),
            *script_args,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_child(tmp: Path, body: str) -> Path:
    path = tmp / "child.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def write_financial_failure_child(tmp: Path, *, degraded: bool) -> Path:
    """Synthetic producer fixture exercising the real wrapper's public protocol."""
    return write_child(tmp, f"""
        import json, os, sys
        from pathlib import Path
        root = Path({str(tmp)!r}) / 'outer'
        root.mkdir(mode=0o700)
        stages = [{{'stage': 'fixture_progress_' + str(i), 'status': 'passed', 'details': {{'fixture_index': i}}}} for i in range(6)]
        for i in range(6):
            print('STATUS | fixture_progress=' + str(i), file=sys.stderr)
        detail = {{'degraded': {{'budget_projection': 'partial_projection_timeout'}}}} if {degraded!r} else {{}}
        stages.append({{'stage': 'source_freshness', 'status': 'failed', 'details': detail}})
        message = 'The scheduled data refresh failed its freshness check.'
        report = {{'operator_message': message, 'status': 'failed', 'stages': stages,
                  'machine_evidence': {{'terminal_marker': 'FIXTURE_DATA_REFRESH_FAIL'}}}}
        path = root / 'stdout-fixture.json'
        path.write_text(json.dumps(report))
        path.chmod(0o600)
        if {degraded!r}:
            print('STATUS | fixture_after_early_flush', file=sys.stderr)
            print('result: failed_degraded_projection', file=sys.stderr)
            print('result: failed_degraded_projection', file=sys.stderr)
        print(message)
        print('EFFECT_PREDICATES ' + json.dumps([{{'satisfied': False, 'reason': 'financial_daily_sync_failed'}}]))
        raise SystemExit(2)
        """)


def wait_until(predicate: Callable[[], bool], timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    status = subprocess.run(
        ["/bin/ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.strip()
    return bool(status) and not status.startswith("Z")


def read_single_receipt(receipt_dir: Path) -> dict[str, Any] | None:
    paths = list(receipt_dir.glob("*.json"))
    if len(paths) != 1:
        return None
    return json.loads(paths[0].read_text(encoding="utf-8"))


def write_process_tree_child(tmp: Path) -> Path:
    return write_child(
        tmp,
        """
        import json
        import os
        from pathlib import Path
        import signal
        import subprocess
        import sys
        import time

        mode = sys.argv[2] if len(sys.argv) > 2 else "running"
        if mode == "ignore_all":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

        state_path = Path(sys.argv[1])
        descendant_ready_path = state_path.with_suffix(".descendant-ready")
        descendant_code = (
            "import signal\\n"
            "import sys\\n"
            "import time\\n"
            "from pathlib import Path\\n"
            "if sys.argv[2] in {'ignore_all', 'target_exits'}:\\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
            "Path(sys.argv[1]).write_text('ready', encoding='utf-8')\\n"
            "time.sleep(120)\\n"
        )
        descendant = subprocess.Popen(
            [
                sys.executable,
                "-c",
                descendant_code,
                str(descendant_ready_path),
                mode,
            ]
        )
        deadline = time.monotonic() + 5
        while not descendant_ready_path.is_file():
            if time.monotonic() >= deadline:
                raise RuntimeError("descendant did not become ready")
            time.sleep(0.01)
        temp_path = state_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(
                {
                    "target_pid": os.getpid(),
                    "target_pgid": os.getpgrp(),
                    "descendant_pid": descendant.pid,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temp_path, state_path)
        if mode == "target_exits":
            raise SystemExit(0)
        while True:
            time.sleep(1)
        """,
    )


class CronPythonEntrypointTests(unittest.TestCase):
    def _assert_financial_failure_forwarding(self, *, degraded: bool) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_financial_failure_child(root, degraded=degraded)
            receipt_dir = root / 'process-receipts'
            proc = run_entrypoint(child, receipt_dir=receipt_dir)
            receipt = read_single_receipt(receipt_dir)
            outer_path = root / 'outer' / 'stdout-fixture.json'
            outer = json.loads(outer_path.read_text())
            self.assertEqual(outer_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, outer['operator_message'] + '\n')
        self.assertEqual(outer['status'], 'failed')
        self.assertEqual(outer['stages'][-1]['stage'], 'source_freshness')
        self.assertEqual(outer['stages'][-1]['status'], 'failed')
        self.assertEqual(
            [row['details']['fixture_index'] for row in outer['stages'][:6]],
            list(range(6)),
        )
        self.assertEqual(outer['machine_evidence']['terminal_marker'], 'FIXTURE_DATA_REFRESH_FAIL')
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertFalse(receipt['business_effect']['satisfied'])
        self.assertEqual(receipt['business_effect']['reason'], 'financial_daily_sync_failed')
        stderr_evidence = receipt['process_output']['stderr']
        private_text = stderr_evidence['content_redacted']
        self.assertFalse(stderr_evidence['truncated'])
        self.assertLessEqual(stderr_evidence['retained_bytes'], stderr_evidence['truncation_limit_bytes'])
        for index in range(6):
            line = 'STATUS | fixture_progress=' + str(index)
            self.assertIn(line, private_text)
            self.assertNotIn(line, proc.stdout)
        if degraded:
            self.assertIn('STATUS | fixture_after_early_flush', private_text)
            self.assertEqual(private_text.count('result: failed_degraded_projection'), 2)
            self.assertEqual(
                outer['stages'][-1]['details']['degraded']['budget_projection'],
                'partial_projection_timeout',
            )

    def test_financial_buffered_failure_forwards_exact_operator_message(self) -> None:
        self._assert_financial_failure_forwarding(degraded=False)

    def test_financial_early_degraded_failure_keeps_later_diagnostics_private(self) -> None:
        self._assert_financial_failure_forwarding(degraded=True)

    def test_unrelated_stdout_is_not_hidden_by_machine_line_handling(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_child(root, "print('STATUS | unrelated checkpoint'); print('unrelated malformed-looking output'); print('EFFECT_PREDICATES not-json'); raise SystemExit(2)")
            receipts = root / 'receipts'
            proc = run_entrypoint(child, receipt_dir=receipts)
            receipt = read_single_receipt(receipts)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, 'STATUS | unrelated checkpoint\nunrelated malformed-looking output\n')
        assert receipt is not None
        self.assertIsNone(receipt['business_effect']['satisfied'])
        self.assertEqual(receipt['business_effect']['reason'], 'effect_predicate_line_unparsable')

    def test_success_child_stdout_is_preserved_and_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            child = write_child(
                Path(raw),
                """
                import sys
                print('CHILD_OK')
                raise SystemExit(0)
                """,
            )
            proc = run_entrypoint(child)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "CHILD_OK\n")
        self.assertEqual(proc.stderr, "")

    def test_failing_child_with_stdout_is_preserved_and_propagates_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            child = write_child(
                Path(raw),
                """
                import sys
                print('CHILD_FAIL_WITH_STDOUT')
                raise SystemExit(7)
                """,
            )
            proc = run_entrypoint(child)
        self.assertEqual(proc.returncode, 7)
        self.assertEqual(proc.stdout, "CHILD_FAIL_WITH_STDOUT\n")
        self.assertEqual(proc.stderr, "")

    def test_failing_child_without_stdout_emits_contract_and_propagates_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            child = write_child(
                Path(raw),
                """
                import sys
                print('bad stderr detail', file=sys.stderr)
                raise SystemExit(3)
                """,
            )
            proc = run_entrypoint(child)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("CRON_ENTRYPOINT_FAIL", proc.stdout)
        self.assertIn("result: exit_3", proc.stdout)
        self.assertIn("bad stderr detail", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_failure_receipt_retains_full_redacted_traceback_separate_from_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_child(
                root,
                """
                def fail():
                    raise RuntimeError(
                        'API_KEY=fixture-secret ' + ('x' * 600) + ' TRACEBACK_TAIL_SENTINEL'
                    )

                fail()
                """,
            )
            receipts = root / "receipts"
            proc = run_entrypoint(child, receipt_dir=receipts)
            receipt_path = next(receipts.glob("*.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt_mode = receipt_path.stat().st_mode & 0o777

        evidence = receipt["process_output"]["stderr"]
        retained = evidence["content_redacted"]
        self.assertEqual(proc.returncode, 1)
        self.assertIn("CRON_ENTRYPOINT_FAIL", proc.stdout)
        self.assertNotIn("fixture-secret", proc.stdout)
        self.assertNotIn("TRACEBACK_TAIL_SENTINEL", proc.stdout)
        self.assertEqual(receipt_mode, 0o600)
        self.assertTrue(evidence["traceback_detected"])
        self.assertTrue(evidence["full_redacted_traceback_retained"])
        self.assertFalse(evidence["truncated"])
        self.assertTrue(evidence["redaction_applied"])
        self.assertIn("Traceback (most recent call last):", retained)
        self.assertIn("API_KEY=[REDACTED]", retained)
        self.assertIn("TRACEBACK_TAIL_SENTINEL", retained)
        self.assertNotIn("fixture-secret", retained)
        self.assertEqual(
            evidence["redacted_sha256"],
            hashlib.sha256(retained.encode("utf-8")).hexdigest(),
        )

    def test_missing_child_emits_contract_and_returns_ex_noinput(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "missing.py"
            proc = run_entrypoint(missing)
        self.assertEqual(proc.returncode, getattr(os, "EX_NOINPUT", 66))
        self.assertIn("CRON_ENTRYPOINT_MISSING", proc.stdout)
        self.assertIn("result: missing_entrypoint", proc.stdout)
        self.assertIn(str(missing), proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_success_writes_process_and_residual_process_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_child(
                root,
                """
                print('CHILD_OK')
                """,
            )
            receipts = root / "receipts"
            proc = run_entrypoint(child, receipt_dir=receipts)
            receipt_paths = list(receipts.glob("*.json"))
            self.assertEqual(len(receipt_paths), 1)
            receipt = json.loads(receipt_paths[0].read_text(encoding="utf-8"))

        self.assertEqual(proc.returncode, 0)
        self.assertEqual(receipt["schema_version"], "openclaw.cron_python_entrypoint.process_receipt.v2")
        self.assertIsNone(receipt["business_effect"])
        self.assertEqual(receipt["receipt_owner"], "supervisor")
        self.assertEqual(receipt["status"], "completed")
        self.assertIn("ended_at", receipt)
        self.assertEqual(receipt["child"]["exit_code"], 0)
        self.assertNotEqual(receipt["child"]["pid"], receipt["supervisor"]["pid"])
        self.assertTrue(receipt["residual_process"]["checked"])
        self.assertFalse(receipt["residual_process"]["process_group_alive"])

    def test_process_receipt_captures_last_business_effect_predicate_line(self) -> None:
        cases = (
            (
                '[{"name":"readback","satisfied":true,"reason":"satisfied",'
                '"observed_value":"new","threshold":"new"}]',
                True,
                "satisfied",
            ),
            (
                '[{"name":"readback","satisfied":false,"reason":"readback_mismatch",'
                '"observed_value":"old","threshold":"new"}]',
                False,
                "readback_mismatch",
            ),
        )
        for payload, satisfied, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                child = write_child(root, f"print('EFFECT_PREDICATES {payload}')")
                receipts = root / "receipts"
                proc = run_entrypoint(child, receipt_dir=receipts)
                receipt = read_single_receipt(receipts)
                self.assertEqual(proc.returncode, 0)
                assert receipt is not None
                self.assertEqual(receipt["business_effect"]["satisfied"], satisfied)
                self.assertEqual(receipt["business_effect"]["reason"], reason)
                self.assertEqual(receipt["business_effect"]["predicates"][0]["name"], "readback")

    def test_forwarded_stdout_is_the_human_block_only(self) -> None:
        """EFFECT_PREDICATES is parsed into the process receipt and stripped from
        the stdout the cron job announces; an operator message survives
        verbatim and an all-machine stdout collapses to NO_REPLY."""
        payload = (
            '[{"name":"financial_daily_sync","satisfied":true,"reason":"satisfied",'
            '"observed_value":"accepted","threshold":"accepted"}]'
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_child(
                root,
                f"print('EFFECT_PREDICATES {payload}'); "
                "print('Financial — ATTENTION — 2026-09-01'); "
                "print('RBC updated; 2 unmapped token balances excluded.')",
            )
            receipts = root / "receipts"
            proc = run_entrypoint(child, receipt_dir=receipts)
            receipt = read_single_receipt(receipts)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(
            proc.stdout,
            "Financial — ATTENTION — 2026-09-01\nRBC updated; 2 unmapped token balances excluded.\n",
        )
        assert receipt is not None
        self.assertTrue(receipt["business_effect"]["satisfied"])

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_child(root, f"print('EFFECT_PREDICATES {payload}')")
            proc = run_entrypoint(child, receipt_dir=root / "receipts")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "NO_REPLY\n")

        # A failing child keeps its human block and exit code.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_child(
                root,
                "print('EFFECT_PREDICATES [{\"name\":\"x\",\"satisfied\":false,\"reason\":\"failed\"}]'); "
                "print('Financial — FAILED — 2026-09-01'); "
                "print('Nightly sync stopped at RBC authorization.'); "
                "raise SystemExit(2)",
            )
            proc = run_entrypoint(child, receipt_dir=root / "receipts")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            proc.stdout,
            "Financial — FAILED — 2026-09-01\nNightly sync stopped at RBC authorization.\n",
        )

    def test_process_receipt_preserves_malformed_effect_reason(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = write_child(root, "print('EFFECT_PREDICATES not-json')")
            receipts = root / "receipts"
            proc = run_entrypoint(child, receipt_dir=receipts)
            receipt = read_single_receipt(receipts)
            self.assertEqual(proc.returncode, 0)
            assert receipt is not None
            self.assertEqual(
                receipt["business_effect"],
                {"satisfied": None, "reason": "effect_predicate_line_unparsable"},
            )

    def test_process_receipt_captures_json_mode_business_effect(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            payload = {
                "period": "2026-08",
                "business_effect": [
                    {
                        "name": "pnl_panel_period",
                        "satisfied": False,
                        "reason": "readback_mismatch",
                        "observed_value": "2026-07",
                        "threshold": "2026-08",
                    }
                ],
            }
            child = write_child(root, f"import json; print(json.dumps({payload!r}))")
            receipts = root / "receipts"
            proc = run_entrypoint(child, receipt_dir=receipts)
            receipt = read_single_receipt(receipts)

        self.assertEqual(proc.returncode, 0)
        assert receipt is not None
        self.assertFalse(receipt["business_effect"]["satisfied"])
        self.assertEqual(receipt["business_effect"]["reason"], "readback_mismatch")
        self.assertEqual(
            receipt["business_effect"]["predicates"][0]["name"],
            "pnl_panel_period",
        )

    def test_sigterm_terminates_target_group_and_writes_truthful_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "process-tree.json"
            child = write_process_tree_child(root)
            receipts = root / "receipts"
            wrapper = start_entrypoint(child, str(state_path), receipt_dir=receipts)
            try:
                self.assertTrue(wait_until(state_path.is_file), "child process tree did not become ready")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                os.kill(wrapper.pid, signal.SIGTERM)
                stdout, stderr = wrapper.communicate(timeout=20)
                receipt = read_single_receipt(receipts)

                self.assertEqual(wrapper.returncode, 124)
                self.assertEqual(stderr, "")
                self.assertIn("CRON_ENTRYPOINT_FAIL", stdout)
                self.assertIsNotNone(receipt)
                assert receipt is not None
                self.assertEqual(receipt["receipt_owner"], "supervisor")
                self.assertEqual(receipt["status"], f"terminated_signal_{signal.SIGTERM}")
                self.assertIn("ended_at", receipt)
                self.assertEqual(receipt["child"]["exit_code"], -signal.SIGTERM)
                self.assertEqual(receipt["child"]["signal"], signal.SIGTERM)
                self.assertTrue(receipt["termination"]["term_sent"])
                self.assertFalse(receipt["termination"]["kill_sent"])
                self.assertTrue(receipt["residual_process"]["process_group_alive_before_cleanup"])
                self.assertFalse(receipt["residual_process"]["process_group_alive"])
                self.assertTrue(
                    wait_until(
                        lambda: not process_alive(state["target_pid"])
                        and not process_alive(state["descendant_pid"])
                    ),
                    "target process group survived wrapper SIGTERM",
                )
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.communicate(timeout=5)
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    try:
                        os.killpg(state["target_pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_sigkill_wrapper_watchdog_terminates_target_and_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "process-tree.json"
            child = write_process_tree_child(root)
            receipts = root / "receipts"
            wrapper = start_entrypoint(child, str(state_path), receipt_dir=receipts)
            try:
                self.assertTrue(wait_until(state_path.is_file), "child process tree did not become ready")
                self.assertTrue(
                    wait_until(
                        lambda: (read_single_receipt(receipts) or {}).get("status") == "running"
                    ),
                    "wrapper did not publish its running receipt",
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                running_receipt = read_single_receipt(receipts)
                assert running_receipt is not None
                supervisor_pid = running_receipt["supervisor"]["pid"]
                self.assertEqual(running_receipt["child"]["pid"], state["target_pid"])

                os.kill(wrapper.pid, signal.SIGKILL)
                wrapper.wait(timeout=5)

                self.assertEqual(wrapper.returncode, -signal.SIGKILL)
                self.assertTrue(
                    wait_until(
                        lambda: not process_alive(supervisor_pid)
                        and not process_alive(state["target_pid"])
                        and not process_alive(state["descendant_pid"])
                    ),
                    "parent-death watchdog left a supervised process alive",
                )
                self.assertTrue(
                    wait_until(
                        lambda: (
                            (read_single_receipt(receipts) or {}).get("status")
                            == "terminated_parent_lost"
                        )
                    ),
                    "supervisor did not terminalize the parent-loss receipt",
                )
                terminal_receipt = read_single_receipt(receipts)
                assert terminal_receipt is not None
                self.assertEqual(terminal_receipt["receipt_owner"], "supervisor")
                self.assertIn("ended_at", terminal_receipt)
                self.assertEqual(terminal_receipt["child"]["exit_code"], -signal.SIGTERM)
                self.assertEqual(terminal_receipt["child"]["signal"], signal.SIGTERM)
                self.assertEqual(
                    terminal_receipt["termination"]["reason"],
                    "parent_process_lost",
                )
                self.assertTrue(terminal_receipt["termination"]["term_sent"])
                self.assertFalse(terminal_receipt["termination"]["kill_sent"])
                self.assertTrue(
                    terminal_receipt["residual_process"]["process_group_alive_before_cleanup"]
                )
                self.assertFalse(terminal_receipt["residual_process"]["process_group_alive"])
                wrapper.communicate(timeout=5)
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.communicate(timeout=5)
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    try:
                        os.killpg(state["target_pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_sigterm_escalates_to_sigkill_for_ignoring_target_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "process-tree.json"
            child = write_process_tree_child(root)
            receipts = root / "receipts"
            wrapper = start_entrypoint(
                child,
                str(state_path),
                "ignore_all",
                receipt_dir=receipts,
            )
            try:
                self.assertTrue(wait_until(state_path.is_file), "child process tree did not become ready")
                state = json.loads(state_path.read_text(encoding="utf-8"))

                os.kill(wrapper.pid, signal.SIGTERM)
                stdout, stderr = wrapper.communicate(timeout=30)
                receipt = read_single_receipt(receipts)

                self.assertEqual(wrapper.returncode, 124)
                self.assertEqual(stderr, "")
                self.assertIn("CRON_ENTRYPOINT_FAIL", stdout)
                self.assertIsNotNone(receipt)
                assert receipt is not None
                self.assertEqual(receipt["receipt_owner"], "supervisor")
                self.assertEqual(receipt["status"], f"terminated_signal_{signal.SIGTERM}")
                self.assertIn("ended_at", receipt)
                self.assertEqual(receipt["child"]["exit_code"], -signal.SIGKILL)
                self.assertEqual(receipt["child"]["signal"], signal.SIGKILL)
                self.assertTrue(receipt["termination"]["term_sent"])
                self.assertTrue(receipt["termination"]["kill_sent"])
                self.assertFalse(receipt["residual_process"]["process_group_alive"])
                self.assertTrue(
                    wait_until(
                        lambda: not process_alive(state["target_pid"])
                        and not process_alive(state["descendant_pid"])
                    ),
                    "SIGKILL escalation left an ignoring process alive",
                )
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.communicate(timeout=5)
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    try:
                        os.killpg(state["target_pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_parent_death_during_residual_cleanup_cannot_orphan_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "process-tree.json"
            child = write_process_tree_child(root)
            receipts = root / "receipts"
            wrapper = start_entrypoint(
                child,
                str(state_path),
                "target_exits",
                receipt_dir=receipts,
            )
            try:
                self.assertTrue(wait_until(state_path.is_file), "child process tree did not become ready")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                running_receipt = read_single_receipt(receipts)
                assert running_receipt is not None
                supervisor_pid = running_receipt["supervisor"]["pid"]
                self.assertTrue(
                    wait_until(
                        lambda: not process_alive(state["target_pid"])
                        and process_alive(state["descendant_pid"])
                        and process_alive(supervisor_pid)
                    ),
                    "supervisor did not retain ownership of the residual descendant",
                )

                os.kill(wrapper.pid, signal.SIGKILL)
                wrapper.wait(timeout=5)

                self.assertEqual(wrapper.returncode, -signal.SIGKILL)
                self.assertTrue(
                    wait_until(
                        lambda: not process_alive(supervisor_pid)
                        and not process_alive(state["target_pid"])
                        and not process_alive(state["descendant_pid"]),
                        timeout_seconds=20,
                    ),
                    "wrapper death orphaned a descendant after the direct target exited",
                )
                self.assertTrue(
                    wait_until(
                        lambda: (
                            (read_single_receipt(receipts) or {}).get("status")
                            == "terminated_parent_lost"
                        )
                    ),
                    "residual cleanup did not terminalize the parent-loss receipt",
                )
                terminal_receipt = read_single_receipt(receipts)
                assert terminal_receipt is not None
                self.assertEqual(terminal_receipt["receipt_owner"], "supervisor")
                self.assertIn("ended_at", terminal_receipt)
                self.assertEqual(terminal_receipt["child"]["exit_code"], 0)
                self.assertIsNone(terminal_receipt["child"]["signal"])
                self.assertTrue(terminal_receipt["termination"]["term_sent"])
                self.assertTrue(terminal_receipt["termination"]["kill_sent"])
                self.assertTrue(
                    terminal_receipt["residual_process"]["process_group_alive_before_cleanup"]
                )
                self.assertFalse(terminal_receipt["residual_process"]["process_group_alive"])
                wrapper.communicate(timeout=5)
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.communicate(timeout=5)
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    try:
                        os.killpg(state["target_pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_receipt_claim_failure_cannot_orphan_launched_target_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_path = root / "process-tree.json"
            child = write_process_tree_child(root)
            receipt_fifo = root / "receipt.json"
            os.mkfifo(receipt_fifo)
            ready_read_fd, ready_write_fd = os.pipe()
            supervisor = subprocess.Popen(
                [
                    sys.executable,
                    str(ENTRYPOINT),
                    "--_supervise-cron-child",
                    "--parent-pid",
                    str(os.getpid()),
                    "--ready-fd",
                    str(ready_write_fd),
                    "--cwd",
                    str(root),
                    "--receipt-path",
                    str(receipt_fifo),
                    "--",
                    sys.executable,
                    str(child),
                    str(state_path),
                    "ignore_all",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                pass_fds=(ready_write_fd,),
            )
            os.close(ready_write_fd)
            try:
                self.assertTrue(
                    wait_until(state_path.is_file),
                    "target process tree did not start before receipt claim",
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                root.chmod(0o500)
                with receipt_fifo.open("w", encoding="utf-8") as handle:
                    handle.write("{}")

                stdout, stderr = supervisor.communicate(timeout=30)

                self.assertEqual(supervisor.returncode, 70)
                self.assertEqual(stdout, "")
                self.assertIn(
                    "CRON_CHILD_SUPERVISOR_RECEIPT_CLAIM_FAILED "
                    "type=PermissionError",
                    stderr,
                )
                self.assertEqual(os.read(ready_read_fd, 4096), b"")
                self.assertTrue(
                    wait_until(
                        lambda: not process_alive(state["target_pid"])
                        and not process_alive(state["descendant_pid"])
                    ),
                    "receipt claim failure left the launched target group alive",
                )
            finally:
                root.chmod(0o700)
                os.close(ready_read_fd)
                if supervisor.poll() is None:
                    try:
                        os.killpg(supervisor.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    supervisor.communicate(timeout=5)
                if state_path.is_file():
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    try:
                        os.killpg(state["target_pgid"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
