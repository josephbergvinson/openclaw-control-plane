#!/usr/bin/env python3
"""Historical v3 fixture proof, not current native weekly backup acceptance."""
from __future__ import annotations
try:
    from .operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
from typing import Any, Callable


JOB_ID = OPERATOR.require_string('scheduler.local_backup_job_id')
WEEKLY_SCHEDULE = "15 5 * * 0"
OUTER_TIMEOUT_SECONDS = 7200
CHILD_TIMEOUT_SECONDS = 6900
YIELD_MILLISECONDS = 120000
CANONICAL_ROOT = Path((str(OPERATOR.require_path('paths.workspace'))))
PROCESS_RECEIPT_SCHEMA = "openclaw.cron_python_entrypoint.process_receipt.v2"
BACKUP_RECEIPT_SCHEMA = "openclaw.weekly_archive_backup.phase_receipt.v3"
BACKUP_MANIFEST_SCHEMA = "openclaw.owc_weekly_backup.manifest.v3"
PROOF_SCHEMA = "openclaw.weekly_backup.integration_proof.v3"
PAYLOAD_CONTRACT = "three-gzip-tarballs-v1"
EXPECTED_ARCHIVE_NAMES = (
    "openclaw-state.tgz",
    "openclaw-workspace-policy.tgz",
    "git-remotes.tgz",
)
POLICY_FILES = (
    "AGENTS.md",
    "TOOLS.md",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "POLICY_CHANGELOG.md",
)
LEGACY_BACKUP_ENV_KEYS = {
    "OPENCLAW_BACKUP_ANNOUNCE_SUCCESS",
    "OPENCLAW_BACKUP_RECEIPT_DIR",
    "OPENCLAW_BACKUP_FORCE_OFFLOAD",
    "OPENCLAW_OWC_ROOT",
    "OPENCLAW_OWC_VOLUME_MOUNT",
    "OPENCLAW_OWC_VOLUME_UUID",
    "OPENCLAW_OWC_DEVICE",
    "OPENCLAW_WEEKLY_BACKUP_ROOT",
    "OPENCLAW_WEEKLY_RETENTION_COUNT",
    "ICLOUD_OFFLOAD_CADENCE_DAYS",
    "ICLOUD_OFFLOAD_RETENTION_COUNT",
    "ICLOUD_BACKUPS_ROOT",
    "LOCAL_RETENTION_COUNT",
}


class ProofError(RuntimeError):
    pass


