from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuntimeEnvironmentTemplateTests(unittest.TestCase):
    def test_template_renders_with_the_native_operator_contract(self):
        spec = importlib.util.spec_from_file_location('startup_operator_contract',
            ROOT / 'workspace/scripts/operator_contract.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = (ROOT / 'workspace/launchd/runtime-environment.plist.template.json').read_text()
        paths = {key: '/fixture/' + key for key in re.findall(r'\$\{operator:paths\.([^}]+)\}', source)}
        result = module.OperatorContract({'paths': paths}).render(json.loads(source))
        self.assertEqual('/fixture/state_root', result['EnvironmentVariables']['OPENCLAW_STATE_DIR'])
        self.assertNotIn('${operator:', json.dumps(result))


@unittest.skipUnless(shutil.which('zsh'), 'runtime loader uses the macOS zsh contract')
class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'external state'
        self.state.mkdir()
        (self.state / 'openclaw.json').write_text('{}')
        self.calls = self.root / 'calls.jsonl'
        self.observed = self.root / 'app.json'
        self.native = self.root / 'launchctl-fixture'
        self.native.write_text('#!' + sys.executable + '\n' +
            'import json, os, sys\n' +
            'with open(os.environ["FIXTURE_CALLS"], "a") as f: f.write(json.dumps(sys.argv[1:]) + "\\n")\n' +
            'sys.exit(int(os.environ.get("FIXTURE_NATIVE_EXIT", "0")))\n')
        self.make_executable(self.native)
        self.app = self.root / 'app-fixture'
        self.app.write_text('#!' + sys.executable + '\n' +
            'import json, os, pathlib, sys\n' +
            'data = {"args": sys.argv[1:], "env": {k:v for k,v in os.environ.items() if k.startswith("OPENCLAW_")}, '
            '"defaultMarker": (pathlib.Path(os.environ["HOME"]) / ".openclaw/disable-launchagent").is_file(), '
            '"canonicalMarker": (pathlib.Path(os.environ["OPENCLAW_STATE_DIR"]) / "disable-launchagent").is_file()}\n' +
            'pathlib.Path(os.environ["FIXTURE_OBSERVED"]).write_text(json.dumps(data))\n')
        self.make_executable(self.app)
        source = (ROOT / 'workspace/scripts/runtime-environment.zsh').read_text()
        # Replace only the OS boundary, never execute real launchctl in fixtures.
        self.loader = self.root / 'loader.zsh'
        self.loader.write_text(source.replace('/bin/launchctl', shlex.quote(str(self.native))))
        self.env = {
            'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(self.home),
            'OPENCLAW_STATE_DIR': str(self.state), 'OPENCLAW_RUNTIME_APP': str(self.app),
            'CLAUDE_CODE_TMPDIR': str(self.root / 'scratch'),
            'PYTEST_DEBUG_TEMPROOT': str(self.root / 'pytest'),
            'NODE_COMPILE_CACHE': str(self.root / 'node-cache'),
            'PYTHONPYCACHEPREFIX': str(self.root / 'python-cache'),
            'FIXTURE_CALLS': str(self.calls), 'FIXTURE_OBSERVED': str(self.observed),
        }

    @staticmethod
    def make_executable(path):
        implementation = path.with_suffix('.py')
        implementation.write_text(path.read_text().split('\n', 1)[1])
        path.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' +
                        shlex.quote(str(implementation)) + ' "$@"\n')
        path.chmod(0o700)

    def run_loader(self):
        return subprocess.run([shutil.which('zsh'), str(self.loader)], env=self.env,
                              capture_output=True, text=True)

    def test_attach_only_startup_drops_legacy_token_without_credential_lookup(self):
        self.env['OPENCLAW_GATEWAY_TOKEN'] = 'fixture-old-token'
        result = self.run_loader()
        self.assertEqual(0, result.returncode, result.stderr)
        app = json.loads(self.observed.read_text())
        self.assertEqual(['--attach-only'], app['args'])
        self.assertNotIn('OPENCLAW_GATEWAY_TOKEN', app['env'])
        self.assertEqual('external', app['env']['OPENCLAW_SUPERVISOR_MODE'])
        self.assertEqual('external', app['env']['OPENCLAW_SERVICE_REPAIR_POLICY'])
        self.assertEqual(str(self.state / 'openclaw.json'), app['env']['OPENCLAW_CONFIG_PATH'])
        self.assertTrue(app['defaultMarker'])
        self.assertTrue(app['canonicalMarker'])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(['unsetenv', 'OPENCLAW_GATEWAY_TOKEN'], calls[0])
        self.assertNotIn('fixture-old-token', self.calls.read_text() + result.stdout + result.stderr)
        self.assertEqual(8, len(calls[1:]))
        self.assertTrue(all(call[0] == 'setenv' for call in calls[1:]))
        self.assertEqual(str(self.state), dict((c[1], c[2]) for c in calls[1:])['OPENCLAW_STATE_DIR'])

    def test_absent_external_state_preserves_default_marker_without_shadow_or_app(self):
        shutil.rmtree(self.state)
        self.env['OPENCLAW_GATEWAY_TOKEN'] = 'fixture-old-token'
        result = self.run_loader()
        self.assertNotEqual(0, result.returncode)
        self.assertIn('state is unavailable', result.stderr)
        self.assertFalse(self.state.exists())
        marker = self.home / '.openclaw/disable-launchagent'
        self.assertTrue(marker.is_file())
        self.assertFalse(marker.is_symlink())
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([['unsetenv', 'OPENCLAW_GATEWAY_TOKEN']], calls)
        self.assertNotIn('fixture-old-token', self.calls.read_text() + result.stdout + result.stderr)
        self.assertFalse(self.observed.exists())

    def test_existing_marker_is_preserved_on_repeated_start(self):
        profile = self.home / '.openclaw'
        profile.mkdir()
        marker = profile / 'disable-launchagent'
        marker.write_text('existing ownership policy\n')
        inode = marker.stat().st_ino
        for _ in range(2):
            result = self.run_loader()
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(inode, marker.stat().st_ino)
        self.assertEqual('existing ownership policy\n', marker.read_text())

    def test_marker_symlink_is_not_followed_or_overwritten(self):
        profile = self.home / '.openclaw'
        profile.mkdir()
        target = self.root / 'unrelated'
        target.write_text('preserve me')
        (profile / 'disable-launchagent').symlink_to(target)
        result = self.run_loader()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('preserve me', target.read_text())
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.observed.exists())

    def test_failed_global_environment_update_does_not_launch_app(self):
        self.env['FIXTURE_NATIVE_EXIT'] = '23'
        result = self.run_loader()
        self.assertEqual(23, result.returncode)
        self.assertFalse(self.observed.exists())

    def test_template_has_no_credential_requirement_or_active_trigger(self):
        template = json.loads((ROOT / 'workspace/launchd/runtime-environment.plist.template.json').read_text())
        environment = template['EnvironmentVariables']
        self.assertEqual('external', environment['OPENCLAW_SUPERVISOR_MODE'])
        self.assertEqual('external', environment['OPENCLAW_SERVICE_REPAIR_POLICY'])
        self.assertFalse(any('KEYCHAIN' in key or 'TOKEN' in key for key in environment))
        self.assertTrue(template['Disabled'])
        self.assertFalse(template['RunAtLoad'])


if __name__ == '__main__':
    unittest.main()
