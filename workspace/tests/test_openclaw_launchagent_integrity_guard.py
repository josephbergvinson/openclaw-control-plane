from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path

import pytest

from scripts import openclaw_launchagent_integrity_guard as guard


DEFAULT_PROGRAM_ARGUMENTS = object()
NO_OPERATIVE_TRIGGER = "no operative trigger configured; job can never run"


def write_plist(path: Path, payload: object) -> Path:
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)
    return path


def write_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def launch_agent(
    label: str,
    *,
    program_arguments: object = DEFAULT_PROGRAM_ARGUMENTS,
    include_trigger: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "Label": label,
        "ProgramArguments": (
            [sys.executable]
            if program_arguments is DEFAULT_PROGRAM_ARGUMENTS
            else program_arguments
        ),
    }
    if include_trigger:
        payload["RunAtLoad"] = True
    return payload


def configure_expected(
    monkeypatch: pytest.MonkeyPatch,
    *labels: str,
) -> None:
    monkeypatch.setattr(guard, "EXPECTED_MANAGED_LABELS", frozenset(labels))


def test_check_accepts_healthy_plist(tmp_path: Path) -> None:
    label = "com.openclaw.healthy"
    path = write_plist(tmp_path / f"{label}.plist", launch_agent(label))

    assert guard.check(path) == []


def direct_gateway_plist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, object]]:
    node = write_executable(tmp_path / "node")
    package_link = tmp_path / "package-link"
    working_directory = tmp_path / "operator-home"
    working_directory.mkdir()
    monkeypatch.setattr(guard, "GATEWAY_NODE", node)
    monkeypatch.setattr(guard, "GATEWAY_PACKAGE_LINK", package_link)
    monkeypatch.setattr(guard, "GATEWAY_WORKING_DIRECTORY", working_directory)
    payload: dict[str, object] = {
        "Label": guard.GATEWAY_LABEL,
        "ProgramArguments": [
            str(node),
            str(package_link / "dist" / "index.js"),
            "gateway",
            "--port",
            str(guard.GATEWAY_PORT),
        ],
        "WorkingDirectory": str(working_directory),
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    return tmp_path / f"{guard.GATEWAY_LABEL}.plist", payload


def test_check_accepts_exact_direct_gateway_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, payload = direct_gateway_plist(tmp_path, monkeypatch)

    assert guard.check(write_plist(path, payload)) == []


@pytest.mark.parametrize("drift", ["python", "working-directory", "keep-alive"])
def test_check_rejects_gateway_supervisor_or_direct_owner_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    path, payload = direct_gateway_plist(tmp_path, monkeypatch)
    if drift == "python":
        payload["ProgramArguments"] = [sys.executable, "gateway-relay.py"]
    elif drift == "working-directory":
        payload["WorkingDirectory"] = str(tmp_path / "physical-release")
    else:
        payload["KeepAlive"] = False

    problems = guard.check(write_plist(path, payload))

    assert any("gateway" in problem for problem in problems)


def test_check_rejects_top_level_plist_array(tmp_path: Path) -> None:
    path = write_plist(tmp_path / "com.openclaw.array.plist", [sys.executable])

    assert guard.check(path) == ["top level is list, expected dict"]


def test_check_rejects_historical_json_array_corruption(tmp_path: Path) -> None:
    path = tmp_path / "com.openclaw.json-array.plist"
    path.write_text(json.dumps([sys.executable]), encoding="utf-8")

    problems = guard.check(path)

    assert len(problems) == 1
    assert problems[0].startswith("does not parse as a plist:")


def test_check_rejects_label_mismatch(tmp_path: Path) -> None:
    path = write_plist(
        tmp_path / "com.openclaw.expected.plist",
        launch_agent("com.openclaw.actual"),
    )

    assert guard.check(path) == [
        "Label 'com.openclaw.actual' does not match filename 'com.openclaw.expected'"
    ]


@pytest.mark.parametrize(
    ("program_arguments", "expected"),
    [
        pytest.param(None, "Program or ProgramArguments is required", id="missing"),
        pytest.param(
            [],
            "ProgramArguments must not be empty without Program",
            id="empty",
        ),
    ],
)
def test_check_rejects_missing_or_empty_program_arguments_without_program(
    tmp_path: Path,
    program_arguments: object,
    expected: str,
) -> None:
    label = "com.openclaw.invalid-arguments"
    payload = launch_agent(label, program_arguments=program_arguments)
    if program_arguments is None:
        payload.pop("ProgramArguments")
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == [expected]


def test_check_accepts_valid_program_only_form(tmp_path: Path) -> None:
    label = "com.openclaw.program-only"
    payload = launch_agent(label)
    payload["Program"] = sys.executable
    payload.pop("ProgramArguments")
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == []


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        pytest.param(42, "Program must be a non-empty string", id="non-string"),
        pytest.param("", "Program must be a non-empty string", id="empty"),
        pytest.param("   ", "Program must be a non-empty string", id="whitespace"),
    ],
)
def test_check_rejects_invalid_program_values(
    tmp_path: Path,
    program: object,
    expected: str,
) -> None:
    label = "com.openclaw.invalid-program"
    payload = launch_agent(label)
    payload["Program"] = program
    payload.pop("ProgramArguments")
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == [expected]


