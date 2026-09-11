#!/usr/bin/env python3
"""Integrity guard with bounded canonical-template repair for user LaunchAgents.

This detects a failure class in which managed plists are
overwritten with bare JSON arrays containing only ``ProgramArguments``. Jobs
already loaded in launchd kept running from memory, so ordinary service status
did not reveal that the next login or reboot would lose every damaged job.

For each installed ``com.openclaw.*`` or ``ai.openclaw.*`` plist, the guard
requires a parseable dictionary, a filename-matched label, a valid launchd
program declaration whose executable resolves to a regular executable file,
and at least one operative trigger. It also fails closed when any declared
managed plist is missing. Explicitly retired jobs may remain trigger-less.

Exit 0 means healthy. Exit 1 prints a versioned JSON finding to stdout and a
short operator summary to stderr. --repair can restore the explicitly listed
non-PersonalData templates on disk, without changing loaded services.
"""

from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from datetime import datetime, timezone
import json
import os
import plistlib
import shutil
import socket
import sys
import tempfile
import uuid
from pathlib import Path

LAUNCH_AGENTS = OPERATOR.require_path('paths.host_home') / "Library" / "LaunchAgents"
PREFIXES = ("com.openclaw.", "ai.openclaw.")
PAYLOAD_KIND = "openclaw.launchagent-integrity-guard.v1"
GATEWAY_LABEL = OPERATOR.require_string('runtime.gateway_label')
GATEWAY_PORT = OPERATOR.require_int('runtime.gateway_port')
GATEWAY_NODE = OPERATOR.require_path('paths.node_binary')
GATEWAY_PACKAGE_LINK = OPERATOR.require_path('paths.runtime_package_link')
GATEWAY_WORKING_DIRECTORY = OPERATOR.require_path('paths.host_home')
SOURCE_ROOT = OPERATOR.require_path('paths.workspace')
REPAIR_TEMPLATES = {
    OPERATOR.require_string('services.backblaze.watchdog_label'): SOURCE_ROOT / 'launchd' / 'com.openclaw.backblaze-resource-watchdog.plist',
    'com.openclaw.thread-context-rollover-monitor': SOURCE_ROOT / 'launchd' / 'com.openclaw.thread-context-rollover-monitor.plist',
}
REPAIR_EVIDENCE_ROOT = Path((str(OPERATOR.require_path('paths.workspace')) + '/artifacts/launchagent_integrity_repairs'))

# launchd does not inherit the interactive shell PATH. Managed plists should
# use absolute executable paths; this fixed system path is the conservative
# fallback for the command-name form supported by execvp(3).
LAUNCHD_EXECUTABLE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# The adopter inventory is required: a deleted managed definition must stay visible.
EXPECTED_MANAGED_LABELS = frozenset(OPERATOR.require_list('maintenance.expected_managed_labels'))

# Retired definitions must remain explicitly untriggered until separately reviewed.
INTENTIONALLY_UNTRIGGERED = set(OPERATOR.require_list('maintenance.intentionally_untriggered_labels'))

CALENDAR_RANGES = {
    "Minute": (0, 59),
    "Hour": (0, 23),
    "Day": (1, 31),
    "Weekday": (0, 7),
    "Month": (1, 12),
}

SOCKET_STRING_FIELDS = frozenset(
    {
        "SockNodeName",
        "SockPathName",
        "SecureSocketWithKey",
        "MulticastGroup",
    }
)
SOCKET_INTEGER_FIELDS = frozenset({"SockPathOwner", "SockPathGroup", "SockPathMode"})
SOCKET_ENDPOINT_FIELDS = frozenset(
    {"SockNodeName", "SockPathName", "SecureSocketWithKey", "SockServiceName"}
)
MACH_SERVICE_OPTION_FIELDS = frozenset({"ResetAtClose", "HideUntilCheckIn"})
UINT32_MAX = 2**32 - 1


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _resolved_executable_problem(command: str) -> str | None:
    try:
        candidate: Path
        if os.path.isabs(command):
            candidate = Path(command)
        else:
            if os.sep in command or (os.altsep is not None and os.altsep in command):
                return (
                    "relative executable must be a command name resolved on launchd "
                    f"PATH: {command}"
                )
            resolved = shutil.which(command, path=LAUNCHD_EXECUTABLE_PATH)
            if resolved is None:
                return f"relative executable not found on launchd PATH: {command}"
            candidate = Path(resolved)

        if not candidate.exists():
            return f"executable does not exist: {candidate}"
        if not candidate.is_file():
            return f"executable is not a regular file: {candidate}"
        if not os.access(candidate, os.X_OK):
            return f"executable is not executable: {candidate}"
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        return f"executable cannot be inspected: {detail}"
    return None


