"""Exercise only a staged internal package; all Backblaze state is tiny fixture data."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = json.loads((ROOT / 'dependencies/backblaze-package.json').read_text())
FILES = tuple(PACKAGE['files'])
PYTHON = sys.executable

PROBE = r'''
import hashlib, importlib.util, json, os, sys, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

stage = Path(__file__).resolve().parent
assert os.environ.get('PYTHONPATH') == ''
assert importlib.util.find_spec('scripts') is None
import backblaze_resource_watchdog as watchdog
r = watchdog.resources
assert Path(r.__file__).resolve().parent == stage
expected = json.loads((stage / 'expected.json').read_text())
assert set(r.SOURCE_IDENTITIES) == set(expected)
r.CONTROL_DIR = stage / 'control'
r.BZDATA = stage / 'bzdata'
mode = sys.argv[1]
if mode == 'changed_before_import':
    assert r.SOURCE_IDENTITIES != expected
    try:
        r.require_transition_watchdog(time.time())
    except ValueError:
        print('changed dependency rejected by receipt admission')
    else:
        raise AssertionError('changed helper admitted')
elif mode in ('changed_after_import', 'missing_after_import'):
    assert r.SOURCE_IDENTITIES == expected
    helper = stage / 'external_volume_guard.py'
    if mode == 'changed_after_import':
        helper.write_bytes(helper.read_bytes() + b'\n# changed fixture\n')
    else:
        helper.unlink()
    try:
        r.require_bootstrap_binding({'binding': {'source_sha256': expected}})
    except (r.BootstrapError, FileNotFoundError):
        print('loaded dependency drift rejected')
    else:
        raise AssertionError('loaded helper drift admitted')
else:
    assert r.SOURCE_IDENTITIES == expected
    import external_volume_guard as volume_guard
    class Volume:
        def __str__(self):
            return str(stage / 'fixture-volume')
        def is_mount(self):
            return True
        def stat(self):
            return SimpleNamespace(st_dev=Path('/').stat().st_dev + 1)
    config = {'fixture': 'exact configuration binding'}
    record = {'binding': {'native_cli': {'fixture': 'signed CLI'}, 'configuration': config},
              'binding_sha256': 'a' * 64}
    memory = dict(memory_available_gib=14, memory_pressure_level=1,
                  heavy_build_running=False, memory_free_percent=85, swap_used_gib=25)
    def forbidden(*args, **kwargs):
        raise AssertionError('no native subprocess or Backblaze control is permitted')
    with patch.object(r, 'OWC', Volume()), \
         patch.object(volume_guard, 'read_volume_uuid', return_value=r.OWC_UUID) as native_uuid, \
         patch.object(r, 'require_bootstrap_binding') as binding, \
         patch.object(r, 'require_signed_cli'), \
         patch.object(r, 'native_cli_identity', return_value=record['binding']['native_cli']), \
         patch.object(r, 'bootstrap_config', return_value=config), \
         patch.object(r, 'host_memory_status', return_value=memory), \
         patch.object(r.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * r.GIB)), \
         patch.object(r.subprocess, 'run', side_effect=forbidden), \
         patch.object(r.subprocess, 'Popen', side_effect=forbidden):
        observation = r.observe_bootstrap(record)
    helper = sys.modules['external_volume_guard']
    assert Path(helper.__file__).resolve().parent == stage
    assert hashlib.sha256(Path(helper.__file__).read_bytes()).hexdigest() == expected['external_volume_guard.py']
    assert observation['catalog_state'] == 'not_created'
    assert observation['vendor_progress'] == 'unknown'
    assert binding.call_count == 2
    native_uuid.assert_called_once()
    print('four-file isolated bootstrap observation passed with mocked native volume metadata')
'''


class InternalPackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='backblaze-package-')
        self.addCleanup(temp.cleanup)
        self.stage = Path(temp.name)
        expected = {}
        for name in FILES:
            data = (ROOT / 'scripts' / name).read_bytes()
            expected[name] = hashlib.sha256(data).hexdigest()
            (self.stage / name).write_bytes(data)
        self.assertEqual(expected, PACKAGE['files'])
        contract = json.loads(Path(os.environ['OPENCLAW_OPERATOR_CONFIG']).read_text())
        (self.stage / 'operator.json').write_text(json.dumps(contract))
        (self.stage / 'expected.json').write_text(json.dumps(expected))
        (self.stage / 'probe.py').write_text(PROBE)
        (self.stage / 'control').mkdir(mode=0o700)
        receipt = self.stage / 'control/backblaze-watchdog-latest.json'
        receipt.write_text(json.dumps({'status': 'installation_hold_pause_requested',
                                      'observed_epoch': __import__('time').time(),
                                      'source_sha256': expected, 'pause_reasserted': True,
                                      'installation_state_verified_idle': False}))
        receipt.chmod(0o600)
        (self.stage / 'bzdata/bzreports').mkdir(parents=True)
        (self.stage / 'bzdata/bzbackup').mkdir()
        (self.stage / 'bzdata/overviewstatus.xml').write_text('<status><bztransmit cur_state="not_running"/></status>')
        (self.stage / 'bzdata/bzreports/bzdc_synchostinfo.xml').write_text('<content><response safety_frozen="not_frozen"/></content>')

    def probe(self, mode):
        return subprocess.run([PYTHON, '-E', '-S', '-B', str(self.stage / 'probe.py'), mode],
                              cwd=self.stage, env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': '',
                                                   'PYTHONDONTWRITEBYTECODE': '1',
                                                   'OPENCLAW_OPERATOR_CONFIG': str(self.stage / 'operator.json')},
                              text=True, capture_output=True, timeout=20)

    def test_four_file_package_reaches_real_helper_import_and_mocked_native_observe(self):
        result = self.probe('observe')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('isolated bootstrap observation passed', result.stdout)

    def test_missing_helper_fails_at_package_import(self):
        (self.stage / 'external_volume_guard.py').unlink()
        result = self.probe('observe')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('FileNotFoundError', result.stderr)
        self.assertIn('external_volume_guard.py', result.stderr)

    def test_changed_helper_rejects_exact_receipt_before_admission(self):
        helper = self.stage / 'external_volume_guard.py'
        helper.write_bytes(helper.read_bytes() + b'\n# changed fixture\n')
        result = self.probe('changed_before_import')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('changed dependency rejected', result.stdout)

    def test_changed_or_missing_helper_after_import_rejects_binding(self):
        original = (self.stage / 'external_volume_guard.py').read_bytes()
        for mode in ('changed_after_import', 'missing_after_import'):
            with self.subTest(mode=mode):
                (self.stage / 'external_volume_guard.py').write_bytes(original)
                result = self.probe(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('loaded dependency drift rejected', result.stdout)