def test_check_rejects_relative_program_even_when_command_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "com.openclaw.relative-program"
    launchd_bin = tmp_path / "launchd-bin"
    launchd_bin.mkdir()
    write_executable(launchd_bin / "managed-command")
    monkeypatch.setattr(guard, "LAUNCHD_EXECUTABLE_PATH", str(launchd_bin))
    payload = launch_agent(label)
    payload["Program"] = "managed-command"
    payload.pop("ProgramArguments")
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == ["Program must be an absolute executable path"]


@pytest.mark.parametrize(
    ("program_arguments", "expected"),
    [
        pytest.param(
            sys.executable,
            "ProgramArguments must be a list when present",
            id="not-a-list",
        ),
        pytest.param(
            [sys.executable, 3],
            "ProgramArguments[1] must be a string",
            id="non-string-element",
        ),
        pytest.param(
            ["", "argument"],
            "ProgramArguments[0] executable must not be empty",
            id="empty-argv-zero",
        ),
    ],
)
def test_check_rejects_malformed_program_arguments(
    tmp_path: Path,
    program_arguments: object,
    expected: str,
) -> None:
    label = "com.openclaw.malformed-arguments"
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=program_arguments),
    )

    assert guard.check(path) == [expected]


def test_check_accepts_empty_non_executable_argument(tmp_path: Path) -> None:
    label = "com.openclaw.empty-argument"
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=[sys.executable, ""]),
    )

    assert guard.check(path) == []


def test_check_accepts_empty_argv_zero_when_program_selects_executable(
    tmp_path: Path,
) -> None:
    label = "com.openclaw.empty-argv-zero-with-program"
    payload = launch_agent(label, program_arguments=[""])
    payload["Program"] = sys.executable
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == []


def test_check_accepts_empty_program_arguments_with_program(tmp_path: Path) -> None:
    label = "com.openclaw.empty-arguments-with-program"
    payload = launch_agent(label, program_arguments=[])
    payload["Program"] = sys.executable
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == []


def test_check_rejects_nonexistent_absolute_executable(tmp_path: Path) -> None:
    label = "com.openclaw.missing-executable"
    missing_executable = tmp_path / "does-not-exist"
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=[str(missing_executable)]),
    )

    assert guard.check(path) == [f"executable does not exist: {missing_executable}"]


def test_check_rejects_directory_as_executable(tmp_path: Path) -> None:
    label = "com.openclaw.directory-executable"
    directory = tmp_path / "not-an-executable"
    directory.mkdir()
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=[str(directory)]),
    )

    assert guard.check(path) == [f"executable is not a regular file: {directory}"]


def test_check_rejects_non_executable_file(tmp_path: Path) -> None:
    label = "com.openclaw.non-executable"
    target = tmp_path / "not-executable"
    target.write_text("not executable\n", encoding="utf-8")
    target.chmod(0o644)
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=[str(target)]),
    )

    assert guard.check(path) == [f"executable is not executable: {target}"]