def _program_problems(data: dict[str, object]) -> list[str]:
    problems: list[str] = []
    program_present = "Program" in data
    arguments_present = "ProgramArguments" in data
    program = data.get("Program")
    arguments = data.get("ProgramArguments")
    executable: str | None = None

    if program_present:
        if not _non_empty_string(program):
            problems.append("Program must be a non-empty string")
        elif not os.path.isabs(program):
            problems.append("Program must be an absolute executable path")
        else:
            executable = program

    if arguments_present:
        if not isinstance(arguments, list):
            problems.append("ProgramArguments must be a list when present")
        elif not arguments:
            if not program_present:
                problems.append("ProgramArguments must not be empty without Program")
        else:
            for index, argument in enumerate(arguments):
                if not isinstance(argument, str):
                    problems.append(f"ProgramArguments[{index}] must be a string")
                elif index == 0 and not program_present and not argument.strip():
                    problems.append("ProgramArguments[0] executable must not be empty")
            if not program_present and _non_empty_string(arguments[0]):
                executable = arguments[0]
    elif not program_present:
        problems.append("Program or ProgramArguments is required")

    if executable is not None:
        executable_problem = _resolved_executable_problem(executable)
        if executable_problem is not None:
            problems.append(executable_problem)

    return problems


def _gateway_direct_owner_problems(data: dict[str, object]) -> list[str]:
    expected_arguments = [
        str(GATEWAY_NODE),
        str(GATEWAY_PACKAGE_LINK / "dist" / "index.js"),
        "gateway",
        "--port",
        str(GATEWAY_PORT),
    ]
    problems: list[str] = []
    if "Program" in data or data.get("ProgramArguments") != expected_arguments:
        problems.append(
            "gateway ProgramArguments must use the exact direct pinned-Node owner"
        )
    if data.get("WorkingDirectory") != str(GATEWAY_WORKING_DIRECTORY):
        problems.append(
            "gateway WorkingDirectory must use the operator home"
        )
    if data.get("RunAtLoad") is not True or data.get("KeepAlive") is not True:
        problems.append(
            "gateway direct owner requires RunAtLoad=true and KeepAlive=true"
        )
    return problems


def _operative_start_interval(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value <= UINT32_MAX
    )


def _operative_keep_alive(value: object) -> bool:
    if value is True:
        return True
    if not isinstance(value, dict) or not value:
        return False
    if isinstance(value.get("SuccessfulExit"), bool):
        return True
    if isinstance(value.get("Crashed"), bool):
        return True
    path_states = value.get("PathState")
    if isinstance(path_states, dict) and path_states and all(
        _non_empty_string(path) and isinstance(expected_state, bool)
        for path, expected_state in path_states.items()
    ):
        return True
    other_jobs = value.get("OtherJobEnabled")
    if isinstance(other_jobs, dict) and other_jobs and all(
        _non_empty_string(label) and isinstance(expected_state, bool)
        for label, expected_state in other_jobs.items()
    ):
        return True
    return False


def _operative_calendar_entry(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    for key, field_value in value.items():
        bounds = CALENDAR_RANGES.get(key)
        if bounds is None:
            return False
        if not isinstance(field_value, int) or isinstance(field_value, bool):
            return False
        if not bounds[0] <= field_value <= bounds[1]:
            return False
    return True


def _operative_calendar(value: object) -> bool:
    if isinstance(value, dict):
        return _operative_calendar_entry(value)
    if isinstance(value, list):
        return bool(value) and all(_operative_calendar_entry(item) for item in value)
    return False


def _operative_paths(value: object) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_non_empty_string(item) for item in value)
    )


