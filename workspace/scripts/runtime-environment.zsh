#!/bin/zsh

set -eu
umask 077

: "${HOME:?required account home}"
: "${CLAUDE_CODE_TMPDIR:?required installation binding}"
: "${OPENCLAW_STATE_DIR:?required installation binding}"
: "${PYTEST_DEBUG_TEMPROOT:?required installation binding}"
: "${NODE_COMPILE_CACHE:?required installation binding}"
: "${PYTHONPYCACHEPREFIX:?required installation binding}"
: "${OPENCLAW_RUNTIME_APP:?required installation binding}"

ensure_attach_only_marker() {
    local marker="$1"
    if [[ -L "$marker" || ( -e "$marker" && ! -f "$marker" ) ]]; then
        print -u2 -- "Attach-only marker must be a physical regular file: $marker"
        return 1
    fi
    if [[ ! -e "$marker" ]]; then
        # Never truncate an existing marker, including a concurrent creator's file.
        ( set -C; : > "$marker" ) || [[ -f "$marker" && ! -L "$marker" ]]
    fi
}

# The native app can be opened by Finder before this login owner runs. Install
# this same physical marker before enabling autostart (see the adoption guide),
# and retain it on internal storage when the canonical volume is unavailable.
[[ -d "$HOME" && ! -L "$HOME/.openclaw" ]] || {
    print -u2 -- "The default native profile requires a physical account-home directory"
    exit 1
}
/bin/mkdir -p "$HOME/.openclaw"
ensure_attach_only_marker "$HOME/.openclaw/disable-launchagent"

# The native auth bridge resolves the canonical configuration. A global token
# would override that authority and survive account/configuration rotation.
unset OPENCLAW_GATEWAY_TOKEN
/bin/launchctl unsetenv OPENCLAW_GATEWAY_TOKEN

# Do not create a shadow state directory or launch an app against a missing mount.
[[ -d "$OPENCLAW_STATE_DIR" && -f "$OPENCLAW_STATE_DIR/openclaw.json" ]] || {
    print -u2 -- "Canonical OpenClaw state is unavailable; app startup deferred"
    exit 1
}
ensure_attach_only_marker "$OPENCLAW_STATE_DIR/disable-launchagent"

export OPENCLAW_SUPERVISOR_MODE=external
export OPENCLAW_SERVICE_REPAIR_POLICY=external
export OPENCLAW_CONFIG_PATH="$OPENCLAW_STATE_DIR/openclaw.json"

# Keep the established state and developer cache bindings for later GUI launches.
for oc_env_key in CLAUDE_CODE_TMPDIR OPENCLAW_STATE_DIR OPENCLAW_CONFIG_PATH \
    OPENCLAW_SUPERVISOR_MODE OPENCLAW_SERVICE_REPAIR_POLICY \
    PYTEST_DEBUG_TEMPROOT NODE_COMPILE_CACHE PYTHONPYCACHEPREFIX
do
    /bin/launchctl setenv "$oc_env_key" "${(P)oc_env_key}"
done

# This loader is the sole app login owner. Manual launches are protected by the
# persistent native marker too; the app never owns our external Gateway lifecycle.
exec "$OPENCLAW_RUNTIME_APP" --attach-only
