# BOOTSTRAP.md — Contract Bootstrap / Load Policy

Role boundary:
- Bootstrap/load policy only.
- This file does not override the normative precedence in `AGENTS.md` / `TOOLS.md`.
- It records the desired default-loading posture, sensitivity discipline, and size budgets for the control-plane contract stack.
- Current implementation note: this file and `policy_bootstrap_manifest.json` are documentation + lint targets; they do not by themselves change runtime loading behavior.

## Bootstrap defaults
- `always` — keep available in routine execution/bootstrap because the file is broadly useful and low enough risk to default-load.
- `conditional` — load only when the task/session explicitly needs that surface.
- `never` — do not default-load; read deliberately for archaeology, compatibility, or deep follow-up only.

## Contract registry

| Path | contract_role | bootstrap_default | sensitivity | owner_file | budget_chars | Notes |
|---|---|---|---|---|---:|---|
| `AGENTS.md` | normative | always | low | `AGENTS.md` | 40000 | Behavioral authority |
| `TOOLS.md` | normative | always | low | `TOOLS.md` | 45000 | Execution mechanics authority |
| `SOUL.md` | style | always | low | `SOUL.md` | 6000 | Voice/style only |
| `USER.md` | preference | always | medium | `USER.md` | 12000 | Default operating profile only |
| `MEMORY.md` | memory | always | low | `MEMORY.md` | 4000 | Compressed pointer layer |
| `IDENTITY.md` | identity | always | low | `IDENTITY.md` | 1000 | Minimal persona/defaults |
| `HEARTBEAT.md` | liveness | conditional | low | `HEARTBEAT.md` | 1000 | Apply only when heartbeat behavior matters |
| `capabilities.md` | factual | conditional | low | `capabilities.md` | 42000 | Factual registry input, not normative authority |
| `BOOTSTRAP.md` | bootstrap | conditional | low | `BOOTSTRAP.md` | 8000 | Human-readable bootstrap policy |
| `policy_bootstrap_manifest.json` | bootstrap | conditional | low | `BOOTSTRAP.md` | 12000 | Machine-checkable manifest |
| `memory/<conditional-context>.md` | memory | conditional | high | `USER.md` | 16000 | Sensitive context kept out of default profile |
| `memory/<private-profile>.md` | memory | conditional | high | `MEMORY.md` | 24000 | Deeper private profile facts |
| `memory/<private-model>.md` | memory | conditional | high | `MEMORY.md` | 24000 | Deeper subject/context model |
| `memory/<private-history>.md` | memory | conditional | high | `MEMORY.md` | 32000 | Long-form private background material |
| `POLICY_CHANGELOG.md` | history | never | low | `POLICY_CHANGELOG.md` | 30000 | Policy archaeology / audit trail |
| `runbook/capabilities.md` | legacy-compat | never | low | `capabilities.md` | 4000 | Thin compatibility shim only |

## Bootstrap discipline
- Keep high-sensitivity private context out of the default `USER.md` profile when a conditional memory source is sufficient.
- Do not let bootstrap convenience promote a lower-precedence or non-normative file into live authority.
- If a contract approaches its budget ceiling, prefer moving secondary detail into the appropriate owner file, runbook, artifact, or conditional memory source rather than widening default bootstrap.
- Successful bootstrap/status handling is internal-only. Do not mention `BOOTSTRAP.md`, the contract stack, loaded context, or no-blocker status in user-visible replies unless the user explicitly asks or there is a real blocker/degraded state.
- When bootstrap is satisfied, answer the user's actual message directly; do not turn bootstrap success into the reply.
