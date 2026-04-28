#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FAILURES=0

fail() {
  printf 'FAIL: %s\n' "$1"
  FAILURES=$((FAILURES + 1))
}

require_file() {
  local rel="$1"
  local path="$ROOT/$rel"
  if [[ ! -f "$path" ]]; then
    fail "missing file: $rel"
  fi
}

require_executable() {
  local rel="$1"
  local path="$ROOT/$rel"
  if [[ ! -x "$path" ]]; then
    fail "missing executable: $rel"
  fi
}

require_heading() {
  local rel="$1"
  local heading="$2"
  local path="$ROOT/$rel"
  if ! /opt/homebrew/bin/rg -F -q "$heading" "$path"; then
    fail "$rel missing heading/marker: $heading"
  fi
}

require_text() {
  local rel="$1"
  local text="$2"
  local path="$ROOT/$rel"
  if ! /opt/homebrew/bin/rg -F -q "$text" "$path"; then
    fail "$rel missing text: $text"
  fi
}

require_absent() {
  local rel="$1"
  local text="$2"
  local path="$ROOT/$rel"
  if /opt/homebrew/bin/rg -F -q "$text" "$path"; then
    fail "$rel still contains retired text: $text"
  fi
}

# Core files
require_file "AGENTS.md"
require_file "TOOLS.md"
require_file "SOUL.md"
require_file "USER.md"
require_file "MEMORY.md"
require_file "IDENTITY.md"
require_file "HEARTBEAT.md"
require_file "BOOTSTRAP.md"
require_file "policy_bootstrap_manifest.json"
require_file "capabilities.md"
require_file "runbook/capabilities.md"
require_file "policy_manifest.json"
require_file "registry/surfaces.json"
require_file "registry/host_profile.openclaw-macmini.json"
require_file "registry/integration_routes.json"
require_file "registry/probes.json"
require_file "status/capability_status.json"
require_file "status/live_bindings.json"
require_file "status/known_issues.json"
require_file "status/heartbeat.json"
require_file "control/delegation_policy.json"
require_file "control/worker_entrypoints.json"
require_file "control/checkpoint_rules.json"
require_file "control/schedulers.json"
require_text "capabilities.md" "Generated; do not edit. Canonical sources: registry/*.json, status/*.json, control/*.json."
require_text "capabilities.md" "## Surface registry snapshot"
require_text "capabilities.md" "## Capability status snapshot"
require_text "HEARTBEAT.md" "Generated; do not edit. Canonical source: status/heartbeat.json."
require_text "INDEX.md" "Generated; do not edit. Landing page for current control-plane views."
require_file "runbook/capabilities-evidence-notes.md"
# Private conditional memory files are intentionally untracked in the public template.
require_file "tests/test_policy_stack_round1_contracts.py"
require_file "tests/test_policy_stack_round2_contracts.py"
require_file "tests/test_policy_stack_round3_contracts.py"
require_executable "scripts/coding_completion_gate.sh"
require_executable "scripts/generate_control_plane_views.py"

# AGENTS structural checks
require_heading "AGENTS.md" "### Task-classification shorthand"
require_heading "AGENTS.md" "## 19) Context retention, compaction, and rollover"
require_heading "AGENTS.md" "## 20) Hot-reload caveat"
require_text "AGENTS.md" "channel:<POLICY_AUDIT_LOG_CHANNEL_ID>"
require_text "AGENTS.md" "operator-visible reporting"
require_text "AGENTS.md" "policy_bootstrap_manifest.json"

# TOOLS structural checks
require_heading "TOOLS.md" "## 14) Completion verification and handoff mechanics"
require_heading "TOOLS.md" "MacBook handoff contract:"
require_heading "TOOLS.md" "Chat-thread delegation route:"
require_text "TOOLS.md" "runtime-generated notices are not a substitute"
require_heading "TOOLS.md" "## 20) Hot-reload and session refresh"

# Generated view / authority checks
require_text "runbook/capabilities.md" "legacy compatibility shim/mirror only"
require_text "runbook/capabilities.md" "non-authoritative"
require_text "INDEX.md" "## Authority shortcut"
require_text "INDEX.md" "control/*.json"
require_text "policy_manifest.json" "\"cutover_state\": \"authority-flipped-in-branch\""
require_text "policy_manifest.json" "\"generated_views_state\": \"generated-non-authoritative\""

if [[ "$FAILURES" -ne 0 ]]; then
  exit 1
fi

printf 'PASS: policy_drift_check\n'
