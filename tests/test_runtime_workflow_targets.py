from __future__ import annotations

import importlib.util
from pathlib import Path
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