def _operative_socket_declarations(value: object) -> bool:
    def valid_service_name(field_value: object) -> bool:
        if isinstance(field_value, int) and not isinstance(field_value, bool):
            return 0 <= field_value <= 65_535
        if not _non_empty_string(field_value):
            return False
        if field_value != field_value.strip():
            return False
        if field_value.isdecimal():
            return 0 <= int(field_value) <= 65_535
        return True

    def valid_field(name: object, field_value: object) -> bool:
        if name in SOCKET_STRING_FIELDS:
            return _non_empty_string(field_value)
        if name == "SockServiceName":
            return valid_service_name(field_value)
        if name == "SockType":
            return isinstance(field_value, str) and field_value in {
                "stream",
                "dgram",
                "seqpacket",
            }
        if name == "SockFamily":
            return isinstance(field_value, str) and field_value in {
                "IPv4",
                "IPv6",
                "IPv4v6",
            }
        if name == "SockProtocol":
            return isinstance(field_value, str) and field_value in {"TCP", "UDP"}
        if name == "SockPassive":
            return isinstance(field_value, bool)
        if name in SOCKET_INTEGER_FIELDS:
            return isinstance(field_value, int) and not isinstance(field_value, bool)
        if name == "Bonjour":
            return (
                isinstance(field_value, bool)
                or _non_empty_string(field_value)
                or isinstance(field_value, list)
                and bool(field_value)
                and all(_non_empty_string(item) for item in field_value)
            )
        return False

    def valid_unix_endpoint(declaration: dict[object, object]) -> bool:
        if "SecureSocketWithKey" in declaration:
            return True
        path_value = declaration.get("SockPathName")
        if not isinstance(path_value, str):
            return False
        path = Path(path_value)
        if not path.is_absolute():
            path = Path("/") / path
        try:
            return (
                len(os.fsencode(path)) < 104
                and path.parent.is_dir()
                and os.access(path.parent, os.W_OK | os.X_OK)
            )
        except OSError:
            return False

    def endpoint_resolves(declaration: dict[object, object]) -> bool:
        if "SockPathName" in declaration or "SecureSocketWithKey" in declaration:
            return valid_unix_endpoint(declaration)

        node = declaration.get("SockNodeName")
        service = declaration.get("SockServiceName", 0)

        family = {
            "IPv4": socket.AF_INET,
            "IPv6": socket.AF_INET6,
            "IPv4v6": socket.AF_UNSPEC,
        }.get(declaration.get("SockFamily"), socket.AF_UNSPEC)
        socket_type = {
            "stream": socket.SOCK_STREAM,
            "dgram": socket.SOCK_DGRAM,
            "seqpacket": socket.SOCK_SEQPACKET,
        }.get(declaration.get("SockType", "stream"), socket.SOCK_STREAM)
        protocol = {
            "TCP": socket.IPPROTO_TCP,
            "UDP": socket.IPPROTO_UDP,
        }.get(declaration.get("SockProtocol"), 0)
        flags = socket.AI_PASSIVE if declaration.get("SockPassive", True) else 0
        try:
            addresses = socket.getaddrinfo(
                node,
                service,
                family,
                socket_type,
                protocol,
                flags,
            )
        except (OSError, OverflowError, TypeError, ValueError):
            return False
        return bool(addresses)

    def valid_dictionary(declaration: object) -> bool:
        return (
            isinstance(declaration, dict)
            and bool(declaration)
            and all(valid_field(name, field_value) for name, field_value in declaration.items())
            and any(name in SOCKET_ENDPOINT_FIELDS for name in declaration)
            and endpoint_resolves(declaration)
        )

    def valid_declaration(declaration: object) -> bool:
        if isinstance(declaration, dict):
            return valid_dictionary(declaration)
        return (
            isinstance(declaration, list)
            and bool(declaration)
            and all(valid_dictionary(item) for item in declaration)
        )

    return (
        isinstance(value, dict)
        and bool(value)
        and all(
            _non_empty_string(name) and valid_declaration(declaration)
            for name, declaration in value.items()
        )
    )


def _operative_mach_services(value: object) -> bool:
    def valid_declaration(declaration: object) -> bool:
        if declaration is True:
            return True
        return (
            isinstance(declaration, dict)
            and declaration.get("HideUntilCheckIn") is not True
            and all(
                name in MACH_SERVICE_OPTION_FIELDS and isinstance(field_value, bool)
                for name, field_value in declaration.items()
            )
        )

    return (
        isinstance(value, dict)
        and bool(value)
        and any(
            _non_empty_string(name) and valid_declaration(declaration)
            for name, declaration in value.items()
        )
    )


def _has_operative_trigger(data: dict[str, object]) -> bool:
    return any(
        (
            data.get("RunAtLoad") is True,
            _operative_keep_alive(data.get("KeepAlive")),
            _operative_start_interval(data.get("StartInterval")),
            _operative_calendar(data.get("StartCalendarInterval")),
            _operative_paths(data.get("WatchPaths")),
            _operative_paths(data.get("QueueDirectories")),
            data.get("StartOnMount") is True,
            _operative_socket_declarations(data.get("Sockets")),
            _operative_mach_services(data.get("MachServices")),
        )
    )


def check(path: Path) -> list[str]:
    problems: list[str] = []
    label = path.stem
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except Exception as exc:  # noqa: BLE001 - any parse failure is the finding
        return [f"does not parse as a plist: {exc}"]

    if not isinstance(data, dict):
        return [f"top level is {type(data).__name__}, expected dict"]
    if data.get("Label") != label:
        problems.append(f"Label {data.get('Label')!r} does not match filename {label!r}")

    problems.extend(_program_problems(data))
    template = REPAIR_TEMPLATES.get(label)
    if template and path.resolve() != template.resolve():
        try:
            expected = plistlib.loads(template.read_bytes())
            if data != expected:
                problems.append("installed definition differs from its canonical maintenance template")
        except (OSError, ValueError, plistlib.InvalidFileException):
            problems.append("canonical maintenance template cannot be read")


    if label == GATEWAY_LABEL:
        problems.extend(_gateway_direct_owner_problems(data))

    if label not in INTENTIONALLY_UNTRIGGERED and not _has_operative_trigger(data):
        problems.append("no operative trigger configured; job can never run")

    return problems



