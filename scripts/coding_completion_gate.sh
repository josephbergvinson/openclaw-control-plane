#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/coding_completion_gate.sh --repo <repo-or-path> --run "<run-command>" [options]

Required:
  --repo <repo-or-path>      Repo folder under canonical handoff root (or absolute path under it)
  --run  "<run-command>"     Command to run the app on MacBook

Optional:
  --root <path>              Override canonical handoff root (useful for tests/debugging)
  --refresh-source <path>    Refresh the handoff copy from this canonical source repo before emitting output
  --commit <sha>             Commit hash to include in report
  --branch <name>            Branch name to include in report
  --tests  "<summary>"       Test summary to include in report
  --emit command|report      Output mode (default: report)

Behavior:
  - Validates repo path exists under canonical handoff root.
  - Optionally refreshes the handoff copy via scripts/macbook_refresh_handoff.py.
  - Emits deterministic MacBook run command via scripts/macbook_run_handoff.sh.
  - In report mode, prints a completion block that includes the required handoff line.

Examples:
  scripts/coding_completion_gate.sh --repo health_dashboard_macos --run "swift run HealthDashboardApp" --emit command
  scripts/coding_completion_gate.sh --repo <PROJECT_HANDOFF> --run "open Example ProjectExampleExporter.xcodeproj" --refresh-source <HOME>/<LOCAL_SOURCE_ROOT>/repos/example-project-project component
  scripts/coding_completion_gate.sh --repo health_dashboard_macos --run "swift run HealthDashboardApp" --commit abc1234 --branch feat/x --tests "swift test -q passed: 28/28"
EOF
}

ROOT="${OPENCLAW_HANDOFF_ROOT:-$HOME/OpenClawHandoff}"
REPO=""
RUN_CMD=""
REFRESH_SOURCE=""
COMMIT_SHA=""
BRANCH=""
TESTS=""
EMIT_MODE="report"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO="${2:-}"
      shift 2
      ;;
    --run)
      RUN_CMD="${2:-}"
      shift 2
      ;;
    --root)
      ROOT="${2:-}"
      shift 2
      ;;
    --refresh-source)
      REFRESH_SOURCE="${2:-}"
      shift 2
      ;;
    --commit)
      COMMIT_SHA="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    --tests)
      TESTS="${2:-}"
      shift 2
      ;;
    --emit)
      EMIT_MODE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$REPO" || -z "$RUN_CMD" ]]; then
  echo "Missing required args: --repo and --run are required." >&2
  usage >&2
  exit 2
fi

case "$EMIT_MODE" in
  command|report) ;;
  *)
    echo "Invalid --emit value: $EMIT_MODE (expected: command|report)" >&2
    exit 2
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HANDOFF_SCRIPT="$SCRIPT_DIR/macbook_run_handoff.sh"
REFRESH_SCRIPT="$SCRIPT_DIR/macbook_refresh_handoff.py"

if [[ ! -x "$HANDOFF_SCRIPT" ]]; then
  echo "Required helper not executable: $HANDOFF_SCRIPT" >&2
  exit 1
fi

REFRESH_STATUS=""
if [[ -n "$REFRESH_SOURCE" ]]; then
  if [[ ! -x "$REFRESH_SCRIPT" ]]; then
    echo "Required helper not executable: $REFRESH_SCRIPT" >&2
    exit 1
  fi
  REFRESH_STATUS="$($REFRESH_SCRIPT --repo "$REPO" --source "$REFRESH_SOURCE" --root "$ROOT")"
fi

HANDOFF_CMD="$($HANDOFF_SCRIPT --repo "$REPO" --run "$RUN_CMD" --root "$ROOT")"

if [[ "$EMIT_MODE" == "command" ]]; then
  printf '%s\n' "$HANDOFF_CMD"
  exit 0
fi

printf 'Done — shipped.\n\n'
if [[ -n "$TESTS" ]]; then
  printf 'Validation:\n- %s\n\n' "$TESTS"
fi
if [[ -n "$REFRESH_SOURCE" ]]; then
  printf 'Handoff:\n- Refreshed from `%s`\n\n' "$REFRESH_SOURCE"
fi
printf 'Git:\n'
if [[ -n "$COMMIT_SHA" ]]; then
  printf '- Commit: `%s`\n' "$COMMIT_SHA"
fi
if [[ -n "$BRANCH" ]]; then
  printf '- Branch: `%s`\n' "$BRANCH"
fi
printf '\nMacBook run command:\n`%s`\n' "$HANDOFF_CMD"
