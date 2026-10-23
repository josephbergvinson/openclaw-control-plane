from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_runtime_targets.py"
SPEC = importlib.util.spec_from_file_location("runtime_workflow_targets", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeWorkflowTargetsTests(unittest.TestCase):
    def test_admits_only_native_step_paths_and_keeps_runtime_config_targets(self):
        workflow = '''
      - name: Public preparation
        run: python3 scripts/public-only.py
      - name: Native owner checks
        working-directory: ${{ runner.temp }}/openclaw-reference
        run: |
          node scripts/run-vitest.mjs --config test/vitest/owner.config.ts src/owner.test.ts src/current-view.test.tsx
          python3 "$GITHUB_WORKSPACE/scripts/public-only.py"
      - name: Another public step
        run: node scripts/another-public.mjs
      - name: Steering owner
        working-directory: ${{ runner.temp }}/openclaw-reference
        run: pnpm test src/queue.test.ts
'''
        self.assertEqual(
            {"scripts/run-vitest.mjs", "test/vitest/owner.config.ts", "src/owner.test.ts",
             "src/current-view.test.tsx", "src/queue.test.ts"},
            MODULE.runtime_targets(workflow),
        )

    def test_focused_runtime_regressions_are_actual_arguments_in_the_existing_job(self):
        workflow = (SCRIPT.parents[1] / ".github/workflows/verify-reference.yml").read_text()
        steps = re.split(r"(?=^      - name: )", workflow, flags=re.M)
        focused = next(step for step in steps
                       if re.search(r"^        id: focused-reliability$", step, re.M))
        self.assertIn("working-directory: ${{ runner.temp }}/openclaw-reference", focused)
        self.assertIn("node scripts/run-vitest.mjs", focused)
        targets = MODULE.runtime_targets(focused)
        for target in ["src/agents/mcp-oauth.fresh-token.test.ts",
                       "extensions/memory-core/src/memory-tool-contract.profile-context.test.ts",
                       "src/agents/embedded-agent-runner/run/terminal-resolution.test.ts",
                       "src/agents/embedded-agent-runner/run/attempt-trajectory-status.test.ts",
                       "src/agents/embedded-agent-runner/run/attempt-finalize.media.test.ts",
                       "src/agents/subagents/registry/subagent-delivery-state.test.ts",
                       "src/agents/tools/message-tool-execution.test.ts",
                       "src/agents/agent-command.cleanup-failure.test.ts",
                       "src/audit/execution-owner-binding.test.ts",
                       "src/infra/diagnostic-tool-execution-liveness.test.ts",
                       "src/logging/diagnostic-run-activity-owners.test.ts",
                       "src/logging/diagnostic-session-attention.test.ts",
                       "src/logging/diagnostic-stuck-session-exec-deadline.test.ts",
                       "src/agents/subagents/registry/subagent-registry-read-snapshot.test.ts",
                       "src/agents/embedded-agent-runner/run.compaction-delivery.integration.test.ts",
                       "src/gateway/agent-turn/agent-run-dispatch.owner.test.ts",
                       "src/auto-reply/reply/dispatch-from-config.continuation-settlement.native.test.ts",
                       "src/auto-reply/reply/waiting-status.delivery.test.ts",
                       "src/auto-reply/reply/followup-turn-execution.lifecycle.test.ts",
                       "extensions/telegram/src/bot-message-dispatch.progress-lifecycle.telegram-http.test.ts",
                       "src/gateway/agent-turn/agent-run-execution-phase.start-admission.test.ts"]:
            self.assertIn(target, targets)
            self.assertRegex(focused, r"(?m)^            " + re.escape(target) + r" \\$")
        self.assertIn('2>&1 | tee "$RUNNER_TEMP/reference-focused-reliability.log"', focused)

    def test_a_stale_target_or_checkout_escape_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "src").mkdir()
            (root / "src/current.test.ts").write_text("current assertion")
            self.assertEqual(
                ["src/missing.test.ts", "src/../../outside.ts"],
                sorted(MODULE.missing_targets(root, {
                    "src/current.test.ts", "src/missing.test.ts", "src/../../outside.ts",
                }), reverse=True),
            )

    def test_an_existing_symlink_to_an_outside_assertion_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            checkout = parent / "checkout"
            (checkout / "src").mkdir(parents=True)
            outside = parent / "outside.test.ts"
            outside.write_text("an assertion outside the reconstructed source")
            (checkout / "src/borrowed.test.ts").symlink_to(outside)
            self.assertEqual(
                ["src/borrowed.test.ts"],
                MODULE.missing_targets(checkout, {"src/borrowed.test.ts"}),
            )


if __name__ == "__main__":
    unittest.main()
