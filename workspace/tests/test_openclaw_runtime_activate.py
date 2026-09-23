from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import inspect
import io
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "openclaw_runtime_activate.py"
PREDECESSOR_COMMIT = "1" * 40
CANDIDATE_COMMIT = "2" * 40
SENSITIVE_TEST_PROFILE_IDS = (
    "openai:owner-one@example.invalid",
    "openai:owner-two@example.invalid",
    "openai:owner-three@example.invalid",
)
LEGACY_OPENAI_AUTH_ORDER_COUNT = 2
LEGACY_OPENAI_AUTH_ORDER_SHA256 = (
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
)


def approval_report(
    *, semantic_sha256: str = "a" * 64, raw_cas_sha256: str = "b" * 64
) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "databasePresent": True,
        "rowPresent": True,
        "rowCount": 1,
        "currentKeyOnly": True,
        "legacySourceAbsent": True,
        "doctorClaimAbsent": True,
        "schemaValid": True,
        "projectionsValid": True,
        "expectedSocketPathMatched": True,
        "tokenPresent": True,
        "agentCount": 1,
        "allowlistCount": 64,
        "semanticSha256": semantic_sha256,
        "rawCasSha256": raw_cas_sha256,
        "authProfileStateReadable": True,
        "authProfileStoreReadable": True,
        "openAIProfileOrderCount": activate_module.expected_auth_order_count(),
        "openAIProfileOrderSha256": activate_module.expected_auth_order_sha256(),
        "openAIProfileOrderCredentialsUsable": True,
    }


def load_module():
    spec = importlib.util.spec_from_file_location("scripts.openclaw_activate_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


activate_module = load_module()


def chmod_tree(root: Path, writable: bool) -> None:
    for directory, directories, files in os.walk(root, topdown=False):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o644 if writable else (0o555 if path.name == "openclaw.mjs" else 0o444))
        for name in directories:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o755 if writable else 0o555)
    root.chmod(0o755 if writable else 0o555)


def make_release(
    root: Path,
    name: str,
    commit: str,
    *,
    include_candidate_native_entrypoints: bool = True,
) -> Path:
    release = root / name
    (release / "dist").mkdir(parents=True)
    (release / "dist" / "build-info.json").write_text(json.dumps({"commit": commit}) + "\n")
    (release / "dist" / "index.js").write_text("export {};\n")
    (release / "openclaw.mjs").write_text("#!/usr/bin/env node\n")
    (release / "package.json").write_text(json.dumps({"name": "openclaw", "version": "2026.8.1-beta.3"}) + "\n")
    (release / "NOTICE").write_text(f"release {name}\n")
    if include_candidate_native_entrypoints:
        chrome_extension = (
            release / "dist" / "extensions" / "browser" / "chrome-extension"
        )
        chrome_extension.mkdir(parents=True)
        (chrome_extension / "manifest.json").write_text(
            json.dumps({"manifest_version": 3, "name": "OpenClaw Browser Relay"})
            + "\n"
        )
        (chrome_extension / "background.js").write_text("export {};\n")
    ai_package = release / "packages" / "ai"
    ai_dist = ai_package / "dist"
    ai_internal = ai_dist / "internal"
    ai_internal.mkdir(parents=True)
    (ai_package / "package.json").write_text(
        json.dumps(
            {
                "name": "@openclaw/ai",
                "exports": {
                    ".": {
                        "import": "./dist/index.mjs",
                        "default": "./dist/index.mjs",
                    },
                    "./internal/openai-responses-payload-policy": {
                        "import": "./dist/internal/openai-responses-payload-policy.mjs",
                        "default": "./dist/internal/openai-responses-payload-policy.mjs",
                    },
                },
            }
        )
        + "\n"
    )
    (ai_dist / "index.mjs").write_text("export {};\n")
    (ai_internal / "openai-responses-payload-policy.mjs").write_text(
        "export {};\n"
    )
    ai_link = release / "node_modules" / "@openclaw" / "ai"
    ai_link.parent.mkdir(parents=True)
    ai_link.symlink_to("../../packages/ai")
    if include_candidate_native_entrypoints:
        inspector = release / "dist" / "infra" / "exec-approvals-inspection.js"
        inspector.parent.mkdir(parents=True)
        inspector.write_text(
            "export function inspectExecApprovalsState() { return "
            f"{json.dumps(approval_report(), separators=(',', ':'))}; }}\n"
        )
    for plugin_id in activate_module.required_bundled_extensions():
        extension = release / "dist" / "extensions" / plugin_id
        extension.mkdir(parents=True)
        (extension / "index.js").write_text(f"export const id = {plugin_id!r};\n")
        (extension / "openclaw.plugin.json").write_text(json.dumps({"id": plugin_id}) + "\n")
        (extension / "package.json").write_text(json.dumps({"name": f"@openclaw/{plugin_id}", "version": "2026.8.1-beta.3"}) + "\n")
    chmod_tree(release, False)
    return release


@dataclass
class Fixture:
    root: Path
    paths: object
    predecessor: Path
    candidate: Path
    seal: Path
    snapshot: Path
    legacy_exec_approvals_path: Path
    external_marker: Path
    config_bytes: bytes


class FakeBackend:
    def __init__(self, fixture: Fixture, *, gateway_failure: bool = False,
                 node_bootstrap_failure: bool = False,
                 node_verification_failure: bool = False,
                 discord_verification_failure: bool = False,
                 approval_reports: list[dict[str, object]] | None = None):
        self.fixture = fixture
        self.gateway_failure = gateway_failure
        self.node_bootstrap_failure = node_bootstrap_failure
        self.node_verification_failure = node_verification_failure
        self.discord_verification_failure = discord_verification_failure
        self.command_evidence: list[dict[str, object]] = []
        self.gateway_boots = 0
        self.node_boots = 0
        self.verifications = 0
        self.stopped_checks = 0
        self.inspection_calls = 0
        self.events: list[str] = []
        self.approval_reports = approval_reports or [approval_report()]
        self.install_records = {"whatsapp": {"source": "npm", "version": "0"}}
        self._bootstrap_bindings: dict[str, dict[str, object]] = {}
        self.receipt_bindings: list[tuple[str, bool]] = []

    def _bootstrap_binding(self) -> dict[str, object]:
        selector_info = os.lstat(self.fixture.paths.current_link)
        release = self.fixture.paths.current_link.resolve(strict=True)
        release_info = os.lstat(release)
        return {
            "release": str(release),
            "releaseDevice": release_info.st_dev,
            "releaseInode": release_info.st_ino,
            "selectorDevice": selector_info.st_dev,
            "selectorInode": selector_info.st_ino,
            "startedAtUs": 1,
        }

    def _bootstrap_evidence(self, label: str, returncode: int):
        plist = (
            self.fixture.paths.gateway_plist
            if label == activate_module.OPERATOR.require_string('runtime.gateway_label')
            else self.fixture.paths.node_plist
        )
        prefix = "gateway" if label == activate_module.OPERATOR.require_string('runtime.gateway_label') else "node"
        argv = (
            "/bin/launchctl",
            "bootstrap",
            f"gui/{self.fixture.paths.operator_uid}",
            str(plist),
        )
        return activate_module.CommandResult(
            argv,
            returncode,
            b"",
            b"",
            1,
            False,
        ).evidence(f"{prefix}_bootstrap")

    def assert_gateway_and_node_stopped(self):
        self.stopped_checks += 1
        self.events.append("stopped")
        self.command_evidence.extend(
            activate_module.expected_stopped_command_evidence(self.fixture.paths)
        )

    def verify_screen_capture_continuity(self):
        return None

    def inspect_exec_approvals(self, release):
        self.events.append("inspect")
        index = min(self.inspection_calls, len(self.approval_reports) - 1)
        self.inspection_calls += 1
        assert release == self.fixture.candidate
        self.command_evidence.append(
            activate_module.CommandResult(
                ("/fake/node", "inspect"), 0, b"{}", b"", 1, False
            ).evidence("exec_approvals_inspection")
        )
        return activate_module.validate_exec_approvals_inspection(
            json.loads(json.dumps(self.approval_reports[index]))
        )

    def verify_candidate_discord(self, release, config_path):
        before = json.loads(json.dumps(self.install_records))
        if self.discord_verification_failure or "discord" in self.install_records:
            raise activate_module.ActivationError("private Discord verification diagnostic")
        assert json.loads(json.dumps(self.install_records)) == before
        assert config_path.read_bytes() == self.fixture.config_bytes
        return {
            "installRecordAbsent": True,
            "installedIds": sorted(self.install_records),
            "configSha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
            "origin": "bundled",
            "source": str(release / "dist" / "extensions" / "discord" / "index.js"),
        }

    def bootstrap_gateway_once(self):
        self.gateway_boots += 1
        if self.gateway_failure:
            self.command_evidence.append(
                self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.gateway_label'), 1)
            )
            raise activate_module.ActivationError("private gateway diagnostic")
        self.command_evidence.append(
            self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.gateway_label'), 0)
        )
        self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.gateway_label')] = (
            self._bootstrap_binding()
        )

    def bootstrap_node_once(self):
        self.node_boots += 1
        if self.node_bootstrap_failure:
            self.command_evidence.append(
                self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.node_label'), 1)
            )
            raise activate_module.ActivationError("private node bootstrap diagnostic")
        self.command_evidence.append(
            self._bootstrap_evidence(activate_module.OPERATOR.require_string('runtime.node_label'), 0)
        )
        self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.node_label')] = (
            self._bootstrap_binding()
        )

    def bootstrap_bindings(self):
        return {
            label: dict(binding)
            for label, binding in self._bootstrap_bindings.items()
        }

    def bind_bootstrap_receipt(self, label, binding, loaded=None):
        self._bootstrap_bindings[label] = dict(binding)
        self.receipt_bindings.append((label, loaded is not None))

    def verify(self, release, device, inode):
        self.verifications += 1
        info = os.lstat(release)
        assert (info.st_dev, info.st_ino) == (device, inode)
        binding = self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.gateway_label')]
        return {
            "loaded": {"release": str(release), "releaseDevice": device,
                       "releaseInode": inode, "pid": 100, "runs": 1,
                       "startToken": "darwin:2:0",
                       "bootstrapBinding": dict(binding)},
            "health": {"healthz": {"accepted": True, "statusCode": 200}, "readyz": {"accepted": True, "statusCode": 200}},
        }

    def verify_node(self, release, device, inode):
        self.verifications += 1
        info = os.lstat(release)
        assert (info.st_dev, info.st_ino) == (device, inode)
        if self.node_verification_failure:
            raise activate_module.ActivationError("private node diagnostic")
        binding = self._bootstrap_bindings[activate_module.OPERATOR.require_string('runtime.node_label')]
        return {"release": str(release), "releaseDevice": device,
                "releaseInode": inode, "pid": 200, "runs": 1,
                "startToken": "darwin:2:0",
                "bootstrapBinding": dict(binding)}


class StoppedRestoreBackend(FakeBackend):
    def _print(self, label):
        paths = self.fixture.paths
        argv = ("/bin/launchctl", "print", f"gui/{paths.operator_uid}/{label}")
        stderr = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{paths.operator_uid}\n"
        ).encode()
        result = activate_module.CommandResult(argv, 113, b"", stderr, 1, False)
        self.command_evidence.append(result.evidence(f"{label}_launchctl_print"))
        return result

    def _service_missing(self, result, label):
        paths = self.fixture.paths
        expected = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{paths.operator_uid}\n"
        ).encode()
        return result.returncode == 113 and result.stdout == b"" and result.stderr == expected


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch) -> Fixture:
    # The real Darwin volume query is an environmental boundary. These owner
    # tests also run on Linux; remount tests vary this UUID explicitly below.
    if hasattr(activate_module, "_history_volume_guard"):
        monkeypatch.setattr(activate_module._history_volume_guard(), "read_volume_uuid",
                            lambda _: "11111111-2222-3333-4444-555555555555")
    releases = tmp_path / "runtime" / "Releases"
    releases.mkdir(parents=True)
    predecessor = make_release(
        releases,
        "openclaw-predecessor",
        PREDECESSOR_COMMIT,
        include_candidate_native_entrypoints=False,
    )
    candidate = make_release(releases, "openclaw-candidate", CANDIDATE_COMMIT)
    runtime = tmp_path / "runtime"
    current = runtime / "current"
    current.symlink_to(predecessor)
    gateway_working_directory = current.parent.parent
    cli = tmp_path / "cli"
    package_link = cli / "lib" / "node_modules" / "openclaw"
    package_link.parent.mkdir(parents=True)
    package_link.symlink_to(current)
    bin_link = cli / "bin" / "openclaw"
    bin_link.parent.mkdir(parents=True)
    bin_link.symlink_to(current / "openclaw.mjs")
    node_root = cli / "tools" / "node-v24.15.0"
    node = node_root / "bin" / "node"
    node.parent.mkdir(parents=True)
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o555)
    node_alias = cli / "tools" / "node"
    node_alias.symlink_to(node_root)

    state = tmp_path / "owc-state"
    state.mkdir()
    legacy_exec_approvals_path = tmp_path / "legacy-state" / "exec-approvals.json"
    sessions = state / "agents" / "main" / "sessions"
    sessions.mkdir(parents=True)
    reservations = sessions / "sessions.json.quarantine-reservations.json"
    reservations.write_text(json.dumps({"storePath": str(sessions / "sessions.json")}) + "\n")
    reservations.chmod(0o600)
    from scripts.operator_contract import OperatorContract
    configured = {section: activate_module.OPERATOR.get(section, {}) for section in ("paths", "runtime", "identifiers")}
    configured["paths"].update({"host_home": str(tmp_path), "session_reservations": str(reservations), "session_store": str(sessions / "sessions.json"), "legacy_exec_approvals": str(legacy_exec_approvals_path)})
    configured["runtime"].update({"session_reservations_mode": "required", "session_reservations_sha256": hashlib.sha256(reservations.read_bytes()).hexdigest()})
    monkeypatch.setattr(activate_module, "OPERATOR", OperatorContract(configured))
    config = {
        "auth": {},
        "skills": {"entries": {"goplaces": {"enabled": False}}},
        "plugins": {
            "entries": {plugin_id: {"enabled": True} for plugin_id in activate_module.required_bundled_extensions()},
            "load": {"paths": []},
        },
    }
    config_path = state / "openclaw.json"
    config_path.write_text(json.dumps(config, sort_keys=True) + "\n")
    config_path.chmod(0o600)
    external_marker = state / "unchanged-external-package.marker"
    external_marker.write_text("preserve exact external package\n")
    external_runtime_assets = tmp_path / "external-runtime-assets"
    external_runtime_assets.mkdir()
    external_runtime_sentinel = external_runtime_assets / "sentinel.txt"
    external_runtime_sentinel.write_text("preserve external target bytes and inode\n")
    (state / "external-runtime-assets").symlink_to(
        external_runtime_assets,
        target_is_directory=True,
    )

    gateway_plist = tmp_path / "gateway.plist"
    node_plist = tmp_path / "node.plist"
    common_environment = {
        "OPENCLAW_STATE_DIR": str(state),
        "OPENCLAW_SUPERVISOR_MODE": "external",
        "OPENCLAW_SERVICE_REPAIR_POLICY": "external",
    }
    gateway_plist.write_bytes(
        plistlib.dumps(
            {
                "EnvironmentVariables": common_environment,
                "ProgramArguments": [
                    str(node),
                    str(package_link / "dist" / "index.js"),
                    "gateway",
                    "--port",
                    str(activate_module.runtime_gateway_port()),
                ],
                "WorkingDirectory": str(gateway_working_directory),
                "RunAtLoad": True,
                "KeepAlive": True,
            }
        )
    )
    node_plist.write_bytes(plistlib.dumps({"EnvironmentVariables": common_environment, "ProgramArguments": [str(node), str(package_link / "dist" / "index.js"), "node", "run"]}))
    gateway_plist.chmod(0o600)
    node_plist.chmod(0o600)
    subprocess.run(
        [
            "/usr/bin/xattr",
            "-w",
            "com.openclaw.snapshot-test",
            "preserve exact gateway metadata",
            str(gateway_plist),
        ],
        check=True,
        capture_output=True,
    )
    paths = activate_module.ActivationPaths(
        releases, current, package_link, bin_link, gateway_plist, node_plist,
        node, node_alias, state, state, runtime / "activation.lock",
        runtime / "activation-result.json", os.getuid(), 1.0, 1.0, 0.01,
    )
    seal = tmp_path / "candidate-seal.json"
    activate_module.create_candidate_seal(paths, candidate, CANDIDATE_COMMIT, seal)
    archive = tmp_path / "stopped-snapshot.tar"
    subprocess.run(
        [
            "/usr/bin/tar", "--xattrs", "--acls", "--fflags", "-cpf",
            str(archive), "-C", "/",
            str(state).lstrip("/"),
            str(gateway_plist).lstrip("/"),
            str(node_plist).lstrip("/"),
            str(current).lstrip("/"),
        ],
        check=True,
        capture_output=True,
    )
    archive.chmod(0o400)
    g6 = tmp_path / "stopped-quiescence.json"
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    stopped_evidence = activate_module.expected_stopped_command_evidence(paths)
    g6.write_text(json.dumps({
        "schemaVersion": 1,
        "capturedAt": "2026-08-27T00:00:00Z",
        "archivePath": str(archive),
        "archiveSha256": archive_sha256,
        "preCapture": stopped_evidence,
        "postCapture": stopped_evidence,
    }, indent=2, sort_keys=True) + "\n")
    g6.chmod(0o400)
    predecessor_record = activate_module.validate_release(
        paths,
        predecessor,
        PREDECESSOR_COMMIT,
        require_candidate_native_entrypoints=False,
    )
    invariants = activate_module.validate_recovered_invariants(paths)
    plists = activate_module.validate_plists(paths)
    snapshot = tmp_path / "stopped-snapshot.json"
    snapshot.write_text(json.dumps({
        "schemaVersion": 2,
        "tarPath": str(archive),
        "tarSha256": archive_sha256,
        "inventoryCount": 4,
        "g6QuiescencePath": str(g6),
        "g6QuiescenceSha256": hashlib.sha256(g6.read_bytes()).hexdigest(),
        "predecessorStateDir": str(state),
        "candidateStateDir": str(state),
        "currentTarget": str(predecessor),
        "predecessor": predecessor_record,
        "recoveredInvariants": invariants,
        "execApprovalsInspector": activate_module.exec_approvals_inspector_binding(
            activate_module.validate_candidate_seal(
                paths, candidate, CANDIDATE_COMMIT, seal
            )
        ),
        "execApprovals": approval_report(),
        "gatewayPlistSha256": plists["gateway"]["sha256"],
        "nodePlistSha256": plists["node"]["sha256"],
    }, indent=2, sort_keys=True) + "\n")
    snapshot.chmod(0o400)
    item = Fixture(
        tmp_path,
        paths,
        predecessor,
        candidate,
        seal,
        snapshot,
        legacy_exec_approvals_path,
        external_marker,
        config_path.read_bytes(),
    )
    yield item
    chmod_tree(predecessor, True)
    chmod_tree(candidate, True)


