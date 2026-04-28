#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/install_template.sh --target <OPENCLAW_WORKSPACE> [--apply]

Copies the template control-plane files into an OpenClaw workspace.
Default mode is dry-run. Pass --apply to copy files.

This script does not copy example registries as live files; it places them under
control/ with their .example suffix intact.
USAGE
}

TARGET=""
APPLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  echo "missing --target" >&2
  usage
  exit 2
fi

if [[ ! -d "$TARGET" ]]; then
  echo "target directory does not exist: $TARGET" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILES=(
  AGENTS.md TOOLS.md SOUL.md USER.md MEMORY.md IDENTITY.md BOOTSTRAP.md HEARTBEAT.md
)

echo "Template root: $ROOT"
echo "Target workspace: $TARGET"
if [[ "$APPLY" -eq 0 ]]; then
  echo "Mode: dry-run"
else
  echo "Mode: apply"
fi

for f in "${FILES[@]}"; do
  src="$ROOT/templates/$f"
  dst="$TARGET/$f"
  echo "copy $src -> $dst"
  if [[ "$APPLY" -eq 1 ]]; then
    cp "$src" "$dst"
  fi
done

for dir in control scripts docs; do
  echo "ensure $TARGET/$dir"
  if [[ "$APPLY" -eq 1 ]]; then
    mkdir -p "$TARGET/$dir"
  fi
done

for f in "$ROOT"/control/*.example.json; do
  [[ -e "$f" ]] || continue
  echo "copy $f -> $TARGET/control/$(basename "$f")"
  if [[ "$APPLY" -eq 1 ]]; then
    cp "$f" "$TARGET/control/$(basename "$f")"
  fi
done

echo "Done. Review placeholders before using the control plane."
