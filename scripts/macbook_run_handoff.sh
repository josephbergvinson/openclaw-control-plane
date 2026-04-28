#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  scripts/macbook_run_handoff.sh --repo <repo-or-path> --run "<run-command>" [--root <path>] [--verbose]

Description:
  Emits one copy/paste command for running an app from the configured handoff root.
  Set OPENCLAW_HANDOFF_ROOT or pass --root to select your local handoff folder.

Output format:
  cd <handoff-root>/<repo> && <run-command>
EOF
}

ROOT_DEFAULT="${OPENCLAW_HANDOFF_ROOT:-$HOME/OpenClawHandoff}"
ROOT="$ROOT_DEFAULT"
REPO_INPUT=""
RUN_CMD=""
VERBOSE=0
PROJECT_HANDOFF="<PROJECT_HANDOFF>"

fail_project_component_siblings() {
  local canonical="$ROOT/$PROJECT_HANDOFF"
  local siblings=()
  local child

  shopt -s nullglob
  for child in "$ROOT"/"$PROJECT_HANDOFF"*; do
    if [[ ! -e "$child" ]]; then
      continue
    fi
    if [[ "$child" != "$canonical" ]]; then
      siblings+=("$(basename "$child")")
    fi
  done
  shopt -u nullglob

  if (( ${#siblings[@]} > 0 )); then
    printf 'Noncanonical project component handoff siblings present under %s: %s\n' "$ROOT" "${siblings[*]}" >&2
    exit 1
  fi
}

validate_project_component_repo_path() {
  local canonical="$ROOT/$PROJECT_HANDOFF"
  local rel_path="$1"

  if [[ "$REPO_PATH" == "$canonical" ]]; then
    fail_project_component_siblings
    return
  fi

  if [[ "$rel_path" == "$PROJECT_HANDOFF"* || "$rel_path" == */"$PROJECT_HANDOFF"* ]]; then
    printf 'Project component handoff path must be exactly %s\n' "$canonical" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_INPUT="${2:-}"
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
    --verbose)
      VERBOSE=1
      shift
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

if [[ -z "$REPO_INPUT" || -z "$RUN_CMD" ]]; then
  usage >&2
  exit 2
fi

if [[ "$REPO_INPUT" = /* ]]; then
  REPO_PATH="$REPO_INPUT"
else
  REPO_PATH="$ROOT/$REPO_INPUT"
fi

if [[ ! -d "$REPO_PATH" ]]; then
  echo "Repo path not found: $REPO_PATH" >&2
  exit 1
fi

case "$REPO_PATH" in
  "$ROOT"/*) ;;
  "$ROOT") ;;
  *)
    echo "Repo path must be under configured handoff root: $ROOT" >&2
    exit 1
    ;;
esac

REPO_REL="${REPO_PATH#"$ROOT"/}"
if [[ "$REPO_PATH" == "$ROOT" ]]; then
  REPO_REL=""
fi

if [[ $VERBOSE -eq 1 ]]; then
  echo "# repo_path=$REPO_PATH" >&2
fi

validate_project_component_repo_path "$REPO_REL"

if [[ -z "$REPO_REL" ]]; then
  printf 'cd %q && %s\n' "$ROOT" "$RUN_CMD"
  exit 0
fi

printf 'cd %q && %s\n' "$ROOT/$REPO_REL" "$RUN_CMD"