def run(fixture: Fixture, backend: FakeBackend | None = None):
    return activate_module.activate(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
        fixture.snapshot, backend or FakeBackend(fixture),
    )


@pytest.mark.parametrize(
    "drift",
    [
        "python-supervisor",
        "wrong-working-directory",
        "wrong-port",
        "wrong-entrypoint",
        "extra-argument",
        "program-override",
        "run-at-load-disabled",
        "keep-alive-disabled",
    ],
)
def test_gateway_plist_requires_exact_direct_launchd_node_contract(
    fixture: Fixture,
    drift: str,
):
    value = plistlib.loads(fixture.paths.gateway_plist.read_bytes())
    if drift == "python-supervisor":
        value["ProgramArguments"] = [
            "/usr/bin/python3",
            str(fixture.paths.current_link),
            str(fixture.paths.node),
            "gateway",
        ]
    elif drift == "wrong-working-directory":
        value["WorkingDirectory"] = str(fixture.candidate)
    elif drift == "wrong-port":
        value["ProgramArguments"][-1] = "18790"
    elif drift == "wrong-entrypoint":
        value["ProgramArguments"][1] = str(
            fixture.paths.current_link / "openclaw.mjs"
        )
    elif drift == "extra-argument":
        value["ProgramArguments"].append("--unexpected")
    elif drift == "program-override":
        value["Program"] = str(fixture.paths.node)
    elif drift == "run-at-load-disabled":
        value["RunAtLoad"] = False
    else:
        value["KeepAlive"] = False
    fixture.paths.gateway_plist.write_bytes(plistlib.dumps(value))

    with pytest.raises(
        activate_module.ActivationError,
        match="gateway plist selector contract drift",
    ):
        activate_module.validate_plists(fixture.paths)


def sealed_candidate(fixture: Fixture) -> dict[str, object]:
    return activate_module.validate_candidate_seal(
        fixture.paths,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
    )


def restore_result_path(fixture: Fixture) -> Path:
    digest = hashlib.sha256(fixture.snapshot.read_bytes()).hexdigest()[:16]
    return fixture.paths.result.parent / f"activation-restore-result-{digest}.json"


def restore(fixture: Fixture, backend: StoppedRestoreBackend):
    return activate_module.restore_failed_activation(
        fixture.paths,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
        fixture.snapshot,
        restore_result_path(fixture),
        backend,
    )


def test_seal_binds_only_required_bundled_extensions(fixture: Fixture):
    seal = json.loads(fixture.seal.read_text())
    assert set(seal["bundledExtensions"]) == {"discord", "lane-contract"}
    discord = seal["bundledExtensions"]["discord"]
    assert discord["path"] == str(fixture.candidate / "dist" / "extensions" / "discord")
    assert discord["entrypointSha256"] == hashlib.sha256(
        (fixture.candidate / "dist" / "extensions" / "discord" / "index.js").read_bytes()
    ).hexdigest()
    assert stat.S_IMODE(os.lstat(fixture.seal).st_mode) == 0o400


def test_seal_accepts_complete_ai_runtime_export_closure(fixture: Fixture):
    candidate = sealed_candidate(fixture)

    assert candidate["path"] == str(fixture.candidate)
    assert (
        fixture.candidate
        / "node_modules"
        / "@openclaw"
        / "ai"
        / "dist"
        / "internal"
        / "openai-responses-payload-policy.mjs"
    ).resolve(strict=True).is_file()


@pytest.mark.parametrize("plugin_id", ["discord", "lane-contract"])
@pytest.mark.parametrize("target", ["../../../node_modules", "../../.."])
def test_seal_preserves_in_release_bundled_dependency_links(fixture: Fixture, plugin_id: str, target: str):
    root = fixture.candidate / "dist" / "extensions" / plugin_id
    root.chmod(0o755)
    (root / "node_modules").symlink_to(target)
    root.chmod(0o555)
    seal_path = fixture.root / f"{plugin_id}-dependencies-seal.json"
    activate_module.create_candidate_seal(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, seal_path
    )
    activate_module.validate_candidate_seal(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, seal_path
    )
    # The shared target is still bound by the full-release inventory.
    dependency = fixture.candidate / "packages" / "ai" / "dist" / "index.mjs"
    dependency.chmod(0o644)
    dependency.write_text("export const changed = true;\n")
    dependency.chmod(0o444)
    with pytest.raises(activate_module.ActivationError, match="candidate seal surface drift"):
        activate_module.validate_candidate_seal(
            fixture.paths, fixture.candidate, CANDIDATE_COMMIT, seal_path
        )


def test_bundled_inventory_rejects_dependency_links_outside_release(fixture: Fixture):
    root = fixture.candidate / "dist" / "extensions" / "discord"
    root.chmod(0o755)
    (root / "node_modules").symlink_to(fixture.predecessor / "node_modules")
    root.chmod(0o555)
    with pytest.raises(activate_module.ActivationError, match="release symlink escapes"):
        activate_module.validate_bundled_extensions(fixture.candidate)


def test_bundled_inventory_rejects_missing_dependency_target(fixture: Fixture):
    root = fixture.candidate / "dist" / "extensions" / "discord"
    root.chmod(0o755)
    (root / "node_modules").symlink_to("../../../missing-dependencies")
    root.chmod(0o555)
    with pytest.raises(FileNotFoundError):
        activate_module.validate_bundled_extensions(fixture.candidate)


def test_seal_rejects_missing_ai_runtime_export_entry(fixture: Fixture):
    target = (
        fixture.candidate
        / "packages"
        / "ai"
        / "dist"
        / "internal"
        / "openai-responses-payload-policy.mjs"
    )
    target.parent.chmod(0o755)
    target.unlink()
    target.parent.chmod(0o555)

    with pytest.raises(
        activate_module.ActivationError,
        match="candidate AI runtime export .* is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / "missing-ai-runtime-seal.json",
        )


@pytest.mark.parametrize(
    ("export_name", "export_value", "missing_relative"),
    [
        (
            "./node-only",
            {"node": "./dist/node-only.mjs", "default": "./dist/index.mjs"},
            "dist/node-only.mjs",
        ),
        (
            "./nested",
            {
                "node": {
                    "import": "./dist/internal/nested-node.mjs",
                    "default": "./dist/index.mjs",
                },
                "default": "./dist/index.mjs",
            },
            "dist/internal/nested-node.mjs",
        ),
    ],
)
def test_seal_rejects_missing_conditional_ai_runtime_export_entry(
    fixture: Fixture,
    export_name: str,
    export_value: object,
    missing_relative: str,
):
    package_path = fixture.candidate / "packages" / "ai" / "package.json"
    package = json.loads(package_path.read_text())
    package["exports"][export_name] = export_value
    package_path.chmod(0o600)
    package_path.write_text(json.dumps(package) + "\n")
    package_path.chmod(0o444)

    with pytest.raises(
        activate_module.ActivationError,
        match=rf"candidate AI runtime export {re.escape(missing_relative)} is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / f"missing-{export_name.removeprefix('./')}-seal.json",
        )


def test_activation_accepts_absent_artifact_retention(fixture: Fixture):
    config = json.loads((fixture.paths.candidate_state_dir / "openclaw.json").read_text())
    assert "artifact-retention" not in config["plugins"]["entries"]
    assert not (fixture.candidate / "dist" / "extensions" / "artifact-retention").exists()

    result = run(fixture)

    assert result["outcome"] == "activated"
    assert result["candidate"]["bundledExtensions"].keys() == {"discord", "lane-contract"}


def test_seal_requires_release_native_exec_approvals_inspector(fixture: Fixture):
    inspector = (
        fixture.candidate / "dist" / "infra" / "exec-approvals-inspection.js"
    )
    inspector.parent.chmod(0o755)
    inspector.unlink()
    inspector.parent.chmod(0o555)

    with pytest.raises(
        activate_module.ActivationError,
        match="candidate entrypoint is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / "missing-inspector-seal.json",
        )


@pytest.mark.parametrize("filename", ["manifest.json", "background.js"])
def test_seal_requires_packaged_chrome_extension_file(
    fixture: Fixture,
    filename: str,
):
    required = (
        fixture.candidate
        / "dist"
        / "extensions"
        / "browser"
        / "chrome-extension"
        / filename
    )
    required.parent.chmod(0o755)
    required.unlink()
    required.parent.chmod(0o555)

    with pytest.raises(
        activate_module.ActivationError,
        match="candidate entrypoint is unavailable",
    ):
        activate_module.create_candidate_seal(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.root / f"missing-chrome-extension-{filename}-seal.json",
        )


def test_seal_entrypoint_streams_release_files_larger_than_read_bound(
    monkeypatch, capsys, fixture: Fixture
):
    large = fixture.candidate / "large-sparse-asset.bin"
    fixture.candidate.chmod(0o755)
    with large.open("wb") as handle:
        handle.truncate(activate_module.MAX_FILE_BYTES + 1)
    large.chmod(0o444)
    fixture.candidate.chmod(0o555)
    fixture.seal.unlink()
    monkeypatch.setattr(activate_module, "live_paths", lambda: fixture.paths)

    exit_code = activate_module.main([
        "seal",
        "--candidate-release", str(fixture.candidate),
        "--source-commit", CANDIDATE_COMMIT,
        "--output", str(fixture.seal),
    ])

    receipt = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert receipt["releaseInventory"] == json.loads(fixture.seal.read_text())["candidate"]["releaseInventory"]
    assert receipt["releaseInventory"]["count"] == activate_module.normalized_tree_inventory(fixture.candidate)["count"]
    assert large.stat().st_size > activate_module.MAX_FILE_BYTES


def test_activation_preflight_streams_pinned_node_larger_than_read_bound(fixture: Fixture):
    fixture.paths.node.chmod(0o755)
    with fixture.paths.node.open("r+b") as handle:
        handle.truncate(activate_module.MAX_FILE_BYTES + 1)
    fixture.paths.node.chmod(0o555)

    result = run(fixture)

    assert fixture.paths.node.stat().st_size > activate_module.MAX_FILE_BYTES
    assert result["outcome"] == "activated"