def test_check_reports_executable_filesystem_probe_error(tmp_path: Path) -> None:
    label = "com.openclaw.uninspectable-executable"
    oversized_path = "/" + "x" * 10_000
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=[oversized_path]),
    )

    assert guard.check(path) == [
        "executable cannot be inspected: File name too long"
    ]


def test_check_resolves_relative_executable_on_controlled_launchd_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "com.openclaw.relative-executable"
    launchd_bin = tmp_path / "launchd-bin"
    launchd_bin.mkdir()
    write_executable(launchd_bin / "managed-command")
    process_bin = tmp_path / "process-bin"
    process_bin.mkdir()
    monkeypatch.setattr(guard, "LAUNCHD_EXECUTABLE_PATH", str(launchd_bin))
    monkeypatch.setenv("PATH", str(process_bin))
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=["managed-command"]),
    )

    assert guard.check(path) == []


def test_check_rejects_relative_executable_missing_from_controlled_launchd_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "com.openclaw.missing-relative-executable"
    launchd_bin = tmp_path / "launchd-bin"
    launchd_bin.mkdir()
    process_bin = tmp_path / "process-bin"
    process_bin.mkdir()
    write_executable(process_bin / "only-on-process-path")
    monkeypatch.setattr(guard, "LAUNCHD_EXECUTABLE_PATH", str(launchd_bin))
    monkeypatch.setenv("PATH", str(process_bin))
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, program_arguments=["only-on-process-path"]),
    )

    assert guard.check(path) == [
        "relative executable not found on launchd PATH: only-on-process-path"
    ]


def test_check_rejects_triggerless_plist(tmp_path: Path) -> None:
    label = "com.openclaw.never-runs"
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, include_trigger=False),
    )

    assert guard.check(path) == [NO_OPERATIVE_TRIGGER]


@pytest.mark.parametrize(
    ("trigger_key", "trigger_value"),
    [
        pytest.param("RunAtLoad", False, id="run-at-load-false"),
        pytest.param("KeepAlive", False, id="keep-alive-false"),
        pytest.param("KeepAlive", {}, id="keep-alive-empty-dict"),
        pytest.param("StartInterval", 0, id="interval-zero"),
        pytest.param(
            "StartInterval",
            2**32,
            id="interval-over-uint32-max",
        ),
        pytest.param("StartCalendarInterval", [], id="calendar-empty-list"),
        pytest.param("WatchPaths", [], id="watch-paths-empty"),
        pytest.param(
            "WatchPaths",
            ["/tmp/guard-test", 3],
            id="watch-paths-invalid-member",
        ),
        pytest.param("QueueDirectories", [""], id="queue-directories-empty-path"),
        pytest.param("StartOnMount", False, id="start-on-mount-false"),
        pytest.param("Sockets", {}, id="sockets-empty"),
        pytest.param(
            "Sockets",
            {"Listener": {}},
            id="socket-declaration-empty",
        ),
        pytest.param(
            "Sockets",
            {"Listener": True},
            id="socket-declaration-wrong-type",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockPathName": 42}},
            id="socket-path-wrong-type",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": 70_000}},
            id="socket-port-out-of-range",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": "70000"}},
            id="socket-numeric-service-out-of-range",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": " 70000 "}},
            id="socket-padded-numeric-service-out-of-range",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": "+70000"}},
            id="socket-signed-numeric-service-out-of-range",
        ),
        pytest.param(
            "Sockets",
            {
                "Listener": {
                    "SockServiceName": 8080,
                    "SockProtocol": "UDP",
                }
            },
            id="socket-default-stream-with-udp",
        ),
        pytest.param(
            "Sockets",
            {
                "Listener": {
                    "SockServiceName": 8080,
                    "SockType": "dgram",
                    "SockProtocol": "TCP",
                }
            },
            id="socket-dgram-with-tcp",
        ),
        pytest.param(
            "Sockets",
            {
                "Listener": {
                    "SockNodeName": "::1",
                    "SockServiceName": 0,
                    "SockFamily": "IPv4",
                }
            },
            id="socket-node-family-mismatch",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": 0, "SockType": []}},
            id="socket-type-unhashable",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": 0, "SockFamily": []}},
            id="socket-family-unhashable",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": 0, "SockProtocol": []}},
            id="socket-protocol-unhashable",
        ),
        pytest.param(
            "MachServices",
            {"com.openclaw.noop": False},
            id="mach-service-false",
        ),
        pytest.param(
            "MachServices",
            {"com.openclaw.noop": "true"},
            id="mach-service-wrong-type",
        ),
        pytest.param(
            "MachServices",
            {"com.openclaw.hidden": {"HideUntilCheckIn": True}},
            id="mach-service-hidden-until-check-in",
        ),
    ],
)
def test_check_rejects_present_but_inoperative_trigger_values(
    tmp_path: Path,
    trigger_key: str,
    trigger_value: object,
) -> None:
    label = "com.openclaw.inoperative-trigger"
    payload = launch_agent(label, include_trigger=False)
    payload[trigger_key] = trigger_value
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == [NO_OPERATIVE_TRIGGER]


