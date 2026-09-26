# OpenClaw Control Plane

A reproducible reference for a customized **OpenClaw** installation: runtime
changes, substantial agent instructions, integration routing, release management and
host operations for a persistent single-operator assistant.

The aim is practical. An operator should be able to ask for useful work in ordinary
language, leave a task running, return through the same conversation and inspect what
actually happened. The assistant should know which sources and accounts to use, carry
unfinished work across context changes and verify the resulting files or provider
records before saying it is done.

Company Alpha, Company Beta and the Personal Data Project are consistent example
identities. Their procedures remain substantial; accounts, host paths and data are
supplied by the adopter.

## How it works

```mermaid
flowchart TD
    Operator[Operator in chat or app] --> Gateway[OpenClaw gateway]
    Gateway --> Runtime[Model and tool harness]
    Runtime --> Native[Native goals, tasks and session state]
    Runtime --> Workspace[Policy, memory and capability routing]
    Workspace --> Sources[Company Alpha, Company Beta and personal sources]
    Runtime --> Tools[APIs, browser and device tools]
    Tools --> Effects[Files and provider records]
    Effects --> Evidence[Readback and acceptance evidence]
    Evidence --> Gateway
    Host[Host services and maintenance] --> Gateway
    Host --> Releases[Sealed releases and runtime selection]
    Host --> Recovery[Local archives, optional offsite and container recovery]
```

Start with [the introduction](docs/00-start-here.md) for the complete request flow.

## What is included

- **The complete runtime delta:** all changed paths from the pinned official release,
  including regression tests. The reconstruction helper verifies the patch checksum
  and resulting Git tree. Production source is unchanged from the pinned source;
  two labels in one test fixture are normalized.
- **Identity and working policy:** IDENTITY, AGENTS, SOUL, USER, TOOLS, WRITING,
  bootstrap and memory templates covering the assistant persona and defaults,
  initiative, approval, source authority, tool selection, writing, long work and
  verification. The [identity template](templates/IDENTITY.example.md) preserves the
  source installation's role, defaults and authority boundary with fictional names.
- **Workspace implementation:** capability routing and adapters, release activation,
  guarded maintenance, backup supervision and supporting contracts and tests.
- **Configuration and adoption guidance:** model preferences, context behavior,
  source layout, account setup, operational definitions and acceptance steps.

The runtime [manifest](runtime/manifest.json) is the authority for the official tag,
custom source commit, normalized tree, patch checksum and exact Node.js/pnpm
versions. CI reads those values from the same manifest and verifies the annotated
tag and reconstructed tree before running the source checks and build. A version
label alone does not identify the custom changes.

The [9.5 upgrade source map](docs/20-runtime-source-changes.md#september-22-upgrade)
describes preserved task ownership and steering, native credential entry, recovery
reporting and diagnostic corrections. Source implementation and tests do not prove
that a gateway, companion or account interaction has been upgraded. The manifest
records exported source and deployed component identities separately: a source
candidate can be newer than the active gateway. Each new installation requires its
own loaded-process and user-path evidence; earlier acceptance retains its dated scope.

The [runtime package](runtime/README.md) preserves earlier repair and acceptance
records under their original source identities. Those historical results do not
qualify a newer manifest, native app or installation.

## Start using the reference

Read the [adoption guide](docs/17-adoption-guide.md) before installing. It separates
source reconstruction, workspace setup, account connection, host installation and
actual user-path acceptance. The [runtime package](runtime/README.md) gives the exact
clone, check, apply and build commands. Installation begins in a separate workspace;
changing an existing service is a later explicit step.

Repository checks use Python's standard library:

```bash
python3 scripts/validate_repo.py
python3 -m unittest discover -s tests -p 'test_*.py'
```

These checks establish repository consistency. Runtime tests, fixture tests for host
helpers and adoption acceptance are separate evidence.

## Reading guide

| Chapter | Purpose |
|---|---|
| [00 Start here](docs/00-start-here.md) | Follow an ordinary request through the system. |
| [01 Glossary](docs/01-glossary.md) | Understand the terminology. |
| [02 Why a control plane](docs/02-why-a-control-plane.md) | See the failures each design choice addresses. |
| [03 Capability provenance](docs/03-capability-provenance.md) | Separate instructions, code and live proof. |
| [04 System architecture](docs/04-system-architecture.md) | Understand ownership between layers. |
| [05 Policy and authority](docs/05-policy-and-authority.md) | Read the instruction hierarchy. |
| [06 Execution and durable lanes](docs/06-execution-and-durable-lanes.md) | Follow native tasks, goals, yields and continuation. |
| [07 Worked example](docs/07-worked-example.md) | Trace a calendar request through source retrieval and readback. |
| [08 Memory and context](docs/08-memory-and-context.md) | Understand retrieval, bootstrap budgets and compaction. |
| [09 Source layout and artifacts](docs/09-source-layout-and-artifacts.md) | Keep source, working data, runtime state and recovery distinct. |
| [10 Runtime releases and promotion](docs/10-runtime-releases-and-promotion.md) | Build, seal, activate and recover a release. |
| [11 Integrations and capability routing](docs/11-integrations-and-capability-routing.md) | Verify routes for the intended account and operation. |
| [12 Scheduling and background work](docs/12-scheduling-and-background-work.md) | Understand native cron and host scheduler ownership. |
| [13 Delivery and the control surface](docs/13-delivery-and-control-surface.md) | Separate task completion from visible delivery. |
| [14 Guards, health and restoration](docs/14-guards-health-and-restoration.md) | Interpret health, containment and recovery evidence. |
| [15 Evidence, audit and verification](docs/15-evidence-audit-and-verification.md) | Match each claim to its actual proof. |
| [16 Security and trust model](docs/16-security-and-trust-model.md) | Define credentials, audience and tool boundaries. |
| [17 Adoption guide](docs/17-adoption-guide.md) | Reconstruct and qualify an installation. |
| [18 Architecture evolution](docs/18-architecture-evolution.md) | Understand the direction and tradeoffs. |
| [19 Host operations and backups](docs/19-host-operations-and-backups.md) | Cover maintenance, optional historical Backblaze templates, Docker/OrbStack and adjacent software. |
| [20 Runtime source changes](docs/20-runtime-source-changes.md) | Inspect the complete patch and source map. |

## Scope and evidence

The reference favors one trusted operator and a macOS host. Core runtime and routing
mechanisms are reusable elsewhere; launchd, Apple applications, native permissions and
Backblaze procedures require platform-specific adaptation.

Policy guides the model. Helpers enforce their own checked operations. Runtime code
implements task and session mechanics. **Live-proven** means an adopter has verified
that behavior on their own installation; importing this repository does not confer
that status. A successful build does not prove account refresh, background device
delivery or a recoverable backup.

MIT licensed. See [LICENSE](LICENSE) and the runtime's
[third-party notices](runtime/THIRD_PARTY_NOTICES.md).
