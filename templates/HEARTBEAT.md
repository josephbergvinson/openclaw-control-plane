# HEARTBEAT.md

Role:
- Liveness/watchdog only.

Rules:
- Leave this file empty or comment-only when no periodic liveness task is required.
- If heartbeat tasks are added later, they must remain liveness/watchdog checks only.
- No hidden side effects.
- No broad diagnostics by default.
- If no heartbeat action is required, reply exactly HEARTBEAT_OK. If attention is required, reply with alert text only and do not include HEARTBEAT_OK.
