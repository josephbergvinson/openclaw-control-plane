from __future__ import annotations
try:
    from scripts.operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import json
from pathlib import Path
import tempfile
import unittest

from scripts import openclaw_weekly_backup_integration_proof as proof


ROOT = Path(__file__).resolve().parents[1]


def weekly_job() -> dict:
    return {
        "id": proof.JOB_ID,
        "name": "OpenClaw Weekly Backup",
        "enabled": True,
        "schedule": {
            "kind": "cron",
            "expr": proof.WEEKLY_SCHEDULE,
            "tz": "Europe/London",
        },
        "sessionTarget": "isolated",
        "payload": {
            "kind": "agentTurn",
            "timeoutSeconds": proof.OUTER_TIMEOUT_SECONDS,
            "message": ('Run the bounded archive-v3 weekly OpenClaw OWC-local backup now.\n\nUse the exec tool exactly once with these parameters:\n- host: gateway\n- security: allowlist\n- ask: off\n- workdir: ' + str(OPERATOR.require_path('paths.workspace')) + '\n- env: {}\n- timeoutSeconds: 6900\n- yieldMs: 120000\n- command: /usr/bin/python3 ' + str(OPERATOR.require_path('paths.workspace')) + '/scripts/cron_python_entrypoint.py --receipt-dir ' + str(OPERATOR.require_path('paths.workspace')) + '/artifacts/openclaw_weekly_archive_backup/process-receipts --script ' + str(OPERATOR.require_path('paths.workspace')) + '/scripts/openclaw_weekly_archive_backup.py --what "run bounded archive-v3 weekly OpenClaw OWC-local backup"\n\nIf exec returns a background session, use process.poll on that exact session with timeout 120000 until it completes.\nAfter the command completes, deliver its final stdout exactly once.\n'),
        },
        "delivery": {
            "mode": "announce",
            "channel": "discord",
            "to": "channel:1000000000000000001",
        },
    }


class OpenClawWeeklyBackupIntegrationProofTests(unittest.TestCase):
    def test_faithful_scaled_chain_emits_all_required_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            job_json = root / "job.json"
            output_dir = root / "proof"
            job_json.write_text(json.dumps(weekly_job()), encoding="utf-8")
            result = proof.run(job_json, ROOT, output_dir)
            persisted = json.loads((output_dir / "integration-proof.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["acceptance_level"], "historical_v3_integration_harness")
        self.assertFalse(result["current_native_backup_acceptance"])
        self.assertTrue(all(result["receipts"].values()))
        self.assertTrue(result["success_scenario"]["exec_process"]["yielded"])
        self.assertGreaterEqual(result["success_scenario"]["exec_process"]["process_poll_count"], 1)
        self.assertFalse(result["success_scenario"]["manifest"]["icloud_offload"])
        self.assertEqual(
            result["success_scenario"]["manifest"]["capture_consistency"],
            "archive-self-consistent-source-tree-non-atomic-v1",
        )
        self.assertFalse(result["success_scenario"]["manifest"]["source_tree_atomic"])
        self.assertEqual(
            result["success_scenario"]["manifest"]["schema_version"],
            proof.BACKUP_MANIFEST_SCHEMA,
        )
        self.assertEqual(
            result["success_scenario"]["manifest"]["payload_contract"],
            proof.PAYLOAD_CONTRACT,
        )
        self.assertEqual(
            result["success_scenario"]["manifest"]["archive_names"],
            list(proof.EXPECTED_ARCHIVE_NAMES),
        )
        self.assertEqual(
            result["success_scenario"]["archive_names"],
            sorted(proof.EXPECTED_ARCHIVE_NAMES),
        )
        self.assertEqual(
            result["success_scenario"]["sqlite_backup"]["command_contract"],
            "openclaw backup sqlite create --json",
        )
        self.assertEqual(
            result["success_scenario"]["sqlite_backup"]["snapshot_count"],
            2,
        )
        self.assertEqual(
            result["success_scenario"]["sqlite_backup"][
                "verification_contract"
            ],
            "openclaw backup sqlite verify <snapshot> --json",
        )
        self.assertEqual(
            len(result["success_scenario"]["manifest"]["archives"]),
            3,
        )
        self.assertTrue(
            all(
                row["full_decompression"] and row["result"] == "verified"
                for row in result["success_scenario"]["manifest"][
                    "verification"
                ]
            )
        )
        self.assertEqual(
            result["success_scenario"]["fixture_bridge"][
                "production_target"
            ],
            str(ROOT / "scripts/openclaw_weekly_archive_backup.py"),
        )
        self.assertEqual(result["success_scenario"]["residual_processes"], [])
        self.assertEqual(result["timeout_scenario"]["residual_processes"], [])
        self.assertTrue(
            result["timeout_scenario"]["exec_process"]["readiness_observed"]
        )
        self.assertEqual(
            result["timeout_scenario"]["process_receipt"]["receipt_owner"],
            "supervisor",
        )
        self.assertTrue(
            result["timeout_scenario"]["process_receipt"]["termination"][
                "term_sent"
            ]
        )
        self.assertFalse(
            result["timeout_scenario"]["process_receipt"][
                "residual_process"
            ]["process_group_alive_after_cleanup"]
        )
        self.assertEqual(persisted["status"], "passed")
        self.assertEqual(persisted["false_receipts"], [])
        self.assertEqual(persisted["proof_sha256"], result["proof_sha256"])

    def test_blocked_receipt_set_is_persisted_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output_dir = Path(raw)
            candidate = {
                "schema_version": proof.PROOF_SCHEMA,
                "receipts": {
                    "job_contract": True,
                    "timeout_cleanup": False,
                },
            }
            with self.assertRaisesRegex(
                proof.ProofError,
                "integration proof receipt set is incomplete: timeout_cleanup",
            ):
                proof.finalize_proof(candidate, output_dir)
            persisted = json.loads(
                (output_dir / "integration-proof.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(persisted["status"], "blocked")
        self.assertEqual(persisted["false_receipts"], ["timeout_cleanup"])
        self.assertIn("proof_sha256", persisted)

    def test_contract_rejects_missing_nested_child_timeout(self) -> None:
        job = weekly_job()
        job["payload"]["message"] = job["payload"]["message"].replace(
            "- timeoutSeconds: 6900\n",
            "",
        )
        with self.assertRaises(proof.ProofError):
            proof.validate_job(job)

    def test_contract_rejects_retired_clone_producer(self) -> None:
        job = weekly_job()
        job["payload"]["message"] = job["payload"]["message"].replace(
            "openclaw_weekly_archive_backup.py",
            "openclaw_daily_backup_cron.py",
        )
        with self.assertRaises(proof.ProofError):
            proof.validate_job(job)

    def test_contract_rejects_icloud_or_offload_environment(self) -> None:
        job = weekly_job()
        job["payload"]["message"] = job["payload"]["message"].replace(
            "- env: {}\n",
            '- env: {"ICLOUD_OFFLOAD_CADENCE_DAYS":"7"}\n',
        )
        with self.assertRaises(proof.ProofError):
            proof.validate_job(job)

    def test_contract_rejects_archive_script_arguments(self) -> None:
        job = weekly_job()
        job["payload"]["message"] = job["payload"]["message"].replace(
            ' --what "run bounded archive-v3 weekly OpenClaw OWC-local backup"\n',
            ' --what "run bounded archive-v3 weekly OpenClaw OWC-local backup" -- --unsafe\n',
        )
        with self.assertRaises(proof.ProofError):
            proof.validate_job(job)


if __name__ == "__main__":
    unittest.main()
