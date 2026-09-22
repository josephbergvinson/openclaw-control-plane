from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "runtime_ci_config", Path(__file__).resolve().parents[1] / "scripts/runtime_ci_config.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeCiConfigTests(unittest.TestCase):
    def manifest(self, tag="v2026.9.5", pnpm="12.4.0"):
        return {"schemaVersion": 1, "upstream": {
            "repository": "https://github.com/openclaw/openclaw.git", "tag": tag},
            "toolchain": {"node": "24.16.0", "pnpm": pnpm}}

    def test_follows_each_package_instead_of_an_old_workflow_pin(self):
        for tag, manager in [("v2026.9.3", "12.3.4"), ("v2026.9.5", "12.4.0")]:
            with self.subTest(tag=tag):
                result = MODULE.load_ci_config(self.manifest(tag, manager))
                self.assertEqual((tag, manager), (result["tag"], result["pnpm"]))

    def test_rejects_moving_refs_output_injection_and_unreviewed_origin(self):
        for key, value in [("tag", "main"), ("tag", "v2026.9.5\nnode=0.0.0"),
                           ("repository", "https://example.invalid/runtime.git")]:
            with self.subTest(key=key, value=value):
                manifest = self.manifest()
                manifest["upstream"][key] = value
                with self.assertRaises(ValueError):
                    MODULE.load_ci_config(manifest)
        with self.assertRaises(ValueError):
            MODULE.load_ci_config(self.manifest(pnpm="latest"))


if __name__ == "__main__":
    unittest.main()