def test_check_rejects_unix_socket_with_missing_parent(tmp_path: Path) -> None:
    label = "com.openclaw.unbindable-unix-socket"
    missing_socket = tmp_path / "missing-parent" / "listener.sock"
    payload = launch_agent(label, include_trigger=False)
    payload["Sockets"] = {"Listener": {"SockPathName": str(missing_socket)}}
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == [NO_OPERATIVE_TRIGGER]


@pytest.mark.parametrize(
    ("trigger_key", "trigger_value"),
    [
        pytest.param("RunAtLoad", True, id="run-at-load"),
        pytest.param("KeepAlive", True, id="keep-alive"),
        pytest.param(
            "KeepAlive",
            {"PathState": {"/tmp/guard-test": True}},
            id="path-state-present",
        ),
        pytest.param(
            "KeepAlive",
            {"PathState": {"/tmp/guard-test": False}},
            id="path-state-absent",
        ),
        pytest.param(
            "KeepAlive",
            {"SuccessfulExit": False},
            id="unsuccessful-exit",
        ),
        pytest.param(
            "KeepAlive",
            {"OtherJobEnabled": {"com.openclaw.other": True}},
            id="other-job-enabled",
        ),
        pytest.param(
            "KeepAlive",
            {"OtherJobEnabled": {"com.openclaw.other": False}},
            id="other-job-disabled",
        ),
        pytest.param("StartInterval", 30, id="interval"),
        pytest.param("StartInterval", 2**32 - 1, id="interval-uint32-max"),
        pytest.param(
            "StartCalendarInterval",
            {},
            id="calendar-all-wildcards",
        ),
        pytest.param(
            "StartCalendarInterval",
            {"Hour": 0, "Minute": 0},
            id="calendar-dict-with-valid-zero-fields",
        ),
        pytest.param(
            "StartCalendarInterval",
            [{"Hour": 1}, {"Hour": 2}],
            id="calendar-list",
        ),
        pytest.param(
            "StartCalendarInterval",
            [{}],
            id="calendar-list-all-wildcards",
        ),
        pytest.param("WatchPaths", ["/tmp/guard-test"], id="watch-paths"),
        pytest.param(
            "QueueDirectories",
            ["/tmp/guard-test"],
            id="queue-directories",
        ),
        pytest.param("StartOnMount", True, id="start-on-mount"),
        pytest.param(
            "Sockets",
            {"Listener": {"SockPathName": "/tmp/guard-test.sock"}},
            id="socket",
        ),
        pytest.param(
            "Sockets",
            {
                "Listeners": [
                    {"SockServiceName": 8080},
                    {"SockServiceName": 8081},
                ]
            },
            id="socket-array",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": 0}},
            id="socket-dynamic-port",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockServiceName": "0"}},
            id="socket-dynamic-port-string",
        ),
        pytest.param(
            "Sockets",
            {
                "Listener": {
                    "SockServiceName": 0,
                    "SockType": "dgram",
                    "SockProtocol": "UDP",
                }
            },
            id="socket-dynamic-udp-port",
        ),
        pytest.param(
            "Sockets",
            {"Listener": {"SockNodeName": "127.0.0.1", "SockFamily": "IPv4"}},
            id="socket-node-dynamic-port",
        ),
        pytest.param(
            "MachServices",
            {"com.openclaw.service": True},
            id="mach-service",
        ),
        pytest.param(
            "MachServices",
            {"com.openclaw.service": {"ResetAtClose": False}},
            id="mach-service-options",
        ),
        pytest.param(
            "MachServices",
            {"com.openclaw.service": {}},
            id="mach-service-empty-options",
        ),
        pytest.param(
            "MachServices",
            {
                "com.openclaw.visible": True,
                "com.openclaw.hidden": {"HideUntilCheckIn": True},
            },
            id="mach-service-visible-and-hidden",
        ),
    ],
)
def test_check_accepts_supported_operative_triggers(
    tmp_path: Path,
    trigger_key: str,
    trigger_value: object,
) -> None:
    label = "com.openclaw.operative-trigger"
    payload = launch_agent(label, include_trigger=False)
    payload[trigger_key] = trigger_value
    path = write_plist(tmp_path / f"{label}.plist", payload)

    assert guard.check(path) == []


