# Documentation

Start with [the introduction](00-start-here.md), then follow the
[adoption guide](17-adoption-guide.md) to build an installation. The
[runtime package](../runtime/README.md) owns exact source reconstruction;
[the manifest](../runtime/manifest.json) supplies its current identities.

## Understand the design

| Chapter | Question it answers |
| --- | --- |
| [Introduction](00-start-here.md) | What happens between a request and its result? |
| [Glossary](01-glossary.md) | What do gateway, workspace, task, seal and other terms mean? |
| [Motivation](02-why-a-control-plane.md) | Which failures does each control address? |
| [Capability provenance](03-capability-provenance.md) | Which claims belong to policy, helpers, runtime code or live checks? |
| [Architecture](04-system-architecture.md) | Which component owns each responsibility? |
| [Policy and authority](05-policy-and-authority.md) | How do requests, standing policy and fetched information interact? |
| [Worked example](07-worked-example.md) | How does a calendar task work end to end? |
| [Design decisions](18-architecture-evolution.md) | Why keep these boundaries? |

## Configure behavior and integrations

| Chapter | Task |
| --- | --- |
| [Continuation](06-execution-and-durable-lanes.md) | Delegate, yield, resume and complete long work. |
| [Memory and context](08-memory-and-context.md) | Load policy, retrieve facts and preserve privacy across text and voice. |
| [Integration routing](11-integrations-and-capability-routing.md) | Select the source, account, operation and recovery route. |
| [Delivery and voice](13-delivery-and-control-surface.md) | Provide progress, finish replies and capture minutes. |
| [Configuration](../config/README.md) | Supply private adopter bindings and native settings. |
| [Examples](../examples/README.md) | Inspect synthetic task, scheduler and company-workspace contracts. |

## Install and operate

| Chapter | Task |
| --- | --- |
| [Adoption guide](17-adoption-guide.md) | Follow one reconstruction-to-acceptance path. |
| [Source layout](09-source-layout-and-artifacts.md) | Keep source, runtime state, artifacts and recovery distinct. |
| [Release promotion](10-runtime-releases-and-promotion.md) | Build, seal, activate and recover an exact release. |
| [Scheduling](12-scheduling-and-background-work.md) | Assign ownership and verify execution, effects and delivery. |
| [Health and restoration](14-guards-health-and-restoration.md) | Interpret health checks and repair demonstrated faults. |
| [Host operations](19-host-operations-and-backups.md) | Configure maintenance and optional backup/recovery services. |
| [Security](16-security-and-trust-model.md) | Bind identities, protect secrets and control disclosure. |
| [Evidence](15-evidence-audit-and-verification.md) | Match a completion claim to its actual observable. |

## Inspect and maintain the reference

The [source map](20-runtime-source-changes.md) groups the patch by mechanism.
[Native reconstruction](21-native-runtime-reconstruction.md) explains the optional
distribution and archive binding. The [history index](history/README.md) summarizes
earlier design changes without making deployment diaries part of installation.
[CONTRIBUTING](../CONTRIBUTING.md) lists the repository checks.
