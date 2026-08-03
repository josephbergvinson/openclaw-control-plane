# Load policy and sensitivity manifest template

Copy to `<workspace-root>/BOOTSTRAP.md`. This file declares, for every contract and deep memory file, what role it
plays, whether it should load by default, how sensitive it is, and which file owns its subject.

## What this file is, and is not

**It is documentation and a lint target.** It is not the loader. Unless the runtime is modified to read it, changing
a row here changes an intention, not what reaches the model. Two consequences follow, and both should be stated
plainly rather than papered over.

1. **Declared tiers and injected reality can diverge.** The runtime injects a fixed, hard-coded set of filenames
   resolved against the workspace root. Adding a file to the workspace does not add it to the prompt, and marking a
   file `conditional` here does not remove it from the prompt if it is in that fixed set. Where the table and the
   runtime disagree, the runtime is what happens; record the divergence in the notes column.
2. **Live enforcement comes from character budgets, not from this table.** The binding constraint is the runtime's
   per-file and total bootstrap character budget. A file over the per-file budget is not dropped: it is truncated
   head and tail with a visible marker naming the file and the kept-versus-original size. Check contract sizes
   against the live runtime configuration, never against numbers written here, because a static copy goes stale the
   first time the configuration changes.

This file is itself **non-normative**. It never promotes a lower-precedence or non-normative file into live
authority: being loaded early is not the same as being authoritative.

## Tiers

| Tier | Meaning |
|---|---|
| `always` | Broadly useful and low-risk enough to sit in every eligible session |
| `conditional` | Load only when the task needs that surface |
| `never` | Not default-loaded; read deliberately, for history or compatibility |

## Sensitivity

Rate `low`, `medium`, or `high` per file, judged on content, not on tier. Tier says *when* a file loads; sensitivity
says *what is in it*. They are independent axes, and the reverse inference — "it is always-loaded, so it must be low"
— is exactly the mistake this rating exists to prevent.

Two disciplines follow. Keep higher-sensitivity context out of the default profile whenever a conditional source
would do, so the `always` plus `high` cell stays empty by intent rather than by luck. And review the whole
always-loaded set periodically as a single disclosure surface, since a pointer can disclose as much as its target.

Sensitivity gates injection only. A conditional file is still indexed and still one query away, so the default-load
surface and the retrieval surface are reasoned about separately.

## Contract registry

| File | Role | Tier | Sensitivity | Owner of subject |
|---|---|---|---|---|
| `AGENTS.md` | normative behaviour | always | low | itself |
| `TOOLS.md` | normative mechanics | always | low | itself |
| `SOUL.md` | style | always | low | itself |
| `WRITING.md` | style child, long-form prose only | conditional | low | `SOUL.md` |
| `USER.md` | preference | always | medium | itself |
| `MEMORY.md` | memory pointers | always | medium | itself |
| `IDENTITY.md` | identity and defaults | always | low | itself |
| `HEARTBEAT.md` | liveness view, generated | conditional | low | the status layer |
| `BOOTSTRAP.md` | load policy | conditional | low | itself |
| `policy-manifest.json` | manifest of authoritative layers | never | low | itself |
| `capabilities.md` | generated capability view | conditional | low | the registry and status layers |
| `POLICY_CHANGELOG.md` | history | never | low | itself |
| `runbook/<legacy-doc>.md` | legacy compatibility shim | never | low | the structured layers |
| `memory/<topic>.md` | deeper context, per topic | conditional | rate per file | itself |

Keep this table and `policy-manifest.json` in agreement. A structural check may assert that every row here has a
counterpart there and that the roles match; that check verifies agreement between two declarations and proves
nothing about what the runtime loads.

## Bootstrap conduct

- Successful loading is internal. Do not report the contract stack, the loaded context, or a no-blocker status in a
  visible reply unless asked or unless a real blocker exists.
- A missing contract file is recorded as missing and the run continues. A missing *normative* contract is a blocker
  and is reported before any mutation.
- Delegated and scheduled sessions receive a reduced set — in the reference runtime the two normative contracts plus
  style, identity, and preferences, dropping the liveness view, this file, and the memory pointer layer. A
  lightweight heartbeat run may receive the liveness view alone, and a lightweight scheduled run may receive nothing
  at all. Write jobs so they do not depend on a file they will not get, and put any rule a delegated session must
  obey inside a contract that session actually receives.

Background: [memory and context](../docs/08-memory-and-context.md) and
[policy and authority](../docs/05-policy-and-authority.md).