def test_check_allows_explicit_retired_external_volume_guard_exception(
    tmp_path: Path,
) -> None:
    label = "com.openclaw.external-volume-guard"
    path = write_plist(
        tmp_path / f"{label}.plist",
        launch_agent(label, include_trigger=False),
    )

    assert guard.check(path) == []


def test_retired_rollover_template_has_no_automatic_trigger() -> None:
    label = "com.openclaw.thread-context-rollover-monitor"
    template = guard.REPAIR_TEMPLATES[label]
    payload = plistlib.loads(template.read_bytes())

    assert not {
        "RunAtLoad",
        "KeepAlive",
        "StartInterval",
        "StartCalendarInterval",
        "WatchPaths",
        "QueueDirectories",
        "StartOnMount",
        "Sockets",
        "MachServices",
    }.intersection(payload)
    assert guard.check(template) == []


def test_repair_retires_stale_scheduled_rollover_definition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = "com.openclaw.thread-context-rollover-monitor"
    template = guard.REPAIR_TEMPLATES[label]
    scheduled = plistlib.loads(template.read_bytes())
    scheduled.update(RunAtLoad=True, StartInterval=120)
    installed = write_plist(tmp_path / f"{label}.plist", scheduled)
    monkeypatch.setattr(guard, "REPAIR_EVIDENCE_ROOT", tmp_path / "repair-evidence")

    assert guard.repair_from_template(installed)
    repaired = plistlib.loads(installed.read_bytes())
    assert "RunAtLoad" not in repaired
    assert "StartInterval" not in repaired
    assert guard.check(installed) == []


def test_main_returns_zero_with_typed_healthy_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_labels = {"com.openclaw.alpha", "ai.openclaw.beta"}
    for label in expected_labels:
        write_plist(tmp_path / f"{label}.plist", launch_agent(label))
    write_plist(
        tmp_path / "org.example.ignored.plist",
        launch_agent("org.example.ignored"),
    )
    configure_expected(monkeypatch, *expected_labels)
    monkeypatch.setattr(guard, "LAUNCH_AGENTS", tmp_path)

    assert guard.main() == 0

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload == {
        "checked": 2,
        "expected": 2,
        "findings": {},
        "healthy": True,
        "kind": "openclaw.launchagent-integrity-guard.v1",
        "missing": [],
    }
    assert output.err == ""