def write_real_sqlite_fixture(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    try:
        database.execute("PRAGMA user_version = 1")
        database.execute(
            "CREATE TABLE fixture_state (label TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO fixture_state(label) VALUES (?)",
            (label,),
        )
        database.commit()
    finally:
        database.close()


def write_sqlite_fixture_cli(path: Path) -> None:
    """Write a deterministic stock-shaped create/verify fixture CLI."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import hashlib
            import json
            import os
            from pathlib import Path
            import sqlite3
            import sys

            def emit(payload):
                print(json.dumps(payload, separators=(",", ":"), sort_keys=True))

            def file_hash(path):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block:
                            return digest.hexdigest()
                        digest.update(block)

            args = sys.argv[1:]
            if args == ["agents", "list", "--json"]:
                emit([{"id": "main"}])
                raise SystemExit(0)

            if args[:3] == ["backup", "sqlite", "create"]:
                repository = Path(args[args.index("--repository") + 1])
                repository.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(repository, 0o700)
                state_root = Path(os.environ["OPENCLAW_STATE_DIR"])
                if "--global" in args:
                    role = "global"
                    agent_id = None
                    basename = "openclaw.sqlite"
                    snapshot_id = "fixture-global"
                    source = state_root / "state/openclaw.sqlite"
                else:
                    role = "agent"
                    agent_id = args[args.index("--agent") + 1]
                    basename = "openclaw-agent.sqlite"
                    snapshot_id = "fixture-agent-{}".format(agent_id)
                    source = (
                        state_root / "agents" / agent_id
                        / "agent/openclaw-agent.sqlite"
                    )
                snapshot = repository / snapshot_id
                snapshot.mkdir(mode=0o700)
                artifact = snapshot / "database.sqlite"
                source_database = sqlite3.connect(
                    "file:{}?mode=ro".format(source), uri=True
                )
                target_database = sqlite3.connect(artifact)
                try:
                    source_database.backup(target_database)
                    target_database.commit()
                finally:
                    target_database.close()
                    source_database.close()
                os.chmod(artifact, 0o600)
                readonly = sqlite3.connect(
                    "file:{}?mode=ro".format(artifact), uri=True
                )
                try:
                    user_version = readonly.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                finally:
                    readonly.close()
                database = {
                    "role": role,
                    "basename": basename,
                    "userVersion": user_version,
                }
                if agent_id is not None:
                    database["agentId"] = agent_id
                manifest = {
                    "schemaVersion": 1,
                    "snapshotId": snapshot_id,
                    "createdAt": "2026-08-26T00:00:00.000Z",
                    "database": database,
                    "artifact": {
                        "path": "database.sqlite",
                        "sha256": file_hash(artifact),
                        "sizeBytes": artifact.stat().st_size,
                    },
                }
                manifest_path = snapshot / "manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, separators=(",", ":"), sort_keys=True)
                    + "\\n",
                    encoding="utf-8",
                )
                os.chmod(manifest_path, 0o600)
                emit({
                    "ok": True,
                    "snapshotPath": str(snapshot),
                    "manifest": manifest,
                })
                raise SystemExit(0)

            if (
                len(args) == 5
                and args[:3] == ["backup", "sqlite", "verify"]
                and args[4] == "--json"
            ):
                snapshot = Path(args[3])
                if {child.name for child in snapshot.iterdir()} != {
                    "manifest.json", "database.sqlite"
                }:
                    raise SystemExit(3)
                manifest_path = snapshot / "manifest.json"
                artifact = snapshot / "database.sqlite"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                expected_artifact = manifest["artifact"]
                if (
                    manifest["snapshotId"] != snapshot.name
                    or expected_artifact["path"] != "database.sqlite"
                    or expected_artifact["sizeBytes"] != artifact.stat().st_size
                    or expected_artifact["sha256"] != file_hash(artifact)
                ):
                    raise SystemExit(4)
                database = sqlite3.connect(
                    "file:{}?mode=ro".format(artifact), uri=True
                )
                try:
                    integrity = database.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0]
                    user_version = database.execute(
                        "PRAGMA user_version"
                    ).fetchone()[0]
                finally:
                    database.close()
                if (
                    integrity != "ok"
                    or user_version != manifest["database"]["userVersion"]
                ):
                    raise SystemExit(5)
                emit({
                    "ok": True,
                    "snapshotPath": str(snapshot),
                    "manifest": manifest,
                })
                raise SystemExit(0)

            raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o755)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_job(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("id") == JOB_ID:
        return payload
    for job in payload.get("jobs", []):
        if job.get("id") == JOB_ID:
            return job
    raise ProofError("weekly backup job {} not found".format(JOB_ID))


def message_value(message: str, label: str) -> str:
    prefix = "- {}: ".format(label)
    for line in message.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise ProofError("cron message is missing {}".format(prefix.strip()))


def optional_message_value(message: str, label: str) -> str | None:
    prefix = "- {}: ".format(label)
    for line in message.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def parse_wrapper_command(command: list[str]) -> dict[str, str]:
    required_wrapper = str(
        CANONICAL_ROOT / "scripts/cron_python_entrypoint.py"
    )
    if command[:2] != ["/usr/bin/python3", required_wrapper]:
        raise ProofError(
            "cron command does not start with the canonical Python wrapper"
        )
    allowed_options = {"--receipt-dir", "--script", "--what", "--cwd"}
    options: dict[str, str] = {}
    index = 2
    while index < len(command):
        option = command[index]
        if option not in allowed_options:
            raise ProofError(
                "cron command has an unsupported wrapper/script argument: "
                + option
            )
        if option in options:
            raise ProofError("cron command repeats wrapper option " + option)
        if index + 1 >= len(command):
            raise ProofError("cron command is missing a value for " + option)
        options[option] = command[index + 1]
        index += 2
    return options


def validate_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") or {}
    message = str(payload.get("message") or "")
    env_text = optional_message_value(message, "env")
    env = {} if env_text is None else json.loads(env_text)
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in env.items()
    ):
        raise ProofError("cron env must be a JSON object of string values")
    command = shlex.split(message_value(message, "command"))
    options = parse_wrapper_command(command)
    required_backup = str(
        CANONICAL_ROOT / "scripts/openclaw_weekly_archive_backup.py"
    )
    child_timeout = int(message_value(message, "timeoutSeconds"))
    yield_milliseconds = int(message_value(message, "yieldMs"))
    no_icloud_or_offload_env = not any(
        fragment in "{}={}".format(key, value).upper()
        for key, value in env.items()
        for fragment in ("ICLOUD", "OFFLOAD")
    )
    checks = {
        "job_id": job.get("id") == JOB_ID,
        "name": job.get("name") == "OpenClaw Weekly Backup",
        "enabled": job.get("enabled") is True,
        "weekly_schedule": job.get("schedule", {}).get("expr") == WEEKLY_SCHEDULE,
        "timezone": job.get("schedule", {}).get("tz") == "Europe/London",
        "isolated_session": job.get("sessionTarget") == "isolated",
        "outer_timeout": payload.get("timeoutSeconds") == OUTER_TIMEOUT_SECONDS,
        "child_timeout": child_timeout == CHILD_TIMEOUT_SECONDS,
        "yield_cap": yield_milliseconds == YIELD_MILLISECONDS,
        "bounded_timeout": (
            0 < child_timeout < OUTER_TIMEOUT_SECONDS
            and 0 < yield_milliseconds <= child_timeout * 1000
        ),
        "no_icloud_or_offload_env": no_icloud_or_offload_env,
        "no_legacy_clone_env": not (set(env) & LEGACY_BACKUP_ENV_KEYS),
        "wrapper_receipt_arg": bool(options.get("--receipt-dir")),
        "backup_path": options.get("--script") == required_backup,
        "no_script_arguments": "--" not in command,
        "process_poll_instruction": "process.poll" in message,
        "single_terminal_instruction": "exactly once" in message,
        "announce_delivery": job.get("delivery", {}).get("mode") == "announce",
        "discord_delivery": job.get("delivery", {}).get("channel") == "discord",
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise ProofError("cron contract failed: " + ", ".join(failed))
    return {
        "checks": checks,
        "env": env,
        "command": command,
        "wrapper_options": options,
        "delivery": job.get("delivery"),
        "outer_timeout_seconds": OUTER_TIMEOUT_SECONDS,
        "child_timeout_seconds": CHILD_TIMEOUT_SECONDS,
        "yield_milliseconds": YIELD_MILLISECONDS,
    }


def replace_source_root(command: list[str], source_root: Path) -> list[str]:
    return [part.replace(str(CANONICAL_ROOT), str(source_root)) for part in command]


def read_single_json(directory: Path, schema: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") == schema:
            matches.append(payload)
    if len(matches) != 1:
        raise ProofError("expected one {} receipt under {}, found {}".format(schema, directory, len(matches)))
    return matches[0]


def process_receipt_is_running(directory: Path) -> bool:
    try:
        receipt = read_single_json(directory, PROCESS_RECEIPT_SCHEMA)
    except (ProofError, OSError, json.JSONDecodeError):
        return False
    child = receipt.get("child") or {}
    supervisor = receipt.get("supervisor") or {}
    child_pid = child.get("pid")
    return (
        receipt.get("receipt_owner") == "supervisor"
        and receipt.get("status") == "running"
        and isinstance(child_pid, int)
        and child_pid > 0
        and child.get("process_group_id") == child_pid
        and isinstance(supervisor.get("pid"), int)
        and supervisor.get("pid") > 0
    )


def create_backup_fixture(root: Path) -> dict[str, Path | str | int]:
    mount = root / "OWC"
    owc = mount / "OpenClaw"
    workspace = owc / "Workspace"
    bare = mount / "ProjectInfrastructure/GitRemotes"
    agents = owc / ".state/OpenClaw/agents"
    sessions = owc / ".state/OpenClaw/Sessions"
    sessions_logical = agents / "main/sessions"
    browser = owc / ".state/OpenClaw/Browser"
    media = owc / ".state/OpenClaw/Media"
    for directory in (workspace, bare, agents, sessions, browser, media):
        directory.mkdir(parents=True)
    sessions_logical.parent.mkdir(parents=True, exist_ok=True)
    sessions_logical.symlink_to(sessions, target_is_directory=True)

    for name in POLICY_FILES:
        (workspace / name).write_text(
            "{} integration fixture\n".format(name),
            encoding="utf-8",
        )
    (workspace / "runbook").mkdir()
    (workspace / "runbook/restore.md").write_text(
        "restore integration fixture\n",
        encoding="utf-8",
    )
    (workspace / "memory").mkdir()
    (workspace / "memory/fact.md").write_text(
        "memory integration fixture\n",
        encoding="utf-8",
    )

    state = root / "home/.openclaw"
    state.mkdir(parents=True)
    (agents / "main/agent").mkdir(parents=True)
    (state / "agents").symlink_to(agents, target_is_directory=True)
    (state / "state").mkdir()
    (state / "cron").mkdir()
    (state / "credentials").mkdir()
    (state / "openclaw.json").write_text(
        '{"integration_fixture":true}\n',
        encoding="utf-8",
    )
    (state / "cron/jobs.json").write_text(
        '{"version":1,"jobs":[]}\n',
        encoding="utf-8",
    )
    (state / "credentials/token.txt").write_text(
        "sensitive integration fixture\n",
        encoding="utf-8",
    )
    for database in (
        state / "state/openclaw.sqlite",
        agents / "main/agent/openclaw-agent.sqlite",
    ):
        write_real_sqlite_fixture(database, database.name)
        Path("{}-wal".format(database)).write_bytes(
            b""
        )
    (state / "node.json.tmp").write_text(
        "excluded volatile fixture\n",
        encoding="utf-8",
    )
    (state / "browser").symlink_to(browser, target_is_directory=True)
    (state / "media").symlink_to(media, target_is_directory=True)
    for shadow in (
        agents / "main/sessions.pre-owc-openclaw-sessions",
        state / "browser.pre-owc-openclaw-browser",
        state / "media.pre-owc-openclaw-media",
    ):
        shadow.mkdir(parents=True)
        (shadow / "stale.txt").write_text(
            "excluded shadow fixture\n",
            encoding="utf-8",
        )
    (sessions / "session.jsonl").write_text(
        '{"session":"integration"}\n',
        encoding="utf-8",
    )
    (browser / "Preferences").write_text(
        '{"browser":"integration"}\n',
        encoding="utf-8",
    )
    (media / "item.txt").write_text(
        "media integration fixture\n",
        encoding="utf-8",
    )

    repository = bare / "OpenClaw/openclaw-workspace.git"
    git = subprocess.run(
        ["/usr/bin/git", "init", "--bare", str(repository)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if git.returncode != 0:
        raise ProofError(
            "fixture bare repository initialization failed: "
            + git.stderr.decode(errors="replace").strip()
        )

    workspace_logical = root / "home/OpenClawWorkspace"
    workspace_logical.symlink_to(workspace, target_is_directory=True)
    git_remotes_logical = root / "home/git-remotes"
    git_remotes_logical.symlink_to(bare, target_is_directory=True)
    fake_cli = root / "bin/openclaw"
    write_sqlite_fixture_cli(fake_cli)
    device = owc.stat().st_dev
    return {
        "owc": owc,
        "mount": mount,
        "backup_root": owc / "Backups/weekly",
        "state": state,
        "workspace_logical": workspace_logical,
        "git_remotes_logical": git_remotes_logical,
        "openclaw_cli": fake_cli,
        "backup_receipts": (
            workspace / "artifacts/openclaw_weekly_archive_backup/receipts"
        ),
        "process_receipts": root / "process-receipts",
        "uuid": "FIXTURE-UUID",
        "device": device,
    }


def write_fixture_bridge(
    path: Path,
    *,
    source_root: Path,
    paths: dict[str, Path | str | int],
) -> None:
    bridge = """\
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

sys.path.insert(0, {source_root!r})
sys.path.insert(0, str(Path({source_root!r}) / "tests"))
from scripts import openclaw_weekly_archive_backup as backup
from openclaw_archive_v3_test_fixture import create_v3_fixture

config = backup.BackupConfig(
    owc_root=Path({owc!r}),
    owc_volume_mount=Path({mount!r}),
    expected_volume_uuid={uuid!r},
    expected_device={device},
    state_root=Path({state!r}),
    workspace_logical=Path({workspace_logical!r}),
    git_remotes_logical=Path({git_remotes_logical!r}),
    backup_root=Path({backup_root!r}),
    receipt_dir=Path({backup_receipts!r}),
    min_post_backup_free_bytes=0,
    openclaw_cli=Path({openclaw_cli!r}),
)
identity = backup.VolumeIdentity(
    uuid={uuid!r},
    device={device},
    mount=Path({mount!r}),
    writable=True,
    owners_enabled=True,
)
time.sleep(0.05)
code, result = create_v3_fixture(
    config,
    identity_reader=lambda _config: identity,
    now=datetime(2026, 7, 25, 18, 0, tzinfo=timezone.utc),
)
if code == 0:
    print("BACKUP_OK")
    print(
        "STATUS | backup: {{}} | result: verified | payloads: 3".format(
            result["backup"]
        )
    )
else:
    print("BACKUP_FAIL")
    print(result)
raise SystemExit(code)
""".format(
        source_root=str(source_root),
        owc=str(paths["owc"]),
        mount=str(paths["mount"]),
        uuid=str(paths["uuid"]),
        device=int(paths["device"]),
        state=str(paths["state"]),
        workspace_logical=str(paths["workspace_logical"]),
        git_remotes_logical=str(paths["git_remotes_logical"]),
        backup_root=str(paths["backup_root"]),
        backup_receipts=str(paths["backup_receipts"]),
        openclaw_cli=str(paths["openclaw_cli"]),
    )
    path.write_text(bridge, encoding="utf-8")


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def execute_with_yield_and_poll(
    command: list[str],
    env: dict[str, str],
    *,
    timeout_seconds: float,
    yield_seconds: float,
    readiness_predicate: Callable[[], bool] | None = None,
    readiness_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    proc = subprocess.Popen(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    yielded = False
    polls = 0
    readiness_observed = readiness_predicate is None
    started = time.monotonic()
    runtime_started = started
    try:
        stdout, stderr = proc.communicate(timeout=yield_seconds)
    except subprocess.TimeoutExpired:
        yielded = True
        if readiness_predicate is not None:
            readiness_deadline = time.monotonic() + readiness_timeout_seconds
            while proc.poll() is None and time.monotonic() < readiness_deadline:
                polls += 1
                if readiness_predicate():
                    readiness_observed = True
                    break
                time.sleep(0.02)
        runtime_started = time.monotonic()
        deadline = runtime_started + timeout_seconds
        while (
            readiness_observed
            and proc.poll() is None
            and time.monotonic() < deadline
        ):
            polls += 1
            time.sleep(0.02)
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=20)
    ended = time.monotonic()
    return {
        "pid": proc.pid,
        "yielded": yielded,
        "process_poll_count": polls,
        "readiness_observed": readiness_observed,
        "startup_duration_seconds": round(runtime_started - started, 6),
        "runtime_duration_seconds": round(ended - runtime_started, 6),
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": round(ended - started, 6),
        "process_group_alive": process_group_exists(proc.pid),
    }


def success_scenario(contract: dict[str, Any], source_root: Path, root: Path) -> dict[str, Any]:
    paths = create_backup_fixture(root)
    command = replace_source_root(contract["command"], source_root)
    receipt_index = command.index("--receipt-dir") + 1
    command[receipt_index] = str(paths["process_receipts"])
    fixture_bridge = root / "archive_v3_fixture_bridge.py"
    write_fixture_bridge(
        fixture_bridge,
        source_root=source_root,
        paths=paths,
    )
    script_index = command.index("--script") + 1
    production_target = command[script_index]
    command[script_index] = str(fixture_bridge)
    env = dict(os.environ)
    env.update(contract["env"])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = execute_with_yield_and_poll(command, env, timeout_seconds=30.0, yield_seconds=0.0001)
    if result["returncode"] != 0:
        raise ProofError("success fixture failed: {} {}".format(result["stdout"], result["stderr"]))
    if not result["yielded"] or result["process_poll_count"] < 1:
        raise ProofError("success fixture did not traverse yield -> process.poll")
    if result["process_group_alive"]:
        raise ProofError("success fixture left a residual wrapper process group")
    process_receipt = read_single_json(Path(paths["process_receipts"]), PROCESS_RECEIPT_SCHEMA)
    backup_receipt = read_single_json(Path(paths["backup_receipts"]), BACKUP_RECEIPT_SCHEMA)
    if (
        process_receipt.get("residual_process", {}).get(
            "process_group_alive_after_cleanup"
        )
        is not False
    ):
        raise ProofError(
            "success fixture process receipt did not prove residual cleanup"
        )
    generations = sorted(
        Path(paths["backup_root"]).glob("openclaw-archive-v3-*")
    )
    if len(generations) != 1:
        raise ProofError("success fixture did not publish exactly one backup")
    manifest = json.loads((generations[0] / "MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != BACKUP_MANIFEST_SCHEMA:
        raise ProofError("success fixture manifest schema mismatch")
    actual_archives = tuple(
        sorted(path.name for path in generations[0].glob("*.tgz"))
    )
    if actual_archives != tuple(sorted(EXPECTED_ARCHIVE_NAMES)):
        raise ProofError("success fixture archive payload set mismatch")
    if manifest.get("archive_names") != list(EXPECTED_ARCHIVE_NAMES):
        raise ProofError("success fixture manifest archive set mismatch")
    sqlite_backup = manifest.get("sqlite_backup")
    if (
        not isinstance(sqlite_backup, dict)
        or sqlite_backup.get("command_contract")
        != "openclaw backup sqlite create --json"
        or sqlite_backup.get("verification_contract")
        != "openclaw backup sqlite verify <snapshot> --json"
        or sqlite_backup.get("agent_ids") != ["main"]
        or sqlite_backup.get("snapshot_count") != 2
        or sqlite_backup.get("result") != "verified"
    ):
        raise ProofError("success fixture SQLite backup contract mismatch")
    state_verification = next(
        (
            row
            for row in manifest.get("verification", [])
            if row.get("name") == "openclaw-state.tgz"
        ),
        None,
    )
    if (
        not isinstance(state_verification, dict)
        or state_verification.get("restore_probes", {})
        .get("sqlite-snapshots", {})
        .get("result")
        != "verified"
        or len(
            state_verification.get(
                "sqlite_snapshot_verification",
                [],
            )
        )
        != 2
    ):
        raise ProofError("success fixture SQLite archive proof mismatch")
    with tarfile.open(
        generations[0] / "openclaw-state.tgz",
        mode="r:gz",
    ) as archive:
        state_names = {member.name for member in archive}
    if any(
        name in state_names
        for name in (
            ".openclaw/state/openclaw.sqlite",
            ".openclaw/state/openclaw.sqlite-wal",
            ".openclaw/agents/main/agent/openclaw-agent.sqlite",
            ".openclaw/agents/main/agent/openclaw-agent.sqlite-wal",
        )
    ):
        raise ProofError("success fixture archived a live SQLite database")
    if not {
        ".openclaw/sqlite-snapshots/fixture-global/database.sqlite",
        ".openclaw/sqlite-snapshots/fixture-agent-main/database.sqlite",
    } <= state_names:
        raise ProofError("success fixture omitted verified SQLite snapshots")
    return {
        "exec_process": result,
        "process_receipt": process_receipt,
        "phase_receipt": backup_receipt,
        "manifest": manifest,
        "generation": str(generations[0]),
        "archive_names": list(actual_archives),
        "sqlite_backup": sqlite_backup,
        "fixture_bridge": {
            "path": str(fixture_bridge),
            "production_target": production_target,
            "injected_backup_config": True,
        },
        "residual_processes": [],
        "result": "verified",
    }


def timeout_scenario(source_root: Path, root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    sleeper = root / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    receipt_dir = root / "process-receipts"
    command = [
        sys.executable,
        str(source_root / "scripts/cron_python_entrypoint.py"),
        "--receipt-dir",
        str(receipt_dir),
        "--script",
        str(sleeper),
        "--what",
        "scaled weekly backup timeout",
    ]
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = execute_with_yield_and_poll(
        command,
        env,
        timeout_seconds=0.2,
        yield_seconds=0.0001,
        readiness_predicate=lambda: process_receipt_is_running(receipt_dir),
    )
    if not result["readiness_observed"]:
        raise ProofError("timeout fixture did not observe a running supervisor receipt")
    if result["returncode"] != 124:
        raise ProofError("timeout fixture expected exit 124, got {}".format(result["returncode"]))
    if result["process_group_alive"]:
        raise ProofError("timeout fixture left a residual wrapper process group")
    receipt = read_single_json(receipt_dir, PROCESS_RECEIPT_SCHEMA)
    if (
        receipt.get("residual_process", {}).get(
            "process_group_alive_after_cleanup"
        )
        is not False
    ):
        raise ProofError(
            "timeout fixture process receipt did not prove residual cleanup"
        )
    return {
        "exec_process": result,
        "process_receipt": receipt,
        "residual_processes": [],
        "result": "verified",
    }


def finalize_proof(proof: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    false_receipts = sorted(
        name for name, passed in proof["receipts"].items() if not passed
    )
    proof["false_receipts"] = false_receipts
    proof["status"] = "blocked" if false_receipts else "passed"
    proof["proof_sha256"] = __import__("hashlib").sha256(
        json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    write_json(output_dir / "integration-proof.json", proof)
    if false_receipts:
        raise ProofError(
            "integration proof receipt set is incomplete: "
            + ", ".join(false_receipts)
        )
    return proof


def run(job_json: Path, source_root: Path, output_dir: Path) -> dict[str, Any]:
    contract = validate_job(load_job(job_json))
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="openclaw-weekly-backup-proof-") as raw:
        fixture_root = Path(raw)
        success = success_scenario(contract, source_root, fixture_root / "success")
        timeout = timeout_scenario(source_root, fixture_root / "timeout")
    proof = {
        "schema_version": PROOF_SCHEMA,
        "generated_at_utc": utc_now(),
        "acceptance_level": "historical_v3_integration_harness",
        "current_native_backup_acceptance": False,
        "job_contract": contract,
        "success_scenario": success,
        "timeout_scenario": timeout,
        "receipts": {
            "job_contract": True,
            "process_completion": success["process_receipt"].get("status") == "completed",
            "backup_phase": success["phase_receipt"].get("status") == "completed",
            "manifest_v3": (
                success["manifest"].get("schema_version")
                == BACKUP_MANIFEST_SCHEMA
            ),
            "three_archive_payload_contract": (
                success["manifest"].get("payload_contract")
                == PAYLOAD_CONTRACT
                and success["manifest"].get("archive_names")
                == list(EXPECTED_ARCHIVE_NAMES)
                and success["archive_names"]
                == sorted(EXPECTED_ARCHIVE_NAMES)
                and len(success["manifest"].get("archives", [])) == 3
            ),
            "full_decompression_and_restore": (
                len(success["manifest"].get("verification", [])) == 3
                and all(
                    row.get("result") == "verified"
                    and row.get("full_decompression") is True
                    for row in success["manifest"].get("verification", [])
                )
            ),
            "no_icloud_offload": success["manifest"].get("icloud_offload") is False,
            "separate_retention_lane": (
                success["manifest"].get("retention", {}).get("performed")
                is False
            ),
            "success_cleanup": (
                success["process_receipt"]
                .get("residual_process", {})
                .get("process_group_alive_after_cleanup")
                is False
                and not success["exec_process"]["process_group_alive"]
            ),
            "timeout_cleanup": (
                timeout["process_receipt"]
                .get("termination", {})
                .get("term_sent")
                is True
                and timeout["process_receipt"]
                .get("residual_process", {})
                .get("process_group_alive_after_cleanup")
                is False
                and not timeout["exec_process"]["process_group_alive"]
            ),
        },
    }
    return finalize_proof(proof, output_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-json", required=True, type=Path)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        proof = run(args.job_json, args.source_root, args.output_dir)
    except (ProofError, OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "blocked", "error": "{}: {}".format(type(exc).__name__, exc)}, sort_keys=True))
        return 1
    print(json.dumps(proof, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
