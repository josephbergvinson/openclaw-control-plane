"""Cheap local resource checks and serialized supported Backblaze controls."""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import stat
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

GIB = 1024 ** 3
BZCLI = OPERATOR.require_path('backup.client_executable')
BZDATA = OPERATOR.require_path('backup.client_data_root')
CONTROL_DIR = OPERATOR.require_path('paths.internal_control_root')
PAUSE_STATE = CONTROL_DIR / 'backblaze-resource-state.json'
# Capture source identities when this process loads its implementation.
SOURCE_IDENTITIES = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                     for name in ('backblaze_resources.py', 'backblaze_resource_watchdog.py', 'external_volume_guard.py', 'operator_contract.py')}
HOLD_SCHEMA = 'openclaw.backblaze_installation_hold.v1'
TRIAL_SCHEMA = 'openclaw.backblaze_personal_trial_transition.v1'
TRIAL_TYPE = 'trial_15_days_free'


class PersonalTrialError(RuntimeError):
    pass


def trial_path() -> Path:
    return CONTROL_DIR / 'backblaze-personal-trial-transition.json'


def private_bytes(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError('maintenance metadata is not a bounded private regular file')
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError('maintenance metadata grew beyond its limit')
        return data


def trial_expiry(status: str) -> float:
    if not isinstance(status, str) or re.fullmatch(r'expires_[0-9]{14}', status) is None:
        raise ValueError('the trial expiry is malformed')
    stamp = datetime.strptime(status[8:], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    if stamp.strftime('%Y%m%d%H%M%S') != status[8:]:
        raise ValueError('the trial expiry is malformed')
    return stamp.timestamp()


def require_trial_license(license: dict, expected_status: str, now: float) -> None:
    if (not isinstance(license, dict) or license.get('type') != TRIAL_TYPE or license.get('status') != expected_status
            or license.get('renewal_fail') != {'gmt_time': 'none'} or license.get('renewal_failure') != 'none'
            or not math.isfinite(now) or now >= trial_expiry(expected_status)):
        raise ValueError('the exact unexpired personal trial is not verified')


def local_trial_license() -> dict:
    # Only these four fields are returned; the same response contains credentials.
    response = ET.parse(BZDATA / 'bzreports' / 'bzdc_synchostinfo.xml').getroot().find('response')
    if response is None:
        raise ValueError('local trial licensing is unavailable')
    return {'type': response.get('bzlicense'), 'status': response.get('bzlicense_status'),
            'renewal_fail': {'gmt_time': response.get('renewal_fail_datetime_gmt')},
            'renewal_failure': response.get('renewal_fail_reason')}


def read_personal_trial() -> dict | None:
    """Validate the explicit exception each time; missing state grants no exception."""
    try:
        try:
            raw = private_bytes(trial_path(), 65536)
        except FileNotFoundError:
            return None
        data = json.loads(raw)
        expected = {'schema', 'original_identity_sha256', 'new_identity_sha256',
                    'original_hold_sha256', 'license_type', 'license_status',
                    'expires_epoch', 'transitioned_epoch', 'prior_request_json', 'prior_request_sha256'}
        if not isinstance(data, dict) or set(data) != expected or data['schema'] != TRIAL_SCHEMA:
            raise ValueError('invalid personal trial transition')
        for key in ('original_identity_sha256', 'new_identity_sha256', 'original_hold_sha256', 'prior_request_sha256'):
            if not isinstance(data[key], str) or re.fullmatch(r'[0-9a-f]{64}', data[key]) is None:
                raise ValueError('invalid transition fingerprint')
        transitioned, expires = data['transitioned_epoch'], data['expires_epoch']
        now = time.time()
        if (type(transitioned) not in (int, float) or not math.isfinite(transitioned)
                or type(expires) not in (int, float) or not math.isfinite(expires)
                or not 0 < transitioned <= now < expires <= transitioned + 15 * 86400
                or expires != trial_expiry(data['license_status']) or data['license_type'] != TRIAL_TYPE
                or data['original_identity_sha256'] == data['new_identity_sha256']):
            raise ValueError('invalid or expired personal trial transition')
        prior = data['prior_request_json']
        if not isinstance(prior, str) or len(prior.encode('utf-8')) > 32768:
            raise ValueError('invalid preserved request')
        if hashlib.sha256(prior.encode('utf-8')).hexdigest() != data['prior_request_sha256']:
            raise ValueError('preserved request hash mismatch')
        prior_state = json.loads(prior)
        if not isinstance(prior_state, dict) or prior_state.get('backup_identity_sha256') != data['original_identity_sha256']:
            raise ValueError('preserved request identity mismatch')
        hold_raw = private_bytes(hold_path(), 8192)
        hold = read_installation_hold()
        if (hashlib.sha256(hold_raw).hexdigest() != data['original_hold_sha256'] or not hold['active']
                or hold['original_identity_sha256'] != data['original_identity_sha256']
                or hold['prepared_epoch'] > transitioned):
            raise ValueError('the original installation hold changed')
        if installed_identity() != data['new_identity_sha256']:
            raise ValueError('the personal trial installation changed')
        require_trial_license(local_trial_license(), data['license_status'], now)
        require_upload_thread_bound()
        return data
    except (OSError, ValueError, KeyError, TypeError, OverflowError, InstallationHoldError, ET.ParseError):
        raise PersonalTrialError('the personal trial transition could not be verified') from None


class InstallationHoldError(RuntimeError):
    def __init__(self, *, invalid: bool = False):
        self.invalid = invalid
        super().__init__('the installation hold could not be verified' if invalid
                         else 'an explicit installation hold is active')


def hold_path() -> Path:
    return CONTROL_DIR / 'backblaze-installation-hold.json'


def read_installation_hold() -> dict | None:
    """An absent hold is normal; malformed or non-private existing state blocks work."""
    try:
        try:
            fd = os.open(hold_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'r') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 8192):
                raise ValueError('hold is not a private regular file')
            data = json.loads(handle.read(8193))
        if (not isinstance(data, dict) or data.get('schema') != HOLD_SCHEMA
                or type(data.get('active')) is not bool
                or not isinstance(data.get('original_identity_sha256'), str)
                or re.fullmatch(r'[0-9a-f]{64}', data['original_identity_sha256']) is None):
            raise ValueError('invalid hold schema')
        prepared = data.get('prepared_epoch')
        if type(prepared) not in (int, float) or not math.isfinite(prepared) or prepared <= 0:
            raise ValueError('invalid hold preparation time')
        if not data['active']:
            released = data.get('released_epoch')
            if (type(released) not in (int, float) or not math.isfinite(released)
                    or released < prepared
                    or not isinstance(data.get('released_identity_sha256'), str)
                    or data['released_identity_sha256'] != data['original_identity_sha256']):
                raise ValueError('invalid hold release')
        elif 'released_epoch' in data or 'released_identity_sha256' in data:
            raise ValueError('active hold has release metadata')
        return data
    except (OSError, ValueError, TypeError, OverflowError):
        raise InstallationHoldError(invalid=True) from None


def require_no_installation_hold() -> None:
    trial = read_personal_trial()
    hold = read_installation_hold()
    if hold is not None and hold['active'] and trial is None:
        raise InstallationHoldError()


def require_upload_thread_bound() -> None:
    configured = ET.parse(BZDATA / 'bzinfo.xml').getroot().find('do_backup')
    if (configured is None or configured.get('num_backup_threads') != '1'
            or configured.get('net_auto_throttle') != 'false'):
        raise ValueError('automatic throttle disabled and one upload thread are not verified')


def require_manual_and_drained() -> None:
    catalog_size_gib()
    overview = ET.parse(BZDATA / 'overviewstatus.xml').getroot().find('bztransmit')
    if overview is None or overview.get('cur_state') not in ('not_running', 'transmitting'):
        raise ValueError('transmitter status is unavailable')
    configured = ET.parse(BZDATA / 'bzinfo.xml').getroot().find('do_backup')
    if configured is None or configured.get('backup_schedule_type') != 'only_when_click_backup_now':
        raise ValueError('manual scheduling is not verified')
    if process_status()['transmitter_process_running'] is not False:
        raise ValueError('the transmitter has not drained')


def atomic_json(path: Path, data: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(temp, path)  # Atomic create; never replace a concurrent record.
        else:
            os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def output(args: list[str]) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=10).stdout


def identity_fingerprint(hguid: str) -> str:
    if not isinstance(hguid, str) or not re.fullmatch(r'[0-9a-fA-F]{24}', hguid) or int(hguid, 16) == 0:
        raise ValueError('Backblaze installation identity is missing or invalid')
    return hashlib.sha256(hguid.lower().encode('ascii')).hexdigest()


def installed_identity() -> str:
    node = ET.parse(BZDATA.parent / 'bzinstall.xml').getroot().find('bzuniqueid')
    return identity_fingerprint(node.get('hguid') if node is not None else None)


def catalog_size_gib() -> float:
    value = (BZDATA / 'bzbackup' / 'bzfileids.dat').lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_size <= 0:
        raise ValueError('Backblaze catalog is missing or invalid')
    return value.st_size / GIB


def process_status() -> dict:
    heavy = False
    transmitter = False
    valid_rows = 0
    for line in output(['/bin/ps', '-axo', 'rss=,comm=']).splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            raise ValueError('Mac process snapshot is malformed')
        valid_rows += 1
        name = Path(parts[1]).name
        if parts[1] == str(BZDATA.parent / 'bztransmit'):
            transmitter = True
        if name in ('tsgo', 'tsgolint', 'xcodebuild', 'swift-frontend', 'clang', 'rustc') or (name == 'node' and int(parts[0]) > 1024 * 1024):
            heavy = True
    if not valid_rows:
        raise ValueError('Mac process snapshot is empty')
    return {'heavy_build_running': heavy, 'transmitter_process_running': transmitter}


def host_memory_status(processes: dict | None = None) -> dict:
    free = re.search(r'System-wide memory free percentage:\s*(\d+)%', output(['/usr/bin/memory_pressure', '-Q']))
    used = re.search(r'used\s*=\s*([0-9.]+)M', output(['/usr/sbin/sysctl', '-n', 'vm.swapusage']))
    pressure_level = int(output(['/usr/sbin/sysctl', '-n', 'kern.memorystatus_vm_pressure_level']))
    vm = output(['/usr/bin/vm_stat'])
    page_match = re.search(r'page size of (\d+) bytes', vm)
    pages = dict(re.findall(r'^(Pages (?:free|inactive)):\s*(\d+)\.', vm, re.MULTILINE))
    if page_match is None or set(pages) != {'Pages free', 'Pages inactive'}:
        raise ValueError('Mac page counts could not be measured')
    # memory_pressure reports memorystatus_get_level, not literal RAM bytes.
    # Count free and potentially reclaimable inactive pages conservatively;
    # exclude compressor and purgeable pages to avoid counting them twice.
    available = sum(map(int, pages.values())) * int(page_match.group(1)) / GIB
    if free is None or used is None:
        raise ValueError('Mac memory pressure could not be measured')
    return {'memory_free_percent': int(free.group(1)),
            'memory_available_gib': round(available, 2), 'memory_estimate': 'free_plus_inactive_pages',
            'memory_pressure_level': pressure_level,
            'swap_used_gib': round(float(used.group(1)) / 1024, 2),
            **(process_status() if processes is None else processes)}


def memory_status(processes: dict | None = None) -> dict:
    host = host_memory_status(processes)
    catalog = catalog_size_gib()
    return {**host, 'catalog_gib': round(catalog, 2), 'memory_required_gib': round(catalog + 2, 2)}


def resource_blockers(s: dict) -> list[str]:
    issues = []
    if s['internal_free_gib'] < 30:
        issues.append('the Mac needs at least 30 GiB free to start rebuilding its backup catalog')
    if s['memory_available_gib'] < s['memory_required_gib']:
        issues.append('more free memory is needed to load the backup catalog safely')
    if s['memory_pressure_level'] != 1:
        issues.append('Mac memory pressure must be normal before backup resumes')
    if s['heavy_build_running']:
        issues.append('an active build must finish before backup resumes')
    return issues


def watchdog_status(now: float) -> str:
    try:
        data = json.loads((CONTROL_DIR / 'backblaze-watchdog-latest.json').read_text())
        age = now - float(data['observed_epoch'])
        if not -60 <= age <= 360:
            return 'stale'
        if data['status'] not in ('idle', 'observing', 'pause_requested', 'installation_held'):
            return 'failed'
        if data.get('source_sha256') != SOURCE_IDENTITIES:
            return 'outdated'
        return 'healthy'
    except (OSError, ValueError, KeyError, TypeError):
        return 'unavailable'


def require_transition_watchdog(now: float) -> None:
    """The old identity hold pauses a replacement; this is never normal admission."""
    data = json.loads(private_bytes(CONTROL_DIR / 'backblaze-watchdog-latest.json', 8192))
    age = now - float(data['observed_epoch'])
    if not -60 <= age <= 360 or data.get('source_sha256') != SOURCE_IDENTITIES:
        raise ValueError('a fresh current-source installation pause receipt is required')
    if (data.get('status') == 'installation_hold_pause_requested'
            and data.get('pause_reasserted') is True and data.get('installation_state_verified_idle') is False):
        return
    if (data.get('status') == 'bootstrap_paused_catalog' and data.get('pause_accepted') is True
            and data.get('cancellation_persisted') is True and data.get('manual_verified') is True
            and data.get('drained') is True):
        attempt = read_bootstrap()
        if (attempt is not None and attempt['phase'] == 'catalog_established'
                and attempt.get('first_catalog_seen') is not None
                and data.get('binding_sha256') == attempt['binding_sha256']):
            require_bootstrap_binding(attempt, owner=False)
            catalog_size_gib()
            return
    raise ValueError('a fresh current-source installation pause receipt is required')


def read_control() -> dict:
    if not PAUSE_STATE.exists():
        return {}
    data = json.loads(PAUSE_STATE.read_text())
    if not isinstance(data, dict):
        raise ValueError('Backblaze resource state is malformed')
    return data


@contextmanager
def admin_lock():
    CONTROL_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CONTROL_DIR / 'backblaze-admin.lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def configure_schedule(mode: str) -> None:
    result = subprocess.run([str(BZCLI), 'configure', '--value', 'backup_schedule_type=' + mode],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Backblaze schedule update failed')
    configured = ET.parse(BZDATA / 'bzinfo.xml').getroot().find('do_backup')
    if configured is None or configured.get('backup_schedule_type') != mode:
        raise RuntimeError('Backblaze schedule update was not confirmed')


def request_pause(reason: str) -> None:
    with admin_lock():
        _request_pause_locked(reason)


def _request_pause_locked(reason: str, *, reassert: bool = False) -> None:
    """Caller owns admin_lock. Reassert is reserved for an installation hold."""
    try:
        state = read_control()
    except (OSError, ValueError, TypeError):
        if not reassert:
            raise
        state = {}  # A damaged pause latch cannot defeat an installation hold.
    try:
        configured = ET.parse(BZDATA / 'bzinfo.xml').getroot().find('do_backup')
    except (OSError, ET.ParseError):
        configured = None
    try:
        identity = installed_identity()
    except (OSError, ValueError, ET.ParseError):
        identity = None
    if (not reassert and state.get('paused') and state.get('pause_accepted_epoch')
            and identity is not None and state.get('backup_identity_sha256') == identity
            and configured is not None
            and configured.get('backup_schedule_type') == 'only_when_click_backup_now'):
        return  # One accepted pause remains pending; do not repeatedly dispatch it.
    state = {'paused': True, 'reason': reason,
             'backup_identity_sha256': identity,
             'pause_requested_epoch': (state.get('pause_requested_epoch', time.time())
                                       if state.get('backup_identity_sha256') == identity else time.time())}
    atomic_json(PAUSE_STATE, state)
    configuration_failed = False
    try:
        configure_schedule('only_when_click_backup_now')
    except (OSError, RuntimeError, ValueError, ET.ParseError, subprocess.SubprocessError):
        configuration_failed = True
    # Missing configuration must not prevent attempting the supported pause.
    response = subprocess.run([str(BZCLI), 'action', '--pause-backup'],
                              capture_output=True, text=True, timeout=60)
    if response.returncode:
        raise RuntimeError('Backblaze resource pause was not accepted')
    state['pause_accepted_epoch'] = time.time()
    state['manual_schedule_confirmed'] = not configuration_failed
    atomic_json(PAUSE_STATE, state)
    if configuration_failed:
        raise RuntimeError('Backblaze pause requested but manual scheduling is unverified')


def protect_installation_hold() -> dict | None:
    """Keep installer transitions paused without depending on a complete catalog."""
    with admin_lock():
        try:
            if read_personal_trial() is not None:
                return None
        except PersonalTrialError:
            _request_pause_locked('personal_trial_unverified', reassert=True)
            return {'status': 'personal_trial_invalid_pause_requested', 'observed_epoch': time.time()}
        invalid = False
        try:
            hold = read_installation_hold()
        except InstallationHoldError:
            invalid = True
            hold = None
        if not invalid and (hold is None or not hold['active']):
            return None
        unavailable = False
        reassert = True
        try:
            if hold is None or installed_identity() != hold['original_identity_sha256']:
                raise ValueError('the held installation identity changed')
            require_manual_and_drained()
            reassert = False
        except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError):
            unavailable = True
        # Reassert if liveness/configuration is unknown or work restarted with the
        # same identity; a previous accepted pause does not cover a new process.
        _request_pause_locked('installation_hold', reassert=reassert or invalid)
        return {'status': ('installation_hold_invalid' if invalid else
                           'installation_hold_pause_requested' if unavailable else 'installation_held'),
                'observed_epoch': time.time(), 'pause_reasserted': reassert or invalid,
                'installation_state_verified_idle': not unavailable}


# This exception is confined to first catalog creation. Ordinary health admission
# continues to enforce the preserved installation hold and strict catalog rules.
BOOTSTRAP_SCHEMA = 'openclaw.backblaze_personal_catalog_bootstrap.v1'
BOOTSTRAP_STOP_SCHEMA = 'openclaw.backblaze_personal_catalog_bootstrap_stop.v1'
BOOTSTRAP_PHASES = {'dispatching', 'observing', 'pause_pending', 'catalog_established', 'failed'}
MANUAL = 'only_when_click_backup_now'
OWC = OPERATOR.require_path('paths.data_root')
OWC_UUID = OPERATOR.require_string('volumes.expected_uuid').upper()
BZCLI_REQUIREMENT = '=anchor apple generic and identifier "bzcli" and certificate leaf[subject.OU] = "G4K4BQ7S8J"'


class BootstrapError(RuntimeError):
    pass


def bootstrap_path() -> Path:
    return CONTROL_DIR / 'backblaze-personal-catalog-bootstrap.json'


def bootstrap_stop_path() -> Path:
    return CONTROL_DIR / 'backblaze-personal-catalog-bootstrap-stop.json'


def digest_json(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def process_identity(pid: int) -> dict | None:
    if type(pid) is not int or pid <= 0:
        raise BootstrapError('invalid process identity')
    result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True, timeout=10)
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr:
        return None
    if result.returncode or result.stderr or not result.stdout.strip():
        raise BootstrapError('process identity unavailable')
    # lstart plus command binds PID reuse without retaining arbitrary arguments.
    return {'pid': pid, 'start_command_sha256': hashlib.sha256(result.stdout.strip().encode()).hexdigest()}


def native_cli_identity() -> dict:
    info = BZCLI.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise BootstrapError("native CLI is not a physical executable")
    return {key: getattr(info, "st_" + key) for key in ("dev", "ino", "size", "mode", "mtime_ns", "ctime_ns")}


def require_signed_cli() -> dict:
    info = BZCLI.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise BootstrapError('native CLI is not a physical executable')
    identity = native_cli_identity()
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '-R', BZCLI_REQUIREMENT, str(BZCLI)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    if native_cli_identity() != identity:
        raise BootstrapError('native CLI changed during signature verification')
    return identity


def bootstrap_config(config=None) -> dict:
    if config is None:
        config = ET.parse(BZDATA / 'bzinfo.xml').getroot()
    settings, scratch = config.find('do_backup'), config.find('scratch_drive')
    if (settings is None or settings.get('backup_schedule_type') != MANUAL
            or settings.get('num_backup_threads') != '1' or settings.get('net_auto_throttle') != 'false'
            or scratch is None or scratch.get('scratch_mountpoint', '').rstrip('/') != str(OWC)):
        raise BootstrapError('manual schedule, disabled automatic throttle, one thread and OWC scratch are required')
    volumes = {}
    for node in config.findall('hard_drives_to_backup/bzvolume'):
        mount = node.get('mountPointPath', '').rstrip('/') or '/'
        if mount in ('/', str(OWC)):
            guid = node.get('bzVolumeGuid')
            if not guid or mount in volumes:
                raise BootstrapError('native volume bindings are unavailable')
            volumes[mount] = guid
    if set(volumes) != {'/', str(OWC)} or len(set(volumes.values())) != 2:
        raise BootstrapError('both native volume bindings are required')
    return {'schedule': MANUAL, 'net_auto_throttle': False, 'num_backup_threads': 1,
            'scratch_mountpoint': str(OWC), 'volumes': volumes}


# An adopter must supply independently reviewed native publication evidence.
# Missing evidence grants no first-catalog exception; no source-host pins ship.
FIRST_SCAN_PUBLICATION = OPERATOR.get('backup.first_scan_publication')


def scan_stat(info) -> list[int]:
    return [getattr(info, 'st_' + key) for key in ('dev', 'ino', 'mode', 'uid', 'gid', 'nlink', 'size', 'mtime_ns', 'ctime_ns')]


def bounded_native_bytes(path: Path, limit: int) -> tuple[bytes, list[int]]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise BootstrapError('native scan evidence is not a bounded regular file')
        raw = handle.read(limit + 1)
        identity = scan_stat(before)
        if (len(raw) > limit or scan_stat(os.fstat(handle.fileno())) != identity
                or scan_stat(path.lstat()) != identity):
            raise BootstrapError('native scan evidence changed during observation')
    return raw, identity


def first_scan_names(directory: Path) -> list[str]:
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise BootstrapError('first_scan_directory_invalid')
    names = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if len(names) >= 64 or entry.name.endswith('.future'):
                raise BootstrapError('first_scan_publication_incomplete')
            names.append(entry.name)
    return sorted(names)


def require_first_scan_completion(config: dict, new_identity_sha256: str) -> dict:
    expected = FIRST_SCAN_PUBLICATION
    if not isinstance(expected, dict) or set(expected) != {
            'identity_sha256', 'started_utc', 'finished_utc', 'filestats_sha256',
            'filestats_stat', 'volumes'}:
        raise BootstrapError('first_scan_publication_binding_missing_or_invalid')
    fingerprint = lambda value: isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None
    stamp = lambda value: isinstance(value, list) and len(value) == 9 and all(type(item) is int and item >= 0 for item in value)
    volumes = expected['volumes']
    if (not all(fingerprint(expected[key]) for key in ('identity_sha256', 'filestats_sha256'))
            or not all(isinstance(expected[key], str) and re.fullmatch(r'[0-9]{14}', expected[key])
                       for key in ('started_utc', 'finished_utc'))
            or not stamp(expected['filestats_stat']) or not isinstance(volumes, dict)
            or set(volumes) != {'/', str(OWC)}
            or any(not isinstance(value, dict) or set(value) != {'guid_sha256', 'name_sha256', 'stat'}
                   or not fingerprint(value['guid_sha256']) or not fingerprint(value['name_sha256'])
                   or not stamp(value['stat']) for value in volumes.values())):
        raise BootstrapError('first_scan_publication_binding_missing_or_invalid')
    if new_identity_sha256 != expected['identity_sha256']:
        raise BootstrapError('first_scan_identity_not_bound')
    install_path, config_path = BZDATA.parent / 'bzinstall.xml', BZDATA / 'bzinfo.xml'
    install_raw, install_stat = bounded_native_bytes(install_path, 16384)
    install = ET.fromstring(install_raw).find('bzuniqueid')
    if install is None or identity_fingerprint(install.get('hguid')) != new_identity_sha256:
        raise BootstrapError('first_scan_identity_not_bound')
    config_raw, config_stat = bounded_native_bytes(config_path, 262144)
    if config != bootstrap_config(ET.fromstring(config_raw)) or set(config['volumes']) != set(expected['volumes']):
        raise BootstrapError('first_scan_selected_volumes_changed')
    directory = BZDATA / 'bzfilelists'
    names = first_scan_names(directory)
    stats_path = directory / 'filestats.xml'
    raw, stats_stat = bounded_native_bytes(stats_path, 16384)
    if stats_stat != expected['filestats_stat']:
        raise BootstrapError('first_scan_final_stats_changed')
    if hashlib.sha256(raw).hexdigest() != expected['filestats_sha256']:
        raise BootstrapError('first_scan_final_stats_changed')
    root = ET.fromstring(raw)
    progress, info = root.find('progress'), root.find('info')
    if (root.tag != 'filestats' or progress is None or info is None
            or progress.get('totally_final') != 'true' or progress.get('good_for_installer') != 'true'
            or info.get('datetime') != expected['finished_utc']
            or any(identity_fingerprint(info.get(key)) != new_identity_sha256 for key in ('hguid', 'ahguid'))):
        raise BootstrapError('first_scan_native_final_flags_missing')
    start = datetime.strptime(expected['started_utc'], '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).timestamp()
    finished = datetime.strptime(info.get('datetime'), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc).timestamp()
    if not 0 < start < finished <= time.time():
        raise BootstrapError('first_scan_publication_time_invalid')
    files = {}
    for mount, bound in expected['volumes'].items():
        guid = config['volumes'][mount]
        if hashlib.sha256(guid.encode()).hexdigest() != bound['guid_sha256']:
            raise BootstrapError('first_scan_selected_volume_identity_changed')
        matches = [name for name in names if name.startswith(guid + '_') and name.endswith('filelist.dat')]
        if len(matches) != 1 or hashlib.sha256(matches[0].encode()).hexdigest() != bound['name_sha256']:
            raise BootstrapError('first_scan_final_filelist_missing')
        current = (directory / matches[0]).lstat()
        if (not stat.S_ISREG(current.st_mode) or current.st_size <= 0 or scan_stat(current) != bound['stat']
                or not finished <= current.st_mtime < finished + 1):
            raise BootstrapError('first_scan_final_filelist_changed')
        files[mount] = {'guid_sha256': bound['guid_sha256'], 'name_sha256': bound['name_sha256'], 'stat': scan_stat(current)}
    if (scan_stat(stats_path.lstat()) != expected['filestats_stat']
            or scan_stat(install_path.lstat()) != install_stat or scan_stat(config_path.lstat()) != config_stat
            or first_scan_names(directory) != names
            or any(scan_stat((directory / next(name for name in names if hashlib.sha256(name.encode()).hexdigest() == value['name_sha256'])).lstat()) != value['stat']
                   for value in files.values())):
        raise BootstrapError('first_scan_publication_changed_during_observation')
    return {'identity_sha256': new_identity_sha256, 'started_epoch': start, 'finished_epoch': finished,
            'filestats_sha256': expected['filestats_sha256'], 'files': files,
            'scope': 'native_scan_processing_publication_only'}


def bootstrap_catalog(*, strict: bool = False) -> dict:
    parent = BZDATA / 'bzbackup'
    if not stat.S_ISDIR(parent.lstat().st_mode):
        raise BootstrapError('native catalog parent is invalid')
    try:
        info = (parent / 'bzfileids.dat').lstat()
    except FileNotFoundError:
        if strict:
            raise BootstrapError('the established catalog disappeared') from None
        return {'catalog_state': 'not_created', 'catalog_gib': None, 'memory_required_gib': 2.0}
    if not stat.S_ISREG(info.st_mode):
        raise BootstrapError('native catalog is not a regular file')
    if info.st_size == 0 and strict:
        raise BootstrapError('the established catalog is empty')
    stamp = {key: getattr(info, 'st_' + key) for key in ('dev', 'ino', 'size', 'mode', 'mtime_ns', 'ctime_ns')}
    return {'catalog_state': 'established' if info.st_size else 'initializing',
            'catalog_gib': info.st_size / GIB if info.st_size else None,
            'memory_required_gib': info.st_size / GIB + 2,
            'catalog_observation': stamp}


def read_bootstrap() -> dict | None:
    try:
        raw = private_bytes(bootstrap_path(), 32768)
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        binding = data['binding']
        allowed = {'schema', 'binding', 'binding_sha256', 'phase', 'heartbeat_epoch', 'first_catalog_seen',
                   'native_child', 'native_exit', 'pause_accepted_epoch', 'manual_verified', 'drained', 'last_observation'}
        keys = {'original_identity_sha256', 'new_identity_sha256', 'original_hold_sha256',
                'prior_request_sha256', 'prior_request_path', 'license_status', 'expires_epoch',
                'created_epoch', 'source_sha256', 'owner_source_path', 'owner_source_sha256',
                'admission_sha256', 'configuration', 'native_cli', 'owner', 'argv', 'attempt_consumed'}
        if (not isinstance(data, dict) or set(data) - allowed or data['schema'] != BOOTSTRAP_SCHEMA
                or not isinstance(binding, dict) or set(binding) != keys
                or data['binding_sha256'] != digest_json(binding) or data['phase'] not in BOOTSTRAP_PHASES
                or binding['attempt_consumed'] is not True or binding['argv'] != [str(BZCLI), 'action', '--backup-now']
                or binding['original_identity_sha256'] == binding['new_identity_sha256']):
            raise ValueError('invalid bootstrap schema')
        for key in ('original_identity_sha256', 'new_identity_sha256', 'original_hold_sha256',
                    'prior_request_sha256', 'owner_source_sha256', 'admission_sha256'):
            if re.fullmatch(r'[0-9a-f]{64}', binding[key]) is None:
                raise ValueError('invalid bootstrap fingerprint')
        if (type(binding['created_epoch']) not in (int, float) or not math.isfinite(binding['created_epoch'])
                or not 0 < binding['created_epoch'] < binding['expires_epoch'] <= binding['created_epoch'] + 15 * 86400
                or binding['expires_epoch'] != trial_expiry(binding['license_status'])
                or type(data['heartbeat_epoch']) not in (int, float) or not math.isfinite(data['heartbeat_epoch'])
                or set(binding['owner']) != {'pid', 'start_command_sha256'}
                or type(binding['owner']['pid']) is not int or binding['owner']['pid'] <= 0
                or re.fullmatch(r'[0-9a-f]{64}', binding['owner']['start_command_sha256']) is None):
            raise ValueError('invalid bootstrap time or owner')
        first = data.get('first_catalog_seen')
        if first is not None and (not isinstance(first, dict) or set(first) != {'dev', 'ino', 'size', 'mode', 'mtime_ns', 'ctime_ns'}
                                  or any(type(value) is not int for value in first.values())
                                  or first['size'] <= 0 or not stat.S_ISREG(first['mode'])):
            raise ValueError('invalid first catalog observation')
        return data
    except (ValueError, KeyError, TypeError, OverflowError):
        raise BootstrapError('bootstrap record is invalid') from None


def bootstrap_cancelled(record: dict) -> bool:
    # Presence dominates even if the stop record cannot be decoded or does not
    # match. Never interpret damaged cancellation data as continued permission.
    return os.path.lexists(bootstrap_stop_path())


def cancel_bootstrap(record: dict, reason: str) -> None:
    stop = {'schema': BOOTSTRAP_STOP_SCHEMA, 'binding_sha256': record['binding_sha256'],
            'requested_epoch': time.time(), 'reason': reason}
    try:
        atomic_json(bootstrap_stop_path(), stop, exclusive=True)
    except FileExistsError:
        pass


def update_bootstrap(expected: dict, **changes) -> dict:
    """Small metadata transaction; callers never pass a stale replacement record."""
    with admin_lock():
        current = read_bootstrap()
        if current is None or current['binding_sha256'] != expected['binding_sha256']:
            raise BootstrapError('bootstrap binding changed')
        if set(changes) - {'phase', 'heartbeat_epoch', 'first_catalog_seen', 'native_child', 'native_exit',
                          'pause_accepted_epoch', 'manual_verified', 'drained', 'last_observation'}:
            raise BootstrapError('immutable bootstrap update refused')
        stopped = bootstrap_cancelled(current) or current['phase'] in ('pause_pending', 'catalog_established', 'failed')
        if stopped and changes.get('phase') in ('dispatching', 'observing'):
            raise BootstrapError('cancelled bootstrap cannot resume')
        if current.get('first_catalog_seen') is not None:
            changes.pop('first_catalog_seen', None)  # Sticky, including stale explicit None.
        if stopped and current['phase'] in ('dispatching', 'observing'):
            current['phase'] = 'pause_pending'
        if current['phase'] in ('catalog_established', 'failed'):
            changes.pop('phase', None)  # Terminal records cannot be reopened.
        if 'heartbeat_epoch' in changes:
            changes['heartbeat_epoch'] = max(current['heartbeat_epoch'], changes['heartbeat_epoch'])
        current.update(changes)
        atomic_json(bootstrap_path(), current)
        return current


def require_bootstrap_binding(record: dict, *, owner: bool = True) -> None:
    binding = record['binding']
    now = time.time()
    current_sources = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in SOURCE_IDENTITIES}
    if (binding['source_sha256'] != SOURCE_IDENTITIES or current_sources != SOURCE_IDENTITIES
            or hashlib.sha256(Path(binding['owner_source_path']).read_bytes()).hexdigest() != binding['owner_source_sha256']
            or installed_identity() != binding['new_identity_sha256']
            or not binding['created_epoch'] <= now < binding['expires_epoch'] <= binding['created_epoch'] + 15 * 86400
            or binding['expires_epoch'] != trial_expiry(binding['license_status'])):
        raise BootstrapError('bootstrap source, identity or lifetime changed')
    raw = private_bytes(hold_path(), 8192)
    hold = read_installation_hold()
    if (hashlib.sha256(raw).hexdigest() != binding['original_hold_sha256'] or not hold or not hold['active']
            or hold['original_identity_sha256'] != binding['original_identity_sha256']
            or hold['prepared_epoch'] > binding['created_epoch']
            or hashlib.sha256(private_bytes(Path(binding['prior_request_path']), 32768)).hexdigest() != binding['prior_request_sha256']
            or os.path.lexists(trial_path())):
        raise BootstrapError('preserved bootstrap inputs changed')
    require_trial_license(local_trial_license(), binding['license_status'], now)
    if owner and (not 0 <= now - record['heartbeat_epoch'] <= 90
                  or process_identity(binding['owner']['pid']) != binding['owner']):
        raise BootstrapError('bootstrap owner is missing, replaced or stale')


def observe_bootstrap(record: dict) -> dict:
    require_bootstrap_binding(record)
    receipt = json.loads(private_bytes(CONTROL_DIR / 'backblaze-watchdog-latest.json', 16384))
    if (not -60 <= time.time() - float(receipt['observed_epoch']) <= 360
            or receipt.get('source_sha256') != SOURCE_IDENTITIES
            or receipt.get('status') not in ('installation_hold_pause_requested', 'bootstrap_observing')
            or (receipt.get('status') == 'bootstrap_observing' and receipt.get('binding_sha256') != record['binding_sha256'])):
        raise BootstrapError('bootstrap watchdog is unavailable or changed')
    require_signed_cli()
    if native_cli_identity() != record['binding']['native_cli']:
        raise BootstrapError('the signed native CLI changed')
    overview = ET.parse(BZDATA / 'overviewstatus.xml').getroot().find('bztransmit')
    license_response = ET.parse(BZDATA / 'bzreports/bzdc_synchostinfo.xml').getroot().find('response')
    if (overview is None or overview.get('cur_state') not in ('not_running', 'transmitting')
            or license_response is None or license_response.get('safety_frozen') != 'not_frozen'):
        raise BootstrapError('native status or safety freeze is unavailable')
    config = bootstrap_config()
    if config != record['binding']['configuration']:
        raise BootstrapError('selected volume or configuration binding changed')
    try:
        from .external_volume_guard import read_volume_uuid
    except ImportError:
        from external_volume_guard import read_volume_uuid
    if (not OWC.is_mount() or OWC.stat().st_dev == Path('/').stat().st_dev
            or read_volume_uuid(OWC).upper() != OWC_UUID):
        raise BootstrapError('registered OWC volume is unavailable')
    observation = {**host_memory_status(), **bootstrap_catalog(strict=record.get('first_catalog_seen') is not None),
                   'internal_free_gib': shutil.disk_usage('/').free / GIB,
                   'owc_free_gib': shutil.disk_usage(OWC).free / GIB,
                   'vendor_progress': 'unknown', 'observed_epoch': time.time()}
    if (resource_blockers(observation) or observation['owc_free_gib'] < 20
            or observation['memory_free_percent'] < 15):
        raise BootstrapError('bootstrap resource admission revoked')
    require_bootstrap_binding(record)
    return observation


def native_start_commands() -> list[int]:
    found = []
    rows = 0
    for line in output(['/bin/ps', '-axo', 'pid=,comm=,args=']).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit():
            raise BootstrapError('native start-command observation unavailable')
        rows += 1
        if parts[1] == str(BZCLI) and '--backup-now' in parts[2].split():
            found.append(int(parts[0]))
    if not rows:
        raise BootstrapError('native start-command observation is empty')
    return found


def supervised_cli(arguments: list[str], timeout: float, tick=None) -> int:
    """Native output can contain account data; retain only the exit status."""
    child = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    try:
        while child.poll() is None:
            if tick is not None:
                tick()
            if time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(arguments, timeout)
            time.sleep(0.2)
        return child.returncode
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)


