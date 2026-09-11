#!/bin/zsh

set -eu

: "${CLAUDE_CODE_TMPDIR:?required installation binding}"
: "${OPENCLAW_STATE_DIR:?required installation binding}"
: "${OPENCLAW_GATEWAY_KEYCHAIN_ACCOUNT:?required installation binding}"
: "${OPENCLAW_GATEWAY_KEYCHAIN_SERVICE:?required installation binding}"
: "${PYTEST_DEBUG_TEMPROOT:?required installation binding}"
: "${NODE_COMPILE_CACHE:?required installation binding}"
: "${PYTHONPYCACHEPREFIX:?required installation binding}"
: "${OPENCLAW_RUNTIME_APP:?required installation binding}"

# Keep Claude Code command plumbing on the configured temporary storage. The other developer
# state paths remain fail-closed so they cannot silently refill the internal disk.
: "${CLAUDE_CODE_TMPDIR:?required temporary directory}"
export CLAUDE_CODE_TMPDIR
: "${OPENCLAW_STATE_DIR:?required native state root}"
export OPENCLAW_STATE_DIR
/bin/launchctl setenv CLAUDE_CODE_TMPDIR "$CLAUDE_CODE_TMPDIR"
/bin/launchctl setenv OPENCLAW_STATE_DIR "$OPENCLAW_STATE_DIR"

# Finder-launched OpenClaw cannot yet resolve the configured file SecretRef
# (upstream #128171), and a background shell cannot read that external-volume
# file. Read the mirrored login-Keychain item so no plaintext token is embedded
# in this script or a launchd plist.
gateway_token_value=$(
    /usr/bin/security find-generic-password \
        -a "${OPENCLAW_GATEWAY_KEYCHAIN_ACCOUNT:?required Keychain account}" \
        -s "${OPENCLAW_GATEWAY_KEYCHAIN_SERVICE:?required Keychain service}" \
        -w
)
export OPENCLAW_GATEWAY_TOKEN="$gateway_token_value"
/bin/launchctl setenv OPENCLAW_GATEWAY_TOKEN "$OPENCLAW_GATEWAY_TOKEN"
unset gateway_token_value

: "${PYTEST_DEBUG_TEMPROOT:?required configured directory}"
export PYTEST_DEBUG_TEMPROOT
: "${NODE_COMPILE_CACHE:?required configured directory}"
export NODE_COMPILE_CACHE
: "${PYTHONPYCACHEPREFIX:?required configured directory}"
export PYTHONPYCACHEPREFIX
/bin/launchctl setenv PYTEST_DEBUG_TEMPROOT "$PYTEST_DEBUG_TEMPROOT"
/bin/launchctl setenv NODE_COMPILE_CACHE "$NODE_COMPILE_CACHE"
/bin/launchctl setenv PYTHONPYCACHEPREFIX "$PYTHONPYCACHEPREFIX"

# One login owner: resolve the file-backed secret first, then replace this
# short-lived loader with the app so it inherits the exact process environment.
exec "${OPENCLAW_RUNTIME_APP:?required desktop runtime app executable}"