def test_already_migrated_activation_boots_each_job_once_and_preserves_state(fixture: Fixture):
    backend = FakeBackend(fixture)
    marker = fixture.external_marker.read_bytes()
    result = run(fixture, backend)
    assert result["outcome"] == "activated"
    assert result["statesVisited"] == ["preflight", "apply", "verify", "terminal"]
    assert result["candidateAttemptCount"] == 1
    assert result["rollbackAttemptCount"] == 0
    assert result["firstBootAttempted"] is True
    assert result["restoreRequired"] is False
    assert backend.gateway_boots == 1
    assert backend.node_boots == 1
    assert os.readlink(fixture.paths.current_link) == str(fixture.candidate)
    assert fixture.external_marker.read_bytes() == marker
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes
    assert result["verification"]["bundledExtensions"]["discord"] == result["candidate"]["bundledExtensions"]["discord"]
    assert result["verification"]["pluginSelection"]["origin"] == "bundled"
    assert result["verification"]["pluginSelection"]["installRecordAbsent"] is True
    assert result["verification"]["pluginSelection"]["installedIds"] == ["whatsapp"]
    approvals = result["verification"]["execApprovals"]
    assert approvals["authProfileStateReadable"] is True
    assert approvals["authProfileStoreReadable"] is True
    assert (
        approvals["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        approvals["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert approvals["openAIProfileOrderCredentialsUsable"] is True
    assert "openAIProfileOrder" not in approvals
    config = json.loads(
        (fixture.paths.candidate_state_dir / "openclaw.json").read_text()
    )
    assert "openai" not in config["auth"].get("order", {})
    assert not any(
        key.startswith("openai:") for key in config["auth"].get("profiles", {})
    )
    for path in (fixture.paths.result, fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)):
        info = os.lstat(path)
        assert stat.S_IMODE(info.st_mode) == 0o400
        assert info.st_nlink == 1
        payload = path.read_text()
        assert all(
            profile_id not in payload
            for profile_id in SENSITIVE_TEST_PROFILE_IDS
        )


def test_openai_auth_order_attestation_is_value_free_in_reports_and_receipts(
    fixture: Fixture,
):
    result = run(fixture)

    assert result["outcome"] == "activated"
    report = result["verification"]["execApprovals"]
    assert (
        report["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        report["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert "openAIProfileOrder" not in report
    surfaces = (
        json.dumps(report, sort_keys=True),
        fixture.snapshot.read_text(),
        fixture.paths.result.read_text(),
        (
            fixture.candidate
            / "dist"
            / "infra"
            / "exec-approvals-inspection.js"
        ).read_text(),
    )
    assert all(
        profile_id not in surface
        for profile_id in SENSITIVE_TEST_PROFILE_IDS
        for surface in surfaces
    )


def test_expected_openai_auth_order_binding_is_value_free():
    assert activate_module.expected_auth_order_count() == 3
    assert activate_module.expected_auth_order_sha256() == (
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    )
    assert not hasattr(activate_module, "EXPECTED_OPENAI_AUTH_ORDER")


def test_activation_accepts_unrelated_install_records_without_mutation(fixture: Fixture):
    backend = FakeBackend(fixture)
    backend.install_records["owner-local-plugin"] = {
        "source": "npm",
        "version": "1.0.0",
    }
    before = json.loads(json.dumps(backend.install_records))

    result = run(fixture, backend)

    assert result["outcome"] == "activated"
    assert backend.install_records == before
    assert result["verification"]["pluginSelection"]["installedIds"] == [
        "owner-local-plugin",
        "whatsapp",
    ]


def test_terminal_receipt_retirement_moves_exact_pair_and_preserves_inodes(fixture: Fixture):
    result = run(fixture)
    assert result["outcome"] == "activated"
    fence_path = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    result_info = os.lstat(fixture.paths.result)
    fence_info = os.lstat(fence_path)
    archive_dir = fixture.paths.result.parent / "archive" / "prior-success"

    receipt = activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert not fixture.paths.result.exists()
    assert not fence_path.exists()
    archived_result = archive_dir / fixture.paths.result.name
    archived_fence = archive_dir / fence_path.name
    assert os.lstat(archived_result).st_ino == result_info.st_ino
    assert os.lstat(archived_fence).st_ino == fence_info.st_ino
    assert receipt["result"]["path"] == str(archived_result)
    assert receipt["startFence"]["path"] == str(archived_fence)
    retirement = archive_dir / activate_module.RETIREMENT_RECEIPT_NAME
    assert stat.S_IMODE(retirement.stat().st_mode) == 0o400


def simulate_release_remount(fixture, monkeypatch):
    original = os.lstat
    releases = {fixture.candidate, fixture.predecessor}
    current_device = original(fixture.candidate).st_dev + 4

    def remounted(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) in releases:
            values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
            values["st_dev"] = current_device
            return SimpleNamespace(**values)
        return info

    monkeypatch.setattr(os, "lstat", remounted)
    if hasattr(activate_module, "_history_volume_guard"):
        guard = activate_module._history_volume_guard()
        monkeypatch.setattr(guard, "normalize_contract", lambda _: {
            "mountPoint": str(fixture.root), "volumeUuid": "11111111-2222-3333-4444-555555555555"})
        monkeypatch.setattr(guard, "volume_metadata", lambda _: {
            "available": True, "mounted": True, "mountDevice": current_device,
            "volumeUuid": "11111111-2222-3333-4444-555555555555"})
    return current_device


def test_terminal_receipt_retirement_revalidates_legacy_release_after_remount(fixture, monkeypatch):
    result = run(fixture)
    original = {path: path.read_bytes() for path in (
        fixture.paths.result, fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME),
        fixture.seal, fixture.snapshot)}
    current_device = simulate_release_remount(fixture, monkeypatch)
    archive = fixture.paths.result.parent / "archive" / "after-remount"

    retired = activate_module.retire_terminal_receipts(fixture.paths, archive)

    assert retired["outcome"] == "activated"
    identity = retired["releaseIdentities"]["selected"]
    assert identity["device"] == current_device != result["candidate"]["device"]
    assert identity["inode"] == result["candidate"]["inode"]
    assert identity["volumeUuid"] == "11111111-2222-3333-4444-555555555555"
    assert identity["recordSha256"] == activate_module.sha256_bytes(activate_module.canonical_json_bytes(result["candidate"]))
    for path, payload in original.items():
        assert (path if path in (fixture.seal, fixture.snapshot) else archive / path.name).read_bytes() == payload


@pytest.mark.parametrize("corruption", ["volume", "inode", "inventory", "seal", "snapshot", "missing-volume"])
def test_terminal_receipt_remount_rejects_unproven_identity_before_moving(fixture, monkeypatch, corruption):
    run(fixture)
    simulate_release_remount(fixture, monkeypatch)
    guard = activate_module._history_volume_guard()
    if corruption == "volume":
        monkeypatch.setattr(guard, "read_volume_uuid", lambda _: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")
    elif corruption == "missing-volume":
        monkeypatch.setattr(guard, "read_volume_uuid", lambda _: "")
    elif corruption == "inventory":
        notice = fixture.candidate / "NOTICE"
        notice.chmod(0o644); notice.write_text("changed bytes"); notice.chmod(0o444)
    elif corruption == "inode":
        prior = os.lstat
        def replaced_inode(path, *args, **kwargs):
            info = prior(path, *args, **kwargs)
            if Path(path) == fixture.candidate:
                values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
                values["st_ino"] += 1
                return SimpleNamespace(**values)
            return info
        monkeypatch.setattr(os, "lstat", replaced_inode)
    else:
        path = fixture.seal if corruption == "seal" else fixture.snapshot
        path.chmod(0o600); path.write_text(path.read_text() + " "); path.chmod(0o400)
    before = fixture.paths.result.read_bytes()
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    fence_before = fence.read_bytes()
    archive = fixture.paths.result.parent / "archive" / "unproven-remount"
    with pytest.raises(activate_module.ActivationError):
        activate_module.retire_terminal_receipts(fixture.paths, archive)
    assert not archive.exists()
    assert fixture.paths.result.read_bytes() == before and fence.read_bytes() == fence_before


def test_remount_does_not_relax_candidate_seal_mutation_admission(fixture, monkeypatch):
    simulate_release_remount(fixture, monkeypatch)
    with pytest.raises(activate_module.ActivationError, match="seal surface drift"):
        activate_module.validate_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)


def test_retirement_rolls_back_moves_if_mount_changes_during_archival(fixture, monkeypatch):
    run(fixture)
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    before = {path: path.read_bytes() for path in (fixture.paths.result, fence)}
    original_rename = os.rename
    archive = fixture.paths.result.parent / "archive" / "racing-mount"
    def rename_and_remount(source, destination):
        original_rename(source, destination)
        if source == fixture.paths.result:
            simulate_release_remount(fixture, monkeypatch)
    monkeypatch.setattr(os, "rename", rename_and_remount)
    with pytest.raises(activate_module.ActivationError, match="changed during retirement"):
        activate_module.retire_terminal_receipts(fixture.paths, archive)
    assert all(path.read_bytes() == payload for path, payload in before.items())
    assert not (archive / activate_module.RETIREMENT_RECEIPT_NAME).exists()


def test_daily_retention_reaches_children_after_legacy_remount_retirement(fixture, monkeypatch):
    from scripts import openclaw_retention_cleanup_cron as cron
    from scripts import openclaw_runtime_activate as live_owner
    run(fixture)
    simulate_release_remount(fixture, monkeypatch)
    monkeypatch.setattr(live_owner, "live_paths", lambda: fixture.paths)
    calls = []
    def child(name, *args, **kwargs):
        calls.append(name)
        return cron.ChildResult(name, 0)
    monkeypatch.setattr(cron, "run_child", child)
    results = cron.run_steps(apply=True)
    assert {row.name for row in results} == {"host_storage", "runtime_releases", "runtime_promotions", "approval_a"}
    assert len(calls) == 4 and all(row.returncode == 0 for row in results)
    assert "ACTIVATION_RETIREMENT_REPORT:" in results[1].stdout
    assert not fixture.paths.result.exists()


def test_release_retention_consumes_durable_retirement_identity(fixture, monkeypatch):
    from scripts import openclaw_runtime_release_retention as retention
    run(fixture)
    archive = fixture.paths.result.parent / "archive" / "retention-history"
    activate_module.retire_terminal_receipts(fixture.paths, archive)
    simulate_release_remount(fixture, monkeypatch)
    monkeypatch.setattr(retention, "effective_activation_result_path", lambda: fixture.paths.result)
    from scripts.operator_contract import OperatorContract
    configured = {section: retention.OPERATOR.get(section, {}) for section in ("paths", "runtime", "identifiers")}
    configured["paths"]["runtime_releases_root"] = str(fixture.paths.releases_root)
    monkeypatch.setattr(retention, "OPERATOR", OperatorContract(configured))
    monkeypatch.setattr(retention, "LAST_PROMOTION_OPERATION_CLASSIFICATIONS", [])
    assert retention.collect_unfinished_release_refs() == []
    assert retention.LAST_TERMINAL_ARCHIVE_NOTES == []
    path = archive / activate_module.RETIREMENT_RECEIPT_NAME
    receipt = json.loads(path.read_text())
    receipt["result"]["volumeUuid"] = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    path.chmod(0o600); path.write_text(json.dumps(receipt)); path.chmod(0o400)
    refs = retention.collect_unfinished_release_refs()
    assert {row.path for row in refs} == {fixture.candidate, fixture.predecessor}
    assert retention.LAST_TERMINAL_ARCHIVE_NOTES


def test_terminal_receipt_retirement_archives_historical_different_profile_binding(
    fixture: Fixture,
):
    result = run(fixture)
    manifest = json.loads(fixture.snapshot.read_text())
    manifest_approvals = manifest["execApprovals"]
    manifest_approvals["openAIProfileOrderCount"] = LEGACY_OPENAI_AUTH_ORDER_COUNT
    manifest_approvals["openAIProfileOrderSha256"] = (
        LEGACY_OPENAI_AUTH_ORDER_SHA256
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)
    manifest_sha256 = hashlib.sha256(fixture.snapshot.read_bytes()).hexdigest()

    fence_path = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    fence = json.loads(fence_path.read_text())
    fence["snapshotManifestSha256"] = manifest_sha256
    fence_path.chmod(0o600)
    fence_path.write_text(json.dumps(fence, indent=2, sort_keys=True) + "\n")
    fence_path.chmod(0o400)

    result["snapshot"]["manifestSha256"] = manifest_sha256
    result["snapshot"]["execApprovals"] = dict(manifest_approvals)
    result["verification"]["execApprovals"] = dict(manifest_approvals)
    result["startConsumption"]["sha256"] = hashlib.sha256(
        fence_path.read_bytes()
    ).hexdigest()
    result_path = fixture.paths.result
    result_path.chmod(0o600)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result_path.chmod(0o400)
    archive_dir = result_path.parent / "archive" / "historical-five-profile"

    receipt = activate_module.retire_terminal_receipts(
        fixture.paths,
        archive_dir,
    )

    archived = json.loads((archive_dir / result_path.name).read_text())
    archived_approvals = archived["snapshot"]["execApprovals"]
    assert receipt["outcome"] == "activated"
    assert archived["snapshot"]["manifestSha256"] == manifest_sha256
    assert archived["startConsumption"]["sha256"] == hashlib.sha256(
        (archive_dir / fence_path.name).read_bytes()
    ).hexdigest()
    assert archived_approvals == manifest_approvals
    assert archived["verification"]["execApprovals"] == manifest_approvals
    assert archived_approvals["openAIProfileOrderCount"] == (
        LEGACY_OPENAI_AUTH_ORDER_COUNT
    )
    assert (
        archived_approvals["openAIProfileOrderSha256"]
        == LEGACY_OPENAI_AUTH_ORDER_SHA256
    )


def test_terminal_receipt_retirement_accepts_exact_bound_legacy_schema(fixture: Fixture):
    result = run(fixture)
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    candidate = result["candidate"]
    snapshot = result["snapshot"]
    bootstrap_path = fixture.root / "legacy-bootstrap.json"
    bootstrap_sha = "b" * 64
    legacy_fence = {
        "schemaVersion": 2,
        "candidatePath": candidate["path"],
        "candidateCommit": candidate["commit"],
        "candidateDevice": candidate["device"],
        "candidateInode": candidate["inode"],
        "candidateSealPath": candidate["sealPath"],
        "candidateSealSha256": candidate["sealSha256"],
        "snapshotManifestPath": snapshot["manifestPath"],
        "snapshotManifestSha256": snapshot["manifestSha256"],
        "bootstrapManifestPath": str(bootstrap_path),
        "bootstrapManifestSha256": bootstrap_sha,
        "consumedAt": "2026-08-27T01:00:00Z",
    }
    fence_path.chmod(0o600)
    fence_path.write_text(json.dumps(legacy_fence, indent=2, sort_keys=True) + "\n")
    fence_path.chmod(0o400)
    result["activationInputs"] = {
        "candidateSealPath": candidate["sealPath"],
        "day0SnapshotManifestPath": snapshot["manifestPath"],
        "externalPluginBootstrapManifestPath": str(bootstrap_path),
        "externalPluginBootstrapManifestSha256": bootstrap_sha,
    }
    result["startConsumption"] = {
        "path": str(fence_path),
        "sha256": hashlib.sha256(fence_path.read_bytes()).hexdigest(),
        "mode": 0o400,
        "nlink": 1,
        "consumedAt": legacy_fence["consumedAt"],
    }
    result_path.chmod(0o600)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result_path.chmod(0o400)

    archive_dir = result_path.parent / "archive" / "legacy-success"
    receipt = activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert receipt["candidateCommit"] == candidate["commit"]
    assert json.loads((archive_dir / fence_path.name).read_text()) == legacy_fence


def test_terminal_receipt_retirement_archives_restored_failed_attempt_and_clears_namespace(
    fixture: Fixture,
):
    failed = run(fixture, FakeBackend(fixture, gateway_failure=True))
    assert failed["outcome"] == "snapshot_restore_required"
    fence_path = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    restored = restore(fixture, StoppedRestoreBackend(fixture))
    assert restored["outcome"] == "restored"
    original_inodes = {
        path.name: os.lstat(path).st_ino
        for path in (fixture.paths.result, fence_path, restore_path)
    }
    archive_dir = fixture.paths.result.parent / "archive" / "restored-failure"

    receipt = activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert receipt["outcome"] == "restored"
    assert receipt["selectedPath"] == str(fixture.predecessor)
    assert receipt["restoreResult"]["path"] == str(archive_dir / restore_path.name)
    for path in (fixture.paths.result, fence_path, restore_path):
        assert not path.exists()
        assert os.lstat(archive_dir / path.name).st_ino == original_inodes[path.name]


def test_terminal_receipt_retirement_late_verifies_boot_failed_restore(fixture: Fixture):
    failed = run(fixture, FakeBackend(fixture, gateway_failure=True))
    assert failed["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    original_inodes = {
        path.name: os.lstat(path).st_ino
        for path in (result_path, fence_path, restore_path)
    }
    archive_dir = result_path.parent / "archive" / "late-restored-failure"
    backend = FakeBackend(fixture)

    receipt = activate_module.retire_terminal_receipts(
        fixture.paths,
        archive_dir,
        backend,
    )

    assert receipt["outcome"] == "restored_after_late_verification"
    assert receipt["lateVerification"]["gateway"]["health"]["readyz"]["accepted"] is True
    assert receipt["lateVerification"]["node"]["release"] == str(fixture.predecessor)
    assert backend.verifications == 2
    assert backend.gateway_boots == backend.node_boots == 0
    assert backend.receipt_bindings == [
        (activate_module.OPERATOR.require_string('runtime.gateway_label'), True),
        (activate_module.OPERATOR.require_string('runtime.node_label'), False),
    ]
    for path in (result_path, fence_path, restore_path):
        assert not path.exists()
        assert os.lstat(archive_dir / path.name).st_ino == original_inodes[path.name]


@pytest.mark.parametrize("failure", ["gateway", "node"])
def test_terminal_receipt_retirement_late_verification_failure_preserves_trio(
    monkeypatch,
    fixture: Fixture,
    failure: str,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )["outcome"] == "restored_boot_failed"
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    original_inodes = {
        path: os.lstat(path).st_ino for path in (result_path, fence_path, restore_path)
    }
    archive_dir = result_path.parent / "archive" / f"failed-late-{failure}"
    backend = FakeBackend(fixture)

    def fail_verification(*_args, **_kwargs):
        raise activate_module.ActivationError(f"injected late {failure} failure")

    monkeypatch.setattr(
        backend,
        "verify" if failure == "gateway" else "verify_node",
        fail_verification,
    )
    with pytest.raises(activate_module.ActivationError, match=f"late {failure} failure"):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            archive_dir,
            backend,
        )

    for path, inode in original_inodes.items():
        assert os.lstat(path).st_ino == inode
        assert not (archive_dir / path.name).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outcome", "restore_failed_preimage_restored"),
        ("error", None),
        ("restoreApplied", False),
        ("failedActivationResultSha256", "0" * 64),
    ],
)
def test_terminal_receipt_retirement_rejects_unbound_boot_failure_before_late_verify(
    fixture: Fixture,
    field: str,
    value: object,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )["outcome"] == "restored_boot_failed"
    restore_path = restore_result_path(fixture)
    receipt = json.loads(restore_path.read_text())
    receipt[field] = value
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(activate_module.ActivationError, match="restore receipt binding drift"):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / f"unbound-{field}",
            backend,
        )

    assert backend.verifications == 0


def test_terminal_receipt_retirement_rejects_partial_old_gateway_verification(
    fixture: Fixture,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )["outcome"] == "restored_boot_failed"
    restore_path = restore_result_path(fixture)
    receipt = json.loads(restore_path.read_text())
    receipt["verification"]["gateway"] = {"stale": True}
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "partial-old-verification",
            backend,
        )

    assert backend.verifications == 0


@pytest.mark.parametrize(
    "verification",
    [
        {"gateway": None, "node": {"impossible": True}},
        {"gateway": {"stale": True}, "node": {"impossible": True}},
    ],
)
def test_terminal_receipt_retirement_rejects_impossible_boot_failure_verification_shape(
    fixture: Fixture,
    verification: dict[str, object],
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    assert restore(
        fixture,
        StoppedRestoreBackend(fixture, gateway_failure=True),
    )["outcome"] == "restored_boot_failed"
    restore_path = restore_result_path(fixture)
    receipt = json.loads(restore_path.read_text())
    receipt["verification"] = verification
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(activate_module.ActivationError, match="restore receipt binding drift"):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "impossible-verification-shape",
            backend,
        )

    assert backend.verifications == 0