def pause_bootstrap(record: dict, reason: str, tick=None) -> dict:
    """Protective fallback never waits for admin_lock and never enables work."""
    durable = False
    try:
        cancel_bootstrap(record, reason)
        durable = True
    except OSError:
        pass  # Still attempt the signed native pause; no durable-success claim.
    try:
        record = update_bootstrap(record, phase='pause_pending')
    except (OSError, BootstrapError):
        pass
    require_signed_cli()
    manual = False
    try:
        manual = supervised_cli([str(BZCLI), 'configure', '--value', 'backup_schedule_type=' + MANUAL], 30, tick) == 0
    except (OSError, subprocess.SubprocessError):
        pass
    accepted = supervised_cli([str(BZCLI), 'action', '--pause-backup'], 60, tick) == 0
    try:
        bootstrap_config()
    except (OSError, ValueError, RuntimeError, ET.ParseError):
        manual = False
    drained = process_status()['transmitter_process_running'] is False and not native_start_commands()
    catalog_ok = False
    if record.get('first_catalog_seen') is not None:
        try:
            bootstrap_catalog(strict=True)
            require_bootstrap_binding(record, owner=False)
            catalog_ok = True
        except (OSError, ValueError, RuntimeError, ET.ParseError):
            pass
    # This is pause/drain evidence only. A fresh strict report after pause is
    # required for the foreground owner to mark catalog_established.
    success = durable and accepted and manual and drained and catalog_ok
    changes = {'phase': 'pause_pending',
               'manual_verified': manual, 'drained': drained}
    if accepted:
        changes['pause_accepted_epoch'] = time.time()
    try:
        record = update_bootstrap(record, **changes)
    except (OSError, BootstrapError):
        success = False
    return {'status': 'bootstrap_paused_catalog' if success else 'bootstrap_pause_pending',
            'observed_epoch': time.time(), 'binding_sha256': record.get('binding_sha256'), 'pause_accepted': accepted,
            'cancellation_persisted': durable, 'manual_verified': manual, 'drained': drained}


