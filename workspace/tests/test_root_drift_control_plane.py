from __future__ import annotations
try:
    from scripts.operator_contract import load_operator_contract
except ImportError:
    from operator_contract import load_operator_contract
OPERATOR = load_operator_contract()


import json
import subprocess
import sys
import tempfile
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKTREE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKTREE_ROOT))

from scripts.root_drift_state import classify_item, current_state, load_policy, summarize


POLICY_PATH = WORKTREE_ROOT / "control" / "root_drift_policy.json"


def git_init_repo(root: Path) -> None:
    subprocess.run(["/usr/bin/git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(["/usr/bin/git", "config", "user.name", "Test User"], cwd=root, check=True)
    subprocess.run(["/usr/bin/git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    (root / ".gitignore").write_text("", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "add", ".gitignore"], cwd=root, check=True)
    subprocess.run(["/usr/bin/git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True, text=True)


def test_classify_root_drift_reports_machine_readable_state() -> None:
    policy = load_policy(POLICY_PATH)
    item = classify_item({"status": " M", "path": "scripts/openclaw_daily_backup_cron.py"}, Path((str(OPERATOR.require_path('paths.workspace')))), policy)
    assert item["classification"] == "root-local"

    item2 = classify_item({"status": "??", "path": "audit/events.jsonl"}, Path((str(OPERATOR.require_path('paths.workspace')))), policy)
    assert item2["classification"] == "retained-state"


def test_summarize_exposes_classification_buckets() -> None:
    summary = summarize(
        [
            {"path": "scripts/openclaw_daily_backup_cron.py", "status": " M", "classification": "root-local"},
            {"path": "Users/", "status": "??", "classification": "safe-remove"},
            {"path": "audit/events.jsonl", "status": "??", "classification": "retained-state"},
            {"path": ".worktrees/chess960-mode/foo.py", "status": "??", "classification": "blocker"},
        ]
    )
    assert summary["classifications"]["root-local"] == ["scripts/openclaw_daily_backup_cron.py"]
    assert summary["classifications"]["safe-remove"] == ["Users/"]
    assert summary["classifications"]["retained-state"] == ["audit/events.jsonl"]
    assert summary["classifications"]["blocker"] == [".worktrees/chess960-mode/foo.py"]
    assert summary["hasBlockers"] is True






def test_root_drift_state_classifies_mixed_noise_and_retained_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        git_init_repo(root)
        (root / "audit").mkdir(exist_ok=True)
        (root / "audit/events.jsonl").write_text("[]\n", encoding="utf-8")
        (root / "scripts").mkdir(exist_ok=True)
        tracked = root / "scripts/openclaw_daily_backup_cron.py"
        tracked.write_text("#!/bin/sh\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "add", "scripts/openclaw_daily_backup_cron.py"], cwd=root, check=True)
        subprocess.run(["/usr/bin/git", "commit", "-m", "add wrapper"], cwd=root, check=True, capture_output=True, text=True)
        tracked.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

        state = current_state(root, POLICY_PATH)
        assert "scripts/openclaw_daily_backup_cron.py" in state["classifications"]["root-local"]
        assert any(path.startswith("audit") for path in state["classifications"]["retained-state"])


def test_safe_remove_rule_classifies_generated_residue() -> None:
    policy = load_policy(POLICY_PATH)
    item = classify_item({"status": "??", "path": "Users/example/Workspace/README.md"}, Path((str(OPERATOR.require_path('paths.workspace')))), policy)
    assert item["classification"] == "safe-remove"


def test_unmatched_paths_fail_closed_to_blocker() -> None:
    policy = load_policy(POLICY_PATH)
    item = classify_item({"status": "??", "path": ".worktrees/chess960-mode/src/app.py"}, Path((str(OPERATOR.require_path('paths.workspace')))), policy)
    assert item["classification"] == "blocker"


def test_untracked_hooks_path_fails_closed_to_blocker() -> None:
    policy = load_policy(POLICY_PATH)
    item = classify_item({"status": "??", "path": "hooks/unexpected/HOOK.md"}, Path((str(OPERATOR.require_path('paths.workspace')))), policy)
    assert item["classification"] == "blocker"


def test_retired_discord_followup_relay_is_not_a_normalization_target() -> None:
    policy = load_policy(POLICY_PATH)
    normalize = next(
        entry for entry in policy["classes"] if entry["class"] == "normalize-to-root"
    )
    assert "scripts/discord_followup_relay.py" not in normalize["paths"]


def test_installed_documentation_and_local_bindings_keep_distinct_ownership() -> None:
    policy = load_policy(POLICY_PATH)
    root = OPERATOR.require_path('paths.workspace')
    for path in ('REFERENCE.md', '.gitignore', 'docs/17-adoption-guide.md',
                 'runtime/openclaw.patch', 'config/operator.example.json',
                 'scripts/materialize_host.py', 'scripts/reconstruct_runtime.py',
                 'scripts/lib/company_alpha_walletconnect_hedera_adapter.mjs',
                 'dependencies/requirements.txt', 'walletconnect/package-lock.json',
                 'skills/work-product/SKILL.md',
                 'launchd/example.plist.template.json', 'scheduler/maintenance-jobs.json',
                 'host-templates.json'):
        assert classify_item({'status': '??', 'path': path}, root, policy)['classification'] == 'normalize-to-root'
    for path in ('operator.json', 'installation-manifest.json', 'config/openclaw.json',
                 'MEMORY.md', 'memory/current.md', 'artifacts/local-receipt.json',
                 'status/capability_status.json'):
        assert classify_item({'status': '??', 'path': path}, root, policy)['classification'] == 'retained-state'
    assert classify_item({'status': '??', 'path': 'scripts/unreviewed_helper.py'}, root, policy)['classification'] == 'blocker'