def test_terminal_receipt_retirement_restores_bound_trio_on_post_move_failure(
    monkeypatch, fixture: Fixture,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    restore(fixture, StoppedRestoreBackend(fixture))
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    restore_path = restore_result_path(fixture)
    original_inodes = {
        path: os.lstat(path).st_ino for path in (result_path, fence_path, restore_path)
    }
    original_read = activate_module.read_physical

    def fail_restore_read(path, label, maximum=activate_module.MAX_FILE_BYTES):
        if label == "archived activation restore result":
            raise activate_module.ActivationError("injected restored trio readback failure")
        return original_read(path, label, maximum)

    monkeypatch.setattr(activate_module, "read_physical", fail_restore_read)
    archive_dir = result_path.parent / "archive" / "failed-restored-retirement"
    with pytest.raises(activate_module.ActivationError, match="restored trio readback"):
        activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    for path, inode in original_inodes.items():
        assert os.lstat(path).st_ino == inode
        assert not (archive_dir / path.name).exists()


def test_terminal_receipt_retirement_restores_pair_on_post_move_failure(
    monkeypatch, fixture: Fixture
):
    result = run(fixture)
    assert result["outcome"] == "activated"
    result_path = fixture.paths.result
    fence_path = result_path.with_name(activate_module.START_CONSUMED_NAME)
    result_inode = os.lstat(result_path).st_ino
    fence_inode = os.lstat(fence_path).st_ino
    original_read = activate_module.read_physical

    def fail_archived_read(path, label, maximum=activate_module.MAX_FILE_BYTES):
        if label == "archived activation result":
            raise activate_module.ActivationError("injected archived readback failure")
        return original_read(path, label, maximum)

    monkeypatch.setattr(activate_module, "read_physical", fail_archived_read)
    archive_dir = result_path.parent / "archive" / "failed-retirement"
    with pytest.raises(activate_module.ActivationError, match="injected archived"):
        activate_module.retire_terminal_receipts(fixture.paths, archive_dir)

    assert os.lstat(result_path).st_ino == result_inode
    assert os.lstat(fence_path).st_ino == fence_inode
    assert not (archive_dir / result_path.name).exists()
    assert not (archive_dir / fence_path.name).exists()


@pytest.mark.parametrize(
    "drift",
    ["reservation", "goplaces", "profile-order", "load-path", "discord", "lane-contract"],
)
def test_protected_migrated_invariants_fail_before_selector_or_boot(fixture: Fixture, drift: str):
    paths = fixture.paths
    approval_reports = None
    if drift == "reservation":
        path = paths.candidate_state_dir / activate_module.OPERATOR.require_path("paths.session_reservations")
        path.write_text('{"storePath":"/wrong"}\n')
    elif drift == "profile-order":
        report = approval_report()
        report["openAIProfileOrderCount"] = 4
        approval_reports = [report]
    else:
        path = paths.candidate_state_dir / "openclaw.json"
        value = json.loads(path.read_text())
        if drift == "goplaces":
            value["skills"]["entries"]["goplaces"]["apiKey"] = "forbidden"
        elif drift == "load-path":
            value["plugins"]["load"]["paths"] = ["/external/discord"]
        else:
            value["plugins"]["entries"][drift]["enabled"] = False
        path.write_text(json.dumps(value) + "\n")
    backend = FakeBackend(fixture, approval_reports=approval_reports)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(paths.current_link) == str(fixture.predecessor)


def test_legacy_openai_config_auth_order_shadow_fails_before_selector_or_boot(
    fixture: Fixture,
):
    config_path = fixture.paths.candidate_state_dir / "openclaw.json"
    config = json.loads(config_path.read_text())
    config["auth"]["order"] = {"openai": list(SENSITIVE_TEST_PROFILE_IDS)}
    config_path.write_text(json.dumps(config) + "\n")
    backend = FakeBackend(fixture)

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


def test_legacy_openai_config_profile_shadow_fails_before_selector_or_boot(
    fixture: Fixture,
):
    config_path = fixture.paths.candidate_state_dir / "openclaw.json"
    config = json.loads(config_path.read_text())
    config["auth"]["profiles"] = {
        "openai:chatgpt-default": {"provider": "openai", "mode": "oauth"}
    }
    config_path.write_text(json.dumps(config) + "\n")
    backend = FakeBackend(fixture)

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


def test_non_openai_config_auth_entries_remain_supported(fixture: Fixture):
    config_path = fixture.paths.candidate_state_dir / "openclaw.json"
    config = json.loads(config_path.read_text())
    config["auth"] = {
        "order": {"anthropic": ["anthropic:claude-cli"]},
        "profiles": {
            "anthropic:claude-cli": {
                "provider": "anthropic",
                "mode": "token",
            }
        },
    }
    config_path.write_text(json.dumps(config) + "\n")
    fixture.config_bytes = config_path.read_bytes()
    snapshot = json.loads(fixture.snapshot.read_text())
    snapshot["recoveredInvariants"] = activate_module.validate_recovered_invariants(
        fixture.paths
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    result = run(fixture)

    assert result["outcome"] == "activated"
    approvals = result["verification"]["execApprovals"]
    assert (
        approvals["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        approvals["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert "openAIProfileOrder" not in approvals


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("openAIProfileOrderCount", 4),
        ("openAIProfileOrderSha256", "c" * 64),
    ],
    ids=("count", "digest"),
)
def test_authoritative_openai_auth_order_drift_fails_before_selector_or_boot(
    fixture: Fixture,
    field: str,
    value: object,
):
    report = approval_report()
    report[field] = value
    backend = FakeBackend(fixture, approval_reports=[report])

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *((field, False) for field in activate_module.EXEC_APPROVALS_REQUIRED_TRUE),
        ("schemaVersion", 1),
        ("schemaVersion", True),
        ("rowCount", 0),
        ("rowCount", True),
        ("agentCount", -1),
        ("agentCount", True),
        ("allowlistCount", -1),
        ("allowlistCount", False),
        ("semanticSha256", "not-a-digest"),
        ("rawCasSha256", "F" * 64),
        ("openAIProfileOrderCount", 4),
        ("openAIProfileOrderCount", True),
        ("openAIProfileOrderSha256", "not-a-digest"),
        ("openAIProfileOrderSha256", "F" * 64),
    ],
)
def test_exec_approvals_contract_drift_fails_before_selector_or_boot(
    fixture: Fixture, field: str, value: object
):
    report = approval_report()
    report[field] = value
    backend = FakeBackend(fixture, approval_reports=[report])

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert not fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME).exists()


@pytest.mark.parametrize("shape", ["missing", "extra", "raw-order"])
def test_exec_approvals_contract_rejects_nonexact_keys_before_apply(
    fixture: Fixture, shape: str
):
    report = approval_report()
    if shape == "missing":
        report.pop("tokenPresent")
    elif shape == "raw-order":
        report["openAIProfileOrder"] = list(SENSITIVE_TEST_PROFILE_IDS)
    else:
        report["socketToken"] = "private-value-must-not-escape"
    backend = FakeBackend(fixture, approval_reports=[report])

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert "private-value-must-not-escape" not in json.dumps(result)
    assert all(
        profile_id not in json.dumps(result)
        for profile_id in SENSITIVE_TEST_PROFILE_IDS
    )
    assert backend.gateway_boots == backend.node_boots == 0


@pytest.mark.parametrize("plugin_id", activate_module.required_bundled_extensions())
def test_required_candidate_extension_drift_fails_before_boot(
    fixture: Fixture, plugin_id: str
):
    target = fixture.candidate / "dist" / "extensions" / plugin_id / "index.js"
    target.chmod(0o600)
    target.write_text("export const drift = true;\n")
    target.chmod(0o444)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


def test_noncritical_candidate_asset_drift_fails_before_boot(fixture: Fixture):
    target = fixture.candidate / "NOTICE"
    target.chmod(0o600)
    target.write_text("drift\n")
    target.chmod(0o444)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


def test_gateway_boot_failure_requires_snapshot_restore_without_retry(fixture: Fixture):
    backend = FakeBackend(fixture, gateway_failure=True)
    result = run(fixture, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["restoreRequired"] is True
    assert result["error"] == "activation_failed"
    assert "private gateway diagnostic" not in json.dumps(result)
    assert backend.gateway_boots == 1
    assert backend.node_boots == 0


def test_exec_approvals_raw_cas_drift_fails_before_fence(fixture: Fixture):
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(raw_cas_sha256="c" * 64)],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0
    assert not fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME).exists()


def test_postboot_exec_approvals_raw_cas_drift_activates_when_semantics_match(
    fixture: Fixture,
):
    backend = FakeBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(raw_cas_sha256="c" * 64),
        ],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "activated"
    assert result["restoreRequired"] is False
    assert result["verification"]["execApprovals"]["rawCasSha256"] == "c" * 64
    assert backend.gateway_boots == backend.node_boots == 1


def test_postboot_exec_approvals_semantic_drift_requires_restore(
    fixture: Fixture,
):
    backend = FakeBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(semantic_sha256="c" * 64),
        ],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "snapshot_restore_required"
    assert result["restoreRequired"] is True
    assert backend.gateway_boots == backend.node_boots == 1


def test_postboot_openai_auth_order_drift_requires_restore(fixture: Fixture):
    drifted = approval_report()
    drifted["openAIProfileOrderSha256"] = "c" * 64
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(), drifted],
    )

    result = run(fixture, backend)

    assert result["outcome"] == "snapshot_restore_required"
    assert result["restoreRequired"] is True
    assert backend.gateway_boots == backend.node_boots == 1


def test_activated_result_replay_rejects_openai_auth_order_drift(
    fixture: Fixture,
):
    assert run(fixture)["outcome"] == "activated"
    drifted = approval_report()
    drifted["openAIProfileOrderSha256"] = "c" * 64
    backend = FakeBackend(fixture, approval_reports=[drifted])

    with pytest.raises(
        activate_module.ActivationError,
        match="exec approvals inspection contract drift",
    ):
        run(fixture, backend)

    assert backend.gateway_boots == backend.node_boots == 0


def test_failed_activation_restore_quarantines_and_boots_predecessor_once(fixture: Fixture):
    external_runtime_assets = fixture.root / "external-runtime-assets"
    external_runtime_sentinel = external_runtime_assets / "sentinel.txt"
    sentinel_before = (
        external_runtime_sentinel.read_bytes(),
        os.lstat(external_runtime_sentinel).st_dev,
        os.lstat(external_runtime_sentinel).st_ino,
        stat.S_IMODE(os.lstat(external_runtime_sentinel).st_mode),
        os.lstat(external_runtime_sentinel).st_nlink,
    )
    failed_backend = FakeBackend(fixture, gateway_failure=True)
    failed = run(fixture, failed_backend)
    assert failed["outcome"] == "snapshot_restore_required"

    backend = StoppedRestoreBackend(fixture)
    output = restore_result_path(fixture)
    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restored"
    assert receipt["restoreApplied"] is True
    assert (
        receipt["snapshot"]["execApprovals"]["openAIProfileOrderCount"]
        == activate_module.expected_auth_order_count()
    )
    assert (
        receipt["snapshot"]["execApprovals"]["openAIProfileOrderSha256"]
        == activate_module.expected_auth_order_sha256()
    )
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert os.readlink(
        fixture.paths.predecessor_state_dir / "external-runtime-assets"
    ) == str(external_runtime_assets)
    assert (
        external_runtime_sentinel.read_bytes(),
        os.lstat(external_runtime_sentinel).st_dev,
        os.lstat(external_runtime_sentinel).st_ino,
        stat.S_IMODE(os.lstat(external_runtime_sentinel).st_mode),
        os.lstat(external_runtime_sentinel).st_nlink,
    ) == sentinel_before
    assert backend.gateway_boots == 1
    assert backend.node_boots == 1
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    assert all(
        Path(path).exists() or Path(path).is_symlink()
        for path in receipt["quarantinedFailedSurfaces"]
    )
    assert len(receipt["quarantinedFailedSurfaces"]) == 4
    assert all("exec-approvals.json" not in path for path in receipt["quarantinedFailedSurfaces"])


def test_historical_different_profile_snapshot_cannot_start_activation(
    fixture: Fixture,
):
    manifest = json.loads(fixture.snapshot.read_text())
    manifest["execApprovals"]["openAIProfileOrderCount"] = (
        LEGACY_OPENAI_AUTH_ORDER_COUNT
    )
    manifest["execApprovals"]["openAIProfileOrderSha256"] = (
        LEGACY_OPENAI_AUTH_ORDER_SHA256
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)
    backend = FakeBackend(fixture)

    result = run(fixture, backend)

    assert result["outcome"] == "failed_before_apply"
    assert result["error"] == "preflight_failed"
    assert backend.inspection_calls == 0
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert not fixture.paths.result.with_name(
        activate_module.START_CONSUMED_NAME
    ).exists()


def test_historical_different_profile_snapshot_cannot_restore_failed_activation(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    manifest = json.loads(fixture.snapshot.read_text())
    manifest["execApprovals"]["openAIProfileOrderCount"] = (
        LEGACY_OPENAI_AUTH_ORDER_COUNT
    )
    manifest["execApprovals"]["openAIProfileOrderSha256"] = (
        LEGACY_OPENAI_AUTH_ORDER_SHA256
    )
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)
    backend = StoppedRestoreBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="exec approvals inspection contract drift",
    ):
        restore(fixture, backend)

    assert backend.stopped_checks == 0
    assert backend.inspection_calls == 0
    assert backend.gateway_boots == backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.candidate)


def test_restore_inspects_preimage_and_extracted_state_only_after_stopped_proofs(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    backend = StoppedRestoreBackend(fixture)
    original_run = activate_module.run_bounded

    def track_extract(argv, timeout, **kwargs):
        if argv[0] == "/usr/bin/tar":
            backend.events.append("extract")
        return original_run(argv, timeout, **kwargs)

    monkeypatch.setattr(activate_module, "run_bounded", track_extract)

    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restored"
    assert backend.events[:5] == [
        "stopped",
        "inspect",
        "extract",
        "stopped",
        "inspect",
    ]


def test_restore_rejects_extracted_exec_approvals_raw_drift_and_restores_preimage(
    fixture: Fixture,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    live_paths = (
        fixture.paths.predecessor_state_dir,
        fixture.paths.gateway_plist,
        fixture.paths.node_plist,
        fixture.paths.current_link,
    )
    original_inodes = [os.lstat(path).st_ino for path in live_paths]
    backend = StoppedRestoreBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(raw_cas_sha256="c" * 64),
            approval_report(),
        ],
    )

    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert receipt["restoreApplied"] is False
    assert [os.lstat(path).st_ino for path in live_paths] == original_inodes
    assert backend.gateway_boots == backend.node_boots == 0


def test_failed_activation_restore_preserves_partial_extract_and_restores_original_inodes(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    live_paths = (
        fixture.paths.predecessor_state_dir,
        fixture.paths.gateway_plist,
        fixture.paths.node_plist,
        fixture.paths.current_link,
    )
    original_inodes = [os.lstat(path).st_ino for path in live_paths]
    original_run = activate_module.run_bounded

    def partial_extract(argv, timeout, **kwargs):
        if argv[0] != "/usr/bin/tar":
            return original_run(argv, timeout, **kwargs)
        live_paths[0].mkdir()
        (live_paths[0] / "partial.marker").write_text("partial\n")
        live_paths[1].write_text("partial gateway\n")
        live_paths[2].write_text("partial node\n")
        live_paths[3].symlink_to(fixture.candidate)
        return activate_module.CommandResult(tuple(argv), 2, b"", b"partial", 1, False)

    monkeypatch.setattr(activate_module, "run_bounded", partial_extract)
    backend = StoppedRestoreBackend(fixture)
    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert receipt["restoreApplied"] is False
    assert backend.gateway_boots == backend.node_boots == 0
    assert [os.lstat(path).st_ino for path in live_paths] == original_inodes
    suffix = CANDIDATE_COMMIT[:12]
    partials = [path.with_name(f"{path.name}.partial-{suffix}") for path in live_paths]
    assert all(path.exists() or path.is_symlink() for path in partials)


def test_failed_activation_restore_post_rename_fsync_failure_restores_moved_inode(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    state = fixture.paths.predecessor_state_dir
    original_inode = os.lstat(state).st_ino
    quarantine = state.with_name(f"{state.name}.failed-{CANDIDATE_COMMIT[:12]}")
    original_fsync = activate_module.fsync_directory
    injected = False

    def fail_first_fsync(path):
        nonlocal injected
        if (not injected and path == state.parent and quarantine.exists()
                and not state.exists()):
            injected = True
            raise OSError("injected post-rename fsync failure")
        return original_fsync(path)

    monkeypatch.setattr(activate_module, "fsync_directory", fail_first_fsync)
    receipt = restore(fixture, StoppedRestoreBackend(fixture))

    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert os.lstat(state).st_ino == original_inode
    assert not state.with_name(f"{state.name}.failed-{CANDIDATE_COMMIT[:12]}").exists()


def test_failed_activation_restore_quarantine_collision_precedes_lifecycle(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    collision = fixture.paths.predecessor_state_dir.with_name(
        f"{fixture.paths.predecessor_state_dir.name}.failed-{CANDIDATE_COMMIT[:12]}"
    )
    collision.mkdir()
    calls: list[tuple[str, ...]] = []

    def record_call(argv, _timeout, **_kwargs):
        calls.append(tuple(argv))
        raise AssertionError("lifecycle must not run after a quarantine collision")

    monkeypatch.setattr(activate_module, "run_bounded", record_call)
    with pytest.raises(activate_module.ActivationError, match="quarantine collision"):
        restore(fixture, StoppedRestoreBackend(fixture))
    assert calls == []
    assert not restore_result_path(fixture).exists()


def test_failed_activation_restore_rejects_third_selector_before_lifecycle(
    monkeypatch, fixture: Fixture
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    third = make_release(
        fixture.paths.releases_root,
        "openclaw-unrelated",
        "3" * 40,
    )
    fixture.paths.current_link.unlink()
    fixture.paths.current_link.symlink_to(third)
    calls: list[tuple[str, ...]] = []

    def record_call(argv, _timeout, **_kwargs):
        calls.append(tuple(argv))
        raise AssertionError("lifecycle must not run for an unrelated selector")

    monkeypatch.setattr(activate_module, "run_bounded", record_call)
    with pytest.raises(activate_module.ActivationError, match="selector target drift"):
        restore(fixture, StoppedRestoreBackend(fixture))
    assert calls == []


def test_failed_activation_restore_boot_failure_is_typed_and_not_retried(fixture: Fixture):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    backend = StoppedRestoreBackend(fixture, gateway_failure=True)
    receipt = restore(fixture, backend)

    assert receipt["outcome"] == "restored_boot_failed"
    assert receipt["error"] == "predecessor_boot_failed"
    assert receipt["restoreApplied"] is True
    assert backend.gateway_boots == 1
    assert backend.node_boots == 0
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)


@pytest.mark.parametrize("service_returncode", [0, 2])
def test_failed_activation_restore_requires_exact_stopped_precondition_without_state_move(
    monkeypatch, fixture: Fixture, service_returncode: int,
):
    assert run(fixture, FakeBackend(fixture, gateway_failure=True))["outcome"] == (
        "snapshot_restore_required"
    )
    state_inode = os.lstat(fixture.paths.predecessor_state_dir).st_ino

    calls: list[tuple[str, ...]] = []

    def service_not_stopped(argv, _timeout, **_kwargs):
        calls.append(tuple(argv))
        assert argv[0:2] == ("/bin/launchctl", "print")
        return activate_module.CommandResult(
            tuple(argv), service_returncode,
            b"loaded\n" if service_returncode == 0 else b"",
            b"ambiguous\n" if service_returncode else b"",
            1, False,
        )

    monkeypatch.setattr(activate_module, "run_bounded", service_not_stopped)
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="stopped-state proof failed"):
        restore(fixture, backend)

    assert os.lstat(fixture.paths.predecessor_state_dir).st_ino == state_inode
    assert len(calls) == 1
    assert all("bootout" not in call for call in calls)
    assert not restore_result_path(fixture).exists()


def test_stale_discord_install_record_fails_before_fence_without_mutation(fixture: Fixture):
    backend = FakeBackend(fixture)
    backend.install_records["discord"] = {
        "source": "npm",
        "installPath": "/old/discord",
    }
    before = json.loads(json.dumps(backend.install_records))

    result = run(fixture, backend)

    assert backend.install_records == before
    assert result["outcome"] == "failed_before_apply"
    assert result["statesVisited"] == ["preflight", "terminal"]
    assert result["restoreRequired"] is False
    assert result["error"] == "preflight_failed"
    assert "private Discord verification diagnostic" not in json.dumps(result)
    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert backend.gateway_boots == backend.node_boots == 0
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    assert not fence.exists()
    assert not fixture.paths.result.exists()

    del backend.install_records["discord"]
    retry = run(fixture, backend)

    assert retry["outcome"] == "activated"
    assert backend.gateway_boots == backend.node_boots == 1


def test_activated_result_replay_reverifies_live_candidate_without_boot(fixture: Fixture):
    first = run(fixture)
    backend = FakeBackend(fixture)
    replay = run(fixture, backend)
    assert replay == first
    assert backend.gateway_boots == backend.node_boots == 0
    assert backend.verifications == 2
    assert backend.receipt_bindings == [
        (activate_module.OPERATOR.require_string('runtime.gateway_label'), True),
        (activate_module.OPERATOR.require_string('runtime.node_label'), True),
    ]


def test_activated_result_replay_rejects_mutable_receipt_before_verification(
    fixture: Fixture,
):
    run(fixture)
    fixture.paths.result.chmod(0o600)
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="existing activation result binding drift",
    ):
        run(fixture, backend)

    assert backend.inspection_calls == backend.verifications == 0


def test_activated_result_replay_accepts_raw_cas_change_with_same_semantics(
    fixture: Fixture,
):
    first = run(fixture)
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(raw_cas_sha256="c" * 64)],
    )

    replay = run(fixture, backend)

    assert replay == first
    assert backend.inspection_calls == 1
    assert backend.gateway_boots == backend.node_boots == 0