def repair_from_template(path: Path) -> bool:
    """Restore one known template, never arbitrary launchd or PersonalData definitions."""
    template = REPAIR_TEMPLATES.get(path.stem)
    if template is None or check(template) or path.is_symlink():
        return False
    before = path.read_bytes() if path.exists() else None
    expected = template.read_bytes()
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}"
    evidence = REPAIR_EVIDENCE_ROOT / stamp
    evidence.mkdir(parents=True, mode=0o700)
    if before is not None:
        previous = evidence / path.name
        descriptor = os.open(previous, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        (evidence / "previously-absent").touch(mode=0o600)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        # Do not overwrite a concurrent operator/installer change.
        observed = path.read_bytes() if path.exists() else None
        if observed != before or path.is_symlink():
            return False
        os.replace(temporary, path)
        return path.read_bytes() == expected and not check(path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def _payload(
    *,
    checked: int,
    findings: dict[str, list[str]],
    missing: list[str],
) -> dict[str, object]:
    return {
        "kind": PAYLOAD_KIND,
        "checked": checked,
        "expected": len(EXPECTED_MANAGED_LABELS),
        "healthy": not findings,
        "missing": missing,
        "findings": findings,
    }


def _write_failure_summary(*, checked: int, findings: int, missing: int) -> None:
    missing_suffix = ""
    if missing:
        noun = "plist" if missing == 1 else "plists"
        missing_suffix = f" ({missing} expected {noun} missing)"
    sys.stderr.write(
        f"LAUNCHAGENT_INTEGRITY_FAIL: {findings} of {checked} plists have problems"
        f"{missing_suffix}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="Print a concise operator result; retain JSON on stderr.")
    parser.add_argument("--repair", action="store_true", help="Restore only validated non-PersonalData canonical maintenance templates on disk.")
    args = parser.parse_args([] if argv is None else argv)
    if not LAUNCH_AGENTS.is_dir():
        missing = sorted(EXPECTED_MANAGED_LABELS)
        findings = {
            "__launch_agents_directory__": [
                f"LaunchAgents directory is missing: {LAUNCH_AGENTS}"
            ]
        }
        print(
            json.dumps(
                _payload(checked=0, findings=findings, missing=missing),
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr if args.report else sys.stdout,
        )
        if args.report:
            print("Automation integrity check failed: the LaunchAgents folder is missing; scheduled Mac maintenance is not protected across login.")
        noun = "plist" if len(missing) == 1 else "plists"
        sys.stderr.write(
            "LAUNCHAGENT_INTEGRITY_FAIL: LaunchAgents directory is missing; "
            f"{len(missing)} expected {noun} unavailable\n"
        )
        return 1

    managed_paths = {
        path.stem: path
        for path in sorted(LAUNCH_AGENTS.glob("*.plist"))
        if path.name.startswith(PREFIXES)
    }
    missing = sorted(EXPECTED_MANAGED_LABELS.difference(managed_paths))
    findings: dict[str, list[str]] = {
        f"{label}.plist": ["expected managed LaunchAgent is missing"]
        for label in missing
    }

    for path in managed_paths.values():
        problems = check(path)
        if problems:
            findings[path.name] = problems

    repaired = []
    if args.repair:
        for name in list(findings):
            path = LAUNCH_AGENTS / name
            try:
                if repair_from_template(path):
                    repaired.append(path.stem)
                    del findings[name]
                    missing = [label for label in missing if label != path.stem]
            except (OSError, ValueError, plistlib.InvalidFileException):
                findings[name].append("canonical template repair could not complete")
    checked = len(managed_paths) + sum(label not in managed_paths for label in repaired)
    print(
        json.dumps(
            _payload(checked=checked, findings=findings, missing=missing),
            indent=2,
            sort_keys=True,
        ),
        file=sys.stderr if args.report else sys.stdout,
    )
    if args.report:
        if findings:
            names = ", ".join(name.removesuffix(".plist") for name in sorted(findings)[:3])
            print(f"Automation integrity check needs repair: {len(findings)} service definitions failed validation ({names}). Existing running jobs were preserved.")
        elif repaired:
            print(f"Automation integrity check repaired {len(repaired)} maintenance definition(s) and validated all {checked} installed services. Loaded service settings were preserved.")
        else:
            print(f"Automation integrity check passed: all {checked} managed Mac service definitions are valid and their executables are available.")
    if findings:
        _write_failure_summary(
            checked=checked,
            findings=len(findings),
            missing=len(missing),
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
