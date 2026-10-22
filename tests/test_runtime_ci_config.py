from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "runtime_ci_config", Path(__file__).resolve().parents[1] / "scripts/runtime_ci_config.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RuntimeCiConfigTests(unittest.TestCase):
    def manifest(self, tag="v2026.9.5", pnpm="12.4.0", node="24.22.1"):
        return {"schemaVersion": 1, "upstream": {
            "repository": "https://github.com/openclaw/openclaw.git", "tag": tag},
            "toolchain": {"node": node, "pnpm": pnpm}}

    def test_follows_each_package_instead_of_an_old_workflow_pin(self):
        for tag, manager, node in [("v2026.9.3", "12.3.4", "24.16.0"),
                                   ("v2026.9.5", "12.4.0", "24.22.1")]:
            with self.subTest(tag=tag, node=node):
                result = MODULE.load_ci_config(self.manifest(tag, manager, node))
                self.assertEqual((tag, manager, node),
                                 (result["tag"], result["pnpm"], result["node"]))

    def test_published_toolchain_uses_fixed_node_and_preserves_engine_floor(self):
        path = Path(__file__).resolve().parents[1] / "runtime/manifest.json"
        manifest = json.loads(path.read_text())
        self.assertEqual(MODULE.load_ci_config(manifest)["node"], "24.22.1")
        self.assertEqual(manifest["toolchain"]["nodeEngineRange"],
                         ">=24.16.0 <25 || >=26.1.0")

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