def test_activated_result_replay_rejects_exec_approvals_semantic_drift(
    fixture: Fixture,
):
    run(fixture)
    backend = FakeBackend(
        fixture,
        approval_reports=[approval_report(semantic_sha256="c" * 64)],
    )

    with pytest.raises(
        activate_module.ActivationError,
        match="exec approvals semantic drift",
    ):
        run(fixture, backend)

    assert backend.gateway_boots == backend.node_boots == 0


def test_orphaned_fence_terminalizes_without_second_boot(fixture: Fixture):
    candidate = activate_module.validate_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)
    snapshot = activate_module.load_stopped_snapshot(
        fixture.paths, fixture.snapshot, candidate
    )
    fence = fixture.paths.result.with_name(activate_module.START_CONSUMED_NAME)
    bindings = {
        "schemaVersion": 1,
        "candidateSealPath": str(fixture.seal),
        "candidateSealSha256": candidate["sealSha256"],
        "snapshotManifestPath": str(fixture.snapshot),
        "snapshotManifestSha256": snapshot["manifestSha256"],
        "terminalResultPath": str(fixture.paths.result),
        "consumedAt": activate_module.utc_now(),
    }
    activate_module.create_immutable_file(fence, json.dumps(bindings, indent=2, sort_keys=True).encode() + b"\n")
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["error"] == "start_already_consumed"
    assert backend.gateway_boots == backend.node_boots == 0