def test_main_returns_one_with_typed_findings_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_labels = {"com.openclaw.good", "com.openclaw.bad"}
    write_plist(
        tmp_path / "com.openclaw.good.plist",
        launch_agent("com.openclaw.good"),
    )
    write_plist(
        tmp_path / "com.openclaw.bad.plist",
        launch_agent("com.openclaw.wrong"),
    )
    configure_expected(monkeypatch, *expected_labels)
    monkeypatch.setattr(guard, "LAUNCH_AGENTS", tmp_path)

    assert guard.main() == 1

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload == {
        "checked": 2,
        "expected": 2,
        "findings": {
            "com.openclaw.bad.plist": [
                "Label 'com.openclaw.wrong' does not match filename "
                "'com.openclaw.bad'"
            ]
        },
        "healthy": False,
        "kind": "openclaw.launchagent-integrity-guard.v1",
        "missing": [],
    }
    assert output.err == "LAUNCHAGENT_INTEGRITY_FAIL: 1 of 2 plists have problems\n"


def test_main_fails_closed_when_no_expected_plists_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_labels = {"com.openclaw.alpha", "ai.openclaw.beta"}
    configure_expected(monkeypatch, *expected_labels)
    monkeypatch.setattr(guard, "LAUNCH_AGENTS", tmp_path)

    assert guard.main() == 1

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["kind"] == "openclaw.launchagent-integrity-guard.v1"
    assert payload["checked"] == 0
    assert payload["expected"] == 2
    assert payload["healthy"] is False
    assert payload["missing"] == sorted(expected_labels)
    assert payload["findings"] == {
        f"{label}.plist": ["expected managed LaunchAgent is missing"]
        for label in sorted(expected_labels)
    }
    assert output.err == (
        "LAUNCHAGENT_INTEGRITY_FAIL: 2 of 0 plists have problems "
        "(2 expected plists missing)\n"
    )


def test_main_fails_closed_when_one_expected_plist_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_labels = {"com.openclaw.present", "com.openclaw.missing"}
    write_plist(
        tmp_path / "com.openclaw.present.plist",
        launch_agent("com.openclaw.present"),
    )
    configure_expected(monkeypatch, *expected_labels)
    monkeypatch.setattr(guard, "LAUNCH_AGENTS", tmp_path)

    assert guard.main() == 1

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload["checked"] == 1
    assert payload["missing"] == ["com.openclaw.missing"]
    assert payload["findings"] == {
        "com.openclaw.missing.plist": ["expected managed LaunchAgent is missing"]
    }
    assert output.err == (
        "LAUNCHAGENT_INTEGRITY_FAIL: 1 of 1 plists have problems "
        "(1 expected plist missing)\n"
    )


def test_main_missing_launchagents_directory_emits_versioned_typed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing_directory = tmp_path / "missing-launch-agents"
    expected_labels = {"com.openclaw.expected"}
    configure_expected(monkeypatch, *expected_labels)
    monkeypatch.setattr(guard, "LAUNCH_AGENTS", missing_directory)

    assert guard.main() == 1

    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert payload == {
        "checked": 0,
        "expected": 1,
        "findings": {
            "__launch_agents_directory__": [
                f"LaunchAgents directory is missing: {missing_directory}"
            ]
        },
        "healthy": False,
        "kind": "openclaw.launchagent-integrity-guard.v1",
        "missing": ["com.openclaw.expected"],
    }
    assert output.err == (
        "LAUNCHAGENT_INTEGRITY_FAIL: LaunchAgents directory is missing; "
        "1 expected plist unavailable\n"
    )


def test_declared_expected_labels_are_locked_to_reviewed_inventory() -> None:
    assert guard.EXPECTED_MANAGED_LABELS == frozenset(guard.OPERATOR.require_list('maintenance.expected_managed_labels'))
    assert 'com.openclaw.unregistered-fixture' not in guard.EXPECTED_MANAGED_LABELS


def test_guard_uses_activation_owner_node_pin():
    from scripts import openclaw_runtime_activate as activation
    assert guard.GATEWAY_NODE == activation.OPERATOR.require_path('paths.node_binary')


