# Deployment Guide

## 1. Review the templates

Read the files in `templates/` and decide which policies fit your OpenClaw setup. The templates assume a trusted operator unless you harden the execution and filesystem policies.

## 2. Back up existing contracts

```bash
mkdir -p ./control-plane-backup
cp AGENTS.md TOOLS.md SOUL.md USER.md MEMORY.md IDENTITY.md BOOTSTRAP.md HEARTBEAT.md ./control-plane-backup/ 2>/dev/null || true
```

## 3. Copy contract templates

```bash
cp templates/AGENTS.md templates/TOOLS.md templates/SOUL.md templates/USER.md templates/MEMORY.md templates/IDENTITY.md templates/BOOTSTRAP.md templates/HEARTBEAT.md ./
```

## 4. Adapt placeholders

Search for angle-bracket placeholders and update them for your environment:

```bash
grep -RIn "<[^>][^>]*>" AGENTS.md TOOLS.md SOUL.md USER.md MEMORY.md IDENTITY.md BOOTSTRAP.md HEARTBEAT.md control scripts docs
```

Common placeholders include workspace paths, channel IDs, audit-log routes, organization/project names, and preferred handoff roots.

## 5. Install optional helpers

Copy helper scripts only if you want their workflows:

```bash
mkdir -p scripts control docs
cp scripts/*.py scripts/*.sh ./scripts/ 2>/dev/null || true
cp control/*.example.json ./control/ 2>/dev/null || true
cp docs/*.md ./docs/ 2>/dev/null || true
```

Remove `.example` suffixes only after adapting values to your host.

## 6. Verify

Run the included tests from this repo before and after local changes:

```bash
python3 -m pytest -q
```

Then run the smallest relevant OpenClaw read-only checks for your host, for example gateway status, channel probe, memory status, and source/worktree status.