def test_fixed_lock_contention_does_not_write_result(fixture: Fixture):
    descriptor = os.open(fixture.paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(activate_module.ActivationError, match="holds the fixed lock"):
            run(fixture)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert not fixture.paths.result.exists()


def test_snapshot_archive_drift_fails_before_boot(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    archive = Path(manifest["tarPath"])
    archive.chmod(0o600)
    archive.write_bytes(b"tampered\n")
    archive.chmod(0o400)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


def test_different_version_without_reviewed_upgrade_input_is_rejected(fixture: Fixture):
    package = fixture.candidate / "package.json"
    package.chmod(0o600)
    value = json.loads(package.read_text())
    value["version"] = "2026.9.0"
    package.write_text(json.dumps(value) + "\n")
    package.chmod(0o444)
    fixture.seal.chmod(0o600)
    fixture.seal.unlink()
    activate_module.create_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)
    backend = FakeBackend(fixture)
    result = run(fixture, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.gateway_boots == backend.node_boots == 0


class UpgradeBackend(FakeBackend):
    """Only the native migration/process boundary is fake; snapshots use real tar."""

    def __init__(self, fixture, *, migration_failure=None, **kwargs):
        super().__init__(fixture, **kwargs)
        self.migration_failure = migration_failure
        self.migrations = 0
        self.inspected_releases = []

    def inspect_exec_approvals(self, release):
        version = (self.fixture.paths.candidate_state_dir / "schema.version").read_text()
        if version not in ("17", "19"):
            raise activate_module.ActivationError("partial migration is not runtime readable")
        assert release == (self.fixture.predecessor if version == "17" else self.fixture.candidate)
        self.inspected_releases.append(release)
        self.events.append("inspect")
        report = approval_report(raw_cas_sha256=("b" if version == "17" else "c") * 64)
        if version == "19" and self.migration_failure == "auth-drift":
            report["semanticSha256"] = "d" * 64
        return activate_module.validate_exec_approvals_inspection(report)

    def migrate_state_once(self, release):
        assert release == self.fixture.candidate
        assert activate_module._start_fence_path(self.fixture.paths).exists()
        assert self.fixture.paths.current_link.resolve() == self.fixture.predecessor
        self.migrations += 1
        self.events.append("migrate")
        state = self.fixture.paths.candidate_state_dir
        (state / "schema.version").write_text("19")
        if self.migration_failure in ("partial", "invalid-config"):
            (state / "schema.version").write_text("partial")
            if self.migration_failure == "invalid-config":
                (state / "openclaw.json").write_text("invalid intermediate config")
            raise activate_module.ActivationError("private migration diagnostic")
        if self.migration_failure == "config-drift":
            config = state / "openclaw.json"
            config.write_bytes(config.read_bytes() + b"\n")
        return {"schemaVersion": 1, "status": "completed", "stateSchemaVersion": 15,
                "agentSchemaVersion": 19, "agentDatabaseCount": 1}

    def verify_candidate_discord(self, release, config_path):
        assert (self.fixture.paths.candidate_state_dir / "schema.version").read_text() == "19"
        return {"origin": "bundled", "configSha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}


@pytest.fixture
def upgrade_fixture(fixture):
    chmod_tree(fixture.predecessor, True)
    inspector = fixture.predecessor / "dist" / "infra" / "exec-approvals-inspection.js"
    inspector.parent.mkdir(parents=True)
    inspector.write_text("export function inspectExecApprovalsState() {}\n")
    chmod_tree(fixture.predecessor, False)
    chmod_tree(fixture.candidate, True)
    package = fixture.candidate / "package.json"
    package.write_text(json.dumps({"name": "openclaw", "version": "2026.9.2",
                                   "openclaw": {"schemaVersions": {"state": 15, "agent": 19}}}) + "\n")
    migration = fixture.candidate / "dist" / "infra" / "runtime-state-migration.js"
    migration.write_text("export async function migrateRuntimeState() {}\n")
    chmod_tree(fixture.candidate, False)
    fixture.seal = fixture.root / "upgrade-seal.json"
    activate_module.create_candidate_seal(fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal)
    (fixture.paths.candidate_state_dir / "schema.version").write_text("17")
    output = fixture.root / "upgrade-snapshot"
    fixture.snapshot = output / "stopped-snapshot.json"
    backend = UpgradeBackend(fixture)
    activate_module.create_stopped_snapshot(
        fixture.paths, output, fixture.snapshot, fixture.candidate,
        CANDIDATE_COMMIT, fixture.seal, backend,
    )
    assert backend.inspected_releases == [fixture.predecessor, fixture.predecessor]
    assert backend.migrations == backend.gateway_boots == backend.node_boots == 0
    config = json.loads(fixture.config_bytes)
    config["agents"] = {"defaults": {"thinkingDefault": "ultra"}}
    target = fixture.root / "reviewed-upgrade-config.json"
    target.write_text(json.dumps(config, sort_keys=True) + "\n")
    target.chmod(0o400)
    return fixture, target


def run_upgrade(fixture, target, backend):
    return activate_module.activate(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
        fixture.snapshot, backend, upgrade_config=target,
    )


def test_upgrade_preserves_old_snapshot_and_uses_native_readers_in_schema_order(upgrade_fixture):
    fixture, target = upgrade_fixture
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "activated"
    assert backend.migrations == backend.gateway_boots == backend.node_boots == 1
    assert backend.inspected_releases == [fixture.predecessor, fixture.candidate, fixture.candidate]
    assert result["snapshot"]["recoveredInvariants"]["config"]["sha256"] == hashlib.sha256(fixture.config_bytes).hexdigest()
    assert result["verification"]["recoveredInvariants"]["config"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["firstBootAttempted"] is True
    assert stat.S_IMODE((fixture.paths.candidate_state_dir / "openclaw.json").stat().st_mode) == 0o600
    repeated = UpgradeBackend(fixture)
    assert run_upgrade(fixture, target, repeated) == result
    assert repeated.migrations == repeated.gateway_boots == repeated.node_boots == 0


@pytest.mark.parametrize("failure", ["partial", "invalid-config", "config-drift", "auth-drift"])
def test_upgrade_migration_failure_is_fenced_before_boot_and_restorable(upgrade_fixture, failure):
    fixture, target = upgrade_fixture
    backend = UpgradeBackend(fixture, migration_failure=failure)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["firstBootAttempted"] is False
    assert backend.migrations == 1
    assert backend.gateway_boots == backend.node_boots == 0
    repeated = UpgradeBackend(fixture)
    assert run_upgrade(fixture, target, repeated) == result
    assert repeated.migrations == repeated.gateway_boots == 0
    restored_backend = UpgradeBackend(fixture)
    receipt = restore(fixture, restored_backend)
    assert receipt["outcome"] == "restored"
    assert restored_backend.inspected_releases == [fixture.predecessor]
    assert restored_backend.gateway_boots == restored_backend.node_boots == 1
    assert (fixture.paths.candidate_state_dir / "schema.version").read_text() == "17"
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes
    assert "private migration diagnostic" not in json.dumps(result)


def test_upgrade_config_write_occurs_after_fence_and_failure_is_restorable(monkeypatch, upgrade_fixture):
    fixture, target = upgrade_fixture
    install = activate_module.install_upgrade_config
    def fail_after_write(paths, binding):
        assert activate_module._start_fence_path(paths).exists()
        install(paths, binding)
        raise OSError("injected post-config-write failure")
    monkeypatch.setattr(activate_module, "install_upgrade_config", fail_after_write)
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["firstBootAttempted"] is False
    assert backend.migrations == backend.gateway_boots == 0
    assert restore(fixture, UpgradeBackend(fixture))["outcome"] == "restored"


@pytest.mark.parametrize("after_success", [False, True])
def test_upgrade_restore_after_boot_preserves_effects_and_does_not_replay(upgrade_fixture, after_success):
    fixture, target = upgrade_fixture
    backend = UpgradeBackend(fixture, gateway_failure=not after_success)
    result = run_upgrade(fixture, target, backend)
    assert result["firstBootAttempted"] is True
    marker = fixture.paths.candidate_state_dir / "completed-after-upgrade"
    marker.write_text("delivery already completed; never replay from restored pending queue")
    if after_success:
        with pytest.raises(activate_module.ActivationError, match="restore binding drift"):
            restore(fixture, UpgradeBackend(fixture))
    recovery = UpgradeBackend(fixture)
    receipt = activate_module.restore_failed_activation(
        fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
        fixture.snapshot, restore_result_path(fixture), recovery, after_success=after_success,
    )
    assert receipt["outcome"] == "restored_stopped"
    assert receipt["restoreApplied"] is True
    assert recovery.gateway_boots == recovery.node_boots == recovery.migrations == 0
    quarantine = Path(receipt["quarantinedFailedSurfaces"][0])
    assert (quarantine / marker.name).read_text() == "delivery already completed; never replay from restored pending queue"
    assert (quarantine / "schema.version").read_text() == "19"
    assert fixture.paths.current_link.resolve() == fixture.predecessor
    assert (fixture.paths.candidate_state_dir / "schema.version").read_text() == "17"
    with pytest.raises(activate_module.ActivationError, match="already exists"):
        activate_module.restore_failed_activation(
            fixture.paths, fixture.candidate, CANDIDATE_COMMIT, fixture.seal,
            fixture.snapshot, restore_result_path(fixture), recovery, after_success=after_success,
        )
    retirement = activate_module.retire_terminal_receipts(
        fixture.paths, fixture.paths.result.parent / "archive" / "restored-upgrade", recovery,
    )
    assert retirement["outcome"] == "restored_stopped"
    assert recovery.gateway_boots == recovery.node_boots == 0


@pytest.mark.parametrize("invalid", ["missing", "writable", "symlink", "policy"])
def test_upgrade_rejects_missing_or_unreviewed_config_before_fence(upgrade_fixture, invalid):
    fixture, target = upgrade_fixture
    if invalid == "missing":
        target = None
    elif invalid == "writable":
        target.chmod(0o600)
    elif invalid == "symlink":
        link = target.with_name("linked-config.json")
        link.symlink_to(target)
        target = link
    else:
        target.chmod(0o600)
        config = json.loads(target.read_bytes())
        config["auth"]["cooldowns"] = {}
        target.write_text(json.dumps(config))
        target.chmod(0o400)
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "failed_before_apply"
    assert backend.migrations == backend.gateway_boots == 0
    assert not activate_module._start_fence_path(fixture.paths).exists()
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes


def test_upgrade_snapshot_rejects_substituted_predecessor_reader(upgrade_fixture):
    fixture, _target = upgrade_fixture
    value = json.loads(fixture.snapshot.read_bytes())
    value["execApprovalsInspector"]["predecessorReader"]["commit"] = "f" * 40
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(value))
    fixture.snapshot.chmod(0o400)
    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(fixture.paths, fixture.snapshot, sealed_candidate(fixture))


def test_upgrade_retirement_rejects_malformed_snapshot_hash_without_mutation(upgrade_fixture):
    fixture, target = upgrade_fixture
    result = run_upgrade(fixture, target, UpgradeBackend(fixture))
    result["snapshot"]["manifestSha256"] = None
    fixture.paths.result.chmod(0o600)
    fixture.paths.result.write_text(json.dumps(result))
    fixture.paths.result.chmod(0o400)
    archive = fixture.paths.result.parent / "archive" / "invalid-upgrade"
    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.retire_terminal_receipts(fixture.paths, archive)
    assert fixture.paths.result.exists()
    assert activate_module._start_fence_path(fixture.paths).exists()
    assert not archive.exists()


def test_orphaned_upgrade_fence_never_assumes_no_boot_or_repeats_migration(upgrade_fixture):
    fixture, target = upgrade_fixture
    assert run_upgrade(fixture, target, UpgradeBackend(fixture, migration_failure="partial"))["outcome"] == "snapshot_restore_required"
    fixture.paths.result.unlink()
    backend = UpgradeBackend(fixture)
    result = run_upgrade(fixture, target, backend)
    assert result["outcome"] == "snapshot_restore_required"
    assert result["firstBootAttempted"] is None
    assert backend.migrations == backend.gateway_boots == backend.node_boots == 0
    recovery = UpgradeBackend(fixture)
    assert restore(fixture, recovery)["outcome"] == "restored_stopped"
    assert recovery.gateway_boots == recovery.node_boots == 0


def test_partial_upgrade_restore_extraction_failure_preserves_unreadable_preimage(monkeypatch, upgrade_fixture):
    fixture, target = upgrade_fixture
    assert run_upgrade(fixture, target, UpgradeBackend(fixture, migration_failure="invalid-config"))["outcome"] == "snapshot_restore_required"
    original_inode = fixture.paths.candidate_state_dir.stat().st_ino
    def failed_extract(argv, _timeout, **_kwargs):
        assert argv[0] == "/usr/bin/tar" and "-xpf" in argv
        return activate_module.CommandResult(argv, 1, b"", b"private extraction error", 1, False)
    monkeypatch.setattr(activate_module, "run_bounded", failed_extract)
    recovery = UpgradeBackend(fixture)
    receipt = restore(fixture, recovery)
    assert receipt["outcome"] == "restore_failed_preimage_restored"
    assert fixture.paths.candidate_state_dir.stat().st_ino == original_inode
    assert (fixture.paths.candidate_state_dir / "schema.version").read_text() == "partial"
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_text() == "invalid intermediate config"
    assert recovery.inspected_releases == []
    assert recovery.gateway_boots == recovery.node_boots == 0


@pytest.mark.parametrize("failure", [None, "nonzero", "timeout", "malformed", "wrong-version", "status-only", "extra-field"])
def test_native_migration_invoker_uses_sealed_export_and_typed_value_free_receipt(monkeypatch, upgrade_fixture, failure):
    fixture, _target = upgrade_fixture
    report = {"schemaVersion": 1, "status": "completed", "stateSchemaVersion": 15,
              "agentSchemaVersion": 19, "agentDatabaseCount": 1}
    expected = dict(report)
    if failure == "wrong-version":
        report["agentSchemaVersion"] = 17
    elif failure == "status-only":
        report = {"status": "completed"}
    elif failure == "extra-field":
        report["privateDiagnostic"] = "must-not-escape"
    calls = []
    def fake_run(argv, timeout, *, cwd, env):
        calls.append((argv, timeout, cwd, env))
        assert env["OPENCLAW_STATE_DIR"] == str(fixture.paths.candidate_state_dir)
        assert env["OPENCLAW_CONFIG_PATH"] == str(fixture.paths.candidate_state_dir / "openclaw.json")
        assert env["XDG_CACHE_HOME"] == str(fixture.paths.candidate_state_dir / ".cache")
        assert env["OPENCLAW_LOG_LEVEL"] == "silent"
        assert env["OPENCLAW_MIGRATION_ENTRYPOINT"] == str(fixture.candidate / "dist" / "infra" / "runtime-state-migration.js")
        assert "migrateRuntimeState" in argv[-1] and "doctor" not in argv[-1]
        return activate_module.CommandResult(
            argv, 2 if failure == "nonzero" else 0,
            b"malformed" if failure == "malformed" else json.dumps(report).encode(),
            b"private diagnostic never exposed", 1, failure == "timeout",
        )
    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    if failure is None:
        assert backend.migrate_state_once(fixture.candidate) == expected
    else:
        with pytest.raises(activate_module.ActivationError) as error:
            backend.migrate_state_once(fixture.candidate)
        assert "private" not in str(error.value)
    assert len(calls) == 1
    evidence = json.dumps(backend.command_evidence)
    assert "private diagnostic never exposed" not in evidence
    assert "privateDiagnostic" not in evidence
    assert "must-not-escape" not in evidence


def test_reduced_source_has_no_initial_cutover_migration_or_plugin_install_contract():
    source = SCRIPT.read_text()
    for retired in (
        "ExternalPluginBootstrapBinding",
        "load_external_plugin_bootstrap_manifest",
        "validate_external_plugin_bootstrap",
        "validate_main_session_sqlite_migration",
        "normalize_expected_beta3_imported_cron_job",
        "EXPECTED_RELEASE_PACKAGE_INVENTORY",
        "EXPECTED_CONFIG_EVIDENCE_SHA256",
        "EXPECTED_OPENAI_PROFILE_ORDER",
        "@openclaw/discord@2026.8.1-beta.3",
        "coordinator",
        "adjudication",
        "operation UUID",
    ):
        assert retired not in source
    # SQLite is confined to the exact read-only TCC query, never state migration.
    permission_source = inspect.getsource(activate_module.screen_capture_permission)
    assert "sqlite3" not in source.replace(permission_source, "").replace("import sqlite3\n", "")
    assert 'modes.add_parser("activate")' in source
    assert 'modes.add_parser("prepare")' not in source
    assert 'modes.add_parser("start")' not in source


def test_lifecycle_bootstrap_vectors_are_exact_and_nonretrying(monkeypatch, fixture: Fixture):
    calls = []
    def fake_run(argv, timeout):
        calls.append(argv)
        return activate_module.CommandResult(argv, 0, b"", b"", 1, False)
    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()
    backend.bootstrap_node_once()
    domain = f"gui/{fixture.paths.operator_uid}"
    assert calls == [
        ("/bin/launchctl", "enable", f"{domain}/{activate_module.OPERATOR.require_string('runtime.gateway_label')}"),
        ("/bin/launchctl", "bootstrap", domain, str(fixture.paths.gateway_plist)),
        ("/bin/launchctl", "enable", f"{domain}/{activate_module.OPERATOR.require_string('runtime.node_label')}"),
        ("/bin/launchctl", "bootstrap", domain, str(fixture.paths.node_plist)),
    ]


def test_failed_enable_does_not_create_bootstrap_receipt_authority(
    monkeypatch,
    fixture: Fixture,
):
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(argv, returncode=1),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="gateway_enable failed"):
        backend.bootstrap_gateway_once()

    assert backend.bootstrap_bindings() == {}


@pytest.mark.parametrize(
    "tamper",
    ["boolean-return", "wrong-executable", "wrong-digest", "missing", "extra"],
)
def test_successful_command_receipt_rejects_malformed_or_relabelled_evidence(
    fixture: Fixture,
    tamper: str,
):
    argv = (
        "/bin/launchctl",
        "bootstrap",
        f"gui/{fixture.paths.operator_uid}",
        str(fixture.paths.node_plist),
    )
    evidence = activate_module.CommandResult(
        argv,
        0,
        b"",
        b"",
        1,
        False,
    ).evidence("node_bootstrap")
    if tamper == "boolean-return":
        evidence["returnCode"] = False
    elif tamper == "wrong-executable":
        evidence["executable"] = "/usr/bin/true"
    elif tamper == "wrong-digest":
        evidence["argumentVectorSha256"] = "0" * 64
    elif tamper == "missing":
        del evidence["stderrSha256"]
    else:
        evidence["unexpected"] = True

    assert activate_module.receipt_has_successful_command(
        [evidence],
        "node_bootstrap",
        argv,
    ) is False


def test_clear_failed_bootstrap_does_not_create_receipt_authority(
    monkeypatch,
    fixture: Fixture,
):
    calls = 0

    def fake_run(argv, _timeout):
        nonlocal calls
        calls += 1
        return command_result(argv, returncode=0 if calls == 1 else 1)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="gateway_bootstrap failed"):
        backend.bootstrap_gateway_once()

    assert backend.bootstrap_bindings() == {}


def test_timed_out_bootstrap_retains_only_the_ambiguous_attempt_binding(
    monkeypatch,
    fixture: Fixture,
):
    calls = 0

    def fake_run(argv, _timeout):
        nonlocal calls
        calls += 1
        return command_result(
            argv,
            returncode=0 if calls == 1 else 124,
            timed_out=calls == 2,
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="gateway_bootstrap failed"):
        backend.bootstrap_gateway_once()

    binding = backend.bootstrap_bindings()[activate_module.OPERATOR.require_string('runtime.gateway_label')]
    assert binding["release"] == str(fixture.predecessor)
    assert binding["releaseDevice"] == os.lstat(fixture.predecessor).st_dev
    assert binding["releaseInode"] == os.lstat(fixture.predecessor).st_ino


def launchctl_payload(fixture: Fixture, label: str, *, pid: int = 100,
                      runs: int = 1, arguments: list[str] | None = None,
                      service_path: Path | None = None,
                      working_directory: Path | None = None) -> bytes:
    plist_path = fixture.paths.gateway_plist if label == activate_module.OPERATOR.require_string('runtime.gateway_label') else fixture.paths.node_plist
    persisted = plistlib.loads(plist_path.read_bytes())
    argv = arguments or persisted["ProgramArguments"]
    path = service_path or plist_path
    if label == activate_module.OPERATOR.require_string('runtime.gateway_label'):
        working_directory = working_directory or Path(persisted["WorkingDirectory"])
    argument_lines = "".join(f"\t\t{item}\n" for item in argv)
    working_line = (
        f"\tworking directory = {working_directory}\n"
        if working_directory is not None
        else ""
    )
    return (
        f"gui/{fixture.paths.operator_uid}/{label} = {{\n"
        f"\tpath = {path}\n"
        f"{working_line}"
        "\targuments = {\n"
        f"{argument_lines}"
        "\t}\n"
        f"\tpid = {pid}\n"
        f"\truns = {runs}\n"
        "}\n"
    ).encode()


def cwd_payload(release: Path, pid: int) -> bytes:
    info = os.lstat(release)
    return (f"p{pid}\0fcwd\0D{info.st_dev:x}\0i{info.st_ino}\0n{release}\0\n").encode()


def start_token_after_bootstrap(
    backend: activate_module.SystemBackend,
    label: str,
) -> str:
    started_at_us = backend._bootstrap_bindings[label]["startedAtUs"] + 1
    return f"darwin:{started_at_us // 1_000_000}:{started_at_us % 1_000_000}"


def test_lsof_cwd_parser_accepts_exact_real_null_newline_bytes(fixture: Fixture):
    pid = 4321
    info = os.lstat(fixture.candidate)
    payload = (
        f"p{pid}\0\nfcwd\0D{info.st_dev:x}\0i{info.st_ino}\0"
        f"n{fixture.candidate}\0\n"
    ).encode()

    assert activate_module.parse_lsof_cwd(payload, pid) == (
        fixture.candidate,
        info.st_dev,
        info.st_ino,
    )


def command_result(argv, returncode=0, stdout=b"", stderr=b"", timed_out=False):
    return activate_module.CommandResult(tuple(argv), returncode, stdout, stderr, 1, timed_out)


def install_system_backend_observation_mocks(
    monkeypatch,
    fixture: Fixture,
) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_run(argv, _timeout, **_kwargs):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:2] == ("/bin/launchctl", "print"):
            label = argv[-1].rsplit("/", 1)[-1]
            pid = 100 if label == activate_module.OPERATOR.require_string('runtime.gateway_label') else 200
            return command_result(
                argv,
                stdout=launchctl_payload(fixture, label, pid=pid),
            )
        if "-iTCP:18789" in argv:
            return command_result(
                argv,
                stdout=b"p100\nf9\nn127.0.0.1:18789\n",
            )
        if argv[0] == "/usr/sbin/lsof" and "-d" in argv:
            return command_result(
                argv,
                stdout=cwd_payload(
                    activate_module.expected_gateway_working_directory(
                        fixture.paths
                    ),
                    100,
                ),
            )
        raise AssertionError(f"unexpected command: {argv}")

    bodies = {
        "/healthz": b'{"ok":true,"status":"live"}',
        "/readyz": b'{"ready":true,"failing":[],"uptimeMs":1}',
    }

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def read(self, _maximum):
            return self.payload

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.path = None

        def request(self, _method, path):
            self.path = path

        def getresponse(self):
            return Response(bodies[self.path])

        def close(self):
            pass

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(activate_module.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda pid: (
            "darwin:2:0",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    return calls


def test_fresh_system_backend_replays_activated_receipt_without_lifecycle(
    monkeypatch,
    fixture: Fixture,
):
    first = run(fixture)
    calls = install_system_backend_observation_mocks(monkeypatch, fixture)
    backend = activate_module.SystemBackend(fixture.paths)
    monkeypatch.setattr(
        backend,
        "inspect_exec_approvals",
        lambda _release: approval_report(),
    )

    replay = activate_module.activate(
        fixture.paths,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
        fixture.snapshot,
        backend,
    )

    assert replay == first
    assert set(backend.bootstrap_bindings()) == {
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        activate_module.OPERATOR.require_string('runtime.node_label'),
    }
    assert all("enable" not in call and "bootstrap" not in call for call in calls)


def test_fresh_system_backend_receipt_binding_rejects_later_generation(
    monkeypatch,
    fixture: Fixture,
):
    first = run(fixture)
    install_system_backend_observation_mocks(monkeypatch, fixture)
    backend = activate_module.SystemBackend(
        replace(fixture.paths, health_timeout_seconds=0.0)
    )
    loaded = first["verification"]["loaded"]
    backend.bind_bootstrap_receipt(
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        loaded["bootstrapBinding"],
        loaded,
    )
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            "darwin:3:0",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    info = os.lstat(fixture.candidate)

    with pytest.raises(
        activate_module.ActivationError,
        match="receipt generation changed",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            info.st_dev,
            info.st_ino,
        )


def test_fresh_system_backend_late_verifies_bound_restore_without_lifecycle(
    monkeypatch,
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    assert set(restored["bootstrapBindings"]) == {
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        activate_module.OPERATOR.require_string('runtime.node_label'),
    }
    calls = install_system_backend_observation_mocks(monkeypatch, fixture)
    backend = activate_module.SystemBackend(fixture.paths)
    archive_dir = (
        fixture.paths.result.parent / "archive" / "real-system-late-restore"
    )

    receipt = activate_module.retire_terminal_receipts(
        fixture.paths,
        archive_dir,
        backend,
    )

    assert receipt["outcome"] == "restored_after_late_verification"
    assert receipt["lateVerification"]["gateway"]["loaded"]["pid"] == 100
    assert receipt["lateVerification"]["node"]["pid"] == 200
    assert all("enable" not in call and "bootstrap" not in call for call in calls)


def test_gateway_first_restore_failure_without_node_binding_is_not_retirable(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, gateway_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    assert restored["bootstrapBindings"] == {}
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "gateway-first-failure",
            backend,
        )

    assert backend.verifications == 0


def test_clear_node_bootstrap_rejection_cannot_be_retired_by_a_later_node(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restored = restore(
        fixture,
        StoppedRestoreBackend(fixture, node_bootstrap_failure=True),
    )
    assert restored["outcome"] == "restored_boot_failed"
    assert set(restored["bootstrapBindings"]) == {
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
    }
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "clear-node-rejection",
            backend,
        )

    assert backend.verifications == 0


def test_ambiguous_node_bootstrap_without_observed_generation_is_not_retirable(
    fixture: Fixture,
):
    assert run(
        fixture,
        FakeBackend(fixture, gateway_failure=True),
    )["outcome"] == "snapshot_restore_required"
    restore(
        fixture,
        StoppedRestoreBackend(fixture, node_verification_failure=True),
    )
    restore_path = restore_result_path(fixture)
    restored = json.loads(restore_path.read_text())
    node_evidence = next(
        item
        for item in restored["commandEvidence"]
        if item.get("purpose") == "node_bootstrap"
    )
    node_evidence["returnCode"] = 124
    node_evidence["timedOut"] = True
    restore_path.chmod(0o600)
    restore_path.write_text(json.dumps(restored, indent=2, sort_keys=True) + "\n")
    restore_path.chmod(0o400)
    backend = FakeBackend(fixture)

    with pytest.raises(
        activate_module.ActivationError,
        match="restore receipt binding drift",
    ):
        activate_module.retire_terminal_receipts(
            fixture.paths,
            fixture.paths.result.parent / "archive" / "ambiguous-node",
            backend,
        )

    assert backend.verifications == 0


def test_activated_replay_rejects_malformed_receipt_binding_before_verification(
    monkeypatch,
    fixture: Fixture,
):
    result = run(fixture)
    result["verification"]["loaded"]["bootstrapBinding"]["startedAtUs"] = "bad"
    fixture.paths.result.chmod(0o600)
    fixture.paths.result.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    fixture.paths.result.chmod(0o400)
    backend = activate_module.SystemBackend(fixture.paths)
    monkeypatch.setattr(
        backend,
        "inspect_exec_approvals",
        lambda _release: approval_report(),
    )

    with pytest.raises(
        activate_module.ActivationError,
        match="bootstrap receipt binding drift",
    ):
        activate_module.activate(
            fixture.paths,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.seal,
            fixture.snapshot,
            backend,
        )


def test_snapshot_capture_accepts_legacy_predecessor_and_observes_stopped(
    monkeypatch, fixture: Fixture
):
    assert not (
        fixture.predecessor / "dist" / "infra" / "exec-approvals-inspection.js"
    ).exists()
    assert not (
        fixture.predecessor
        / "dist"
        / "extensions"
        / "browser"
        / "chrome-extension"
    ).exists()
    calls = []
    backend = FakeBackend(fixture)

    def fake_run(argv, _timeout, **_kwargs):
        calls.append(argv)
        if argv[0] == "/usr/bin/tar":
            backend.events.append("tar")
            subprocess.run(argv, check=True, capture_output=True)
            return command_result(argv)
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    output_root = fixture.root / "observed-snapshot"
    manifest = output_root / "stopped-snapshot.json"

    receipt = activate_module.create_stopped_snapshot(
        fixture.paths,
        output_root,
        manifest,
        fixture.candidate,
        CANDIDATE_COMMIT,
        fixture.seal,
        backend,
    )

    assert receipt["manifestPath"] == str(manifest)
    assert [item[0] for item in calls] == ["/usr/bin/tar"]
    assert calls[0][8:] == (
        str(fixture.paths.predecessor_state_dir).lstrip("/"),
        str(fixture.paths.gateway_plist).lstrip("/"),
        str(fixture.paths.node_plist).lstrip("/"),
        str(fixture.paths.current_link).lstrip("/"),
    )
    assert "exec-approvals.json" not in "\0".join(calls[0])
    assert backend.events == ["stopped", "inspect", "tar", "stopped", "inspect"]
    g6 = json.loads((output_root / "stopped-quiescence.json").read_text())
    activate_module.validate_stopped_command_evidence(fixture.paths, g6["preCapture"])
    activate_module.validate_stopped_command_evidence(fixture.paths, g6["postCapture"])
    assert stat.S_IMODE((output_root / "stopped-snapshot.tar").stat().st_mode) == 0o400
    assert stat.S_IMODE((output_root / "stopped-quiescence.json").stat().st_mode) == 0o400
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o400


def test_snapshot_rejects_raw_cas_drift_before_manifest_publication(
    monkeypatch, fixture: Fixture
):
    backend = FakeBackend(
        fixture,
        approval_reports=[
            approval_report(),
            approval_report(raw_cas_sha256="c" * 64),
        ],
    )

    def fake_run(argv, _timeout, **_kwargs):
        if argv[0] != "/usr/bin/tar":
            raise AssertionError(f"unexpected command: {argv}")
        backend.events.append("tar")
        Path(argv[5]).write_bytes(b"test stopped archive\n")
        return command_result(argv)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    output_root = fixture.root / "drifted-snapshot"
    manifest = output_root / "stopped-snapshot.json"

    with pytest.raises(
        activate_module.ActivationError,
        match="protected surface drift",
    ):
        activate_module.create_stopped_snapshot(
            fixture.paths,
            output_root,
            manifest,
            fixture.candidate,
            CANDIDATE_COMMIT,
            fixture.seal,
            backend,
        )

    assert backend.events == ["stopped", "inspect", "tar", "stopped", "inspect"]
    assert not manifest.exists()


def test_snapshot_rejects_self_attested_quiescence(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    g6 = Path(manifest["g6QuiescencePath"])
    g6.chmod(0o600)
    g6.write_text('{"gatewayStopped":true,"nodeStopped":true}\n')
    g6.chmod(0o400)
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps({
        **manifest,
        "g6QuiescenceSha256": hashlib.sha256(g6.read_bytes()).hexdigest(),
    }, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="observed quiescence"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_writable_manifest(fixture: Fixture):
    fixture.snapshot.chmod(0o600)
    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_inventory_count_drift(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps({
        **manifest,
        "inventoryCount": 5,
    }, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_extra_archive_member_even_with_rebound_hashes(
    fixture: Fixture,
):
    manifest = json.loads(fixture.snapshot.read_text())
    archive = Path(manifest["tarPath"])
    extra = fixture.root / "out-of-scope-snapshot-member.txt"
    extra.write_text("must never be restored\n")
    archive.chmod(0o600)
    archive.unlink()
    subprocess.run(
        [
            "/usr/bin/tar",
            "-cpf",
            str(archive),
            "-C",
            "/",
            str(fixture.paths.predecessor_state_dir).lstrip("/"),
            str(fixture.paths.gateway_plist).lstrip("/"),
            str(fixture.paths.node_plist).lstrip("/"),
            str(fixture.paths.current_link).lstrip("/"),
            str(extra).lstrip("/"),
        ],
        check=True,
        capture_output=True,
    )
    archive.chmod(0o400)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()

    g6 = Path(manifest["g6QuiescencePath"])
    g6_value = json.loads(g6.read_text())
    g6_value["archiveSha256"] = archive_sha256
    g6.chmod(0o600)
    g6.write_text(json.dumps(g6_value, indent=2, sort_keys=True) + "\n")
    g6.chmod(0o400)

    manifest["tarSha256"] = archive_sha256
    manifest["g6QuiescenceSha256"] = hashlib.sha256(g6.read_bytes()).hexdigest()
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(
        activate_module.ActivationError,
        match="archive inventory drift",
    ):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_archive_accepts_appledouble_for_exact_surface(
    fixture: Fixture,
):
    archive = fixture.root / "appledouble-snapshot.tar"
    with tarfile.open(archive, "w") as output:
        for path in (
            fixture.paths.predecessor_state_dir,
            fixture.paths.gateway_plist,
            fixture.paths.node_plist,
            fixture.paths.current_link,
        ):
            output.add(path, arcname=str(path).lstrip("/"), recursive=True)
        gateway_appledouble = tarfile.TarInfo(
            str(
                fixture.paths.gateway_plist.with_name(
                    f"._{fixture.paths.gateway_plist.name}"
                )
            ).lstrip("/")
        )
        gateway_appledouble.size = 5
        output.addfile(gateway_appledouble, io.BytesIO(b"xattr"))

    activate_module.validate_snapshot_archive_members(
        fixture.paths,
        archive,
        str(fixture.predecessor),
    )


@pytest.mark.parametrize(
    ("target", "accepted"),
    [
        ("/Users/operator/.ssh", True),
        ("../../outside-state", False),
        ("nested/target.txt", True),
    ],
)
def test_snapshot_archive_state_symlink_allows_bound_absolute_target(
    fixture: Fixture,
    target: str,
    accepted: bool,
):
    nested = fixture.paths.predecessor_state_dir / "nested"
    nested.mkdir()
    (nested / "target.txt").write_text("internal target\n")
    (fixture.paths.predecessor_state_dir / "state-link").symlink_to(target)
    archive = fixture.root / "state-symlink-snapshot.tar"
    subprocess.run(
        [
            "/usr/bin/tar",
            "-cpf",
            str(archive),
            "-C",
            "/",
            str(fixture.paths.predecessor_state_dir).lstrip("/"),
            str(fixture.paths.gateway_plist).lstrip("/"),
            str(fixture.paths.node_plist).lstrip("/"),
            str(fixture.paths.current_link).lstrip("/"),
        ],
        check=True,
        capture_output=True,
    )

    if accepted:
        activate_module.validate_snapshot_archive_members(
            fixture.paths,
            archive,
            str(fixture.predecessor),
        )
    else:
        with pytest.raises(
            activate_module.ActivationError,
            match="archive inventory drift",
        ):
            activate_module.validate_snapshot_archive_members(
                fixture.paths,
                archive,
                str(fixture.predecessor),
            )


def test_snapshot_archive_rejects_member_beneath_absolute_state_symlink(
    fixture: Fixture,
):
    state_link = fixture.paths.predecessor_state_dir / "state-link"
    state_link.symlink_to("/Users/operator/.ssh")
    archive = fixture.root / "state-symlink-descendant-snapshot.tar"
    with tarfile.open(archive, "w") as output:
        for path in (
            fixture.paths.predecessor_state_dir,
            fixture.paths.gateway_plist,
            fixture.paths.node_plist,
            fixture.paths.current_link,
        ):
            output.add(path, arcname=str(path).lstrip("/"), recursive=True)
        injected = tarfile.TarInfo(f"{str(state_link).lstrip('/')}/injected")
        injected.size = 1
        output.addfile(injected, io.BytesIO(b"x"))

    archive.chmod(0o400)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = json.loads(fixture.snapshot.read_text())
    g6 = Path(manifest["g6QuiescencePath"])
    g6_value = json.loads(g6.read_text())
    g6_value["archivePath"] = str(archive)
    g6_value["archiveSha256"] = archive_sha256
    g6.chmod(0o600)
    g6.write_text(json.dumps(g6_value, indent=2, sort_keys=True) + "\n")
    g6.chmod(0o400)
    manifest["tarPath"] = str(archive)
    manifest["tarSha256"] = archive_sha256
    manifest["g6QuiescenceSha256"] = hashlib.sha256(g6.read_bytes()).hexdigest()
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(
        activate_module.ActivationError,
        match="archive inventory drift",
    ):
        activate_module.load_stopped_snapshot(
            fixture.paths,
            fixture.snapshot,
            sealed_candidate(fixture),
        )


def test_snapshot_rejects_exec_approvals_inspector_candidate_drift(
    fixture: Fixture,
):
    manifest = json.loads(fixture.snapshot.read_text())
    manifest["execApprovalsInspector"]["candidateSealSha256"] = "0" * 64
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_snapshot_rejects_retired_schema_v1(fixture: Fixture):
    manifest = json.loads(fixture.snapshot.read_text())
    fixture.snapshot.chmod(0o600)
    fixture.snapshot.write_text(
        json.dumps(
            {**manifest, "schemaVersion": 1}, indent=2, sort_keys=True
        )
        + "\n"
    )
    fixture.snapshot.chmod(0o400)

    with pytest.raises(activate_module.ActivationError, match="binding drift"):
        activate_module.load_stopped_snapshot(
            fixture.paths, fixture.snapshot, sealed_candidate(fixture)
        )


def test_system_exec_approvals_inspector_uses_exact_candidate_and_private_paths(
    monkeypatch, fixture: Fixture
):
    calls: list[tuple[tuple[str, ...], Path | None, dict[str, str] | None]] = []
    monkeypatch.setenv(
        "OPENCLAW_UNRELATED_PRIVATE_VALUE",
        "ambient-private-value-must-not-reach-inspector",
    )

    def fake_run(argv, _timeout, *, cwd=None, env=None):
        calls.append((tuple(argv), cwd, env))
        return command_result(
            argv,
            stdout=json.dumps(approval_report(), separators=(",", ":")).encode(),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(
        fixture.paths, fixture.legacy_exec_approvals_path
    )

    receipt = backend.inspect_exec_approvals(fixture.candidate)

    assert receipt == approval_report()
    assert len(calls) == 1
    argv, cwd, environment = calls[0]
    assert argv[:3] == (
        str(fixture.paths.node),
        "--input-type=module",
        "--eval",
    )
    assert cwd == fixture.candidate
    assert environment is not None
    assert environment["OPENCLAW_STATE_DIR"] == str(
        fixture.paths.candidate_state_dir
    )
    assert environment["OPENCLAW_APPROVALS_INSPECTOR"] == str(
        fixture.candidate / "dist" / "infra" / "exec-approvals-inspection.js"
    )
    assert environment["OPENCLAW_LEGACY_EXEC_APPROVALS_PATH"] == str(
        fixture.legacy_exec_approvals_path
    )
    assert set(environment) == {
        "OPENCLAW_STATE_DIR",
        "OPENCLAW_APPROVALS_INSPECTOR",
        "OPENCLAW_LEGACY_EXEC_APPROVALS_PATH",
    }
    assert "OPENCLAW_UNRELATED_PRIVATE_VALUE" not in environment
    assert str(fixture.legacy_exec_approvals_path) not in "\0".join(argv)
    assert backend.command_evidence[0]["purpose"] == "exec_approvals_inspection"


@pytest.mark.parametrize(
    "failure",
    [
        "nonzero",
        "timeout",
        "stderr",
        "malformed",
        "value-bearing",
        "raw-order",
    ],
)
def test_system_exec_approvals_inspector_fails_value_free(
    monkeypatch, fixture: Fixture, failure: str
):
    private = b"private-token-value-must-not-escape"

    def fake_run(argv, _timeout, *, cwd=None, env=None):
        assert cwd == fixture.candidate
        assert env is not None
        if failure == "nonzero":
            return command_result(argv, returncode=2, stderr=private)
        if failure == "timeout":
            return command_result(
                argv, returncode=-15, stderr=private, timed_out=True
            )
        if failure == "stderr":
            return command_result(
                argv,
                stdout=json.dumps(approval_report()).encode(),
                stderr=private,
            )
        if failure == "malformed":
            return command_result(argv, stdout=private)
        report = approval_report()
        if failure == "raw-order":
            report["openAIProfileOrder"] = list(SENSITIVE_TEST_PROFILE_IDS)
        else:
            report["socketToken"] = private.decode()
        return command_result(argv, stdout=json.dumps(report).encode())

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(
        fixture.paths, fixture.legacy_exec_approvals_path
    )

    with pytest.raises(activate_module.ActivationError) as caught:
        backend.inspect_exec_approvals(fixture.candidate)

    assert private.decode() not in str(caught.value)
    assert private.decode() not in json.dumps(backend.command_evidence)
    assert all(
        profile_id not in str(caught.value)
        and profile_id not in json.dumps(backend.command_evidence)
        for profile_id in SENSITIVE_TEST_PROFILE_IDS
    )


def test_system_candidate_discord_verifies_bundled_loader_without_mutation(
    monkeypatch, fixture: Fixture
):
    expected_source = fixture.candidate / "dist" / "extensions" / "discord" / "index.js"
    receipt = {
        "discordInstallRecordAbsent": True,
        "installedIds": ["whatsapp"],
        "plugin": {
            "id": "discord",
            "origin": "bundled",
            "source": str(expected_source),
            "status": "loaded",
            "enabled": True,
            "error": None,
            "channelIds": ["discord"],
        },
    }
    captured = {}

    def fake_run(argv, _timeout, *, cwd=None, env=None):
        captured.update({"argv": argv, "cwd": cwd, "env": env})
        return command_result(argv, stdout=json.dumps(receipt).encode())

    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", "/private/tmp/poisoned-openclaw.json")
    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)

    result = backend.verify_candidate_discord(
        fixture.candidate, fixture.paths.candidate_state_dir / "openclaw.json"
    )

    assert result == {
        "installRecordAbsent": True,
        "installedIds": ["whatsapp"],
        "configSha256": hashlib.sha256(fixture.config_bytes).hexdigest(),
        "origin": "bundled",
        "source": str(expected_source),
    }
    assert captured["argv"][:2] == (str(fixture.paths.node), "--input-type=module")
    assert captured["argv"][2] == "--eval"
    child_source = captured["argv"][3]
    assert "dist/config/config.js" in child_source
    assert "dist/plugins/loader.js" in child_source
    assert "loadPluginRegistryHandle" in child_source
    assert "commitPluginInstallRecordsOnly" not in child_source
    assert "removePluginInstallRecordFromRecords" not in child_source
    assert 'moduleAt("src/' not in child_source
    assert "tsx" not in child_source
    assert captured["cwd"] == fixture.candidate
    assert captured["env"]["OPENCLAW_STATE_DIR"] == str(fixture.paths.candidate_state_dir)
    assert captured["env"]["OPENCLAW_CONFIG_PATH"] == str(
        fixture.paths.candidate_state_dir / "openclaw.json"
    )
    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes


@pytest.mark.parametrize("drift", ["source", "status"])
def test_system_candidate_discord_rejects_noncanonical_bundled_selection(
    monkeypatch, fixture: Fixture, drift: str
):
    expected_source = fixture.candidate / "dist" / "extensions" / "discord" / "index.js"
    plugin = {
        "id": "discord",
        "origin": "bundled",
        "source": str(expected_source),
        "status": "loaded",
        "enabled": True,
        "error": None,
        "channelIds": ["discord"],
    }
    if drift == "source":
        plugin["source"] = str(fixture.root / "external-discord" / "index.js")
    else:
        plugin["status"] = "failed"
    receipt = {
        "discordInstallRecordAbsent": True,
        "installedIds": ["whatsapp"],
        "plugin": plugin,
    }

    def fake_run(argv, _timeout, *, cwd=None, env=None):
        return command_result(argv, stdout=json.dumps(receipt).encode())

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="source selection drift"):
        backend.verify_candidate_discord(
            fixture.candidate, fixture.paths.candidate_state_dir / "openclaw.json"
        )

    assert (fixture.paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes


@pytest.mark.parametrize(
    ("module_suffix", "facade_drift"),
    [(".js", None), (".mjs", None), (".js", "duplicate"), (".mjs", "wrong-type")],
)
def test_system_candidate_discord_executes_read_only_facades_with_real_node(
    fixture: Fixture, module_suffix: str, facade_drift: str | None
):
    pinned_node = activate_module.OPERATOR.require_path("paths.node_binary")
    node = pinned_node if pinned_node.is_file() else Path(shutil.which("node") or "")
    if not node.is_file():
        pytest.skip("real Node executable unavailable")

    chmod_tree(fixture.candidate, True)
    package_path = fixture.candidate / "package.json"
    package = json.loads(package_path.read_text())
    package["type"] = "module"
    package_path.write_text(json.dumps(package) + "\n")
    config_module = fixture.candidate / "dist" / "config" / "config.js"
    config_module.parent.mkdir(parents=True)
    config_module.write_text(
        'import fs from "node:fs";\n'
        "export function loadConfig() {\n"
        '  return JSON.parse(fs.readFileSync(process.env.OPENCLAW_CONFIG_PATH, "utf8"));\n'
        "}\n"
    )
    records_module = (
        fixture.candidate / "dist" / f"installed-plugin-index-records-test{module_suffix}"
    )
    records_module.write_text(
        'import fs from "node:fs";\n'
        'const path = `${process.env.OPENCLAW_STATE_DIR}/installed-records.json`;\n'
        "export async function loadInstalledPluginIndexInstallRecords() {\n"
        '  return JSON.parse(fs.readFileSync(path, "utf8"));\n'
        "}\n"
    )
    # Split bundles can include an internal chunk beside the public facade.
    internal_module = fixture.candidate / "dist" / "installed-plugin-index-records-internal.mjs"
    internal_module.write_text("export function internalReader() {}\n")
    if facade_drift == "duplicate":
        internal_module.write_bytes(records_module.read_bytes())
    elif facade_drift == "wrong-type":
        records_module.write_text("export const loadInstalledPluginIndexInstallRecords = {};\n")
    loader_module = fixture.candidate / "dist" / "plugins" / "loader.js"
    loader_module.parent.mkdir(parents=True, exist_ok=True)
    loader_module.write_text(
        "export function loadPluginRegistryHandle() {\n"
        '  const source = `${process.env.OPENCLAW_CANDIDATE_ROOT}/dist/extensions/discord/index.js`;\n'
        "  return { diagnostics: [], plugins: [{ id: 'discord', origin: 'bundled', source,\n"
        "    status: 'loaded', enabled: true, error: null, channelIds: ['discord'] }] };\n"
        "}\n"
    )
    records = {"whatsapp": {"source": "npm", "installPath": "/managed/whatsapp"}}
    records_path = fixture.paths.candidate_state_dir / "installed-records.json"
    records_bytes = (json.dumps(records) + "\n").encode()
    records_path.write_bytes(records_bytes)
    paths = replace(fixture.paths, node=node.resolve(strict=True))
    decoy = fixture.root / "decoy.json"
    decoy.write_text('{"plugins":{"entries":{"discord":{"enabled":false}}}}\n')
    original_config_path = os.environ.get("OPENCLAW_CONFIG_PATH")
    os.environ["OPENCLAW_CONFIG_PATH"] = str(decoy)
    try:
        backend = activate_module.SystemBackend(paths)
        if facade_drift:
            with pytest.raises(
                activate_module.ActivationError, match="candidate Discord verification failed"
            ):
                backend.verify_candidate_discord(
                    fixture.candidate, paths.candidate_state_dir / "openclaw.json"
                )
        else:
            receipt = backend.verify_candidate_discord(
                fixture.candidate, paths.candidate_state_dir / "openclaw.json"
            )
            assert receipt["installRecordAbsent"] is True
            assert receipt["installedIds"] == ["whatsapp"]
    finally:
        if original_config_path is None:
            os.environ.pop("OPENCLAW_CONFIG_PATH", None)
        else:
            os.environ["OPENCLAW_CONFIG_PATH"] = original_config_path

    assert records_path.read_bytes() == records_bytes
    assert (paths.candidate_state_dir / "openclaw.json").read_bytes() == fixture.config_bytes


def test_system_stopped_check_requires_exact_launchctl_service_not_found(monkeypatch, fixture: Fixture):
    seen = []

    def fake_run(argv, _timeout):
        if argv[0] == "/usr/sbin/lsof":
            return command_result(argv, 1)
        label = argv[-1].rsplit("/", 1)[-1]
        seen.append(label)
        stderr = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{fixture.paths.operator_uid}\n"
        ).encode()
        return command_result(argv, 113, stderr=stderr)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.assert_gateway_and_node_stopped()

    assert seen == [activate_module.OPERATOR.require_string('runtime.gateway_label'), activate_module.OPERATOR.require_string('runtime.node_label')]
    assert [item["purpose"] for item in backend.command_evidence] == [
        f"{activate_module.OPERATOR.require_string('runtime.gateway_label')}_launchctl_print",
        f"{activate_module.OPERATOR.require_string('runtime.node_label')}_launchctl_print",
        "gateway_listener_absence",
    ]


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [(1, b"Could not find service\n"), (113, b"permission denied\n"), (0, b"")],
)
def test_system_stopped_check_rejects_ambiguous_nonzero_or_loaded_service(
    monkeypatch, fixture: Fixture, returncode: int, stderr: bytes
):
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(argv, returncode, stderr=stderr),
    )
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="exact stopped-state proof"):
        backend.assert_gateway_and_node_stopped()


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [(0, b"p999\nf9\nn127.0.0.1:18789\n", b""),
     (1, b"unexpected\n", b""), (2, b"", b"tool failure\n")],
)
def test_system_stopped_check_rejects_listener_or_ambiguous_lsof(
    monkeypatch, fixture: Fixture, returncode: int, stdout: bytes, stderr: bytes
):
    def fake_run(argv, _timeout):
        if argv[0] == "/usr/sbin/lsof":
            return command_result(argv, returncode, stdout=stdout, stderr=stderr)
        label = argv[-1].rsplit("/", 1)[-1]
        missing = (
            "Bad request.\n"
            f'Could not find service "{label}" in domain for user gui: '
            f"{fixture.paths.operator_uid}\n"
        ).encode()
        return command_result(argv, 113, stderr=missing)

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(activate_module.ActivationError, match="listener exact stopped-state"):
        backend.assert_gateway_and_node_stopped()


def verify_system_gateway_release(
    monkeypatch,
    fixture: Fixture,
    release: Path,
    gateway_pid: int,
) -> tuple[dict[str, object], dict[str, bytes]]:
    activate_module.atomic_symlink(fixture.paths.current_link, release)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture, activate_module.OPERATOR.require_string('runtime.gateway_label'), pid=gateway_pid
                ),
            )
        if "-iTCP:18789" in argv:
            return command_result(
                argv, stdout=f"p{gateway_pid}\nf9\nn127.0.0.1:18789\n".encode()
            )
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                gateway_pid,
            ),
        )

    bodies = {
        "/healthz": b'{"ok":true,"status":"live"}',
        "/readyz": b'{"ready":true,"failing":[],"uptimeMs":1}',
    }

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def read(self, _maximum):
            return self.payload

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.path = None

        def request(self, _method, path):
            self.path = path

        def getresponse(self):
            return Response(bodies[self.path])

        def close(self):
            pass

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(activate_module.http.client, "HTTPConnection", Connection)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()

    def identity(pid):
        assert pid == gateway_pid
        return (
            start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.gateway_label')),
            fixture.paths.node.resolve(strict=True),
            1,
        )

    monkeypatch.setattr(activate_module, "process_identity", identity)
    result = backend.verify(
        release,
        os.lstat(release).st_dev,
        os.lstat(release).st_ino,
    )
    return result, bodies