def test_human_report_keeps_diagnostics_private(tmp_path, monkeypatch, capsys):
    label = "com.openclaw.fixture"
    configure_expected(monkeypatch, label)
    monkeypatch.setattr(guard, "LAUNCH_AGENTS", tmp_path)
    write_plist(tmp_path / f"{label}.plist", launch_agent(label))
    assert guard.main(["--report"]) == 0
    captured = capsys.readouterr()
    assert "integrity check passed" in captured.out
    assert '"healthy": true' in captured.err
    assert "{" not in captured.out


def test_repair_restores_only_known_template_and_keeps_before_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    installed = tmp_path / "installed"
    installed.mkdir()
    label = "com.openclaw.fixture"
    template = write_plist(source / f"{label}.plist", launch_agent(label))
    target = installed / template.name
    target.write_bytes(b"broken")
    monkeypatch.setattr(guard, "REPAIR_TEMPLATES", {label: template})
    monkeypatch.setattr(guard, "REPAIR_EVIDENCE_ROOT", tmp_path / "evidence")
    assert guard.repair_from_template(target)
    assert target.read_bytes() == template.read_bytes()
    assert next((tmp_path / "evidence").glob("*/*.plist")).read_bytes() == b"broken"
    assert not guard.repair_from_template(installed / "com.openclaw.personal-data-unknown.plist")


def test_repair_rejects_symlink_or_invalid_template(tmp_path, monkeypatch):
    label = "com.openclaw.fixture"
    source = tmp_path / "source"
    source.mkdir()
    template = source / f"{label}.plist"
    template.write_bytes(b"invalid")
    target = tmp_path / f"{label}.plist"
    target.write_bytes(b"before")
    monkeypatch.setattr(guard, "REPAIR_TEMPLATES", {label: template})
    assert not guard.repair_from_template(target)
    write_plist(template, launch_agent(label))
    target.unlink()
    target.symlink_to(template)
    assert not guard.repair_from_template(target)


@pytest.mark.parametrize("existed", [True, False])
def test_main_repair_counts_each_installed_plist_once(tmp_path, monkeypatch, capsys, existed):
    label = "com.openclaw.fixture"
    source = tmp_path / "source"
    source.mkdir()
    template = write_plist(source / f"{label}.plist", launch_agent(label))
    installed = tmp_path / "installed"
    installed.mkdir()
    if existed:
        (installed / template.name).write_bytes(b"broken")
    monkeypatch.setattr(guard, "REPAIR_TEMPLATES", {label: template})
    monkeypatch.setattr(guard, "REPAIR_EVIDENCE_ROOT", tmp_path / "evidence")
    monkeypatch.setattr(guard, "LAUNCH_AGENTS", installed)
    configure_expected(monkeypatch, label)
    assert guard.main(["--repair"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["checked"] == 1
    assert report["healthy"] is True
    assert report["missing"] == []


def test_disabled_legacy_native_launcher_is_intentionally_untriggered(tmp_path):
    label = "ai.openclaw.mac"
    payload = launch_agent(label, include_trigger=False)
    payload.update(Disabled=True, RunAtLoad=False)
    path = write_plist(tmp_path / f"{label}.plist", payload)
    assert guard.check(path) == []


@pytest.mark.parametrize("label,disabled", [("ai.openclaw.mac", False),
                                           ("ai.openclaw.mac", None),
                                           ("com.openclaw.other", True)])
def test_legacy_launcher_retirement_does_not_hide_untriggered_active_jobs(tmp_path, label, disabled):
    payload = launch_agent(label, include_trigger=False)
    payload.update(Disabled=disabled, RunAtLoad=False)
    if disabled is None:
        payload.pop("Disabled")
    path = write_plist(tmp_path / f"{label}.plist", payload)
    assert NO_OPERATIVE_TRIGGER in guard.check(path)


def test_disabled_native_launcher_still_validates_executable(tmp_path):
    label = "ai.openclaw.mac"
    payload = launch_agent(label, program_arguments=[str(tmp_path / "missing")], include_trigger=False)
    payload.update(Disabled=True, RunAtLoad=False)
    path = write_plist(tmp_path / f"{label}.plist", payload)
    assert guard.check(path)
