# Start here

This repository describes a working pattern for one person's OpenClaw installation: a persistent assistant that can move between research, writing, software projects, personal administration and native desktop applications. It includes the operating instructions and source changes needed to build the reference, as well as the reasons for them.

The reference starts from **OpenClaw 2026.9.5**. A maintained set of runtime changes extends that release. The version, source base, patch and validation instructions must travel together; installing the same version number alone does not reproduce a modified build. Follow the [adoption guide](17-adoption-guide.md) for the pinned reconstruction path.

## Follow a request

Suppose the operator asks:

> Add today's calls with the two company leads to my work calendar.

The channel adapter receives the message and binds it to the authenticated requester and session. The agent reads the applicable operating instructions, resolves the relevant company accounts and retrieves the current invitations. It checks for existing destination events, carries across the verified times and joining details, and verifies the destination after any change. Its answer describes the result in ordinary language.

A successful tool call alone does not prove that the request was satisfied. An event can exist while its time or meeting link is wrong. That is why [verification](15-evidence-audit-and-verification.md) is part of task completion.

## What runs where

| Component | Responsibility |
| --- | --- |
| Gateway | Receives requests, coordinates sessions and work, exposes the supported control API and connects channel adapters. |
| Agent harness | Builds context, calls the selected model, executes tools and handles continuation, compaction and cancellation. |
| Workspace | Supplies instructions, useful memory, routing registries, skills and operational helpers. It is not the sole home of runtime state. |
| Node service | Provides registered host or device capabilities. Availability depends on the node and its operating-system permissions. |
| Runtime state directory | Holds the installation's persistent operational state. Treat its live databases and credential-bearing content as private. |
| Project repositories | Hold source for the work the agent performs, each with a canonical root and separate task worktrees. |
| Release directory | Holds an immutable built runtime. Development and dependency installation happen elsewhere. |
| Host services | Run selected maintenance and backup tasks independently of a model conversation. |

The reference uses a local Mac host, Discord and Telegram, Apple applications, a container engine and a cloud backup client. These are concrete examples, not prerequisites for every adoption. Preserve the responsibility boundaries when substituting another operating system or provider.

## Instructions and implementation have different jobs

An instruction can tell the agent to use current invitations. It cannot make a scheduler persist a lost task, deliver a worker result or propagate a timeout into execution. Those require code. Conversely, a durable task database cannot decide which company's account a loosely worded request means without suitable context and judgment.

The [capability provenance chapter](03-capability-provenance.md) identifies policy, helper and runtime responsibilities. The [system architecture](04-system-architecture.md) connects them. The [glossary](01-glossary.md) gives short definitions for the remaining terms.

## Start with a reconstructable installation

Read the [adoption guide](17-adoption-guide.md), inspect the configuration and policy templates, and reconstruct the pinned runtime in an isolated directory. Supply your own accounts, paths, credentials and operating-system permissions. Then test a small real workflow through the channel you will use.

The portable reference deliberately contains no personal history or active credentials. Its example identities are fictitious. Its useful behavior should come from explicit instructions and working code, not from facts that only the original operator knows.
