from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / "templates" / name).read_text()


TEXT_SUFFIXES = {".md", ".py", ".sh", ".json", ".txt", ".gitignore"}


def public_text() -> str:
    parts = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or ".git" in p.parts or ".pytest_cache" in p.parts:
            continue
        if p.suffix.lower() in TEXT_SUFFIXES:
            parts.append(p.read_text(errors="ignore"))
    return "\n".join(parts)


def test_execution_mode_taxonomy_is_present():
    agents = read("AGENTS.md")
    assert "inline" in agents
    assert "durable isolated lane" in agents
    assert "terminal-side/manual containment" in agents


def test_bootstrap_success_is_internal_only():
    bootstrap = read("BOOTSTRAP.md")
    assert "Successful bootstrap/status handling is internal-only" in bootstrap


def test_public_tree_has_expanded_control_plane_components():
    expected = [
        "templates/USER.md",
        "templates/MEMORY.md",
        "templates/IDENTITY.md",
        "templates/capabilities.example.md",
        "templates/policy_bootstrap_manifest.example.json",
        "control/checkpoint_rules.example.json",
        "control/delegation_policy.example.json",
        "control/schedulers.example.json",
        "control/worker_entrypoints.example.json",
        "scripts/discord_coding_slice.py",
        "scripts/slice_closeout_gate.py",
        "docs/deployment.md",
        "docs/control-plane-inventory.md",
        "docs/implementation-guide.md",
        "docs/durable-discord-flow.md",
        "docs/configuration-checklist.md",
        "docs/validation-checklist.md",
        "docs/troubleshooting.md",
        "docs/operator-adaptation-guide.md",
        "scripts/install_template.sh",
        "examples/status-artifact.example.md",
        "examples/final-discord-report.example.md",
    ]
    for rel in expected:
        assert (ROOT / rel).exists(), rel


PLACEHOLDER_PATTERNS = [
    "<AUTHORIZED_DISCORD_USER_ID>",
    "<POLICY_AUDIT_LOG_CHANNEL_ID>",
    "<OPENCLAW_WORKSPACE>",
    "<PROJECT_OR_ORG>",
]

SECRET_PATTERNS = [
    re.compile(r"\bg[h]o_[A-Za-z0-9_]{8,}", re.I),
    re.compile(r"\bn[t]n_[A-Za-z0-9_]{8,}", re.I),
    re.compile(r"\bsk-[A-Za-z0-9]{12,}", re.I),
    re.compile(r"\b[A-Z0-9_]*(?:TOKEN|KEY|PASSWORD)[A-Z0-9_]*\s*=>"),
    re.compile(r"\b\d{17,20}\b"),
    re.compile(r"/Users/(?!\.\.\.|<)[A-Za-z0-9._-]+"),
    re.compile(r"github\.com/(?!example-org(?:/|\b)|<)[A-Za-z0-9._-]+/", re.I),
]


def test_public_tree_does_not_contain_obvious_private_ids_or_secrets():
    combined = public_text()
    for placeholder in PLACEHOLDER_PATTERNS:
        assert placeholder in combined, placeholder
    for pattern in SECRET_PATTERNS:
        assert not pattern.search(combined), pattern.pattern


def test_context_budget_guidance_is_documented():
    config = (ROOT / "docs" / "configuration-checklist.md").read_text()
    implementation = (ROOT / "docs" / "implementation-guide.md").read_text()
    combined = config + "\n" + implementation
    for phrase in [
        "large-context",
        "bootstrap",
        "prompt caching",
        "auto-compaction",
        "checkpoint before",
    ]:
        assert phrase.lower() in combined.lower(), phrase
