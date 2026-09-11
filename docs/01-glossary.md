# Glossary and conventions

These terms describe responsibilities, not a requirement to add a separate service for every noun. In particular, the harness and much of the control plane already live inside OpenClaw.

## The main layers

| Term | Meaning in this repository | Example |
| --- | --- | --- |
| Model | The service that predicts the next response or tool request from supplied context. | The configured OpenAI model and reasoning effort. |
| Agent | The model together with instructions, context, tools and an execution loop pursuing the operator's request. | An assistant investigating a calendar conflict. |
| Harness | The machinery around model calls, including context construction, tool dispatch and continuation. | Passing a worker timeout to its actual execution. |
| Runtime | The running software that hosts agent execution and its supporting state. | OpenClaw gateway, sessions, workers, channels and scheduler. |
| Control plane | The interfaces, configuration and operational rules used to direct and verify execution. | Native task APIs, routing registries and the release activator. |
| Workspace | The directory of agent instructions, memory, skills, registries and operational code. | The installation's AGENTS.md and capability resolver. |
| Periphery | Supporting software and services outside the core agent runtime. | Backblaze, OrbStack, Apple Calendar and a separate personal-data service. |

## Execution and state

| Term | Meaning |
| --- | --- |
| Gateway | The persistent OpenClaw service coordinating requests, state and connected capabilities. |
| Channel | A messaging integration through which the operator communicates with the agent. |
| Session | The retained context and identity of a conversation or worker. A context reset need not create a new physical session UUID. |
| Turn | One admitted unit of interaction. A substantial objective can span turns and worker continuations. |
| Goal | A persisted objective with completion criteria and continuation behavior. A goal is not completed merely because one model response ends. |
| Worker or subagent | A delegated execution with its own context and return path. Delegation does not expand the request's authority. |
| Yield | The parent suspends its current execution while delegated work can complete and return control. |
| Compaction | Reduction of active conversation context while preserving a summary or checkpoint for continuation. It is not a guarantee of perfect recall. |
| Checkpoint | Persisted information sufficient to identify progress, constraints, active work and the next step after interruption. |
| Admission | The runtime's decision to accept work into execution. Admission order matters when maintenance or other work shares the same state. |
| Liveness | Evidence that execution is active, waiting, paused or terminal. A stale status label is not proof that a process is running. |
| Delivery | Transmission of output to its intended destination. Internal completion and external delivery are separate events. |
| Idempotency | Repeating or retrying an operation does not create an additional unintended effect. |
| Reconciliation | Comparing retained evidence with authoritative state to determine what actually happened before deciding whether to retry. |
| Lease | A time- or generation-bound ownership claim used where competing writers must be coordinated. It is meaningful only where the implementation enforces it. |

## Source and operations

| Term | Meaning |
| --- | --- |
| Canonical source | The repository and branch accepted as the authoritative implementation for a surface. |
| Worktree | A separate Git checkout used for a task without mixing its changes with other work. |
| Runtime release | A built, self-contained distribution whose files are held stable during execution. |
| Pin | A specific version, commit, digest or path binding used instead of an ambiguous latest value. |
| Seal | An integrity record for a particular release tree. It detects changes; it does not prove that the release behaves correctly. |
| Activation | The controlled transition from one installed runtime generation to another. |
| Rollback | Returning to a verified prior state after a failed change. It is a planned operation, not a restart loop. |
| Source of truth | The authoritative place to check a particular claim. There is no single source authoritative for every kind of fact. |
| Registry | Structured configuration describing known projects, capabilities or account routes. Entries require reconciliation with current external state. |
| Projection | A derived view of authoritative state, useful for reading but not a replacement for its underlying source. |
| Credential reference | A name or opaque handle resolved through a secret provider. It is not the credential value. |
| Natural scheduled run | A run admitted by the actual schedule, without manually replaying its producer. |
| Restore proof | Evidence that retained backup content can be restored and meets a stated comparison. Enrollment or a green client icon is insufficient. |
| TCC | macOS privacy permissions controlling capabilities such as Accessibility and Screen Recording. These bind to real application or executable identities. |

## Reading examples

**Operator**, **Company Alpha**, **Company Beta** and **Personal Data Project** are neutral example roles. Distinct accounts remain distinct even when their names are fictitious. Never substitute a convenient account for the requested one.

A path or identifier labelled as an example is not a live destination. Setup must replace the relevant deployment values and verify the selected account, project and device before enabling its workflow. Product names such as OpenClaw, Apple Calendar and Backblaze identify real components.

Dates and hashes in a release manifest identify a reference snapshot. A status claim describes only the scope and time of its evidence. Consult [capability provenance](03-capability-provenance.md) and [verification](15-evidence-audit-and-verification.md) before treating a documented feature as accepted on a new installation.
