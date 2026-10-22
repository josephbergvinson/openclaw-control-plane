#!/bin/zsh

set -eu
umask 077

: "${HOME:?required account home}"
: "${CLAUDE_CODE_TMPDIR:?required installation binding}"
: "${OPENCLAW_STATE_DIR:?required installation binding}"
: "${PYTEST_DEBUG_TEMPROOT:?required installation binding}"
: "${NODE_COMPILE_CACHE:?required installation binding}"
: "${PYTHONPYCACHEPREFIX:?required installation binding}"

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

# The native app can be opened by Finder before this environment publisher runs. Install
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

# Do not create a shadow state directory or publish a missing canonical state route.
[[ -d "$OPENCLAW_STATE_DIR" && -f "$OPENCLAW_STATE_DIR/openclaw.json" ]] || {
    print -u2 -- "Canonical OpenClaw state is unavailable; environment publication deferred"
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

# Publish explicit operator CA selection for GUI workers without forwarding shell injection options.
if [[ "${NODE_USE_SYSTEM_CA-}" == 0 && "${SSL_CERT_FILE-}" == /* && \
      "$SSL_CERT_FILE" != *$'\n'* && "$SSL_CERT_FILE" != *$'\r'* ]]; then
    /bin/launchctl setenv SSL_CERT_FILE "$SSL_CERT_FILE"
    /bin/launchctl setenv NODE_USE_SYSTEM_CA 0
    /bin/launchctl setenv NODE_OPTIONS --use-openssl-ca
    if [[ -n "${NODE_EXTRA_CA_CERTS-}" ]]; then
        /bin/launchctl setenv NODE_EXTRA_CA_CERTS "$NODE_EXTRA_CA_CERTS"
    fi
fi

# Environment publication never starts an app or a service. The native
# ai.openclaw.mac login job owns app startup and can remain disabled for maintenance.
