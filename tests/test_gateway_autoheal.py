from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class SafeRestartContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = Path(temporary.name) / "operator.json"
        config.write_text(json.dumps({
            "runtime": {"readyz_url": "http://127.0.0.1:19001/readyz"},
            "paths": {"openclaw_cli": "/fixture/openclaw", "node_binary": "/fixture/bin/node"},
        }))
        spec = importlib.util.spec_from_file_location("autoheal_contract_test", ROOT / "workspace/scripts/gateway_autoheal_cron.py")
        assert spec and spec.loader
        self.module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"OPENCLAW_OPERATOR_CONFIG": str(config)}), patch.object(
            sys, "path", [str(ROOT / "workspace/scripts"), *sys.path],
        ):
            spec.loader.exec_module(self.module)

    def receipt(self, active=None, result="scheduled"):
        counts = {field: 0 for field in self.module.SAFE_RESTART_COUNT_FIELDS}
        counts.update(active or {})
        total = sum(counts.values())
        kinds = {"agentRuns": "agent-run", "acpRuns": "acp-run", "mediaRuns": "media-generation"}
        blockers = [{"kind": kinds[field], "count": count, "message": f"{field}: {count}"}
                    for field, count in counts.items() if count]
        return {"ok": True, "result": result, "message": "Restart accepted", "preflight": {
            "safe": total == 0, "counts": {**counts, "totalActive": total},
            "blockers": blockers, "summary": "safe" if not total else "deferred"},
            "restart": {"ok": True, "pid": 123, "signal": "SIGUSR2", "mode": "signal",
                        "coalesced": result == "coalesced", "delayMs": 0,
                        "cooldownMsApplied": 0, "emitHooksQueued": False}}

    def request(self, receipt):
        with patch.object(self.module, "run", return_value=(0, json.dumps(receipt))) as command:
            result = self.module.request_safe_restart("/example/openclaw")
        command.assert_called_once_with(["/example/openclaw", "gateway", "restart", "--json", "--safe"])
        return result[2]

    def test_accepts_current_native_signal_and_run_counts(self):
        self.assertTrue(self.request(self.receipt())["requestAccepted"])
        for field in ("agentRuns", "acpRuns", "mediaRuns"):
            with self.subTest(field=field):
                self.assertTrue(self.request(self.receipt({field: 2}, "deferred"))["requestAccepted"])

    def test_rejects_old_signal_incomplete_counts_or_inconsistent_total(self):
        cases = []
        old_signal = self.receipt()
        old_signal["restart"]["signal"] = "SIGUSR1"
        cases.append(old_signal)
        missing = self.receipt()
        missing["preflight"]["counts"].pop("mediaRuns")
        cases.append(missing)
        inconsistent = self.receipt()
        inconsistent["preflight"]["counts"]["totalActive"] = 1
        cases.append(inconsistent)
        for receipt in cases:
            with self.subTest(receipt=receipt):
                self.assertFalse(self.request(receipt)["requestAccepted"])

    def test_invalid_acknowledgment_never_replays_restart(self):
        receipt = self.receipt()
        receipt["restart"]["signal"] = "SIGUSR1"
        # Even if a future receipt changes, a completed command is not proof
        # of no effect and must not be retried to manufacture a known shape.
        with patch.object(self.module, "resolve_openclaw_bin", return_value="/example/openclaw"), \
             patch.object(self.module, "probe_health", return_value=(False, "unhealthy")), \
             patch.object(self.module, "run", return_value=(0, json.dumps(receipt))) as command, \
             patch.object(self.module, "observe_replacement") as observe, \
             patch.object(self.module, "fail", return_value=1):
            self.assertEqual(1, self.module.main())
        self.assertEqual(1, command.call_count)
        observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
