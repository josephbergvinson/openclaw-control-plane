# Browser lane: `user-debug`

Current role
- `profile=user-debug` is a separate non-default Chrome user-data-dir lane.
- It is not the steady-state default on this host.
- The current steady-state default browser profile is `automation`.

Configured lane
- OpenClaw profile: `user-debug`
- Driver: `existing-session`
- Chrome profile dir: `~/.openclaw/browser/chrome-user-debug-profile`

Current status semantics
- On the current host, `openclaw browser --browser-profile user-debug status` may report:
  - `transport: chrome-mcp`
  - `running: true`
- Treat `running: true` as transport-level attachability, not proof that a local TCP listener exists on `127.0.0.1:9224`.

How to verify
```bash
openclaw browser --browser-profile user-debug status
openclaw browser --browser-profile user-debug tabs
/usr/sbin/lsof -nP -iTCP:9224 -sTCP:LISTEN
```

Interpretation
- If `status` reports `running: true` and `lsof` shows no `9224` listener, the lane may still be healthy via the current Chrome-MCP/existing-session path.
- If a workflow specifically requires a port-backed dedicated Chrome process, verify that requirement explicitly instead of inferring it from `running: true`.

Host browser-lane semantics
- `profile=automation`: steady-state default and supported signed-in lane
- `profile=user-debug`: separate non-default lane; do not infer `9224` from `running: true`
- `profile=user`: structurally limited on Chrome 136+
- `profile=openclaw`: still exposed by OpenClaw as a built-in managed lane on `18800` even when the local host config does not define it explicitly; removing the local config entry alone does not retire it

Legacy note
- Older artifacts and runbooks describe a `9224` manual launcher path via `scripts/openclaw_browser_user_debug.sh`.
- Treat that path as historical/manual until it is freshly re-verified against the current runtime.
