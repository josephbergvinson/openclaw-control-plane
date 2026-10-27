from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_workspace", ROOT / "scripts/install_workspace.py")
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class WorkspaceInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.destination = self.root / "new-workspace"

    def test_dry_run_leaves_destination_absent(self):
        self.assertEqual(0, INSTALLER.main([str(self.destination), "--dry-run"]))
        self.assertFalse(self.destination.exists())

    def test_full_install_keeps_layout_and_renders_workspace(self):
        files = INSTALLER.make_plan(self.destination)
        receipt = INSTALLER.install(self.destination, files)
        self.assertFalse(receipt["services_started"])
        self.assertTrue((self.destination / "AGENTS.md").is_file())
        self.assertTrue((self.destination / "WRITING.md").is_file())
        canonical = self.destination / "runbook/tools-reference.md"
        self.assertTrue(canonical.is_file())
        self.assertFalse(canonical.is_symlink())
        self.assertEqual(0o600, canonical.stat().st_mode & 0o777)
        self.assertEqual(0o700, canonical.parent.stat().st_mode & 0o777)
        self.assertFalse((self.destination / "TOOLS.md").exists())
        self.assertLessEqual(len((self.destination / "USER.md").read_text()), 4000)
        paths = [row["path"] for row in receipt["files"]]
        self.assertEqual(1, paths.count("runbook/tools-reference.md"))
        self.assertNotIn("TOOLS.md", paths)
        self.assertTrue((self.destination / "templates/analytical-workbook.md").is_file())
        self.assertTrue((self.destination / "scripts/operator_contract.py").is_file())
        config = json.loads((self.destination / "config/openclaw.json").read_text())
        self.assertEqual(str(self.destination), config["agents"]["defaults"]["workspace"])
        self.assertEqual("openai/gpt-image-2.5-flare", config["agents"]["defaults"]["mediaModels"]["image"]["primary"])
        compaction = config["agents"]["defaults"]["compaction"]
        self.assertEqual((48000, 6, "low"),
                         (compaction["keepRecentTokens"], compaction["recentTurnsPreserve"],
                          compaction["thinkingLevel"]))
        self.assertEqual({"mode": "idle", "idleMinutes": 10080},
                         config["session"]["resetByChannel"]["discord"])
        self.assertEqual(0o600, (self.destination / "operator.json").stat().st_mode & 0o777)
        self.assertTrue((self.destination / "installation-manifest.json").is_file())

    def test_installed_policy_files_match_the_manual_backup_contract(self):
        files = INSTALLER.make_plan(self.destination)
        INSTALLER.install(self.destination, files)
        source = files[Path("scripts/openclaw_weekly_archive_backup.py")][0].decode()
        declaration = next(node for node in ast.parse(source).body
                           if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "POLICY_FILES"
                                   for target in node.targets))
        required = ast.literal_eval(declaration.value)
        self.assertIn("runbook/tools-reference.md", required)
        self.assertNotIn("TOOLS.md", required)
        for name in required:
            selected = self.destination / name
            with self.subTest(policy=name):
                self.assertTrue(selected.is_file())
                self.assertFalse(selected.is_symlink())
        self.assertTrue((self.destination / "runbook").is_dir())
        policy = json.loads(files[Path("policy_manifest.json")][0])
        self.assertEqual(["runbook/tools-reference.md"],
                         policy["authoritative_layers"]["mechanics_reference"])
        bootstrap = json.loads(files[Path("policy_bootstrap_manifest.json")][0])
        mechanics = next(row for row in bootstrap["contracts"]
                         if row["contract_role"] == "mechanics-reference")
        self.assertEqual("runbook/tools-reference.md", mechanics["path"])
        self.assertEqual("never", mechanics["bootstrap_default"])
        self.assertFalse(mechanics["recognized_by_workspace_loader"])

    def test_canonical_tools_installation_preserves_operator_path_bindings(self):
        operator = self.root / "operator.json"
        project_data = self.root / "project-data"
        legacy_state = self.root / "legacy-state"
        operator.write_text(json.dumps({"paths": {
            "workspace": str(self.destination), "project_data_root": str(project_data),
            "legacy_state_root": str(legacy_state),
        }}))
        files = INSTALLER.make_plan(self.destination, operator)
        agents = files[Path("AGENTS.md")][0].decode()
        installed_operator = json.loads(files[Path("operator.json")][0])
        self.assertEqual(str(self.destination), installed_operator["paths"]["workspace"])
        self.assertEqual(str(project_data), installed_operator["paths"]["project_data_root"])
        self.assertEqual(str(legacy_state), installed_operator["paths"]["legacy_state_root"])
        self.assertIn("](runbook/tools-reference.md)", agents)
        for name in ("AGENTS.md", "runbook/tools-reference.md"):
            body = files[Path(name)][0].decode()
            for token in ("<artifact-root>", "<legacy-state-root>", "<project-data-root>"):
                with self.subTest(policy=name, token=token):
                    self.assertNotIn(token, body)
        self.assertNotIn(Path("TOOLS.md"), files)
        self.assertFalse(self.destination.exists())

    def test_rendered_model_policy_uses_one_model_without_fallback(self):
        files = INSTALLER.make_plan(self.destination)
        config = json.loads(files[Path("config/openclaw.json")][0])
        defaults = config["agents"]["defaults"]
        model = "openai/gpt-6-astra"
        self.assertEqual({"primary": model, "fallbacks": []}, defaults["model"])
        self.assertEqual({"primary": model, "fallbacks": []}, defaults["imageModel"])
        self.assertEqual(["openai/gpt-6.1-sol", model], defaults["modelPolicy"]["allow"])
        self.assertEqual([model], list(defaults["models"]))
        self.assertEqual("xhigh", defaults["thinkingDefault"])
        self.assertEqual("xhigh", defaults["models"][model]["params"]["thinking"])
        self.assertEqual("openclaw", defaults["models"][model]["agentRuntime"]["id"])
        self.assertEqual(model, defaults["utilityModel"])
        self.assertEqual(model, defaults["heartbeat"]["model"])
        self.assertEqual("xhigh", defaults["heartbeat"]["thinking"])
        self.assertTrue(defaults["heartbeat"]["isolatedSession"])
        self.assertEqual(model, defaults["subagents"]["model"])
        self.assertEqual("xhigh", defaults["subagents"]["thinking"])
        self.assertEqual("suggest", defaults["subagents"]["delegationMode"])
        plugins = config["plugins"]["entries"]
        dreaming = plugins["memory-core"]["config"]["dreaming"]
        self.assertEqual(model, dreaming["model"])
        for phase in ("light", "deep", "rem"):
            self.assertEqual("xhigh", dreaming["phases"][phase]["execution"]["thinking"])
        active_memory = plugins["active-memory"]["config"]
        self.assertEqual(model, active_memory["model"])
        self.assertEqual("xhigh", active_memory["thinking"])

    def test_rendered_chatgpt_model_metadata_preserves_route_limits(self):
        files = INSTALLER.make_plan(self.destination)
        config = json.loads(files[Path("config/openclaw.json")][0])
        provider = config["models"]["providers"]["openai"]
        self.assertEqual("https://chatgpt.com/backend-api", provider["baseUrl"])
        self.assertEqual("openai-chatgpt-responses", provider["api"])
        self.assertNotIn("apiKey", provider)
        self.assertEqual(1, len(provider["models"]))
        model = provider["models"][0]
        self.assertEqual("gpt-6.1-sol", model["id"])
        self.assertEqual("openai-chatgpt-responses", model["api"])
        self.assertEqual(["text", "image"], model["input"])
        self.assertEqual(872000, model["contextWindow"])
        self.assertEqual(272000, model["contextTokens"])
        self.assertEqual(128000, model["maxTokens"])
        self.assertFalse(model["compat"]["supportsTemperature"])
        self.assertEqual(["low", "medium", "high", "xhigh", "max"],
                         model["compat"]["supportedReasoningEfforts"])

    def test_existing_destination_is_untouched(self):
        self.destination.mkdir()
        sentinel = self.destination / "existing.txt"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(ValueError, "already exists"):
            INSTALLER.make_plan(self.destination)
        self.assertEqual([sentinel], list(self.destination.iterdir()))
        self.assertEqual("keep", sentinel.read_text())

    def test_installed_documentation_links_stay_inside_the_installed_closure(self):
        files = INSTALLER.make_plan(self.destination)
        names = set(files)
        self.assertIn(Path("REFERENCE.md"), names)
        self.assertIn(Path("docs/README.md"), names)
        self.assertIn(Path("docs/history/README.md"), names)
        manifest_path = Path("runtime/manifest.json")
        self.assertIn(manifest_path, names)
        manifest = json.loads(files[manifest_path][0])
        self.assertIn(manifest_path.parent / manifest["patch"]["file"], names)
        self.assertIn(Path("scripts/reconstruct_runtime.py"), names)
        self.assertIn(Path("scripts/materialize_host.py"), names)
        for name, (payload, _mode) in files.items():
            if name.suffix != ".md":
                continue
            for link in re.findall(r"\]\(([^)]+)\)", payload.decode()):
                parsed = urlsplit(link)
                if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
                    continue
                target = Path(os.path.normpath(name.parent / unquote(parsed.path)))
                with self.subTest(document=str(name), link=link):
                    self.assertNotIn("..", target.parts)
                    self.assertTrue(target in files or any(target in path.parents for path in files), str(target))
        runbook = files[Path("runbooks/runtime-activation.md")][0].decode()
        self.assertIn("](../docs/17-adoption-guide.md)", runbook)
        mechanics = files[Path("runbook/tools-reference.md")][0].decode()
        self.assertIn("](../docs/16-security-and-trust-model.md#preserve-credential-bytes-within-the-resolver)", mechanics)
        self.assertIn("](../scripts)", files[Path("docs/09-source-layout-and-artifacts.md")][0].decode())

    def test_installed_reconstruction_helper_finds_its_bundled_manifest(self):
        INSTALLER.install(self.destination, INSTALLER.make_plan(self.destination))
        result = subprocess.run([sys.executable, str(self.destination / "scripts/reconstruct_runtime.py"), "--help"], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.destination / "runtime/manifest.json").is_file())

    def test_installed_calendar_lifecycle_preserves_app_ownership(self):
        INSTALLER.install(self.destination, INSTALLER.make_plan(self.destination))
        result = subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_apple_calendar_lifecycle.py"],
            cwd=self.destination, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Ran 0 tests", result.stderr)

    def test_installed_git_policy_ignores_private_state_and_keeps_source_trackable(self):
        INSTALLER.install(self.destination, INSTALLER.make_plan(self.destination))
        private = ["operator.json", "installation-manifest.json", "config/openclaw.json",
                   "artifacts/run.json", "audit/private.md", "memory/private.md", "MEMORY.md", "DREAMS.md",
                   ".env", ".env.local", "scripts/__pycache__/module.pyc", ".pytest_cache/state",
                   ".cache/result", "node_modules/example/index.js", ".venv/pyvenv.cfg"]
        for relative in private:
            target = self.destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("private fixture\n")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "GIT_CONFIG_NOSYSTEM": "1"}
        subprocess.run(["git", "init", "-q", str(self.destination)], env=env, check=True, capture_output=True)
        ignored = subprocess.run(["git", "-C", str(self.destination), "check-ignore", "--stdin"],
                                 input="\n".join(private) + "\n", env=env, capture_output=True, text=True)
        self.assertEqual(0, ignored.returncode, ignored.stderr)
        self.assertEqual(set(private), set(ignored.stdout.splitlines()))
        source = [".gitignore", "AGENTS.md", "SOUL.md", "runbook/tools-reference.md", "WRITING.md", "REFERENCE.md",
                  "scripts/operator_contract.py", "scripts/materialize_host.py", "registry/readers.json",
                  "docs/17-adoption-guide.md", "runtime/manifest.json", "config/openclaw.preferences.json",
                  "config/operator.example.json", "dependencies/requirements.txt", "host-templates.json"]
        visible = subprocess.run(["git", "-C", str(self.destination), "ls-files", "--others", "--exclude-standard"],
                                 env=env, capture_output=True, text=True, check=True)
        self.assertTrue(set(source).issubset(visible.stdout.splitlines()))
        self.assertTrue(set(private).isdisjoint(visible.stdout.splitlines()))

    def test_every_installed_file_has_deliberate_source_or_state_drift_ownership(self):
        files = INSTALLER.make_plan(self.destination)
        INSTALLER.install(self.destination, files)
        check = """import json, sys
from root_drift_state import match_rule
policy = json.load(open(sys.argv[1]))
paths = json.load(sys.stdin)
missing = []
for path in paths:
    rule = match_rule(path, policy)
    if rule is None or rule['class'] not in {'root-local', 'normalize-to-root', 'retained-state'}:
        missing.append(path)
assert not missing, missing
for path in ('operator.json', 'installation-manifest.json', 'config/openclaw.json', 'MEMORY.md', 'status/capability_status.json'):
    assert match_rule(path, policy)['class'] == 'retained-state', path
assert match_rule('scripts/unreviewed-helper.py', policy) is None
"""
        result = subprocess.run([sys.executable, "-c", check, str(self.destination / "control/root_drift_policy.json")],
                                input=json.dumps([str(path) for path in files] + ["installation-manifest.json"]),
                                capture_output=True, text=True,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.root),
                                     "PYTHONPATH": str(self.destination / "scripts"),
                                     "OPENCLAW_OPERATOR_CONFIG": str(self.destination / "operator.json")})
        self.assertEqual(0, result.returncode, result.stderr)

    def test_mismatched_operator_workspace_is_rejected(self):
        config = self.root / "operator.json"
        config.write_text(json.dumps({"paths": {"workspace": str(self.root / "another-workspace")}}))
        with self.assertRaisesRegex(ValueError, "must equal"):
            INSTALLER.make_plan(self.destination, config)
        self.assertFalse(self.destination.exists())

    def test_symlink_parent_is_rejected(self):
        link = self.root / "parent-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            INSTALLER.make_plan(link / "workspace")

    def test_unresolved_workspace_binding_is_rejected(self):
        config = self.root / "operator.json"
        config.write_text(json.dumps({"paths": {"workspace": "<workspace>"}}))
        with self.assertRaises(ValueError):
            INSTALLER.make_plan(self.destination, config)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