def test_system_gateway_verification_binds_service_listener_generation_and_json(
    monkeypatch, fixture: Fixture
):
    gateway_pid = 100
    result, bodies = verify_system_gateway_release(
        monkeypatch,
        fixture,
        fixture.candidate,
        gateway_pid,
    )

    assert result["loaded"]["pid"] == gateway_pid
    assert "supervisorPid" not in result["loaded"]
    assert "directSupervisorChild" not in result["loaded"]
    assert result["loaded"]["directLaunchdOwner"] is True
    assert result["loaded"]["listenerPidMatchesLaunchdPid"] is True
    assert result["loaded"]["executable"] == str(fixture.paths.node.resolve(strict=True))
    assert result["loaded"]["argumentsObservedExact"] is True
    working_directory = activate_module.expected_gateway_working_directory(fixture.paths)
    working_directory_info = os.lstat(working_directory)
    assert result["loaded"]["workingDirectory"] == str(working_directory)
    assert result["loaded"]["workingDirectoryDevice"] == working_directory_info.st_dev
    assert result["loaded"]["workingDirectoryInode"] == working_directory_info.st_ino
    assert result["loaded"]["bootstrapBinding"]["release"] == str(fixture.candidate)
    assert result["health"]["healthz"]["bodySha256"] == hashlib.sha256(bodies["/healthz"]).hexdigest()
    assert result["health"]["readyz"]["bodySha256"] == hashlib.sha256(bodies["/readyz"]).hexdigest()


