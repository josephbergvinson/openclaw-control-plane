# BOOTSTRAP.md — Load-policy reference

This file describes intended use and sensitivity. It is not a loader configuration and creates no behavioral authority. `AGENTS.md` owns behavior. The actual runtime filename list, session classification, privacy filter and context budgets determine what reaches a model turn.

## Files and roles

| File | Role | How it reaches the agent in the pinned reference |
|---|---|---|
| `AGENTS.md` | Sole behavioral authority | Recognized bootstrap file; retained by the native subagent and cron filters |
| `SOUL.md` | Voice and presentation | Recognized; not in the native `subagent:` allowlist |
| `USER.md` | Durable working preferences | Recognized when present; not in the native `subagent:` allowlist |
| `IDENTITY.md` | Concise assistant identity | Recognized; not in the native `subagent:` allowlist |
| `MEMORY.md` | Compact cues and pointers | Recognized when present; root memory is removed for native subagents, cron, groups and channels |
| `BOOTSTRAP.md` | Setup/load reference | Recognized, subject to setup and continuation handling; absent from cron/subagent allowlists |
| `TOOLS.md` | Deliberate-read mechanics | Not in the recognized bootstrap filename list; read the relevant sections when AGENTS routes there |
| `WRITING.md` | Long-form style child of SOUL | Not in the recognized list; deliberately read for applicable prose |
| `WORKER.md` | Legacy generated reference, if retained | Not recognized; does not replace AGENTS in a worker |
| `policy_manifest.json`, `policy_bootstrap_manifest.json` | Advisory metadata | Not runtime loader inputs |
| `capabilities.md`, `INDEX.md`, `HEARTBEAT.md` | Optional derived/reference files | Not recognized by the standard workspace bootstrap list; use only an explicitly configured reader |

The pinned source implements these rules in `src/agents/workspace.ts`, particularly `WORKSPACE_BOOTSTRAP_FILENAMES` and `filterBootstrapFilesForSession`. A visible dashboard worker is not automatically a native `subagent:` session: its session and chat classification determine filtering. Do not infer injected contents from a UI label or a manifest role name.

## Advisory sensitivity exceptions

- `MEMORY.md` is labelled `sensitivity: high` while remaining ordinarily eligible for private main-session bootstrap. This is an advisory exception, not an every-session guarantee.
- No other `high`-sensitivity contract carries this exception. Any future exception must be named here, in this form, before it is relied on.

## Privacy and context size

Loading and retrieval are different disclosure surfaces. A file omitted from default context may still be reachable through a tool. Source/account/audience boundaries continue to apply to deliberate reads. Keep detailed confidential records outside the default profile; a pointer can itself reveal information.

Rate each file's actual content, not its filename. The template MEMORY file contains synthetic operating cues; a real installation's MEMORY may be sensitive and must follow the runtime's private-session boundary. Do not copy a live private profile into shared or scheduled prompts.

Context budgets apply after selection. Long policies may be truncated; a file being eligible does not prove every rule was injected. Keep reusable rules single-owned and load detailed mechanics on demand. Check the actual compiled context for isolated behavioral acceptance rather than raising limits to retain duplication.

## Refresh and startup conduct

Bootstrap contents refresh before the next turn. An edit does not rewrite an in-flight turn. Use a fresh session when accepting a changed behavior in isolation. A successful load stays private: answer the request rather than announcing policy files or readiness.

If a required instruction or route cannot be verified, state the concrete limitation and preserve completed work. Neither this document nor its companion manifest may invent an approval or claim that a missing runtime feature is enabled.