def protect_bootstrap() -> dict | None:
    """Independent watchdog only observes or pauses; it has no dispatch path."""
    if not os.path.lexists(bootstrap_path()) and not os.path.lexists(bootstrap_stop_path()):
        return None
    record = None
    try:
        record = read_bootstrap()
        if record is None:
            raise BootstrapError('orphan bootstrap cancellation')
        # A separately reviewed strict transition ends this exception. Normal
        # source/hold/license/catalog checks then apply without reopening it.
        if record['phase'] == 'catalog_established' and read_personal_trial() is not None:
            catalog_size_gib()
            return None
        if bootstrap_cancelled(record) or record['phase'] not in ('dispatching', 'observing'):
            return pause_bootstrap(record, 'bootstrap_cancelled')
        require_bootstrap_binding(record)
        catalog = bootstrap_catalog(strict=record.get('first_catalog_seen') is not None)
        if catalog['catalog_state'] == 'established':
            record = update_bootstrap(record, first_catalog_seen=catalog['catalog_observation'])
            return pause_bootstrap(record, 'first_catalog_seen')
        observation = observe_bootstrap(record)
        if observation['catalog_state'] == 'established':
            record = update_bootstrap(record, first_catalog_seen=observation['catalog_observation'])
            return pause_bootstrap(record, 'first_catalog_seen')
        record = update_bootstrap(record, last_observation=observation)
        if bootstrap_cancelled(record):
            return pause_bootstrap(record, 'bootstrap_cancelled')
        return {'status': 'bootstrap_observing', 'observed_epoch': time.time(),
                'binding_sha256': record['binding_sha256'], 'snapshot': observation}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, ET.ParseError, subprocess.SubprocessError) as exc:
        if record is None:
            # A malformed record grants no exception, but cannot defeat pause.
            record = {'binding_sha256': 'invalid'}
        result = pause_bootstrap(record, 'bootstrap_unverified')
        result['error_type'] = type(exc).__name__
        return result


if __name__ == '__main__':
    # The foreground owner uses this bounded subprocess for slow local probes,
    # continuing its own five-second heartbeat while this read is outstanding.
    if sys.argv[1:] != ['--bootstrap-probe']:
        raise SystemExit(2)
    try:
        attempt = read_bootstrap()
        if attempt is None or bootstrap_cancelled(attempt):
            raise BootstrapError('bootstrap is cancelled or absent')
        print(json.dumps(observe_bootstrap(attempt), sort_keys=True))
    except Exception as exc:
        print(json.dumps({'error_type': type(exc).__name__}))
        raise SystemExit(1)