def test_system_restore_verifier_accepts_snapshot_bound_direct_predecessor(
    monkeypatch, fixture: Fixture
):
    gateway_pid = 101
    result, bodies = verify_system_gateway_release(
        monkeypatch,
        fixture,
        fixture.predecessor,
        gateway_pid,
    )

    assert os.readlink(fixture.paths.current_link) == str(fixture.predecessor)
    assert result["loaded"]["release"] == str(fixture.predecessor)
    assert result["loaded"]["pid"] == gateway_pid
    assert result["loaded"]["directLaunchdOwner"] is True
    assert result["loaded"]["listenerPidMatchesLaunchdPid"] is True
    assert result["loaded"]["executable"] == str(fixture.paths.node.resolve(strict=True))
    assert result["health"]["healthz"]["bodySha256"] == hashlib.sha256(
        bodies["/healthz"]
    ).hexdigest()
    assert result["health"]["readyz"]["bodySha256"] == hashlib.sha256(
        bodies["/readyz"]
    ).hexdigest()


def test_system_node_verification_binds_entrypoint_executable_and_generation(
    monkeypatch, fixture: Fixture
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)
    node_pid = 300
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(
            argv,
            stdout=launchctl_payload(
                fixture, activate_module.OPERATOR.require_string('runtime.node_label'), pid=node_pid, runs=2
            ),
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_node_once()
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda pid: (
            start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.node_label')),
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    info = os.lstat(fixture.candidate)

    result = backend.verify_node(fixture.candidate, info.st_dev, info.st_ino)

    assert result["pid"] == node_pid
    assert result["runs"] == 2
    assert result["startToken"] == start_token_after_bootstrap(
        backend,
        activate_module.OPERATOR.require_string('runtime.node_label'),
    )
    assert result["executable"] == str(fixture.paths.node.resolve(strict=True))
    assert result["argumentsObservedExact"] is True


def test_system_health_rejects_arbitrary_http_200_json(monkeypatch, fixture: Fixture):
    class Response:
        status = 200

        def read(self, _maximum):
            return b'{"status":"fine"}'

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, _method, _path):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(activate_module.http.client, "HTTPConnection", Connection)
    backend = activate_module.SystemBackend(fixture.paths)
    assert backend._probe("/healthz")["accepted"] is False
    assert backend._probe("/readyz")["accepted"] is False


def test_system_loaded_service_rejects_argv_or_definition_path_drift(monkeypatch, fixture: Fixture):
    persisted = plistlib.loads(fixture.paths.gateway_plist.read_bytes())["ProgramArguments"]
    drifted = [*persisted, "--unexpected"]

    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(
            argv,
            stdout=launchctl_payload(
                fixture,
                activate_module.OPERATOR.require_string('runtime.gateway_label'),
                arguments=drifted,
                service_path=fixture.root / "wrong.plist",
            ),
        ),
    )
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: ("generation", fixture.paths.node.resolve(strict=True), 1),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError, match="does not match its plist"):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


@pytest.mark.parametrize("working_directory", ["missing", "resolved-release"])
def test_system_loaded_gateway_requires_operator_home_working_directory(
    monkeypatch,
    fixture: Fixture,
    working_directory: str,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)
    payload = launchctl_payload(
        fixture,
        activate_module.OPERATOR.require_string('runtime.gateway_label'),
        working_directory=fixture.candidate,
    )
    if working_directory == "missing":
        payload = payload.replace(
            f"\tworking directory = {fixture.candidate}\n".encode(),
            b"",
        )

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(argv, stdout=payload)
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                100,
            ),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            "gateway-generation",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(activate_module.ActivationError):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_loaded_gateway_rejects_process_cwd_outside_operator_home(
    monkeypatch,
    fixture: Fixture,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture,
                    activate_module.OPERATOR.require_string('runtime.gateway_label'),
                ),
            )
        return command_result(argv, stdout=cwd_payload(fixture.candidate, 100))

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            "gateway-generation",
            fixture.paths.node.resolve(strict=True),
            1,
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)

    with pytest.raises(
        activate_module.ActivationError,
        match="gateway working directory identity drift",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_loaded_gateway_rejects_non_pinned_executable(
    monkeypatch,
    fixture: Fixture,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture,
                    activate_module.OPERATOR.require_string('runtime.gateway_label'),
                ),
            )
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                100,
            ),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (
            start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.gateway_label')),
            Path("/usr/bin/python3").resolve(strict=True),
            1,
        ),
    )

    with pytest.raises(
        activate_module.ActivationError,
        match="node service process identity drift",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_loaded_gateway_rejects_selector_retarget_with_stable_pid(
    monkeypatch,
    fixture: Fixture,
):
    activate_module.atomic_symlink(fixture.paths.current_link, fixture.predecessor)

    def fake_run(argv, _timeout):
        if argv[:2] == ("/bin/launchctl", "print"):
            return command_result(
                argv,
                stdout=launchctl_payload(
                    fixture,
                    activate_module.OPERATOR.require_string('runtime.gateway_label'),
                ),
            )
        return command_result(
            argv,
            stdout=cwd_payload(
                activate_module.expected_gateway_working_directory(fixture.paths),
                100,
            ),
        )

    monkeypatch.setattr(activate_module, "run_bounded", fake_run)
    backend = activate_module.SystemBackend(fixture.paths)
    backend.bootstrap_gateway_once()
    start_token = start_token_after_bootstrap(backend, activate_module.OPERATOR.require_string('runtime.gateway_label'))
    monkeypatch.setattr(
        activate_module,
        "process_identity",
        lambda _pid: (start_token, fixture.paths.node.resolve(strict=True), 1),
    )

    activate_module.atomic_symlink(fixture.paths.current_link, fixture.candidate)

    with pytest.raises(
        activate_module.ActivationError,
        match="loaded ai.openclaw.gateway bootstrap selector changed",
    ):
        backend._capture_loaded(
            activate_module.OPERATOR.require_string('runtime.gateway_label'),
            fixture.candidate,
            os.lstat(fixture.candidate).st_dev,
            os.lstat(fixture.candidate).st_ino,
        )


def test_system_listener_rejects_pid_distinct_from_launchd_owner(
    monkeypatch, fixture: Fixture
):
    listener_pid = 200
    monkeypatch.setattr(
        activate_module,
        "run_bounded",
        lambda argv, _timeout: command_result(
            argv, stdout=f"p{listener_pid}\nf9\nn127.0.0.1:18789\n".encode()
        ),
    )
    backend = activate_module.SystemBackend(fixture.paths)
    with pytest.raises(
        activate_module.ActivationError,
        match="listener PID does not match launchd PID",
    ):
        backend._listener(100)


def test_system_gateway_verification_reacquires_replaced_launchd_generation(
    monkeypatch, fixture: Fixture
):
    backend = activate_module.SystemBackend(fixture.paths)
    first = {
        "release": str(fixture.candidate),
        "releaseDevice": 1,
        "releaseInode": 2,
        "pid": 100,
        "runs": 1,
        "startToken": "gateway-one",
        "executable": str(fixture.paths.node),
        "servicePath": str(fixture.paths.gateway_plist),
        "argumentsObservedExact": True,
        "argumentVectorSha256": "a" * 64,
        "bootstrapBinding": {
            "release": str(fixture.candidate),
            "releaseDevice": 1,
            "releaseInode": 2,
            "selectorDevice": 3,
            "selectorInode": 4,
            "startedAtUs": 5,
        },
    }
    second = {
        **first,
        "pid": 101,
        "runs": 2,
        "startToken": "gateway-two",
    }
    loaded = iter([first, second, second])
    monkeypatch.setattr(backend, "_loaded", lambda *_args: dict(next(loaded)))

    def listener(launchd_pid):
        if launchd_pid == 100:
            raise activate_module.ActivationError(
                "gateway listener PID does not match launchd PID"
            )
        return launchd_pid

    monkeypatch.setattr(
        backend,
        "_listener",
        listener,
    )
    monkeypatch.setattr(
        backend,
        "_wait_health",
        lambda _deadline: {"healthz": {"accepted": True}, "readyz": {"accepted": True}},
    )

    result = backend.verify(fixture.candidate, 1, 2)

    assert result["loaded"]["pid"] == 101
    assert result["loaded"]["runs"] == 2


def test_system_gateway_verification_rejects_generation_that_never_stabilizes(
    monkeypatch, fixture: Fixture
):
    backend = activate_module.SystemBackend(
        replace(fixture.paths, health_timeout_seconds=0.5)
    )
    loaded = {
        "release": str(fixture.candidate),
        "releaseDevice": 1,
        "releaseInode": 2,
        "pid": 100,
        "runs": 1,
        "startToken": "gateway",
        "executable": str(fixture.paths.node),
        "servicePath": str(fixture.paths.gateway_plist),
        "argumentsObservedExact": True,
        "argumentVectorSha256": "a" * 64,
        "bootstrapBinding": {
            "release": str(fixture.candidate),
            "releaseDevice": 1,
            "releaseInode": 2,
            "selectorDevice": 3,
            "selectorInode": 4,
            "startedAtUs": 5,
        },
    }
    monotonic = iter([0.0, 1.0])
    generations = iter(
        [
            dict(loaded),
            {**loaded, "startToken": "replacement-gateway"},
        ]
    )
    monkeypatch.setattr(backend, "_loaded", lambda *_args: next(generations))
    monkeypatch.setattr(backend, "_listener", lambda launchd_pid: launchd_pid)
    monkeypatch.setattr(
        backend,
        "_wait_health",
        lambda _deadline: {"healthz": {"accepted": True}, "readyz": {"accepted": True}},
    )
    monkeypatch.setattr(activate_module.time, "monotonic", lambda: next(monotonic))

    with pytest.raises(activate_module.ActivationError, match="stable gateway generation"):
        backend.verify(fixture.candidate, 1, 2)


def test_daily_retention_rejects_invalid_terminal_without_starting_children(fixture, monkeypatch):
    from scripts import openclaw_retention_cleanup_cron as cron
    from scripts import openclaw_runtime_activate as live_owner
    run(fixture)
    result = json.loads(fixture.paths.result.read_text())
    result["outcome"] = "snapshot_restore_required"
    fixture.paths.result.chmod(0o600)
    fixture.paths.result.write_text(json.dumps(result))
    fixture.paths.result.chmod(0o400)
    monkeypatch.setattr(live_owner, "live_paths", lambda: fixture.paths)
    calls = []
    monkeypatch.setattr(cron, "run_child", lambda *args, **kwargs: calls.append(args))
    results = cron.run_steps(apply=True)
    assert len(results) == 4 and all(row.returncode == 1 for row in results)
    assert all("activation retirement blocked" in row.stderr for row in results)
    assert calls == [] and fixture.paths.result.exists()
